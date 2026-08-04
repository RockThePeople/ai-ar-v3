"""instruction → 편집 스펙. **단일 LLM 호출, 구조화 출력.**

목표 문장에서 마지막으로 빠져 있던 조각이다. 지금까지의 관통은 "미리 만든 호박을
끼운 것" 이었고, 자연어 → 편집 스펙 경로는 **0회** 돌았다.

    "머리를 주황색 할로윈 호박으로 바꿔"  →  {op: "replace_region", target_prompt: "주황색 할로윈 호박"}

────────────────────────────────────────────────────────────────────────
🔴 설계 불변식 — **LLM 은 좌표를 만들지 않는다**
────────────────────────────────────────────────────────────────────────
좌표의 유일한 진실은 **클라이언트가 준 마스크**다. 마스크 밖 불변은 서버 코드가
강제한다 (`pipeline/splice.py` 의 `strict_containment`, `pipeline/package.py` 의
부모 바이트 승계) — 프롬프트로 **부탁하지 않는다.**

근거는 두 가지다:
  ① LLM 의 3D 좌표 직접 예측은 경계 이탈이 잦다 (LayoutVLM 계열에서 지적됨).
     경계를 벗어난 좌표는 `place_cells()` 가 거부하지만, 거부되지 않을 만큼만
     틀린 좌표는 **조용히 다른 물체를 만든다** — D9 에서 이미 본 실패 모양이다.
  ② 프롬프트로 부탁한 제약은 지켜지지 않는다. 방법론 5조 4번:
     "규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다."

그래서 이 모듈은 **좌표를 담을 수 있는 필드를 스키마에 두지 않고**, 모델이 그런
필드를 만들어 보내면 `CoordinateLeak` 으로 **거부한다.** 스키마의
`additionalProperties: false` 에만 기대지 않는다 — 그건 API 의 보장이고, 우리
불변식은 우리 코드가 지켜야 한다. `server/tests/test_llm.py` 의 음성 대조가
그 거부를 검증한다. **이 모듈의 유일한 안전 속성이다.**

입력에도 원시 좌표를 넣지 않는다. 마스크는 `MaskSummary` 로 요약해서(셀 수·bbox·
격자 대비 비율) 넣는다 — 모델이 "어디를" 을 정할 근거를 갖지 못하게 한다.

────────────────────────────────────────────────────────────────────────
두 소비자, 하나의 스펙 (D22)
────────────────────────────────────────────────────────────────────────
같은 `{op, target_prompt}` 를 두 경로가 소비한다. **분기는 이 모듈 밖에 둔다** —
여기서 경로를 고르면 지표·게이트가 LLM 출력에 의존하게 된다.

    op                 assemble 경로              VoxHammer 경로
    ─────────────────  ─────────────────────────  ──────────────────────────
    replace_region     도너 생성 → 마스크에 삽입   마스크 조건부 편집
                       (레벨1: 머리를 호박으로)
    add                🔴 **원리적으로 불가**       마스크 조건부 편집
                       (없던 가지를 뻗게 할 수     (레벨2: 머리를 3개로)
                        없다 — D22 ①)
    remove             마스크를 비우기만 한다       마스크 조건부 편집

⚠️ D22 로 레벨2("용의 머리를 3개로")가 확정되면서 `add` 는 **VoxHammer 전용**이
   됐다. 소비자가 `add` 를 assemble 로 보내면 조용히 아무것도 안 일어난다 —
   그 방어는 소비자 쪽에 둔다.

────────────────────────────────────────────────────────────────────────
키가 없으면 규칙 기반 폴백
────────────────────────────────────────────────────────────────────────
`ANTHROPIC_API_KEY` 가 없으면 instruction 을 그대로 `target_prompt` 로 쓰고
`op="replace_region"` 으로 둔다. **키 유무로만 갈린다** — 다른 조건은 없다.
폴백도 같은 `EditSpec` 을 내므로 하류는 분기하지 않는다. `spec.source` 로
어느 경로였는지 항상 알 수 있다.

오케스트레이션 프레임워크(LangGraph/CrewAI 등)는 도입하지 않는다. 호출이 하나다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

__all__ = [
    "COORDINATE_KEY_PATTERN",
    "EDIT_SPEC_SCHEMA",
    "OPS",
    "CoordinateLeak",
    "EditSpec",
    "LLMRefused",
    "MaskSummary",
    "MalformedSpec",
    "parse_edit_spec",
    "plan_edit",
]

# 환경변수는 §7 규칙대로 이름만 쓴다. 값은 .env 에.
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MODEL_ENV = "LLM_MODEL"
_DEFAULT_MODEL = "claude-opus-5"

OPS = ("replace_region", "add", "remove")

# 구조화 출력 스키마. **좌표를 담을 수 있는 필드가 하나도 없다** — 그게 요점이다.
# 수치 제약(minimum/maximum)은 구조화 출력이 지원하지 않으므로 factor 범위는
# 아래 `parse_edit_spec` 이 직접 검증한다.
EDIT_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": list(OPS),
            "description": (
                "replace_region: 마스크 영역의 내용을 다른 것으로 바꾼다. "
                "add: 마스크 영역에 없던 구조를 더한다(개수를 늘리는 지시 포함). "
                "remove: 마스크 영역의 내용을 지운다."
            ),
        },
        "target_prompt": {
            "type": "string",
            "description": (
                "그 자리에 무엇이 있어야 하는지를 묘사하는 짧은 명사구. "
                "위치·좌표·크기 수치를 쓰지 않는다. 지시문의 언어를 유지한다."
            ),
        },
        "factor": {
            "type": ["number", "null"],
            "description": (
                "개수를 명시한 지시에서만 그 개수. 예: '머리를 3개로' → 3. "
                "개수 지시가 아니면 null."
            ),
        },
    },
    "required": ["op", "target_prompt", "factor"],
    "additionalProperties": False,
}

# 좌표를 담으려는 키를 이름으로 잡는다. 스키마 밖 키는 어차피 전부 거부하지만,
# 이 목록에 걸리면 **왜** 거부됐는지가 오류 메시지에 남는다.
#
# ⚠️ 단어경계(`\b`)로만 잡으면 안 된다 — `_` 는 단어 문자라서 `voxel_coords` 안의
#    `coords` 가 `\bcoords\b` 에 안 걸린다. 실제로 테스트가 이걸 잡았다.
#    그래서 키를 **토큰으로 분해**한 뒤 대조한다 (`_coordinate_tokens`).
_COORDINATE_WORDS = frozenset(
    """
    bbox aabb box bounds bound coord coords coordinate coordinates
    cell cells voxel voxels grid mask region span extent
    offset origin position pos translation transform center centre pivot
    min max lo hi start end from to
    x y z xyz index indices idx location loc point points vertex vertices
    """.split()
)

# 진단·문서용. 어떤 이름을 좌표로 보는지 한눈에 보이게 남긴다.
COORDINATE_KEY_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(" + "|".join(sorted(_COORDINATE_WORDS, key=len, reverse=True))
    + r")(?![a-z0-9])"
)


def _coordinate_tokens(name: str) -> list:
    """키 이름을 토큰으로 쪼개 좌표 낱말을 찾는다.

    `voxel_coords` · `bboxMin` · `mask-cells` 를 전부 같은 방식으로 본다 —
    구분자(`_`, `-`, `.`)와 camelCase 경계에서 자른다.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]
    return [t for t in tokens if t in _COORDINATE_WORDS]

