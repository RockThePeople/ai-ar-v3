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
  Keep every detail of the current scene (all listed parts, colors and materials) word for word where unchanged.
  Describe the changed element at the same level of detail as the rest (its parts, color, material).
  Do not drop other elements of the scene. Do not append the instruction text. One sentence, English.
  No camera, view, background, lighting or style words (those are added by the server).

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

# S12: 바뀐 부위를 대상 바로 뒤에 관계어(wearing·holding·with)로 잇는다. 나열 중간에 끼우지 않는다. 명령문 금지.
REFINE_SYSTEM_REL = """You turn a user's edit instruction for a 3D object into an updated English scene description.

The app edits only the region of the object the user selected; your text is the prompt for an image inpainting model.

Return JSON with:
- target_element: the part of the scene the instruction changes, as it appears in the current scene (English, short noun phrase).
- replacement: what it becomes, described positively. For "remove", describe what is visible in its place (e.g. "a smooth pillar"). For "recolor", the element with its new color.
- op: "replace" (swap one element for another), "add" (add a new element), "remove" (take an element away), "recolor" (change only color/material color).
- new_scene_prompt: one English sentence, built in this order:
  1. The main subject (the first noun phrase of the current scene), then RIGHT AFTER IT the changed element, joined with
     exactly one of these relation words:
       "wearing"  for something worn (hat, helmet, mask, jacket, shoes),
       "holding"  for something held in the hands,
       "with"     for a part or attachment of the object (bumper, roof rack, wheels, door, handle).
     If the instruction changes the whole subject into something else, rename the subject itself and use no relation word;
     then every old detail that belonged to the old subject's look (its clothing, uniform, gear, surface, props) overlaps
     with the new subject and is deleted. Keep only details that still fit the new subject (pose, overall build), and
     describe the new subject's own parts, colors and materials.
     For "remove", use no relation word: describe the remaining surface positively where the removed element was
     (e.g. "a smooth right front pillar with olive green paint").
  2. Then all the unchanged details of the current scene, kept word for word, in their original order.
     Delete the old description of the element that was changed (e.g. the old hat when a new hat is worn).
     Never insert the changed element in the middle of the list of other details.
  Describe the changed element at the same level of detail as the rest (its parts, color, material).
  No camera, view, background, lighting or style words (those are added by the server).
  Write a description, never a command: no imperative verbs such as change, replace, turn, make, remove, add, swap, put.

Rules:
- Use only positive descriptions. Never write negations in new_scene_prompt or replacement: no "not", "no", "without",
  "instead of", "never", "nothing". Image models cannot handle negation, and the scene sentence is reused for later edits.
  Turn rejection feedback into a positive description of what should be there.
- The instruction may be in Korean or any language; always write English.
- Never output coordinates, numbers describing positions, or sizes in units. Describe content only.
- If rejection feedback from earlier attempts is given, follow it.

Examples (format only):
current scene: "A firefighter, a yellow helmet with a front badge, a tan turnout coat with reflective stripes, black boots"
instruction: "change the helmet into a black baseball cap"
→ {"target_element": "a yellow helmet with a front badge", "replacement": "a black baseball cap", "op": "replace",
   "new_scene_prompt": "A firefighter wearing a black cotton baseball cap with a curved brim, a tan turnout coat with reflective stripes, black boots"}
current scene: "A delivery van, a boxy cargo body, a sliding side door, black steel wheels, white painted steel"
instruction: "add a roof ladder rack"
→ {"target_element": "the roof", "replacement": "an aluminum ladder rack on the roof", "op": "add",
   "new_scene_prompt": "A delivery van with an aluminum ladder rack on the roof, a boxy cargo body, a sliding side door, black steel wheels, white painted steel"}
current scene: "A hiker, a green rain jacket, a walking stick in the right hand, brown leather boots"
instruction: "remove the walking stick"
→ {"target_element": "a walking stick in the right hand", "replacement": "an open empty right hand", "op": "remove",
   "new_scene_prompt": "A hiker, a green rain jacket, an open empty right hand at the side, brown leather boots"}"""

# ── S13: 라쏘 이미지로 편집 의도 파악 ─────────────────────────────────────
REGION_FITS = ("ok", "too_small", "wrong_part")
REFINE_REGION_SCHEMA = {
    "type": "object",
    "properties": {**REFINE_SCHEMA["properties"],
                   "selected_part": {"type": "string"},
                   "region_fit": {"type": "string", "enum": list(REGION_FITS)},
                   "region_reason": {"type": "string"}},
    "required": REFINE_SCHEMA["required"] + ["selected_part", "region_fit", "region_reason"],
    "additionalProperties": False,
}

# S13b: 인페인팅 전용 문장(바뀐 부위 먼저, 옛 부위 없음, 맥락 한 구절, 좌우 없음, 40단어 안)
REFINE_REGION_IP_SCHEMA = {
    "type": "object",
    "properties": {**REFINE_REGION_SCHEMA["properties"], "inpaint_prompt": {"type": "string"}},
    "required": REFINE_REGION_SCHEMA["required"] + ["inpaint_prompt"],
    "additionalProperties": False,
}

# S20b: 에이전트 진행 보고용 한국어 요약(에이전트 보정만 — 1.0.6 수동 흐름 요청은 그대로)
REFINE_REGION_IP_KO_SCHEMA = {
    "type": "object",
    "properties": {**REFINE_REGION_IP_SCHEMA["properties"], "inpaint_ko": {"type": "string"}},
    "required": REFINE_REGION_IP_SCHEMA["required"] + ["inpaint_ko"],
    "additionalProperties": False,
}
_INPAINT_KO_RULE = """
- inpaint_ko: ONE short Korean line (at most 40 characters) summarizing what inpaint_prompt draws, for the user's
  progress screen (e.g. "오른손에 쥔 짧은 강철 칼"). Korean only, no English words."""


def inpaint_ko_problem(t) -> Optional[str]:
    """S20b inpaint_ko 검사: 비었거나 한글이 없으면 문제."""
    t = str(t or "").strip()
    if not t:
        return "inpaint_ko 가 비었다"
    if not _HANGUL.search(t):
        return "inpaint_ko 에 한글이 없다(한국어 한 줄로)"
    return None


_INPAINT_RULES = """
- inpaint_prompt: a separate English prompt for the image inpainting model, which repaints ONLY the selected region.
  Start with what will be visible inside the region after the edit (the changed element), in detail: shape, color, material.
  Never mention the element it replaces (the old head, the old hat, ...).
  Then add ONE short phrase of surrounding context (e.g. "sitting on a snowman of packed white snow with a red knitted scarf").
  No position or side words (left, right, upper, lower, top-left, "on the ... side"): the mask decides where it goes.
  Positive descriptions only, no commands, no style words beyond the asset style. At most 40 words.
  Example: "A large carved orange Halloween jack-o'-lantern pumpkin head with triangular eyes and a jagged grin, a short green
  stem on top, sitting on a snowman of packed white snow with a red knitted scarf".
  If the whole subject is renamed (mask_ratio 0.6 or more), inpaint_prompt may equal new_scene_prompt shortened to 40 words."""

#: S13b 좌우·위치 단어(지시에 없으면 지운다)
_LATERAL = re.compile(r"\b(?:on\s+the\s+(?:left|right|upper|lower|far)\s+side(?:\s+of\s+(?:the|its|his|her)\s+\w+)?|"
                      r"(?:top|bottom|upper|lower)[- ](?:left|right)|left|right|upper|lower)\b(?:-hand)?", re.I)


def lateral_words(text: str) -> List[str]:
    return [m.group(0).lower() for m in _LATERAL.finditer(text or "")]


def strip_lateral(text: str, instruction: str = "") -> str:
    """지시에 좌우·위치 말이 없으면 지운다(있으면 그 말은 둔다)."""
    if lateral_words(instruction):
        return text
    t = _LATERAL.sub("", text or "")
    t = re.sub(r"\s+([,.])", r"\1", t)
    t = re.sub(r"\b(a|an|the)\s+(?=[,.]|$)", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" ,")


