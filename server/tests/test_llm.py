"""instruction → 편집 스펙 (`server/llm.py`).

두 가지를 본다:

  1. **좌표 거부** — 이 모듈의 유일한 안전 속성. 모델이 좌표를 반환하면 거부하는가
  2. **키 유무로만 갈리는가** — 키가 없으면 폴백, 있으면 실제 호출

⚠️ 키가 없을 때 **skip 하지 않는다.** skip 은 자기를 보호하지 않는다 — 아무도
   안 도는 테스트는 있으나 마나다. 키가 없으면 **폴백 경로를 테스트한다.**
   실제 API 호출이 필요한 것만 `requires_api_key` 로 따로 묶는다.
"""

from __future__ import annotations

import os

import pytest

from server import llm
from server.llm import (
    CoordinateLeak,
    EditSpec,
    MalformedSpec,
    MaskSummary,
    fallback_spec,
    parse_edit_spec,
    plan_edit,
)

# ── 한글 지시 4종 — 세 op 를 전부 덮는다 ────────────────────────────────
KOREAN_INSTRUCTIONS = [
    ("머리를 주황색 할로윈 호박으로 바꿔", "replace_region"),  # 레벨2 (W5 육안 통과)
    ("머리를 3개로 만들어", "add"),                             # 레벨2 (D22)
    ("머리만 빨갛게", "replace_region"),                        # 레벨1 (색)
    ("머리를 지워", "remove"),
]

HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
requires_api_key = pytest.mark.skipif(
    not HAS_KEY, reason="ANTHROPIC_API_KEY 없음 — 폴백 경로 테스트가 대신 돈다"
)


# ══════════════════════════ 1. ★★ 음성 대조 — 좌표 거부
@pytest.mark.parametrize(
    "leaked",
    [
        {"op": "add", "target_prompt": "머리 3개", "factor": 3, "bbox": [0, 0, 0, 8, 8, 8]},
        {"op": "replace_region", "target_prompt": "호박", "factor": None,
         "voxel_coords": [[10, 11, 12]]},
        {"op": "replace_region", "target_prompt": "호박", "factor": None,
         "offset": [3, 0, 1]},
        {"op": "replace_region", "target_prompt": "호박", "factor": None,
         "mask_cells": [1, 2, 3]},
        {"op": "add", "target_prompt": "뿔", "factor": 2, "position": {"x": 1, "y": 2, "z": 3}},
        {"op": "replace_region", "target_prompt": "호박", "factor": None,
         "region": {"min": [0, 0, 0], "max": [8, 8, 8]}},
    ],
)
def test_coordinate_fields_are_rejected(leaked):
    """★★ **이 모듈의 유일한 안전 속성.** 모델이 좌표를 내면 거부한다.

    좌표의 유일한 진실은 클라이언트가 준 마스크다. 모델이 만든 좌표를 받아들이면
    "마스크 밖 불변" 이 정의상 깨지고, 그 위반은 예외 없이 조용히 일어난다 —
    D9 에서 본 것과 같은 실패 모양이다.

    구조화 출력의 `additionalProperties: false` 가 이미 막지만, 그건 API 의
    보장이다. 스키마를 완화하거나 모델을 갈아타도 이 검사는 남아야 한다.
    """
    with pytest.raises(CoordinateLeak, match="좌표"):
        parse_edit_spec(leaked)


def test_numeric_array_smuggled_into_a_string_field_is_rejected():
    """좌표는 키 이름 없이도 샐 수 있다 — 문자열 자리에 숫자 배열이 오는 경우."""
    with pytest.raises(CoordinateLeak, match="숫자 배열"):
        parse_edit_spec({"op": "add", "target_prompt": [10, 11, 12], "factor": None})


def test_clean_spec_survives_the_coordinate_check():
    """거부가 과잉이면 정상 스펙까지 막는다 — 그건 지표를 죽이는 것과 같다."""
    spec = parse_edit_spec(
        {"op": "add", "target_prompt": "머리 3개", "factor": 3}
    )
    assert spec.op == "add"
    assert spec.target_prompt == "머리 3개"
    assert spec.factor == 3.0
    assert spec.source == "llm"


def test_schema_has_no_field_that_can_carry_coordinates():
    """스키마 자체에 좌표 자리가 없어야 한다 — 거부는 2차 방어선이다."""
    props = llm.EDIT_SPEC_SCHEMA["properties"]
    assert set(props) == {"op", "target_prompt", "factor"}
    assert llm.EDIT_SPEC_SCHEMA["additionalProperties"] is False
    for name in props:
        assert not llm.COORDINATE_KEY_PATTERN.search(name), name


