"""`op` → 소비자 배선 (`server/dispatch.py`). **방어가 있는 자리다** (D26).

이 파일이 막는 것은 하나다: **조용한 실패.**

`add` 를 assemble 로 보내면 예외가 안 난다. 지표도 돈다. 신규 복셀이 0 에 가깝게
나오고 그게 "모델이 약하다" 로 오독된다 — 이 프로젝트가 여섯 번 물린 모양 그대로다.
`recolor` 를 assemble 로 보내면 색이 통째로 사라지고 "색이 안 바뀌었다" 로 보인다.
둘 다 **예외 없이** 틀린 결론을 만든다.

그리고 **자동 강등**을 금지한다. `add` 를 `replace_region` 으로 조용히 내려 주면
게이트가 "레벨2 를 했다" 고 적으면서 실제로는 레벨1 결과를 재게 된다.

D30 — `op` 를 늘리려면 `llm.py` 의 **세 곳**을 동시에 고쳐야 한다. 그 결속도
여기서 잠근다 (하나만 고치면 스키마가 거부하거나 프롬프트가 반대로 유도한다).
"""

from __future__ import annotations

import pytest

from server import dispatch, llm
from server.dispatch import (
    CONSUMERS,
    UnsupportedOp,
    capability_table,
    check_supported,
)
from server.llm import OPS, EditSpec


def _spec(op: str, target: str = "주황색 할로윈 호박", **kw) -> EditSpec:
    return EditSpec(op=op, target_prompt=target, **kw)


# ══════════════════════════ 1. 능력표 — recolor 포함
#
# 정본은 server/README.md 의 D26 매핑표다. 여기 표가 그것과 어긋나면 둘 중 하나가
# 틀린 것이고, 그 불일치가 조용한 오배선으로 이어진다.
EXPECTED_CAPABILITY = {
    "assemble":  {"replace_region": True,  "recolor": False, "add": False, "remove": True},
    "recolor":   {"replace_region": False, "recolor": True,  "add": False, "remove": False},
    "voxhammer": {"replace_region": True,  "recolor": False, "add": True,  "remove": True},
}


def test_capability_table_matches_d26():
    """★ 능력표 전체를 못박는다. 한 칸이라도 바뀌면 여기서 걸린다."""
    assert capability_table() == EXPECTED_CAPABILITY


def test_capability_table_covers_every_op():
    """`OPS` 에 op 를 추가하면 모든 소비자가 그 op 에 대해 입장을 밝혀야 한다.

    빠뜨리면 `capability_table()` 에 구멍이 생기고, 구멍은 기본값(거부)으로
    조용히 메워진다 — 그러면 왜 거부됐는지 아무도 모른다.
    """
    table = capability_table()
    for consumer, row in table.items():
        assert set(row) == set(OPS), f"{consumer}: {set(OPS) ^ set(row)}"


def test_every_rejection_carries_a_reason_and_a_remedy():
    """거부 메시지가 "지원 안 함" 뿐이면 다음 세션이 '왜' 를 다시 조사한다."""
    for name, cap in CONSUMERS.items():
        for op in OPS:
            if op in cap.supported:
                continue
            reason = cap.rejects(op)
            assert reason and reason != "지원하지 않는다", f"{name}/{op}: 이유가 없다"
            assert cap.remedy, f"{name}: 대안이 없다"


# ══════════════════════════ 2. ★★ add → assemble 거부
def test_add_to_assemble_is_rejected():
    """★★ **가장 위험한 오배선.** 예외도 화면 변화도 없이 실패하는 조합이다.

    assemble 은 마스크를 비우고 도너를 정수 이동으로 끼운다 — 없던 가지를
    몸통에서 갈라져 나오게 만들 수 없다 (D22 ①).
    """
    with pytest.raises(UnsupportedOp) as exc:
        check_supported(_spec("add", "머리 3개", factor=3.0), "assemble")

    err = exc.value
    assert err.op == "add" and err.consumer == "assemble"
    assert "D22" in err.reason
    assert "voxhammer" in err.remedy
    # 응답 본문에 그대로 실을 수 있어야 한다 — 문자열 파싱을 강요하지 않는다.
    assert err.as_dict()["error"] == "unsupported_op"


def test_add_to_voxhammer_is_allowed():
    """레벨2 의 유일한 경로다 — 여기까지 막으면 D22 가 갈 곳이 없다."""
    check_supported(_spec("add", "머리 3개", factor=3.0), "voxhammer")


# ══════════════════════════ 3. ★ recolor 배선 (W8 판단)
def test_recolor_to_assemble_is_rejected():
    """★ 색 지시를 assemble 로 보내면 **색이 통째로 사라진다** (D24 원인 진단).

    assemble 은 `occupancy_to_mesh` 를 거치는데 그 함수는 정점·면만 낸다.
    `add`→assemble 과 같은 종류의 조용한 실패다.
    """
    with pytest.raises(UnsupportedOp, match="색"):
        check_supported(_spec("recolor", "빨간색"), "assemble")