_REGION_RULES = """
The user selected a region of the object with a lasso. You get two images and a number:
- Image 1: the object from the lasso camera, with the selected region tinted red and outlined.
- Image 2: a close-up around the selected region (red outline only, real colors inside).
- mask_ratio: selected pixels / object pixels in that view (1.0 = the whole object).

Additional output fields:
- selected_part: the name of the selected (red) part, as seen in the images (e.g. "the snowman's head", "a twig arm",
  "a gloved hand"). Never use left/right or other position words in selected_part or target_element unless the
  instruction itself uses them; the mask already fixes the position.
- region_fit: "ok" if the requested result fits in the selected region; "too_small" if the requested result looks larger
  than the selected region (e.g. a long broom when only a hand is selected); "wrong_part" if the part the instruction
  names is outside the selected region.
- region_reason: one short English sentence explaining region_fit.

Region rules (these override the other rules):
- target_element is the selected part, named as it appears in the current scene (or selected_part if the scene has no name for it).
  If the instruction names no part (e.g. "change into a pumpkin"), it is about the selected part.
- Rename the whole subject only when mask_ratio is 0.6 or more. Below 0.6, keep the subject and all its other details,
  and describe the selected part as changed, joined with a relation word
  (e.g. "A big snowman wearing a carved orange Halloween jack-o'-lantern pumpkin as its head, ...").
- The element changed in this edit is always the FIRST relation phrase, right after the subject. Relation phrases added by
  earlier edits (e.g. "wearing sneakers") move after it.
- For "add", keep every existing element; delete an old description only where the new element covers the same spot."""

#: S13 S11 문구 정리: "real-world proportions" 계열은 출력에서 지운다
_REALWORLD = re.compile(r"\s*,?\s*(?:standing\s+)?(?:at|in|with|of)?\s*real[- ]world(?:\s+human)?\s+proportions", re.I)


def strip_realworld(text: str) -> str:
    t = _REALWORLD.sub("", text or "")
    t = re.sub(r"\s+,", ",", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,")


_ART = re.compile(r"^(a|an|the)\s+", re.I)


def subject_head(text: str, n: int = 3) -> str:
    """첫 명사구 앞 n 단어(관사·관계어 뒤는 버림, 소문자)."""
    first = re.split(r",|\b(?:wearing|holding|with)\b", text or "", maxsplit=1)[0]
    w = re.findall(r"[a-z0-9'-]+", _ART.sub("", first.strip()).lower())
    return " ".join(w[:n])


def first_clause(text: str) -> str:
    return (text or "").split(",", 1)[0].lower()


def replacement_lead(repl: str, n: int = 4) -> str:
    w = re.findall(r"[a-z0-9'-]+", _ART.sub("", (repl or "").strip()).lower())
    return " ".join(w[:n])


def _norm_words(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9'-]+", (t or "").lower()))


def placement_ok(nsp: str, repl: str, op: str) -> bool:
    """S13 6: replacement 앞 4단어가 첫 구(주어 + 첫 관계어 구) 안에 있어야 한다. 제거는 검사하지 않는다."""
    if op == "remove":
        return True
    lead = replacement_lead(repl).split()
    if not lead:
        return True
    clause = set(_norm_words(first_clause(nsp)).split())
    hit = sum(1 for w in lead if w in clause)
    return hit >= max(1, -(-len(lead) * 3 // 4))           # 앞 4단어 중 3개 이상(순서 무관) — 표현 흔들림 허용


def _img_block(png: bytes) -> dict:
    import base64
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.standard_b64encode(png).decode()}}


def _scrub(req: dict) -> dict:
    """기록용: 이미지 base64 를 크기·sha256 표시로 바꾼다(전문 대신, 이미지는 작업 폴더에 따로 저장)."""
    import hashlib
    import base64
    out = json.loads(json.dumps(req, ensure_ascii=False))
    for m in out.get("messages", []):
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "image" and b.get("source", {}).get("type") == "base64":
                    raw = base64.b64decode(b["source"]["data"])
                    b["source"]["data"] = f"<png {len(raw)} bytes sha256={hashlib.sha256(raw).hexdigest()[:16]}>"
    return out


#: S12 명령형 검사: 문장 첫머리 또는 쉼표 뒤의 명령형 동사
_IMPERATIVE = re.compile(r"(?:^|,)\s*(change|replace|turn|make|remove|add|swap|convert|put|give|paint|recolor|"
                         r"transform|delete|erase|take|set|use)\b(?!-)", re.I)


def imperative_words(text: str) -> List[str]:
    """S12: 문장 첫머리·쉼표 뒤 명령형 동사(소문자)."""
    return [m.group(1).lower() for m in _IMPERATIVE.finditer(text or "")]


CLASSIFY_SYSTEM = """A user rejected a 2D preview of an edit to a 3D object and wrote a reason (any language).
Classify the reason into exactly one category and write short English guidance.

- "prompt": the wrong thing was made, or the content should be different (wrong object, wrong style, wrong color, wrong size,
  missing detail, the instruction was misunderstood). guidance: what to change in the prompt next time.
- "quality": the idea is right but the image is bad (blurry, distorted, artifacts, unnatural, broken shapes, low detail, odd lighting).
  guidance: what looked bad.
- "region": the selected area is wrong (edit should be in another place, cover more or less of the object, wrong part selected,
  it spilled outside the intended part). guidance: what area the user seems to want.

Return JSON {"category": ..., "guidance": ...}."""


#: S13b: 칠하기·영역을 놓쳤다는 말은 region
CLASSIFY_REGION_RULE = """

- If the user says they missed or got wrong the painting / selection / area / extent (e.g. "missed to select",
  "wrong area", "too small area", "needs to start from ..."), the category is "region"."""

CLASSIFY_IMAGE_RULES = """

You also get two images: Image 1 is the object with the user's selected region tinted red and outlined; Image 2 is the
2D edit preview the user rejected (the region was repainted). Look at them.
- Keep the user's words as they are; never re-interpret them into a different request. If the reason is short or
  unclear, read it literally.
- guidance: describe concretely what is visibly wrong in Image 2 for this reason (shape, size, position, color, what
  part it starts from), in one or two English sentences."""


DETAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject_detailed": {"type": "string"}},
    "required": ["subject_detailed"],
    "additionalProperties": False,
}

# S6: 거친 입력 → 논문 X3 형식의 물체 묘사(물체 한 줄 + 보이는 부품 3–5개 + 주 색·재질). 시점·배경·조명·스타일은 서버 꼬리.
DETAIL_SYSTEM = """You expand a user's short request for a single 3D object into a detailed English object description
for a text-to-image model that produces a 3D game asset.

Write subject_detailed as one line:
  the object in a few words, then everything the user asked for, then 3 to 5 plausible visible parts
  (concrete shapes on the object), then the main color and material.
Rules:
- Keep every attribute the user stated (object type, colors, parts, materials, count). Never contradict or drop them.
- The request may be in Korean or any language; always write English.
- Use only positive descriptions: never "not", "no", "without", "instead of", "never", "nothing".
- No camera/view, background, lighting, shadow, render or style words. The server appends those.
- Describe one object only. Comma-separated phrases, under 400 characters.
Return JSON {"subject_detailed": ...}.
Example (format only; invent parts that fit the actual object): "a mailbox" → {"subject_detailed": "A small curbside mailbox,
a rounded top, a red side flag, a front pull-down door with a brass handle, a square wooden post, dark blue painted metal"}"""

# ── S11: 기본은 실물형, 양식은 사용자가 말할 때만 ─────────────────────────────
#: 사용자 입력에 없으면 넣지 않는 양식 단어(긴 것부터 — 'plastic toy' 를 'toy' 보다 먼저 지운다)
BANNED_STYLE = ("plastic toy", "game asset", "low poly", "low-poly", "figurine", "figure", "toy", "miniature",
                "cartoon", "chibi", "stylized")
_BANNED_RE = re.compile(r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
                                           for w in BANNED_STYLE) + r")(s|ish|y)?\b", re.I)
#: 한국어 양식 주문 → 원문에 그 말이 있으면 금지어 검사에서 그 영어 단어를 허용한다
_KO_STYLE = {"장난감": ("toy", "plastic toy"), "피규어": ("figure", "figurine"), "미니어처": ("miniature",),
             "만화": ("cartoon", "stylized"), "카툰": ("cartoon",), "로우폴리": ("low poly", "low-poly"),
             "로우 폴리": ("low poly", "low-poly"), "치비": ("chibi",), "게임": ("game asset", "stylized")}