# ══════════════════════════ 2. 값 검증
@pytest.mark.parametrize(
    "bad, exc",
    [
        ({"op": "resize", "target_prompt": "호박", "factor": None}, MalformedSpec),
        ({"op": "add", "target_prompt": "   ", "factor": None}, MalformedSpec),
        ({"op": "add", "target_prompt": "x" * 500, "factor": None}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": 0}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": -3}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": 1e9}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": float("nan")}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": True}, MalformedSpec),
        ({"op": "add", "target_prompt": "머리", "factor": None, "note": "hi"}, MalformedSpec),
    ],
)
def test_bad_values_are_rejected(bad, exc):
    with pytest.raises(exc):
        parse_edit_spec(bad)


def test_malformed_is_not_silently_downgraded_to_fallback():
    """깨진 스펙을 폴백으로 삼키지 않는다.

    삼키면 "LLM 이 돌았다" 와 "폴백이 돌았다" 를 구분할 수 없고, 그 구분 불가가
    자연어 경로의 실제 상태를 가린다 (원칙 7).
    """
    with pytest.raises(MalformedSpec):
        parse_edit_spec({"op": "nope", "target_prompt": "호박", "factor": None})


# ══════════════════════════ 3. 폴백 경로 (키 없이 도는 것)
@pytest.mark.parametrize("instruction, _op", KOREAN_INSTRUCTIONS)
def test_fallback_keeps_the_instruction_verbatim(instruction, _op):
    """★ 키가 없으면 instruction 을 그대로 target_prompt 로 쓴다."""
    spec = fallback_spec(instruction)
    assert spec.op == "replace_region"
    assert spec.target_prompt == instruction
    assert spec.factor is None
    assert spec.source == "fallback"


def test_fallback_does_not_guess_the_op():
    """폴백이 op 를 추측하면 "LLM 없이도 대충 된다" 는 착시가 생긴다.

    "머리를 3개로" 는 명백히 `add` 지만 폴백은 그걸 맞히려 하지 않는다 —
    맞히기 시작하면 자연어 경로가 실제로 도는지를 아무도 확인하지 않게 된다.
    """
    assert fallback_spec("머리를 3개로 만들어").op == "replace_region"
    assert fallback_spec("머리를 지워").op == "replace_region"