def test_recolor_to_voxhammer_is_rejected():
    """★★ **W8 판단.** VoxHammer 는 색만 바꾸는 일에 쓰지 않는다.

    레벨1 의 정의는 **기하 불변**인데 VoxHammer 는 재디코딩하므로 기하가 흔들린다
    (A5000 실측 마스크 밖 IoU 0.853 = 잡음 바닥값 대비 2.10배). 색만 바꾸자고
    그 경로를 타면 **레벨1 의 판정 조건 자체가 무너지고** GPU 까지 쓴다.
    recolor 경로는 같은 일을 기하 바이트 100% 보존으로 한다 (D24).

    ⚠️ PR #3 원본은 `voxhammer.supported = frozenset(OPS)` 라 recolor 를 받았다.
       W8 에서 뺐다 — 이 테스트가 그 판단을 잠근다.
    """
    with pytest.raises(UnsupportedOp) as exc:
        check_supported(_spec("recolor", "빨간색"), "voxhammer")
    assert "기하 불변" in exc.value.reason
    assert "consumer='recolor'" in exc.value.remedy


def test_recolor_to_recolor_is_allowed():
    check_supported(_spec("recolor", "빨간색"), "recolor")


def test_shape_ops_are_rejected_by_the_recolor_consumer():
    """recolor 경로는 기하를 만들지도 지우지도 않는다."""
    for op in ("replace_region", "add", "remove"):
        with pytest.raises(UnsupportedOp):
            check_supported(_spec(op), "recolor")


# ══════════════════════════ 4. ★★ 자동 강등 금지
def test_dispatch_never_rewrites_the_op():
    """★★ **자동 강등이 없다.** op 는 들어온 그대로 나간다.

    `add` → `replace_region` 으로 조용히 내려 주면 게이트가 "레벨2 를 했다" 고
    적으면서 **실제로는 레벨1 결과를 잰다.** 그 오기록은 되돌릴 수 없다 —
    숫자는 남고 무엇을 잰 것인지는 안 남는다.
    """
    for op in OPS:
        for consumer, row in capability_table().items():
            if not row[op]:
                continue
            out = dispatch.dispatch(_spec(op, factor=3.0 if op == "add" else None), consumer)
            assert out["op"] == op, f"{consumer}: {op} → {out['op']} 로 바뀌었다"
            assert out["consumer"] == consumer


def test_unsupported_op_raises_instead_of_downgrading():
    """못 하는 것은 **멈춘다.** 대신 할 수 있는 op 로 바꿔 주지 않는다."""
    with pytest.raises(UnsupportedOp):
        dispatch.dispatch(_spec("add", "머리 3개", factor=3.0), "assemble")


def test_check_supported_returns_none_not_bool():
    """★ bool 을 주면 호출부가 검사하고도 무시할 수 있다 — 그게 곧 조용한 실패다."""
    assert check_supported(_spec("replace_region"), "assemble") is None


def test_dispatch_preserves_factor_and_source():
    """factor 를 잃으면 "머리를 3개로" 가 "머리를 여러 개로" 가 된다."""
    out = dispatch.dispatch(
        EditSpec(op="add", target_prompt="머리 3개", factor=3.0, source="llm"),
        "voxhammer",
    )
    assert out["factor"] == 3.0
    assert out["source"] == "llm"


def test_fallback_specs_are_dispatched_but_marked():
    """폴백 스펙도 배선은 된다 — 다만 `source` 로 구분된다.

    폴백은 op 를 추측하지 않으므로 항상 `replace_region` 이다. 소비자가 그것을
    "자연어 경로가 돌았다" 로 읽으면 안 된다.
    """
    spec = llm.fallback_spec("머리를 3개로 만들어")
    out = dispatch.dispatch(spec, "assemble")
    assert out["op"] == "replace_region"     # 추측하지 않는다
    assert out["source"] == "fallback"       # 그리고 그 사실이 남는다


# ══════════════════════════ 5. 미지 소비자
def test_unknown_consumer_is_rejected():
    """오타 난 소비자 이름이 조용히 통과하면 아무 데도 안 간다."""
    with pytest.raises(UnsupportedOp) as exc:
        check_supported(_spec("replace_region"), "assemle")   # 오타
    assert "모르는 소비자" in exc.value.reason
    assert "assemble" in exc.value.reason      # 아는 이름을 알려준다


@pytest.mark.parametrize("consumer", ["", "VoxHammer", "voxhammer ", None])
def test_consumer_name_is_not_normalised(consumer):
    """이름을 관대하게 고쳐 주지 않는다 — 관대함이 오배선을 숨긴다."""
    with pytest.raises(UnsupportedOp):
        check_supported(_spec("replace_region"), consumer)