def _canon(w: str) -> str:
    w = re.sub(r"[\s-]+", " ", w.lower()).strip()
    for b in BANNED_STYLE:
        if w == b.replace("-", " ") or w.startswith(b.replace("-", " ")):
            return b.replace("-", " ")
    return w


def banned_words(text: str, source: str = "") -> List[str]:
    """text 에 있는 금지 양식 단어 중 source(사용자 원문 등)에 없는 것. 대소문자·복수·하이픈 무시."""
    allowed = {_canon(m.group(1)) for m in _BANNED_RE.finditer(source or "")}
    for ko, ens in _KO_STYLE.items():
        if ko in (source or ""):
            allowed |= {e.replace("-", " ") for e in ens}
    out = []
    for m in _BANNED_RE.finditer(text or ""):
        c = _canon(m.group(1))
        if c not in allowed and m.group(0) not in out:
            out.append(m.group(0))
    return out


def strip_banned(text: str, words: List[str]) -> str:
    """그 단어만 지운다. 남는 빈 쉼표·겹 공백을 정리한다."""
    t = text
    for w in sorted(words, key=len, reverse=True):
        t = re.sub(r"\b" + re.escape(w) + r"\b", "", t, flags=re.I)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",(\s*,)+", ",", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    return t


DETAIL_STYLE_SCHEMA = {
    "type": "object",
    "properties": {"subject_detailed": {"type": "string"},
                   "style": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                   "style_words": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    "required": ["subject_detailed", "style", "style_words"],
    "additionalProperties": False,
}

#: S22 APP_NAMED_NODETAIL: 이름만으로 모양이 정해지는 대상 판정(켬일 때만 스키마·규칙에 더한다 — 끔이면 요청이 S21 이전과 같다)
DETAIL_STYLE_SCHEMA_NAMED = {
    "type": "object",
    "properties": dict(DETAIL_STYLE_SCHEMA["properties"], named_subject={"type": "boolean"},
                       subject_name={"anyOf": [{"type": "string"}, {"type": "null"}]}),
    "required": DETAIL_STYLE_SCHEMA["required"] + ["named_subject", "subject_name"],
    "additionalProperties": False,
}

NAMED_RULE = """
- named_subject: true only when the request names one specific well-known character, mascot or person from a brand,
  show, game, film or book whose look is fixed by the name alone (e.g. "Hello Kitty", "SpongeBob", "Pikachu",
  "헬로키티"). false for generic things and generic roles ("an adventurer", "a firefighter", "a red car", "a cat").
- subject_name: when named_subject is true, that name as usually written in English (e.g. "Hello Kitty"); otherwise null."""

#: S14 APP_FACEHINT: 사람·캐릭터·동물이면 얼굴이 보이게
FACE_HINT = """
- If the object is a person, character or animal, describe its head and a clearly visible face (eyes, nose, mouth)
  facing the viewer; never hide the face behind a mask, helmet visor, hat brim or hair unless the request asks for it."""

DETAIL_STYLE_SYSTEM = """You expand a user's short request for a single 3D object into a detailed English description
of the object as it exists in the real world, for a text-to-image model whose image is turned into a 3D model.

Return JSON with:
- subject_detailed: one line. The object in a few words, then everything the user asked for, then 3 to 5 plausible
  visible parts (concrete shapes on the object), then the main colors and real materials.
  When style is null, describe the real thing at real-world proportions (a real person, a real vehicle).
  When style is set, describe the object's parts, colors and materials only, with no size or scale words
  (no "full-size", "life-size", "real"), so they do not fight the style.
  Never use these words unless the user's request itself uses them: figure, figurine, toy, plastic toy, miniature,
  cartoon, chibi, stylized, game asset, low poly.
- style: only if the request explicitly asks for a look or style (e.g. "toy like", "miniature", "cartoon", "low poly",
  "장난감 같은", "만화풍"), that style as a short English phrase (e.g. "toy-like", "cartoon", "low poly"). Otherwise null.
  Keep style words out of subject_detailed; the server adds "<style> style" after it.
- style_words: the exact words from the request that ask for that style, copied as written (any language). null when style is null.
Rules:
- Keep every attribute the user stated (object type, colors, parts, materials, count). Never contradict or drop them.
- The request may be in Korean or any language; subject_detailed and style are always English.
- Use only positive descriptions: never "not", "no", "without", "instead of", "never", "nothing".
- No camera/view, background, lighting, shadow or render words. The server appends those.
- Describe one object only. Comma-separated phrases, under 400 characters.
Examples (format only; invent parts that fit the actual object):
"a desk lamp" → {"subject_detailed": "A desk lamp, a weighted round base, a two-section jointed arm with coil springs,
a cone-shaped shade, a push switch on the base, matte black painted steel with a brushed aluminum shade",
"style": null, "style_words": null}
"만화풍 우체통" → {"subject_detailed": "A curbside mailbox, a rounded top, a red side flag, a front pull-down door,
a square wooden post, dark blue painted metal", "style": "cartoon", "style_words": "만화풍"}"""


MAX_TEXT = 800        # 서버 안 LLM 출력·장면 문장 상한(S6). 앱 입력 1–300자는 계약대로 라우트가 검사한다
_STYLE_WORDS = re.compile(r"\b(background|lighting|studio|three-quarter|front view|side view|top view|camera|render(ed|ing)?|"
                          r"game asset|photograph(y|ic)?|shadows?|centered|white background)\b", re.I)


def is_english(text: str) -> bool:
    """영어로 볼 수 있으면 변환하지 않는다(ASCII 와 일반 따옴표·대시만)."""
    return not _NON_ENGLISH.search(text or "")


def style_note(style: Optional[str]) -> str:
    """S11: 보정(A1)에 알리는 자산 양식."""
    if style:
        return (f"asset style: {style}. The object is shown in a {style} style; keep the scene in that style and "
                "add no other style words.")
    return ("asset style: default realistic. The object is a real-world object at real-world proportions; describe the "
            "changed element as a real thing and use no style words (figure, toy, miniature, cartoon, stylized, "
            "game asset, low poly) unless the instruction asks for them.")


def _style_guard(out: dict, request: str, again: Callable[[List[str]], dict]) -> dict:
    """S11 금지어 검사(양식 없는 경우만). again(found) = 1회 재요청."""
    res = {"subject_detailed": str(out["subject_detailed"]).strip(), "style": (out.get("style") or None),
           "style_words": (out.get("style_words") or None),
           "check": {"banned_found": [], "retried": False, "removed": []}}
    if res["style"] is not None:
        res["style"] = str(res["style"]).strip()
        return res
    found = banned_words(res["subject_detailed"], request)
    if not found:
        return res
    res["check"].update(banned_found=found, retried=True)
    try:
        o2 = again(found)
    except (LLMInvalid, LLMUnavailable) as e:
        o2, res["check"]["retry_error"] = None, f"{type(e).__name__}: {e}"
    if o2 is not None:
        res["subject_detailed"] = str(o2["subject_detailed"]).strip()
        if o2.get("style"):                    # 재요청이 양식을 새로 붙이면 받아들이지 않는다(원문 검사는 check 가 했다)
            res["style"], res["style_words"] = str(o2["style"]).strip(), o2.get("style_words")
            return res
    left = banned_words(res["subject_detailed"], request)
    if left:
        res["subject_detailed"] = strip_banned(res["subject_detailed"], left)
        res["check"]["removed"] = left
    return res


#: 가짜 모드·시험용 양식 판정(원문 단어 → 영어 양식)
_FAKE_STYLE = (("toy like", "toy-like"), ("toy-like", "toy-like"), ("장난감 같은", "toy-like"), ("장난감", "toy-like"),
               ("miniature", "miniature"), ("cartoon", "cartoon"), ("만화풍", "cartoon"), ("low poly", "low poly"))


#: 사용자 결정(2026-11-12): S11 이전 자산(양식 기록 없음)은 생성 원문의 양식어로 양식을 정한다. 장면 문장은 보지 않는다.
#: 긴 말부터 찾는다. (찾을 말, 양식) — 영어는 단어 경계, 한국어는 부분 문자열
_REQUEST_STYLE = (("toy-like", "toy-like"), ("toy like", "toy-like"), ("low-poly", "low poly"), ("low poly", "low poly"),
                  ("cartoon", "cartoon"), ("miniature", "miniature"), ("chibi", "chibi"), ("toy", "toy-like"),
                  ("장난감 같은", "toy-like"), ("장난감", "toy-like"), ("만화풍", "cartoon"), ("만화", "cartoon"),
                  ("카툰", "cartoon"), ("미니어처", "miniature"), ("치비", "chibi"), ("로우폴리", "low poly"),
                  ("로우 폴리", "low poly"))


def style_from_request(text: str):
    """생성 원문(사용자 입력) → (양식, 찾은 말) 또는 (None, None)."""
    t = text or ""
    for w, st in _REQUEST_STYLE:
        if _HANGUL.search(w):
            if w in t:
                return st, w
        elif re.search(r"\b" + re.escape(w) + r"s?\b", t, re.I):
            return st, w
    return None, None


#: S22 가짜 모드·시험용 이름 판정(원문 소문자 부분 문자열 → 영어 이름)
_FAKE_NAMED = (("hello kitty", "Hello Kitty"), ("헬로키티", "Hello Kitty"), ("헬로 키티", "Hello Kitty"),
               ("spongebob", "SpongeBob SquarePants"), ("스폰지밥", "SpongeBob SquarePants"))


def fake_detail_style(prompt: str) -> dict:
    low = prompt.lower()
    for w, st in _FAKE_STYLE:
        if w in low:
            subj = re.sub(re.escape(w), "", prompt, flags=re.I).strip() or prompt.strip()
            return {"subject_detailed": subj, "style": st, "style_words": w,
                    "check": {"banned_found": [], "retried": False, "removed": []}}
    return {"subject_detailed": prompt.strip(), "style": None, "style_words": None,
            "check": {"banned_found": [], "retried": False, "removed": []}}


class FakeAdapter:
    name = "fake"
    model = "rule-based"

    def __init__(self, on_call: Optional[Callable[[dict], None]] = None) -> None:
        self.on_call = on_call or (lambda r: None)
        self.last_ok: Optional[bool] = None

    def refine(self, scene_prompt: str, instruction: str, reject_history: List[str], job_id: str = "",
               changed_from: Optional[dict] = None, style: Optional[dict] = None, avoid: Optional[List[str]] = None,
               relphrase: bool = False, imperative: Optional[List[str]] = None, region: Optional[dict] = None,
               fix: Optional[List[str]] = None) -> dict:
        out = fake_refine(scene_prompt, instruction, reject_history[-1] if reject_history else None, relphrase=relphrase)
        if region is not None:                               # S13 가짜: 이미지는 보지 않는다. 바뀐 부위를 첫 구에
            nsp = out["new_scene_prompt"]
            repl = out.get("replacement") or ""
            if relphrase and repl and repl.lower() not in nsp.split(",", 1)[0].lower():
                head, _, rest = (scene_prompt or "").strip().rstrip(".").partition(",")
                nsp = f"{head} with {repl}" + (f",{rest}" if rest else "") if head else repl
            if region.get("inpaint_prompt"):
                out["inpaint_prompt"] = repl or nsp
                if region.get("inpaint_ko"):                 # S20b 가짜: 한글 지시문이면 그대로(40자), 아니면 빈 값 → 대체 문장
                    out["inpaint_ko"] = instruction.strip()[:40] if _HANGUL.search(instruction or "") else ""
            out = dict(out, new_scene_prompt=nsp, selected_part=out.get("target_element") or "the selected part", region_fit="ok",
                       region_reason="fake adapter")
        self.on_call({"job_id": job_id, "kind": "refine_changed" if changed_from else "refine", "model": self.model,
                      "input": {"scene": scene_prompt, "instruction": instruction, "history": reject_history,
                                "changed_from": changed_from}, "output": out, "latency_s": 0.0,
                      "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": True, "error": None})
        return out

    def detail_prompt(self, prompt: str, job_id: str = "") -> str:
        return prompt.strip()

    def detail_style(self, prompt: str, job_id: str = "", facehint: bool = False, named: bool = False) -> dict:
        out = fake_detail_style(prompt)
        if named:                                     # S22 가짜: 정해 둔 이름만
            nm = next((en for k, en in _FAKE_NAMED if k in prompt.lower()), None)
            out.update(named_subject=nm is not None, subject_name=nm)
        return out

    def classify(self, reason: str, refine: Optional[dict], history: List[str], job_id: str = "",
                 images: Optional[List[bytes]] = None, region_rule: bool = False) -> dict:
        cat, guide = fake_classify(reason)
        out = {"category": cat, "guidance": guide}
        self.on_call({"job_id": job_id, "kind": "classify", "model": self.model, "input": {"reason": reason},
                      "output": out, "latency_s": 0.0, "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": True,
                      "error": None})
        return out


def _raw_response(r) -> dict:
    """S9: 받은 응답 원문(파싱 전 content 블록·stop_reason·usage). SDK 객체를 JSON 으로."""
    try:
        d = r.model_dump(mode="json")
    except Exception:                                       # noqa: BLE001 — SDK 버전 차이 대비
        d = {"stop_reason": getattr(r, "stop_reason", None), "content": [getattr(b, "text", str(b)) for b in r.content],
             "usage": {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}}
    return d


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
               "attempts": [], "client": {"sdk": "anthropic " + getattr(self.anthropic, "__version__", "?"),
                                          "endpoint": "POST /v1/messages", "timeout_s": 30.0, "max_retries": 2,
                                          "auth": "x-api-key 헤더(값은 기록하지 않음)"}}
        t0 = time.time()
        msgs = [{"role": "user", "content": user}]              # user: 문자열 또는 [이미지…, 글자] 블록 목록(S13)
        try:
            for attempt in (1, 2):
                req = {"model": self.model, "max_tokens": 2048, "system": system, "messages": msgs,
                       "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}}}
                # S9: 실제로 보낸 요청 본문 전문(키는 헤더라 여기 없다). 회차마다 남긴다
                raw_req = _scrub(req)
                try:
                    r = self.client.messages.create(**req)
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
                rec["attempts"].append({"attempt": attempt, "stop_reason": r.stop_reason, "text": text,
                                        "problem": problem, "request": raw_req, "response": _raw_response(r),
                                        "client": rec["client"]})
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
        if len(str(o["new_scene_prompt"])) > MAX_TEXT:
            return f"new_scene_prompt 가 너무 길다({MAX_TEXT}자 초과)"
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

    @staticmethod
    def check_refine_region(o: dict) -> Optional[str]:
        if set(o) != set(REFINE_REGION_SCHEMA["properties"]):
            return f"필드가 다르다: {sorted(o)}"
        if o.get("region_fit") not in REGION_FITS:
            return f"region_fit 값: {o.get('region_fit')!r}"
        if not str(o.get("selected_part", "")).strip():
            return "selected_part 가 비었다"
        base = {k: o[k] for k in REFINE_SCHEMA["properties"]}
        return ClaudeAdapter.check_refine(base)

    @staticmethod
    def check_refine_region_ip(o: dict) -> Optional[str]:
        if set(o) != set(REFINE_REGION_IP_SCHEMA["properties"]):
            return f"필드가 다르다: {sorted(o)}"
        t = str(o.get("inpaint_prompt", "")).strip()
        if not t:
            return "inpaint_prompt 가 비었다"
        if len(t.split()) > 60:                               # 규칙 40단어, 검사는 여유 있게(넘으면 재시도)
            return f"inpaint_prompt 가 너무 길다({len(t.split())}단어, 40단어 안)"
        m = _NEG.search(t)
        if m:
            return f"inpaint_prompt 에 부정 표현 {m.group(0)!r} 이 있다"
        if _HANGUL.search(t):
            return "inpaint_prompt 에 한글이 있다"
        return ClaudeAdapter.check_refine_region({k: v for k, v in o.items() if k != "inpaint_prompt"})

    def refine(self, scene_prompt: str, instruction: str, reject_history: List[str], job_id: str = "",
               changed_from: Optional[dict] = None, style: Optional[dict] = None, avoid: Optional[List[str]] = None,
               relphrase: bool = False, imperative: Optional[List[str]] = None, region: Optional[dict] = None,
               fix: Optional[List[str]] = None) -> dict:
        """region["inpaint_prompt"] = True: S13b 인페인팅 전용 문장도 받는다.
        region = {"images": [png, …], "mask_ratio": float}: S13 라쏘 이미지(이미지 실패면 글자만으로 다시, 기록).
        fix = S13 서버 검사 재요청 안내문 목록.
        changed_from = {prev, reason, guidance}: S8 — prompt 거절 뒤 같은 문장이 나와 다시 부를 때.
        style = {"style": None|str}: S11 자산 양식(None = 기본 실물형). avoid = S11 금지어 재요청 때 뺄 단어."""
        hist = "\n".join(f"- {h}" for h in reject_history) if reject_history else "(none)"
        user = (f"current scene: {json.dumps(scene_prompt or '', ensure_ascii=False)}\n"
                f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
                f"rejection feedback from earlier attempts:\n{hist}")
        if changed_from:
            user += ("\n\nThe previous new_scene_prompt "
                     f"{json.dumps(changed_from.get('prev') or '', ensure_ascii=False)} was REJECTED by the user. "
                     f"Rejection reason: {json.dumps(changed_from.get('reason') or '', ensure_ascii=False)}. "
                     f"Guidance: {json.dumps(changed_from.get('guidance') or '', ensure_ascii=False)}. "
                     "Write a different new_scene_prompt that actually changes how the target element is described, "
                     "reflecting the reason and guidance. Keep the same detail level and keep the rest of the scene.")
        if style is not None:
            user += "\n\n" + style_note(style.get("style"))
        if avoid:
            user += (f"\n\nYour previous new_scene_prompt used {json.dumps(avoid)}, which do not fit the asset style "
                     "and the instruction does not ask for them. Leave those words out.")
        if imperative:
            user += (f"\n\nYour previous new_scene_prompt used command words {json.dumps(imperative)}. Write a description "
                     "of the object, never a command.")
        for f in fix or []:
            user += "\n\n" + f
        kind = "refine_changed" if changed_from else ("refine_style_retry" if (avoid or imperative or fix) else "refine")
        log = {"scene": scene_prompt, "instruction": instruction, "history": reject_history, "changed_from": changed_from}
        if style is not None:
            log.update(style=style.get("style"), avoid=avoid)
        if relphrase:
            log.update(relphrase=True, imperative=imperative)
        system = REFINE_SYSTEM_REL if relphrase else REFINE_SYSTEM
        if fix:
            log["fix"] = fix
        if region is not None:
            imgs = [b for b in (region.get("images") or []) if b]
            ratio = region.get("mask_ratio")
            utext = user + f"\n\nmask_ratio: {ratio if ratio is not None else 'unknown'}"
            log.update(region={"mask_ratio": ratio, "n_images": len(imgs)})
            content = [_img_block(b) for b in imgs] + [{"type": "text", "text": utext}]
            ip = bool(region.get("inpaint_prompt"))
            ko = ip and bool(region.get("inpaint_ko"))         # S20b: 에이전트 보정만
            if ko:
                log["inpaint_ko"] = True
                seen = {"n": 0}

                def chk(o: dict) -> Optional[str]:
                    """inpaint_ko 문제는 첫 응답에서만 재요청(1회). 두 번째도 틀리면 받아 두고 호출한 쪽이 대체 문장."""
                    base = self.check_refine_region_ip({k: v for k, v in o.items() if k != "inpaint_ko"}) \
                        if set(o) == set(REFINE_REGION_IP_KO_SCHEMA["properties"]) else f"필드가 다르다: {sorted(o)}"
                    if base:
                        return base
                    seen["n"] += 1
                    return inpaint_ko_problem(o.get("inpaint_ko")) if seen["n"] == 1 else None
            try:
                return self._call(kind, system + "\n" + _REGION_RULES + (_INPAINT_RULES if ip else "")
                                  + (_INPAINT_KO_RULE if ko else ""), content,
                                  REFINE_REGION_IP_KO_SCHEMA if ko else (REFINE_REGION_IP_SCHEMA if ip else REFINE_REGION_SCHEMA),
                                  chk if ko else (self.check_refine_region_ip if ip else self.check_refine_region),
                                  job_id, log)
            except LLMUnavailable as e:
                if not imgs or "API 4" not in str(e):
                    raise
                log["region"]["image_error"] = str(e)[:300]    # 이미지 입력 거부 → 글자만으로(기록)
        return self._call(kind, system, user, REFINE_SCHEMA, self.check_refine, job_id, log)

    # ── S6 생성 프롬프트 상세화(S4 영어 변환을 대체, 영어 입력도 항상 거친다) ──
    @staticmethod
    def check_detail(o: dict) -> Optional[str]:
        t = str(o.get("subject_detailed", "")).strip()
        if set(o) != {"subject_detailed"} or not t:
            return "subject_detailed 가 없거나 비었다"
        if not is_english(t):
            return "영어가 아닌 글자가 있다"
        if _NEG.search(t):
            return "부정 표현이 있다"
        m = _STYLE_WORDS.search(t)
        if m:
            return f"시점·배경·조명·스타일 단어 {m.group(0)!r} 가 있다(서버 꼬리가 붙인다)"
        if len(t) > MAX_TEXT:
            return f"너무 길다({MAX_TEXT}자 초과)"
        return None

    def detail_prompt(self, prompt: str, job_id: str = "") -> str:
        out = self._call("detail", DETAIL_SYSTEM, f"request: {json.dumps(prompt, ensure_ascii=False)}",
                         DETAIL_SCHEMA, self.check_detail, job_id, {"prompt": prompt})
        return out["subject_detailed"].strip()

    # ── S11 상세화 + 양식 판정 ─────────────────────────────────────
    @staticmethod
    def check_detail_style(o: dict, request: str = "", named: bool = False) -> Optional[str]:
        if set(o) != set((DETAIL_STYLE_SCHEMA_NAMED if named else DETAIL_STYLE_SCHEMA)["properties"]):
            return f"필드가 다르다: {sorted(o)}"
        if named and o.get("named_subject"):
            nm = str(o.get("subject_name") or "").strip()
            if not nm or len(nm) > 60 or not is_english(nm) or _NEG.search(nm):
                return f"named_subject 가 true 인데 subject_name 이 짧은 영어 이름이 아니다: {nm!r}"
        p = ClaudeAdapter.check_detail({"subject_detailed": o.get("subject_detailed")})
        if p:
            return p
        st, sw = o.get("style"), o.get("style_words")
        if (st is None) != (sw is None):
            return "style 과 style_words 는 둘 다 null 이거나 둘 다 있어야 한다"
        if st is not None:
            st = str(st).strip()
            if not st or len(st) > 40 or not is_english(st) or _NEG.search(st) or _STYLE_WORDS.search(st.replace("game asset", "")):
                return f"style 값이 짧은 영어 양식 구가 아니다: {st!r}"
            if str(sw).strip().lower() not in (request or "").lower():
                return f"style_words {sw!r} 가 요청 원문에 없다(원문에서 그대로 옮겨야 한다)"
        return None

    def detail_style(self, prompt: str, job_id: str = "", facehint: bool = False, named: bool = False) -> dict:
        """named: S22 — named_subject·subject_name 도 받는다(스키마·규칙 추가). 결과에 그대로 싣는다.
        facehint: S14 — 사람·캐릭터·동물이면 머리와 얼굴이 보이게 묘사하라는 규칙을 더한다.
        S11: {subject_detailed, style, style_words, check}. style null 인데 금지어가 있으면 1회 재요청, 그래도 있으면 지운다."""
        user = f"request: {json.dumps(prompt, ensure_ascii=False)}"
        schema = DETAIL_STYLE_SCHEMA_NAMED if named else DETAIL_STYLE_SCHEMA
        chk = lambda o: self.check_detail_style(o, prompt, named)     # noqa: E731
        system = DETAIL_STYLE_SYSTEM + (FACE_HINT if facehint else "") + (NAMED_RULE if named else "")
        log = dict({"prompt": prompt, "facehint": facehint}, **({"named": True} if named else {}))
        out = self._call("detail_style", system, user, schema, chk, job_id, log)
        res = _style_guard(out, prompt, lambda found: self._call(
            "detail_style_retry", system,
            user + f"\n\nYour previous subject_detailed {json.dumps(out['subject_detailed'], ensure_ascii=False)} used "
                   f"{json.dumps(found)}, which the request does not ask for. Describe the real object at real-world "
                   "proportions and leave those words out.",
            schema, chk, job_id, {"prompt": prompt, "banned_found": found}))
        if named:                                     # 판정은 첫 응답 것을 쓴다(재요청은 금지어만 고친다)
            res["named_subject"] = bool(out.get("named_subject"))
            res["subject_name"] = (str(out.get("subject_name") or "").strip() or None) if res["named_subject"] else None
        return res

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

    def classify(self, reason: str, refine: Optional[dict], history: List[str], job_id: str = "",
                 images: Optional[List[bytes]] = None, region_rule: bool = False) -> dict:
        """images = [라쏘 겹친 렌더, 2d_edit]: S13. 이미지 실패면 글자만으로(기록). region_rule: S13b 영역 규칙."""
        ctx = json.dumps({k: (refine or {}).get(k) for k in ("target_element", "replacement", "op", "new_scene_prompt")},
                         ensure_ascii=False)
        user = (f"edit that was previewed: {ctx}\n"
                f"earlier rejection feedback: {json.dumps(history, ensure_ascii=False)}\n"
                f"user's rejection reason: {json.dumps(reason, ensure_ascii=False)}")
        log = {"reason": reason, "refine": refine, "history": history}
        imgs = [b for b in (images or []) if b]
        if imgs:
            log["n_images"] = len(imgs)
            content = [_img_block(b) for b in imgs] + [{"type": "text", "text": user}]
            try:
                return self._call("classify", CLASSIFY_SYSTEM + (CLASSIFY_REGION_RULE if region_rule else "") +
                                  CLASSIFY_IMAGE_RULES, content, CLASSIFY_SCHEMA,
                                  self.check_classify, job_id, log)
            except LLMUnavailable as e:
                if "API 4" not in str(e):
                    raise
                log["image_error"] = str(e)[:300]
        return self._call("classify", CLASSIFY_SYSTEM + (CLASSIFY_REGION_RULE if region_rule else ""), user,
                          CLASSIFY_SCHEMA, self.check_classify, job_id, log)


def make_adapter(mock: bool, on_call: Optional[Callable[[dict], None]] = None, model: Optional[str] = None):
    """키가 있고 가짜 모드가 아니면 Claude, 아니면 규칙 기반."""
    if mock or not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return FakeAdapter(on_call)
    try:
        return ClaudeAdapter(model=model, on_call=on_call)
    except ImportError:
        return FakeAdapter(on_call)


# ══════════════════════════════════════════════════════════ S20 에이전트 오케스트레이터
#: 시점 이름(automask.VIEW_AZ 와 같다)
AGENT_VIEWS = ("az000", "az090", "az180", "az270")
_BOX = {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}]}
AGENT_MASK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["target_part", "target_part_ko", "new_object", "op", "front_view", "inpaint_view", "regions", "found",
                 "note_ko", "side", "side_frame"],
    "properties": {
        "target_part": {"type": "string"}, "target_part_ko": {"type": "string"}, "new_object": {"type": "string"},
        "op": {"type": "string", "enum": ["add", "replace", "remove", "recolor"]},
        "front_view": {"type": "string", "enum": list(AGENT_VIEWS)},
        "inpaint_view": {"type": "string", "enum": list(AGENT_VIEWS)},
        "regions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "boxes"],
            "properties": {"name": {"type": "string"},
                           "boxes": {"type": "object", "additionalProperties": False, "required": list(AGENT_VIEWS),
                                     "properties": {v: _BOX for v in AGENT_VIEWS}}}}},
        "found": {"type": "boolean"}, "note_ko": {"type": "string"},
        # S20d: 지시의 좌우와 기준(코드 판정이 애매할 때만 쓴다)
        "side": {"anyOf": [{"type": "string", "enum": ["left", "right"]}, {"type": "null"}]},
        "side_frame": {"anyOf": [{"type": "string", "enum": ["viewer", "character"]}, {"type": "null"}]}},
}
AGENT_MASK_SYSTEM = """You are the planning step of an automatic 3D editing service. The user wrote ONE edit instruction
(usually Korean) for a single 3D object. You see four renders of the object from four directions around it at the same
height: az000, az090, az180, az270 (azimuth in degrees, camera circling the vertical axis; az090 is 90 degrees around
from az000). Each image has a 0-1000 coordinate grid (x to the right, y downward; lines every 100, numbers on the top
and left edges). Mark where the edit must happen with boxes; the server turns the boxes into a 3D volume.

Return JSON:
- target_part: the English name of the part to change (e.g. "right hand", "both shoes", "hat"); target_part_ko: Korean.
- new_object: the English name of what appears after the edit (e.g. "torch"); for remove, what is removed.
- op: add (a new thing appears, e.g. holding a sword), replace (a part becomes something else), remove, recolor.
- front_view: the view that shows the character's face / the object's front most directly.
- inpaint_view: the view where the edited part and the new object are seen most clearly and largest, unhidden
  (often the front view or a side view). The image model repaints the box area in this view.
- regions: one region per separate place. Two shoes or two feet = two regions. Each region gives boxes
  [x0, y0, x1, y1] in grid units for AT LEAST TWO views that are 90 degrees apart (e.g. az000 and az090), null for the
  others. The server keeps the cells that fall inside the boxes of every view you gave, so each box must cover the
  same 3D place seen from that view.
- found: false only when the part does not exist on the object at all (then regions may be empty).
- note_ko: one short Korean sentence for the user about what you marked (e.g. "오른손과 칼이 들어갈 자리를 칠했습니다").
- side / side_frame: the left/right the instruction asks for and how to read it (null when it names no side):
  "character" for a body part of the character (오른손, 왼발, 오른쪽 어깨, right hand, left leg ...),
  "viewer" for a plain position (오른쪽에, 왼쪽에, on the left, to the right ...). For an instruction with no side and no
  part, the default right hand gives side "right", side_frame "character".
Rules:
- Left/right rule: a plain position word (오른쪽·왼쪽, on the left/right) means the VIEWER's side: that side of the
  front_view image. A body part (오른손·왼손, right/left hand/foot/arm/leg/eye/ear/shoulder) means the CHARACTER's own
  side; the character faces you, so in the front_view image the character's right hand is on the IMAGE LEFT and the
  left hand is on the IMAGE RIGHT. In other views work it out from where the front is.
- If the instruction does not name a part (e.g. "칼을 들게 해줘"), use the character's right hand.
- For holding a new object, the box covers the hand AND the empty space the object will occupy, generously
  (a sword is long: extend the box well beyond the hand along the likely blade direction, up or outward).
- For replacing an object (e.g. sword -> torch), cover the old object and leave room for the new one's size.
- For shoes/boots/feet cover each foot fully (two regions). Keep boxes tight to the target elsewhere: do not cover
  the head or body when the edit is about a hand.
- Boxes are integers 0-1000 with x0 < x1 and y0 < y1."""