_MAX_FACTOR = 16.0
_MAX_PROMPT_CHARS = 200


class LLMError(RuntimeError):
    """이 모듈의 오류 기반 클래스."""


class CoordinateLeak(LLMError):
    """🔴 모델이 좌표(로 보이는 것)를 반환했다. **이 모듈의 유일한 안전 속성.**

    좌표의 유일한 진실은 클라이언트 마스크다. 모델이 만든 좌표를 받아들이면
    "마스크 밖 불변" 이 정의상 깨지고, 그 위반은 예외 없이 조용히 일어난다.
    그래서 값을 무시하고 넘어가지 않고 **거부한다** — 무시하면 다음 세션이
    "어차피 무시되니 넣어도 된다" 고 판단한다.
    """


class MalformedSpec(LLMError):
    """스키마는 맞췄지만 값이 쓸 수 없다 (빈 target_prompt, 범위 밖 factor 등)."""


class LLMRefused(LLMError):
    """모델이 안전상 거절했다 (`stop_reason == "refusal"`). 폴백으로 내려간다."""


@dataclass(frozen=True)
class MaskSummary:
    """모델에 넣는 마스크 **요약**. 원시 좌표는 넣지 않는다.

    셀 수와 크기만 준다. "어디를" 은 이미 클라이언트가 정했고, 모델이 그것을
    다시 정할 근거를 갖게 하면 안 된다 (모듈 docstring 참고).
    """

    n_cells: int
    span: Sequence[int] = (0, 0, 0)
    grid_fraction: Optional[float] = None

    @classmethod
    def from_mask(cls, mask: Any) -> "MaskSummary":
        """`pipeline.mask.MaskResult` → 요약. 셀 배열은 여기서 버려진다."""
        import numpy as np  # noqa: PLC0415

        from deltacontract.coords import VOXEL_RES  # type: ignore[import-not-found]

        cells = np.asarray(mask.cells, dtype=np.int64).reshape(-1, 3)
        span = (cells.max(axis=0) - cells.min(axis=0) + 1).tolist() if cells.size else [0, 0, 0]
        return cls(
            n_cells=int(cells.shape[0]),
            span=span,
            grid_fraction=float(cells.shape[0]) / float(VOXEL_RES**3),
        )

    def describe(self) -> str:
        """프롬프트에 넣을 한 줄. 좌표가 없다는 것을 눈으로 확인할 수 있게 짧게."""
        parts = [f"선택 영역 셀 수 {self.n_cells}"]
        if any(self.span):
            parts.append(f"크기 {self.span[0]}×{self.span[1]}×{self.span[2]} 복셀")
        if self.grid_fraction is not None:
            parts.append(f"전체 격자의 {self.grid_fraction * 100:.2f}%")
        return " · ".join(parts)


