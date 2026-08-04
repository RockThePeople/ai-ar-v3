"""`delta` — 부기가 **점유**로 정해지는가, 승계가 실제로 승계인가 (D45 · D54 · D61-a).

이 모듈의 방어 넷을 합성으로 잠근다. 실자산(runG)으로 한 번 돌린 것은 실험이고,
실험은 회귀를 못 막는다 — 다음 세션이 규칙을 바꿔도 실험은 다시 안 돈다.

  ① 해시가 전부 달라도 **점유가 같으면** overflow 부기는 비어 있다 (D61-a 의 핵심)
  ② 마스크 밖 **기존** 복셀은 부기에 안 든다. 새로 생긴 것만 든다
  ③ 문턱이 잡음을 버리고 신호를 살린다 — 그리고 **기본값이 없다**
  ④ 승계 청크는 부모 바이트 그대로다 (동일률 100%)
"""

from __future__ import annotations

import numpy as np

from server.delta import assemble_delta, build_bookkeeping, component_sizes


def _blob(n: int) -> bytes:
    return bytes([n % 251]) * (16 + n)


def _cells(*xyz) -> np.ndarray:
    return np.array(list(xyz), dtype=np.int64).reshape(-1, 3)


def _row(x, y0, y1, z):
    return _cells(*[(x, y, z) for y in range(y0, y1)])


# ══════════════════════════════════════════ ① 점유 비교지 해시 비교가 아니다
def test_identical_occupancy_yields_no_overflow_even_if_bytes_differ():
    """🔴 D61-a. 재디코딩하면 바이트는 전부 달라진다 — 그래도 부기는 안 커진다.

    "VoxHammer 절감률 ≈0%" 가 나온 자리가 여기다. 해시로 "바뀐 청크" 를 세면
    재디코딩된 자산은 전부가 바뀐 것이 되고 절감이 0 이 된다. 부기를 점유로
    정하면 그런 일이 없다 — 이 테스트가 그 성질을 잠근다.
    """
    occ = _cells((10, 10, 10), (10, 10, 11), (40, 40, 40), (40, 40, 41))
    bk = build_bookkeeping(occ, occ, _cells((10, 10, 10)), halo=1, min_component=2)
    assert bk.overflow_keys == [], "점유가 같은데 overflow 청크가 생겼다"
    assert bk.new_outside_cells.shape[0] == 0


# ══════════════════════════════════════════ ② 기존 복셀은 안 건드린다
def test_existing_outside_voxels_are_not_booked():
    """마스크 밖 **기존** 복셀은 아무리 많아도 부기에 안 든다."""
    base = np.concatenate([_row(40, 0, 50, 40), _cells((1, 1, 1))])
    bk = build_bookkeeping(base, base, _cells((1, 1, 1)), halo=1, min_component=2)
    assert bk.overflow_keys == []


def test_only_new_outside_voxels_are_booked():
    base = _cells((1, 1, 1))
    grown = _row(40, 0, 40, 40)                 # 마스크 밖에 **새로** 생긴 큰 덩어리
    result = np.concatenate([base, grown])
    bk = build_bookkeeping(base, result, base, halo=1, min_component=10)
    assert bk.kept_components == [len(grown)]
    assert bk.overflow_keys, "마스크 밖 신규 큰 덩어리가 부기에 안 들었다"


# ══════════════════════════════════════════ ③ 문턱
def test_threshold_drops_noise_and_keeps_signal():
    base = _cells((1, 1, 1))
    signal = _row(40, 0, 30, 40)                                 # 30셀 — 신호
    noise = _cells((60, 60, 60), (60, 60, 62), (62, 60, 60))     # 흩어진 1셀 3개
    result = np.concatenate([base, signal, noise])
    bk = build_bookkeeping(base, result, base, halo=1, min_component=10)
    assert bk.kept_components == [30]
    assert sorted(bk.dropped_components) == [1, 1, 1]
    assert bk.dropped_voxels == 3


def test_min_component_has_no_default():
    """`source`·`symmetrize` 와 같은 이유다 — 안 적으면 부를 수 없다.

    기본값을 두면 다음 세션이 안 적고 지나가고, 그 순간 문턱이 근거 없이 굳는다.
    """
    import inspect

    sig = inspect.signature(build_bookkeeping)
    assert sig.parameters["min_component"].default is inspect.Parameter.empty


def test_component_sizes_is_sorted_descending():
    sizes = component_sizes(np.concatenate([_row(40, 0, 30, 40), _cells((60, 60, 60))]))
    assert sizes == sorted(sizes, reverse=True) and sizes[0] == 30


# ══════════════════════════════════════════ ④ 승계 · 절감 분모
def _tetra(shift):
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
    f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    return v * 0.05 + np.asarray(shift, dtype=np.float64), f


def test_inherited_chunks_are_parent_bytes_verbatim():
    """승계 동일률 100% 는 **항진명제**다 (D5-b). 100% 가 아니면 승계가 깨진 것이다."""
    verts, faces = _tetra([-0.4, -0.4, -0.4])
    base_blobs = {"7_7_7": _blob(1), "6_6_6": _blob(2)}          # 부기 밖 청크들
    d = assemble_delta(base_blobs, verts, faces, _cells((2, 2, 2)), book=["0_0_0"])

    assert d.identity == 1.0
    assert d.n_inherited_chunks == 2
    for k, v in base_blobs.items():
        assert d.child_blobs[k] == v


def test_saving_denominator_is_the_result_mesh_not_the_parent():
    """🔴 분모에 부모 바이트를 섞으면 절감이 부풀려진다.

    W18 에서 실제로 밟았다 — 델타는 저해상도 GLB, 승계는 고해상도 `.cbin` 이라
    청크당 30배 차이가 나서 절감이 97.6% 로 나왔다. 분모를 결과 메시로 통일하니
    43.2% 였다. 여기서는 **무거운 부모**를 넣어 분모가 안 흔들리는지만 본다.
    """
    verts, faces = _tetra([-0.4, -0.4, -0.4])
    cells = _cells((2, 2, 2))
    light = assemble_delta({"7_7_7": _blob(1)}, verts, faces, cells, book=["0_0_0"])
    heavy = assemble_delta({"7_7_7": b"x" * 10_000_000}, verts, faces, cells, book=["0_0_0"])

    assert light.full_bytes == heavy.full_bytes, "부모 크기가 분모를 움직였다"
    assert light.saving == heavy.saving
    assert heavy.asset_bytes > heavy.full_bytes          # 자산 크기는 따로 보고된다


def test_reencoded_identity_shows_what_no_inheritance_costs():
    """승계를 안 했을 때의 동일률을 같은 실행에서 낸다 — '절감 0%' 의 출처."""
    verts, faces = _tetra([-0.4, -0.4, -0.4])
    d = assemble_delta({"7_7_7": _blob(1)}, verts, faces, _cells((2, 2, 2)), book=["0_0_0"])
    assert d.identity == 1.0
    assert d.reencoded_identity == 0.0