AGENT_CHECK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok", "reason_ko", "fix"],
                      "properties": {"ok": {"type": "boolean"}, "reason_ko": {"type": "string"}, "fix": {"type": "string"}}}
AGENT_CHECK_SYSTEM = """You check an automatically made edit mask before an image model repaints it. The image shows
one render of a 3D object; the tinted area with an outline is what will be repainted (the edit volume seen from this
view). Given the user's instruction and the plan, decide whether the tinted area is on the right part and leaves enough
room for the new object. Left/right rule: a plain position word (오른쪽·왼쪽, on the left/right) means the viewer's side of
the image; a body part (오른손·왼손, right/left hand/foot/arm ...) means the character's own side, which in a front view is
the OPPOSITE image side (the character's right hand is on the image left). When an "expected position" line is given,
it already applies this rule for this image: trust it.
Return JSON {"ok": bool, "reason_ko": one short Korean sentence, "fix": short English hint for the next masking
(e.g. "extend upward above the right hand for the blade", "move to the character's left hand"), "" when ok}."""

AGENT_JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["checks", "reason_ko", "fix"],
    "properties": {"checks": {"type": "object", "additionalProperties": False,
                              "required": ["element", "position", "mask_fit", "shape_3d"],
                              "properties": {k: {"type": "boolean"} for k in ("element", "position", "mask_fit", "shape_3d")}},
                   "reason_ko": {"type": "string"}, "fix": {"type": "string"}}}