@dataclass(frozen=True)
class EditSpec:
    """instruction 을 옮긴 편집 스펙. **좌표 필드가 없다.**

    `source` 는 이 스펙이 어디서 왔는지다 — `"llm"` 또는 `"fallback"`.
    게이트 판정에 LLM 유무가 섞이지 않도록 항상 기록한다 (원칙 7).
    """

    op: str
    target_prompt: str
    factor: Optional[float] = None
    source: str = "llm"
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "target_prompt": self.target_prompt,
            "factor": self.factor,
            "source": self.source,
        }


# ══════════════════════════════════════════════════════════ 파싱 · 검증
def parse_edit_spec(raw: Mapping[str, Any], *, source: str = "llm") -> EditSpec:
    """모델 출력(dict) → `EditSpec`. **좌표가 보이면 거부한다.**

    구조화 출력의 `additionalProperties: false` 가 이미 막아 주지만, 그건 API 의
    보장이지 우리 불변식이 아니다. 여기서 다시 막는다 — 스키마를 완화하거나
    다른 모델로 갈아타도 이 검사는 남는다.
    """
    if not isinstance(raw, Mapping):
        raise MalformedSpec(f"객체가 아니다: {type(raw).__name__}")

    allowed = set(EDIT_SPEC_SCHEMA["properties"])
    extra = [k for k in raw if k not in allowed]
    if extra:
        coordinate_like = [k for k in extra if _coordinate_tokens(k)]
        if coordinate_like:
            raise CoordinateLeak(
                f"모델이 좌표 필드를 반환했다: {sorted(coordinate_like)}. "
                "좌표의 유일한 진실은 클라이언트가 준 마스크다 — 모델이 만든 좌표는 "
                "받지 않는다. 마스크 밖 불변은 서버 코드가 강제한다."
            )
        raise MalformedSpec(f"스키마 밖 필드: {sorted(extra)}")

    # 값 쪽에도 좌표가 숨을 수 있다 (예: target_prompt 가 리스트로 온다).
    for key in ("op", "target_prompt"):
        if key in raw and not isinstance(raw[key], str):
            raise CoordinateLeak(
                f"{key} 가 문자열이 아니라 {type(raw[key]).__name__} 다: {raw[key]!r}. "
                "숫자 배열은 좌표일 수 있으므로 받지 않는다."
            )

    op = raw.get("op")
    if op not in OPS:
        raise MalformedSpec(f"op 이 {OPS} 중 하나가 아니다: {op!r}")

    target = (raw.get("target_prompt") or "").strip()
    if not target:
        raise MalformedSpec("target_prompt 가 비었다")
    if len(target) > _MAX_PROMPT_CHARS:
        raise MalformedSpec(
            f"target_prompt 가 너무 길다 ({len(target)}자 > {_MAX_PROMPT_CHARS})"
        )

    factor = raw.get("factor")
    if factor is not None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise MalformedSpec(f"factor 가 수가 아니다: {factor!r}")
        factor = float(factor)
        if factor != factor or factor in (float("inf"), float("-inf")):
            raise MalformedSpec(f"factor 가 유한수가 아니다: {factor}")
        if not (0.0 < factor <= _MAX_FACTOR):
            raise MalformedSpec(f"factor 가 (0, {_MAX_FACTOR}] 범위 밖이다: {factor}")

    return EditSpec(
        op=op, target_prompt=target, factor=factor, source=source, raw=dict(raw)
    )


