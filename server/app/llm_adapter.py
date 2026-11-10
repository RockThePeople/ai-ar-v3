"""LLM 어댑터 — 프롬프트 보정(A1)·거절 분류(A4). 공급자 교체는 클래스 하나를 더하면 끝난다(APP_SPEC D5).

- `ClaudeAdapter`: Anthropic Messages API. 키·모델은 기존 `server/llm.py` 와 같은 환경변수 `ANTHROPIC_API_KEY`·`LLM_MODEL`.
  출력은 구조화 출력(`output_config.format` json_schema)으로 받는다. 강제 `tool_choice` 는 쓰지 않는다
  (Sonnet 5.5·Opus 5.5 는 강제 tool_choice 를 400 으로 거부한다 — 모델 비교가 같은 코드로 돌게).
- `FakeAdapter`: S1 규칙 기반(키가 없거나 `APP_MOCK=1`).
- 스키마 위반(필드·값 규칙·한글 섞인 출력·좌표처럼 보이는 숫자 나열)이면 1회 재시도 → `LLMInvalid`.
  키·네트워크·429·5xx·거절(`refusal`) → `LLMUnavailable`.
- 호출마다 기록(입력·출력·모델·지연·토큰·재시도·오류)을 `on_call` 콜백으로 넘긴다(SQLite 저장은 engine).

🔴 LLM 은 좌표를 만들지 않는다(`server/llm.py` 불변식). 스키마에 좌표 필드가 없고(additionalProperties false),
   출력 문자열에 좌표 꼴 숫자 묶음이 있으면 위반으로 본다.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, List, Optional

from .backends import fake_classify, fake_refine

OPS = ("replace", "add", "remove", "recolor")
CATEGORIES = ("prompt", "quality", "region")
_HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")
# S4: 부정 표현(FLUX·TRELLIS 조건은 부정어를 처리하지 못하고, 장면 문장이 다음 편집으로 이어진다)
_NEG = re.compile(r"\b(not|no|without|instead of|never|none|nothing|non|isn't|aren't|doesn't|don't|lacking|free of)\b|n't\b", re.I)
_NON_ENGLISH = re.compile(r"[^\x00-\x7f\u2018\u2019\u201c\u201d\u2013\u2014]")
_COORD = re.compile(r"[\[(]\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*[\])]")


class LLMInvalid(Exception):
    """재시도 뒤에도 스키마 위반."""


class LLMUnavailable(Exception):
    """키 없음·네트워크·한도·서버 오류·거절."""


REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "target_element": {"type": "string"},
        "replacement": {"type": "string"},
        "op": {"type": "string", "enum": list(OPS)},
        "new_scene_prompt": {"type": "string"},
    },
    "required": ["target_element", "replacement", "op", "new_scene_prompt"],
    "additionalProperties": False,
}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "guidance": {"type": "string"},
    },
    "required": ["category", "guidance"],
    "additionalProperties": False,
}

REFINE_SYSTEM = """You turn a user's edit instruction for a 3D object into an updated English scene description.

The app edits only the region of the object the user selected; your text is the prompt for an image inpainting model.

Return JSON with:
- target_element: the part of the scene the instruction changes, as it appears in the current scene (English, short noun phrase).
- replacement: what it becomes, described positively. For "remove", describe what is visible in its place (e.g. "plain open seat sides"). For "recolor", the element with its new color.
- op: "replace" (swap one element for another), "add" (add a new element), "remove" (take an element away), "recolor" (change only color/material color).
- new_scene_prompt: the WHOLE current scene sentence, kept as it is except for the target element, which is changed as instructed.
  Do not drop other elements of the scene. Do not append the instruction text. One sentence, English.

Rules:
- Use only positive descriptions. Never write negations in new_scene_prompt or replacement: no "not", "no", "without",
  "instead of", "never", "nothing". Image models cannot handle negation, and the scene sentence is reused for later edits.
  Turn rejection feedback into a positive description of what should be there (e.g. feedback "not a solid panel" →
  "separate vertical slats with open gaps"). For "remove", describe the remaining appearance (e.g. "a chair with plain open sides").
- The instruction may be in Korean or any language; always write English.
- Never output coordinates, numbers describing positions, or sizes in units. Describe content only.
- If rejection feedback from earlier attempts is given, follow it.

Example:
current scene: "A man riding a motorcycle"
instruction: "change the man into Ironman"
→ {"target_element": "the man", "replacement": "Ironman", "op": "replace", "new_scene_prompt": "An Ironman riding a motorcycle"}"""

CLASSIFY_SYSTEM = """A user rejected a 2D preview of an edit to a 3D object and wrote a reason (any language).
Classify the reason into exactly one category and write short English guidance.