AGENT_JUDGE_SYSTEM = """You grade one 2D edit before it is lifted to 3D. Images in order: (1) the original render,
(2) the same render with the editable area tinted and outlined, (3) the edited image from the image model.
Grade four checks, each true (pass) or false (fail):
- element: the requested thing is clearly drawn (e.g. a torch really looks like a torch).
- position: it is on the requested part (both feet when shoes are asked). Left/right rule: a plain position word
  (오른쪽·왼쪽, on the left/right) means the viewer's side of the image; a body part (오른손·왼손, right/left hand/foot/arm ...)
  means the character's own side, which in a front view is the OPPOSITE image side (the character's right hand is on the
  image left). When an "expected position" line is given, it already applies this rule for this image: trust it.
- mask_fit: the new thing lies inside the editable area and is not cut off at the area's edge.
- shape_3d: it reads as one clear solid piece, not too small for the area, not smeared, not flat decoration.
Return JSON {"checks": {...}, "reason_ko": one short Korean sentence describing the main problem (or "합격" style
praise when all pass), "fix": short English hint for the retry, "" when all pass}."""


def _check_mask_plan(o: dict) -> Optional[str]:
    if not o.get("found"):
        return None
    regs = o.get("regions") or []
    if not regs:
        return "found 인데 regions 가 비었다"
    for rg in regs:
        given = [v for v in AGENT_VIEWS if rg["boxes"].get(v) is not None]
        for v in given:
            b = rg["boxes"][v]
            if len(b) != 4 or not all(0 <= x <= 1000 for x in b) or not (b[0] < b[2] and b[1] < b[3]):
                return f"region {rg.get('name')!r} {v} 상자가 [x0,y0,x1,y1] 0–1000, x0<x1, y0<y1 이 아니다: {b}"
        idx = sorted(AGENT_VIEWS.index(v) for v in given)
        if not any((b - a) % 2 == 1 for i, a in enumerate(idx) for b in idx[i + 1:]):
            return f"region {rg.get('name')!r} 는 서로 90° 떨어진 시점 2개 이상에 상자가 있어야 한다: {given}"
    return None