def _extract_json(text: str) -> Dict[str, Any]:
    """응답 텍스트 → dict. 구조화 출력이라 보통 그대로 파싱되지만 방어적으로 연다."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise MalformedSpec(f"JSON 을 못 읽었다: {e}") from e
    raise MalformedSpec(f"JSON 객체가 없다: {text[:120]!r}")


# ══════════════════════════════════════════════════════════ 프롬프트
_SYSTEM = """\
너는 3D 오브젝트 국소 편집 지시를 편집 스펙으로 옮긴다. JSON 하나만 낸다.

## 네가 정하는 것
- op            무엇을 하는 연산인가 (replace_region / add / remove)
- target_prompt 그 자리에 무엇이 있어야 하는가 (짧은 명사구)
- factor        개수를 명시한 지시에서만 그 개수, 아니면 null

## 네가 정하지 않는 것 — 중요
**어디를 편집할지는 이미 정해져 있다.** 사용자가 화면에서 영역을 직접 골랐고,
그 선택이 좌표의 유일한 진실이다. 너는 좌표·경계상자·복셀 인덱스·오프셋·크기
수치를 **만들지 않는다.** 그런 값을 내면 요청 전체가 거부된다.
선택 영역 정보는 "얼마나 큰가" 를 알려주려고 주는 것이지 "어디인가" 를
다시 정하라는 뜻이 아니다.

## op 고르기
- replace_region  그 자리의 것을 다른 것으로 바꾼다.
                  "머리를 호박으로", "머리만 빨갛게" (색·재질 변경 포함)
- add             없던 구조를 더한다. 개수를 늘리는 지시가 여기 온다.
                  "머리를 3개로", "뿔을 달아"
- remove          그 자리의 것을 지운다. "머리를 지워"

## target_prompt 쓰기
- 결과물의 묘사다. 동사가 아니라 명사구로.
  "머리를 주황색 할로윈 호박으로 바꿔" → "주황색 할로윈 호박"
  "머리만 빨갛게"                     → "빨간색 머리"
  "머리를 3개로 만들어"               → "머리 3개"