def test_plan_edit_uses_fallback_when_key_is_absent(monkeypatch):
    """★ 키 유무 **로만** 갈린다. 다른 조건은 없다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for instruction, _ in KOREAN_INSTRUCTIONS:
        spec = plan_edit(instruction)
        assert spec.source == "fallback"
        assert spec.target_prompt == instruction


def test_plan_edit_rejects_empty_instruction(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MalformedSpec):
        plan_edit("   ")


def test_fallback_and_llm_produce_the_same_type():
    """하류가 분기하지 않도록 두 경로가 같은 타입을 낸다."""
    a = fallback_spec("머리만 빨갛게")
    b = parse_edit_spec({"op": "replace_region", "target_prompt": "빨간 머리", "factor": None})
    assert isinstance(a, EditSpec) and isinstance(b, EditSpec)
    assert set(a.as_dict()) == set(b.as_dict())
    assert a.source != b.source  # 어느 경로였는지는 항상 구분된다


# ══════════════════════════ 4. 마스크 요약 — 원시 좌표를 넣지 않는다
def test_mask_summary_carries_no_raw_coordinates():
    """★ 모델 입력에 셀 좌표가 들어가면 안 된다 — "어디를" 은 이미 정해져 있다."""
    from server.pipeline import build_mask, surface_voxelize, top_region_cells
    from server.tests.fixtures import snowman_mesh

    base = surface_voxelize(*snowman_mesh())
    mask = build_mask(top_region_cells(base, fraction=0.30), halo=1)
    summary = MaskSummary.from_mask(mask)

    assert summary.n_cells == mask.n_cells
    assert 0.0 < summary.grid_fraction < 1.0

    text = summary.describe()
    assert str(mask.n_cells) in text
    # 첫 셀의 좌표가 요약 문자열에 나타나면 원시 좌표가 새고 있는 것이다.
    first = mask.cells[0]
    assert f"{first[0]},{first[1]},{first[2]}" not in text.replace(" ", "")


def test_mask_summary_is_optional():
    """마스크 없이도 계획은 선다 — 요약은 크기 힌트일 뿐이다."""
    assert MaskSummary(n_cells=0).describe()


# ══════════════════════════ 5. 실제 API 호출 (키가 있을 때만)
@requires_api_key
@pytest.mark.parametrize("instruction, expected_op", KOREAN_INSTRUCTIONS)
def test_live_korean_instructions(instruction, expected_op):
    """★ 한글 지시 4종이 실제 호출에서 옳은 op 로 파싱되는지."""
    spec = plan_edit(instruction)
    assert spec.source == "llm"
    assert spec.op == expected_op, f"{instruction!r} → {spec.op} (기대 {expected_op})"
    assert spec.target_prompt.strip()
    # 지시문의 언어를 유지한다 — 한글 지시에 한글 target_prompt.
    assert any("가" <= c <= "힣" for c in spec.target_prompt), spec.target_prompt


@requires_api_key
def test_live_count_instruction_extracts_factor():
    """"머리를 3개로" 는 factor 3 을 낸다 (D22 레벨2)."""
    spec = plan_edit("머리를 3개로 만들어")
    assert spec.op == "add"
    assert spec.factor == 3.0


@requires_api_key
def test_live_never_returns_coordinates():
    """★★ 실제 모델이 좌표를 내지 않는지. 내면 `plan_edit` 이 던진다."""
    mask = MaskSummary(n_cells=3424, span=[18, 18, 14], grid_fraction=0.0131)
    for instruction, _ in KOREAN_INSTRUCTIONS:
        spec = plan_edit(instruction, mask=mask)
        assert set(spec.raw) <= {"op", "target_prompt", "factor"}


# ══════════════ 6. 요청 형태 검증 (키 없이 — 실호출 없이 계약을 지킨다)
class _StubBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _StubResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_StubBlock(text)]
        self.stop_reason = stop_reason
        self.stop_details = None


class _StubMessages:
    def __init__(self, response):
        self._response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class _StubClient:
    def __init__(self, response):
        self.messages = _StubMessages(response)


def _patch_client(monkeypatch, response):
    """anthropic.Anthropic 을 스텁으로 갈아끼운다. 네트워크를 쓰지 않는다."""
    import anthropic

    stub = _StubClient(response)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: stub)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return stub


def test_request_shape_matches_the_model_contract(monkeypatch):
    """★ 실호출 없이 요청 형태를 잠근다.

    Claude Opus 5 는 `temperature`/`top_p`/`top_k` 를 **400 으로 거부**하고,
    `budget_tokens` 도 거부한다. 키가 없어 실호출을 못 도는 동안에도 이 계약이
    깨지지 않게 여기서 검사한다 — 키가 생기는 순간 400 을 맞는 것보다 낫다.
    """
    stub = _patch_client(
        monkeypatch,
        _StubResponse('{"op":"add","target_prompt":"머리 3개","factor":3}'),
    )
    spec = plan_edit("머리를 3개로 만들어")

    kw = stub.messages.kwargs
    assert kw["model"] == "claude-opus-5"
    # 구조화 출력 — output_format(구버전)이 아니라 output_config.format 이다.
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"] is llm.EDIT_SPEC_SCHEMA
    assert kw["output_config"]["effort"] == "low"
    # 🔴 Opus 5 에서 400 을 내는 인자들이 섞이면 안 된다.
    for banned in ("temperature", "top_p", "top_k", "thinking", "output_format"):
        assert banned not in kw, f"{banned} 는 Claude Opus 5 에서 거부된다"
    # 호출은 하나다. 프레임워크도 도구 루프도 없다.
    assert "tools" not in kw
    assert spec.source == "llm" and spec.op == "add" and spec.factor == 3.0


def test_model_is_overridable_by_env(monkeypatch):
    stub = _patch_client(
        monkeypatch, _StubResponse('{"op":"remove","target_prompt":"머리","factor":null}')
    )
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    plan_edit("머리를 지워")
    assert stub.messages.kwargs["model"] == "claude-sonnet-5"


def test_mask_summary_reaches_the_prompt_without_coordinates(monkeypatch):
    stub = _patch_client(
        monkeypatch, _StubResponse('{"op":"add","target_prompt":"머리 3개","factor":3}')
    )
    plan_edit("머리를 3개로", mask=MaskSummary(n_cells=3424, span=[18, 18, 14]))

    user_text = stub.messages.kwargs["messages"][0]["content"]
    assert "3424" in user_text          # 크기는 들어간다
    assert "18×18×14" in user_text
    # 시스템 프롬프트가 좌표 금지를 명시한다 (프롬프트는 2차 방어선일 뿐이다).
    assert "좌표" in stub.messages.kwargs["system"]


def test_refusal_is_raised_not_silently_fallen_back(monkeypatch):
    """모델이 거절하면 예외로 올린다. 조용히 폴백하면 상태를 알 수 없다."""
    _patch_client(monkeypatch, _StubResponse("", stop_reason="refusal"))
    with pytest.raises(llm.LLMRefused):
        plan_edit("머리를 지워")


def test_truncated_response_is_raised(monkeypatch):
    _patch_client(monkeypatch, _StubResponse('{"op":"add"', stop_reason="max_tokens"))
    with pytest.raises(MalformedSpec, match="max_tokens"):
        plan_edit("머리를 3개로")


def test_live_path_rejects_coordinates_end_to_end(monkeypatch):
    """★★ 모델이 좌표를 내면 `plan_edit` 이 통째로 거부한다 (파서만이 아니라)."""
    _patch_client(
        monkeypatch,
        _StubResponse(
            '{"op":"add","target_prompt":"머리 3개","factor":3,'
            '"voxel_coords":[[10,11,12]]}'
        ),
    )
    with pytest.raises(CoordinateLeak):
        plan_edit("머리를 3개로 만들어")