def _labeled(images: List[tuple], text: str) -> list:
    """[(이름, png)…] → [글자 이름, 이미지, …, 지시 글자] 블록."""
    out: list = []
    for name, b in images:
        out += [{"type": "text", "text": name}, _img_block(b)]
    return out + [{"type": "text", "text": text}]


class AgentMixin:
    """S20 오케스트레이터 호출 3종. ClaudeAdapter 는 _call 로 JSON 스키마 응답(1회 재요청 뒤 LLMInvalid)."""

    def agent_mask(self, views: dict, instruction: str, scene: str, feedback: Optional[str], job_id: str = "") -> dict:
        """S20d: 마스킹만 effort AGENT_MASK_EFFORT(기본 medium). 확인·채점은 어댑터 기본(low)."""
        txt = (f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
               f"object description: {json.dumps(scene, ensure_ascii=False)}"
               + (f"\nprevious attempt problem (fix it this time): {feedback}" if feedback else ""))
        imgs = [(v, views[v]) for v in AGENT_VIEWS]
        base = getattr(self, "effort", None)
        if base is not None:
            self.effort = getattr(self, "mask_effort", None) or base
        try:
            return self._call("agent_mask", AGENT_MASK_SYSTEM, _labeled(imgs, txt), AGENT_MASK_SCHEMA, _check_mask_plan,
                              job_id, {"instruction": instruction, "scene": scene, "feedback": feedback, "n_images": 4,
                                       "effort": getattr(self, "effort", None)})
        finally:
            if base is not None:
                self.effort = base

    def agent_mask_check(self, overlay: bytes, instruction: str, plan: dict, job_id: str = "",
                         side_hint: Optional[str] = None) -> dict:
        txt = (f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
               f"plan: {json.dumps({k: plan.get(k) for k in ('target_part', 'new_object', 'op', 'inpaint_view')})}"
               + (f"\nexpected position: {side_hint}" if side_hint else ""))
        return self._call("agent_mask_check", AGENT_CHECK_SYSTEM, _labeled([("edit area", overlay)], txt),
                          AGENT_CHECK_SCHEMA, lambda o: None, job_id, {"instruction": instruction, "n_images": 1})

    def agent_judge(self, render: bytes, overlay: bytes, edit: bytes, instruction: str, prompt: str,
                    job_id: str = "", side_hint: Optional[str] = None) -> dict:
        txt = (f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
               f"image model prompt: {json.dumps(prompt, ensure_ascii=False)}"
               + (f"\nexpected position: {side_hint}" if side_hint else ""))
        return self._call("agent_judge", AGENT_JUDGE_SYSTEM,
                          _labeled([("(1) original", render), ("(2) editable area", overlay), ("(3) edited", edit)], txt),
                          AGENT_JUDGE_SCHEMA, lambda o: None, job_id,
                          {"instruction": instruction, "prompt": prompt, "n_images": 3})