- **지시문의 언어를 그대로 유지한다.** 한국어 지시면 한국어로 쓴다.
- 위치·크기 수치를 넣지 않는다.
"""


def _build_user_message(instruction: str, mask: Optional[MaskSummary]) -> str:
    lines = [f"지시: {instruction.strip()}"]
    if mask is not None:
        lines.append(f"선택 영역(크기 정보만): {mask.describe()}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════ 진입점
def plan_edit(
    instruction: str,
    *,
    mask: Optional[MaskSummary] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 2048,
) -> EditSpec:
    """instruction → `EditSpec`. **호출 1회.**

    `ANTHROPIC_API_KEY` 가 없으면 규칙 기반 폴백으로 내려간다 (모듈 docstring).
    키 유무 **외에** 폴백으로 가는 조건은 없다 — 모델이 거절하거나 스펙이 깨져
    나오면 그건 예외로 올라간다. 조용히 폴백하면 "LLM 이 돌았다" 와 "폴백이
    돌았다" 를 구분할 수 없게 되고, 그게 이 프로젝트가 여섯 번 물린 모양이다.

    Raises:
        CoordinateLeak: 모델이 좌표를 반환했다.
        MalformedSpec:  스펙 값이 쓸 수 없다.
        LLMRefused:     모델이 안전상 거절했다.
    """
    if not instruction or not instruction.strip():
        raise MalformedSpec("instruction 이 비었다")

    key = api_key or os.environ.get(_API_KEY_ENV)
    if not key:
        return fallback_spec(instruction)

    try:
        import anthropic  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 환경 의존
        raise ImportError(
            f"{_API_KEY_ENV} 가 있는데 anthropic SDK 가 없다: pip install anthropic. "
            "키를 지우면 규칙 기반 폴백으로 돈다."
        ) from e

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=model or os.environ.get(_MODEL_ENV) or _DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=_SYSTEM,
        # 짧은 구조화 추출이라 깊게 생각할 일이 없다. max_tokens 는 thinking 과
        # 응답을 **합쳐서** 제한하므로 여유를 둔다.
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": EDIT_SPEC_SCHEMA},
        },
        messages=[{"role": "user", "content": _build_user_message(instruction, mask)}],
    )

    # 안전 분류기가 거절하면 content 가 비거나 부분만 온다. 먼저 본다.
    if response.stop_reason == "refusal":
        raise LLMRefused(
            f"모델이 거절했다 (category="
            f"{getattr(response.stop_details, 'category', None)}). 지시: {instruction!r}"
        )
    if response.stop_reason == "max_tokens":
        raise MalformedSpec(
            f"응답이 max_tokens({max_tokens})에서 잘렸다 — 값을 올려라"
        )

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise MalformedSpec(f"텍스트 블록이 없다 (stop_reason={response.stop_reason})")

    return parse_edit_spec(_extract_json(text), source="llm")


def fallback_spec(instruction: str) -> EditSpec:
    """규칙 기반 폴백. instruction 을 그대로 `target_prompt` 로 쓴다.

    분류를 흉내내지 않는다 — 키워드 매칭으로 op 를 추측하면 "LLM 없이도 대충
    된다" 는 착시가 생기고, 그 착시가 자연어 경로의 실제 상태를 가린다.
    `op` 는 가장 보수적인 `replace_region` 으로 고정하고 `source="fallback"` 을
    남긴다. 게이트는 그 필드를 보고 판단하면 된다.
    """
    target = instruction.strip()
    if not target:
        raise MalformedSpec("instruction 이 비었다")
    if len(target) > _MAX_PROMPT_CHARS:
        target = target[:_MAX_PROMPT_CHARS]
    return EditSpec(
        op="replace_region", target_prompt=target, factor=None, source="fallback"
    )