- "prompt": the wrong thing was made, or the content should be different (wrong object, wrong style, wrong color, wrong size,
  missing detail, the instruction was misunderstood). guidance: what to change in the prompt next time.
- "quality": the idea is right but the image is bad (blurry, distorted, artifacts, unnatural, broken shapes, low detail, odd lighting).
  guidance: what looked bad.
- "region": the selected area is wrong (edit should be in another place, cover more or less of the object, wrong part selected,
  it spilled outside the intended part). guidance: what area the user seems to want.

Return JSON {"category": ..., "guidance": ...}."""


TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"prompt_en": {"type": "string"}},
    "required": ["prompt_en"],
    "additionalProperties": False,
}

TRANSLATE_SYSTEM = """Translate a user's request for a 3D object into one short English sentence for a text-to-image model.
Describe a single object (the subject) with its key visual attributes. Keep the user's meaning; add nothing unrelated.
Use only positive descriptions (no negations). No camera, background, lighting or style words — those are added later.
Return JSON {"prompt_en": ...}.
Example: "검은 오토바이를 탄 남자" → {"prompt_en": "a man riding a black motorcycle"}"""


def is_english(text: str) -> bool:
    """영어로 볼 수 있으면 변환하지 않는다(ASCII 와 일반 따옴표·대시만)."""
    return not _NON_ENGLISH.search(text or "")


class FakeAdapter:
    name = "fake"
    model = "rule-based"

    def __init__(self, on_call: Optional[Callable[[dict], None]] = None) -> None:
        self.on_call = on_call or (lambda r: None)
        self.last_ok: Optional[bool] = None

    def refine(self, scene_prompt: str, instruction: str, reject_history: List[str], job_id: str = "") -> dict:
        out = fake_refine(scene_prompt, instruction, reject_history[-1] if reject_history else None)
        self.on_call({"job_id": job_id, "kind": "refine", "model": self.model, "input": {"scene": scene_prompt,
                      "instruction": instruction, "history": reject_history}, "output": out, "latency_s": 0.0,
                      "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": True, "error": None})
        return out

    def translate_prompt(self, prompt: str, job_id: str = "") -> str:
        return prompt.strip()

    def classify(self, reason: str, refine: Optional[dict], history: List[str], job_id: str = "") -> dict:
        cat, guide = fake_classify(reason)
        out = {"category": cat, "guidance": guide}
        self.on_call({"job_id": job_id, "kind": "classify", "model": self.model, "input": {"reason": reason},
                      "output": out, "latency_s": 0.0, "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": True,
                      "error": None})
        return out


class ClaudeAdapter:
    name = "claude"

    def __init__(self, model: Optional[str] = None, on_call: Optional[Callable[[dict], None]] = None,
                 effort: str = "low") -> None:
        import anthropic   # 지연 import — 키가 없으면 이 클래스를 만들지 않는다

        self.anthropic = anthropic
        self.model = model or os.environ.get("LLM_MODEL") or "claude-haiku-5-5"
        self.client = anthropic.Anthropic(timeout=30.0, max_retries=2)
        self.effort = effort
        self.on_call = on_call or (lambda r: None)
        self.last_ok: Optional[bool] = None

    # ── 공통 호출 ──────────────────────────────────────────────────
    def _call(self, kind: str, system: str, user: str, schema: dict, check: Callable[[dict], Optional[str]],
              job_id: str, log_input: dict) -> dict:
        rec = {"job_id": job_id, "kind": kind, "model": self.model, "input": log_input, "output": None,
               "latency_s": 0.0, "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": False, "error": None,
               "attempts": []}
        t0 = time.time()
        msgs = [{"role": "user", "content": user}]
        try:
            for attempt in (1, 2):
                try:
                    r = self.client.messages.create(
                        model=self.model, max_tokens=2048, system=system, messages=msgs,
                        output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}})
                except self.anthropic.AuthenticationError as e:
                    raise LLMUnavailable(f"인증 실패: {e}") from e
                except self.anthropic.RateLimitError as e:
                    raise LLMUnavailable(f"한도 초과: {e}") from e
                except self.anthropic.APIConnectionError as e:
                    raise LLMUnavailable(f"연결 실패: {e}") from e
                except self.anthropic.APIStatusError as e:
                    raise LLMUnavailable(f"API {e.status_code}: {e}") from e
                rec["in_tokens"] += r.usage.input_tokens
                rec["out_tokens"] += r.usage.output_tokens
                if r.stop_reason == "refusal":
                    raise LLMUnavailable(f"모델이 거절했다({getattr(r, 'stop_details', None)})")
                text = next((b.text for b in r.content if b.type == "text"), "")
                problem = None
                out = None
                if r.stop_reason == "max_tokens":
                    problem = "max_tokens 에서 잘렸다"
                else:
                    try:
                        out = json.loads(text)
                        problem = check(out)
                    except ValueError as e:
                        problem = f"JSON 아님: {e}"
                rec["attempts"].append({"attempt": attempt, "stop_reason": r.stop_reason, "text": text[:2000],
                                        "problem": problem})
                if problem is None:
                    rec["output"], rec["ok"] = out, True
                    return out
                rec["retried"] = True
                msgs = msgs + [{"role": "assistant", "content": text or "{}"},
                               {"role": "user", "content": f"That output is invalid: {problem}. Return corrected JSON only."}]
            raise LLMInvalid(f"{kind}: 스키마 위반(재시도 후): {rec['attempts'][-1]['problem']}")
        except (LLMInvalid, LLMUnavailable) as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec["latency_s"] = round(time.time() - t0, 3)
            self.last_ok = rec["ok"]
            self.on_call(rec)

    # ── A1 보정 ────────────────────────────────────────────────────
    @staticmethod
    def check_refine(o: dict) -> Optional[str]:
        if set(o) != set(REFINE_SCHEMA["properties"]):
            return f"필드가 다르다: {sorted(o)}"
        if o["op"] not in OPS:
            return f"op 값: {o['op']!r}"
        for k in ("target_element", "new_scene_prompt"):
            if not str(o[k]).strip():
                return f"{k} 가 비었다"
        for k in ("new_scene_prompt", "replacement"):
            m = _NEG.search(str(o[k]))
            if m:
                return f"{k} 에 부정 표현 {m.group(0)!r} 이 있다(긍정 묘사로 바꿔야 한다)"
        for k, v in o.items():
            if _HANGUL.search(str(v)):
                return f"{k} 에 한글이 있다(영어로 써야 한다)"
            if _COORD.search(str(v)):
                return f"{k} 에 좌표 꼴 숫자가 있다"
        return None

    def refine(self, scene_prompt: str, instruction: str, reject_history: List[str], job_id: str = "") -> dict:
        hist = "\n".join(f"- {h}" for h in reject_history) if reject_history else "(none)"
        user = (f"current scene: {json.dumps(scene_prompt or '', ensure_ascii=False)}\n"
                f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
                f"rejection feedback from earlier attempts:\n{hist}")
        return self._call("refine", REFINE_SYSTEM, user, REFINE_SCHEMA, self.check_refine, job_id,
                          {"scene": scene_prompt, "instruction": instruction, "history": reject_history})

    # ── S4 생성 프롬프트 영어 변환 ─────────────────────────────────
    @staticmethod
    def check_translate(o: dict) -> Optional[str]:
        t = str(o.get("prompt_en", "")).strip()
        if set(o) != {"prompt_en"} or not t:
            return "prompt_en 이 없거나 비었다"
        if not is_english(t):
            return "영어가 아닌 글자가 있다"
        if _NEG.search(t):
            return "부정 표현이 있다"
        if len(t) > 300:
            return "너무 길다(300자 초과)"
        return None

    def translate_prompt(self, prompt: str, job_id: str = "") -> str:
        out = self._call("translate", TRANSLATE_SYSTEM, f"request: {json.dumps(prompt, ensure_ascii=False)}",
                         TRANSLATE_SCHEMA, self.check_translate, job_id, {"prompt": prompt})
        return out["prompt_en"].strip()

    # ── A4 분류 ────────────────────────────────────────────────────
    @staticmethod
    def check_classify(o: dict) -> Optional[str]:
        if set(o) != {"category", "guidance"}:
            return f"필드가 다르다: {sorted(o)}"
        if o["category"] not in CATEGORIES:
            return f"category 값: {o['category']!r}"
        if not str(o["guidance"]).strip():
            return "guidance 가 비었다"
        return None

    def classify(self, reason: str, refine: Optional[dict], history: List[str], job_id: str = "") -> dict:
        ctx = json.dumps({k: (refine or {}).get(k) for k in ("target_element", "replacement", "op", "new_scene_prompt")},
                         ensure_ascii=False)
        user = (f"edit that was previewed: {ctx}\n"
                f"earlier rejection feedback: {json.dumps(history, ensure_ascii=False)}\n"
                f"user's rejection reason: {json.dumps(reason, ensure_ascii=False)}")
        return self._call("classify", CLASSIFY_SYSTEM, user, CLASSIFY_SCHEMA, self.check_classify, job_id,
                          {"reason": reason, "refine": refine, "history": history})


def make_adapter(mock: bool, on_call: Optional[Callable[[dict], None]] = None, model: Optional[str] = None):
    """키가 있고 가짜 모드가 아니면 Claude, 아니면 규칙 기반."""
    if mock or not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return FakeAdapter(on_call)
    try:
        return ClaudeAdapter(model=model, on_call=on_call)
    except ImportError:
        return FakeAdapter(on_call)