class ClaudeAgentAdapter(AgentMixin, ClaudeAdapter):
    """AGENT_MODEL(기본 claude-opus-5-5). 기록은 같은 on_call(/clients)."""


#: S20d 계약 7절 가짜 모드 X-Mock-Agent 시나리오(작업별)
_PASS = {"element": True, "position": True, "mask_fit": True, "shape_3d": True}
MOCK_AGENT = {
    "pass": {"judge_script": [_PASS]},
    "backtrack": {"judge_script": [dict(_PASS, position=False), _PASS]},
    "below_bar": {"judge_script": [dict(_PASS, position=False), dict(_PASS, element=False), dict(_PASS, shape_3d=False),
                                   dict(_PASS, position=False)]},
    "mask_retry": {"mask_check_ok": [False, True], "judge_script": [_PASS]},
}


class FakeAgentAdapter:
    """가짜 모드·시험. 스크립트: judge_script = [checks dict …](작업마다 순서대로, 끝나면 마지막을 반복),
    mask_found·mask_check_ok·mask_boxes 로 바꿀 수 있다. fail = {'agent_mask': n} 이면 그 호출이 n 번 LLMInvalid."""

    name = "fake"
    model = "rule-based"

    def __init__(self, on_call: Optional[Callable[[dict], None]] = None) -> None:
        self.on_call = on_call or (lambda r: None)
        self.judge_script: List[dict] = [
            {"element": True, "position": False, "mask_fit": True, "shape_3d": True},
            {"element": True, "position": True, "mask_fit": True, "shape_3d": True}]
        self.mask_found = True
        self.mask_check_ok: List[bool] = [True]
        self.mask_boxes = {"az000": [300, 300, 700, 700], "az090": [300, 300, 700, 700], "az180": None, "az270": None}
        self.op = "add"
        self.side: Optional[str] = None
        self.side_frame: Optional[str] = None
        self.fail: Dict[str, int] = {}
        self.per_job: Dict[str, dict] = {}            # S20d 가짜 모드 X-Mock-Agent: job_id → {judge_script, mask_check_ok}
        self._n: Dict[tuple, int] = {}

    def _next(self, kind: str, job_id: str) -> int:
        k = (kind, job_id)
        self._n[k] = self._n.get(k, 0) + 1
        return self._n[k] - 1

    def _rec(self, kind: str, job_id: str, out: dict) -> dict:
        if self.fail.get(kind):
            self.fail[kind] -= 1
            self.on_call({"job_id": job_id, "kind": kind, "model": self.model, "input": {}, "output": None,
                          "latency_s": 0.0, "in_tokens": 0, "out_tokens": 0, "retried": True, "ok": False,
                          "error": "LLMInvalid: fake"})
            raise LLMInvalid(f"{kind}: 가짜 스키마 위반")
        self.on_call({"job_id": job_id, "kind": kind, "model": self.model, "input": {}, "output": out,
                      "latency_s": 0.0, "in_tokens": 0, "out_tokens": 0, "retried": False, "ok": True, "error": None})
        return out

    def agent_mask(self, views: dict, instruction: str, scene: str, feedback: Optional[str], job_id: str = "") -> dict:
        self._next("agent_mask", job_id)
        out = {"target_part": "right hand", "target_part_ko": "오른손", "new_object": "the new object", "op": self.op,
               "front_view": "az000", "inpaint_view": "az000",
               "regions": [{"name": "right hand", "boxes": dict(self.mask_boxes)}] if self.mask_found else [],
               "found": self.mask_found, "note_ko": "오른손과 새 물건이 들어갈 자리를 칠했습니다(가짜)",
               "side": self.side, "side_frame": self.side_frame}
        return self._rec("agent_mask", job_id, out)

    def agent_mask_check(self, overlay: bytes, instruction: str, plan: dict, job_id: str = "",
                         side_hint: Optional[str] = None) -> dict:
        i = self._next("agent_mask_check", job_id)
        seq = self.per_job.get(job_id, {}).get("mask_check_ok", self.mask_check_ok)
        ok = seq[min(i, len(seq) - 1)]
        return self._rec("agent_mask_check", job_id, {"ok": ok, "reason_ko": "칠한 자리가 맞습니다" if ok else
                                                      "칠한 자리가 너무 좁습니다", "fix": "" if ok else "extend upward"})

    def agent_judge(self, render: bytes, overlay: bytes, edit: bytes, instruction: str, prompt: str,
                    job_id: str = "", side_hint: Optional[str] = None) -> dict:
        self.last_judge_hint = side_hint
        i = self._next("agent_judge", job_id)
        script = self.per_job.get(job_id, {}).get("judge_script", self.judge_script)
        c = dict(script[min(i, len(script) - 1)])
        ok = all(c.values())
        return self._rec("agent_judge", job_id, {"checks": c, "reason_ko": "요청대로 그려졌습니다" if ok else
                                                 "새 물건이 다른 손 쪽에 그려졌습니다", "fix": "" if ok else "use the other hand"})