# ══════════════════════════ 6. D30 — llm.py 의 세 곳이 함께 움직이는가
def test_d30_ops_constant_and_schema_enum_agree():
    """① `OPS` 와 ② `EDIT_SPEC_SCHEMA.op.enum` 이 같은가.

    enum 이 뒤처지면 **API 가 그 op 를 낼 구조적 방법이 없다**
    (`additionalProperties:false` + enum). W7 에서 "머리만 빨갛게" 가
    `replace_region` 을 낸 것이 정확히 그 상태였다 — 모델 실패가 아니었다.
    """
    assert llm.EDIT_SPEC_SCHEMA["properties"]["op"]["enum"] == list(OPS)


def test_d30_every_op_appears_in_the_system_prompt():
    """③ 시스템 프롬프트가 모든 op 를 설명하는가.

    프롬프트가 뒤처지면 스키마는 받아 주는데 모델이 그 op 를 **모른다.**
    ①②는 `list(OPS)` 로 구조적으로 묶여 있지만 프롬프트는 자유 텍스트라
    조용히 어긋난다 — 그래서 여기서 잠근다.
    """
    prompt = llm._SYSTEM
    for op in OPS:
        assert op in prompt, f"시스템 프롬프트에 {op} 설명이 없다 (D30 ③)"


def test_d30_prompt_distinguishes_recolor_from_replace_region():
    """색만 바꾸는 지시가 `replace_region` 으로 가면 색이 사라진다 (D24).

    프롬프트가 그 구분을 명시하는지 본다 — "색·재질 변경 포함" 이
    `replace_region` 쪽에 남아 있으면 모델을 반대로 유도한다.
    """
    prompt = llm._SYSTEM
    replace_block = prompt.split("- replace_region", 1)[1].split("- recolor", 1)[0]
    assert "색" not in replace_block, (
        "replace_region 설명이 아직 색을 포함한다 — 모델이 recolor 를 안 고른다 (D30 ③)"
    )
    recolor_block = prompt.split("- recolor", 1)[1].split("- add", 1)[0]
    assert "색" in recolor_block and "형태는 그대로" in recolor_block


def test_d30_dispatch_knows_every_op_in_the_schema():
    """스키마가 낼 수 있는 op 를 소비자 표가 전부 알고 있는가.

    llm.py 만 고치고 dispatch 를 안 고치면, 모델이 낸 op 가 **어느 소비자도
    모르는 값**이 되어 전부 거부된다 — 자연어 경로가 통째로 죽는다.
    """
    known = set().union(*(cap.supported for cap in CONSUMERS.values()))
    assert known == set(OPS), f"소비자가 모르는 op: {set(OPS) - known}"


# ══════════════════════════ 7. D28-a — 격자 출처를 **구조로** 강제한다
def test_mask_carries_its_grid_source():
    """★ D28-a — 마스크가 자기 격자 출처를 들고 다닌다.

    D28 의 `assert_slat_grid()` 는 호출부가 자발적으로 부를 때만 돈다. 좌표 함정이
    이미 다섯이라 자발성에 기댈 자리가 아니다.
    """
    import numpy as np

    from server.pipeline import VOXEL_GRID_SOURCE, build_mask
    from server.pipeline.frames import SURFACE_VOXELIZATION_SOURCE

    cells = np.array([[10, 10, 10], [11, 10, 10]], dtype=np.int64)

    # 기본값은 **진단용**이다 — 정본이 아니다.
    default = build_mask(cells, halo=1)
    assert default.grid_source == SURFACE_VOXELIZATION_SOURCE
    assert not default.is_canonical_grid

    slat = build_mask(cells, halo=1, grid_source=VOXEL_GRID_SOURCE)
    assert slat.is_canonical_grid


def test_diagnostic_mask_is_refused_for_judgement():
    """★★ 자체 복셀화로 만든 마스크로는 판정할 수 없다 (D28-a).

    실측: 같은 dragon-c 를 3090 z=45 / A5000 z=44 로 봤다. 축이 맞아도 한 칸
    밀리고, 그 한 칸이 "목 극소점 위" 와 "극소점에서" 를 갈랐다.
    """
    import numpy as np
    import pytest as _pytest

    from server.pipeline import VOXEL_GRID_SOURCE, build_mask
    from server.pipeline.frames import GridSourceMismatch

    cells = np.array([[10, 10, 10]], dtype=np.int64)

    with _pytest.raises(GridSourceMismatch, match="slat_coords"):
        build_mask(cells, halo=1).require_slat_grid("head3mask")

    build_mask(cells, halo=1, grid_source=VOXEL_GRID_SOURCE).require_slat_grid()


def test_empty_grid_source_is_rejected():
    """출처를 빈 값으로 두면 검사가 무력해진다."""
    import numpy as np
    import pytest as _pytest

    from server.pipeline import build_mask

    cells = np.array([[10, 10, 10]], dtype=np.int64)
    for bad in ("", "   "):
        with _pytest.raises(ValueError, match="grid_source"):
            build_mask(cells, halo=1, grid_source=bad)