def make_agent_adapter(mock: bool, on_call: Optional[Callable[[dict], None]] = None):
    """AGENT_MODEL(기본 claude-opus-5-5). 키가 없거나 가짜 모드면 규칙 기반."""
    if mock or not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return FakeAgentAdapter(on_call)
    try:
        ad = ClaudeAgentAdapter(model=os.environ.get("AGENT_MODEL") or "claude-opus-5-5", on_call=on_call,
                                effort=os.environ.get("AGENT_EFFORT") or "low")
        ad.mask_effort = os.environ.get("AGENT_MASK_EFFORT") or "medium"     # S20d: 마스킹만
        return ad
    except ImportError:
        return FakeAgentAdapter(on_call)

# ══════════════════════════════════════════════════════════ S20e 사용자 라쏘 볼륨 다듬기 판단
_OBOX = {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}]}
AGENT_REFINE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["op", "target_part", "target_part_ko", "new_object", "old_object", "keep_box", "old_object_box",
                 "new_space_box", "found", "note_ko"],
    "properties": {
        "op": {"type": "string", "enum": ["add", "replace", "remove", "recolor"]},
        "target_part": {"type": "string"}, "target_part_ko": {"type": "string"},
        "new_object": {"type": "string"}, "old_object": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "keep_box": _OBOX, "old_object_box": _OBOX, "new_space_box": _OBOX,
        "found": {"type": "boolean"}, "note_ko": {"type": "string"}},
}
AGENT_REFINE_SYSTEM = """You are the mask-refining step of a 3D editing service. The user drew a lasso on one view of a 3D
object and wrote ONE edit instruction (usually Korean). The lasso volume goes straight through the object, so it can
include parts the instruction does not mean (the body behind a hand, a shoulder, the face). Image 1 is the render from the
user's camera; image 2 is the same render with the lasso area tinted. Both have a 0-1000 grid over the WHOLE image
(x to the right, y downward; lines every 100). Give boxes [x0, y0, x1, y1] in these grid units; the server cuts the volume
with them.

Return JSON:
- op: add (a new thing appears, e.g. holding a sword), replace (a part becomes something else), remove, recolor.
- target_part / target_part_ko: the part named or meant (e.g. "right hand" / "오른손", "front wheel" / "앞바퀴").
- new_object: what appears after the edit (for remove: what is removed). old_object: the thing being replaced or removed
  (e.g. "shield"), null for add and recolor.
- keep_box: where the target part is visible in the image, tight (the hand, not the arm and body). It may stay inside the
  tinted area.
- old_object_box: for replace and remove only, the WHOLE old object as visible in the image, even the parts outside the
  tinted area (so nothing of it is left behind). null otherwise.
- new_space_box: for add and replace, the empty space the new object will take (a sword is long, a tower is tall), inside
  or near the tinted area. null otherwise.
- found: false only when the target part is not in the image at all.
- note_ko: one short Korean sentence for the user (e.g. "오른손과 칼이 들어갈 자리만 남겼습니다").
Rules:
- Left/right: a plain position word (오른쪽·왼쪽) is the viewer's side of the image; a body part (오른손·왼손 ...) is the
  character's own side, which is the opposite image side when the character faces the camera.
- Boxes are integers 0-1000 with x0 < x1 and y0 < y1. Never include the face or head unless the instruction is about them."""


def _check_refine_plan(o: dict) -> Optional[str]:
    if not o.get("found"):
        return None
    for k in ("keep_box", "old_object_box", "new_space_box"):
        b = o.get(k)
        if b is not None and (len(b) != 4 or not all(0 <= v <= 1000 for v in b) or not (b[0] < b[2] and b[1] < b[3])):
            return f"{k} 가 [x0,y0,x1,y1] 0–1000, x0<x1, y0<y1 이 아니다: {b}"
    if o.get("keep_box") is None:
        return "keep_box 가 없다"
    if o.get("op") in ("replace", "remove") and o.get("old_object_box") is None:
        return "replace·remove 인데 old_object_box 가 없다"
    return None


def _agent_refine_plan(self, render: bytes, overlay: bytes, instruction: str, scene: str, feedback: Optional[str],
                       job_id: str = "") -> dict:
    txt = (f"instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
           f"object description: {json.dumps(scene, ensure_ascii=False)}"
           + (f"\nprevious attempt problem (fix it this time): {feedback}" if feedback else ""))
    base = getattr(self, "effort", None)
    if base is not None:
        self.effort = getattr(self, "mask_effort", None) or base
    try:
        return self._call("agent_refine", AGENT_REFINE_SYSTEM,
                          _labeled([("(1) render", render), ("(2) lasso area tinted", overlay)], txt),
                          AGENT_REFINE_SCHEMA, _check_refine_plan, job_id,
                          {"instruction": instruction, "feedback": feedback, "n_images": 2,
                           "effort": getattr(self, "effort", None)})
    finally:
        if base is not None:
            self.effort = base


AgentMixin.agent_refine_plan = _agent_refine_plan


def _fake_refine_plan(self, render: bytes, overlay: bytes, instruction: str, scene: str, feedback: Optional[str],
                      job_id: str = "") -> dict:
    self._next("agent_refine", job_id)
    op = self.op
    out = {"op": op, "target_part": "right hand", "target_part_ko": "오른손", "new_object": "the new object",
           "old_object": "the old object" if op in ("replace", "remove") else None,
           "keep_box": list(self.refine_boxes["keep"]),
           "old_object_box": list(self.refine_boxes["old"]) if op in ("replace", "remove") else None,
           "new_space_box": list(self.refine_boxes["new"]) if op in ("add", "replace") else None,
           "found": self.mask_found, "note_ko": "지시 부위와 새 물건 자리만 남겼습니다(가짜)"}
    return self._rec("agent_refine", job_id, out)


FakeAgentAdapter.agent_refine_plan = _fake_refine_plan
_fake_init = FakeAgentAdapter.__init__


def _fake_init2(self, on_call=None):
    _fake_init(self, on_call)
    self.refine_boxes = {"keep": [0, 0, 1000, 1000], "old": [0, 0, 1000, 1000], "new": [0, 0, 1000, 1000]}


FakeAgentAdapter.__init__ = _fake_init2
