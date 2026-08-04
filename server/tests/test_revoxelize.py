"""D11 — 재복셀화가 좌표 스케일과 **다르다**는 주장을 숫자로 검증한다.

이 파일이 D11 의 근거다. 여기가 깨지면 D11 전체가 무너지므로 지우지 마라.

────────────────────────────────────────────────────────────────────────
무엇을 재는가
────────────────────────────────────────────────────────────────────────
`cbin-delta/FINDINGS` 가 좌표 스케일을 기각한 근거는 **6-이웃 유지율**이었다:

    s=1.5 → 50%       s=2.0 → 0%

정의는 "원래 6-인접이던 **쌍**이 변환 후에도 6-인접인 비율" 이다. 이 파일은 그
정의를 그대로 구현해 (a) 좌표 스케일에서 FINDINGS 의 수치를 재현하고,
(b) 재복셀화에서는 표면이 표면으로 남는지 본다.

────────────────────────────────────────────────────────────────────────
🔴 정직하게 적어 둘 것 — 방향에 따라 결론이 다르다
────────────────────────────────────────────────────────────────────────
**업스케일**(FINDINGS 가 잰 방향)에서는 두 경로가 완전히 다르다.
좌표 스케일은 s=2.0 에서 쌍 유지율 0%, 고립 복셀 100% 가 된다. 재복셀화는 0% 고립.

**다운스케일**(D11 이 실제로 하는 방향 — 기증자를 머리 마스크에 맞춘다)에서는
6-이웃 유지율이 두 경로를 **구분하지 못한다.** 둘 다 고립 복셀 0% 다. 다운스케일은
셀을 찢는 게 아니라 뭉치기 때문이다.

그 방향에서 두 경로가 갈리는 것은 연결성이 아니라 **내용**이다 — 좌표 스케일은
이미 양자화된 좌표를 다시 반올림해 표면 셀을 잃는다. 아래 테스트가 그 차이를
IoU 로 잰다.

⇒ 결론: 재복셀화를 쓰는 **결정은 옳다**(양방향에서 안전하고, 양자화 오차를 겹치지
   않는다). 다만 "6-이웃 유지율" 이라는 **근거는 업스케일에만 적용된다.**
"""

from __future__ import annotations

import numpy as np
import pytest

from server.pipeline import revoxelize_to_extent, surface_voxelize
from server.tests.fixtures import cube_mesh, donor_mesh, sphere_mesh

from deltacontract.coords import VOXEL_RES, voxel_code

_N6 = np.array(
    [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)],
    dtype=np.int64,
)
_CENTER = VOXEL_RES // 2


def _code_set(cells: np.ndarray) -> set:
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    return set(voxel_code(a).tolist()) if a.size else set()


def _adjacent_pairs(cells: np.ndarray):
    """원본에서 6-인접인 (i, j) 쌍. +x/+y/+z 만 봐서 중복을 피한다."""
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    index = {int(c): i for i, c in enumerate(voxel_code(a))}
    pairs = []
    for i, c in enumerate(a):
        for d in _N6[:3]:
            n = c + d
            if np.all((n >= 0) & (n < VOXEL_RES)):
                j = index.get(int(voxel_code(n[None, :])[0]))
                if j is not None:
                    pairs.append((i, j))
    return a, pairs


def six_neighbor_pair_retention(cells: np.ndarray, mapped: np.ndarray) -> float:
    """FINDINGS 정의 — 원래 6-인접이던 쌍이 변환 후에도 6-인접인 비율."""
    _a, pairs = _adjacent_pairs(cells)
    if not pairs:
        return float("nan")
    keep = sum(1 for i, j in pairs if int(np.abs(mapped[i] - mapped[j]).sum()) == 1)
    return keep / len(pairs)


def isolated_fraction(cells: np.ndarray) -> float:
    """6-이웃이 하나도 없는 셀의 비율. 디코더가 조각난 표면을 만드는 원인이다."""
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.shape[0] == 0:
        return 0.0
    present = _code_set(a)
    iso = 0
    for c in a:
        nb = c + _N6
        nb = nb[np.all((nb >= 0) & (nb < VOXEL_RES), axis=1)]
        if not (nb.size and any(int(x) in present for x in voxel_code(nb))):
            iso += 1
    return iso / a.shape[0]


def coordinate_scale(cells: np.ndarray, s: float) -> np.ndarray:
    """🔴 계약이 금지한 경로. 이미 만들어진 희소 정수 좌표를 곱한다.

    격자 중심 기준으로 곱한다 — 원점 기준이면 클램프가 섞여서 스케일 자체의
    효과와 구분이 안 된다.
    """
    a = np.asarray(cells, dtype=np.float64).reshape(-1, 3)
    return np.rint((a - _CENTER) * s + _CENTER).astype(np.int64)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ca, cb = _code_set(a), _code_set(b)
    return len(ca & cb) / len(ca | cb) if (ca | cb) else 1.0


# ══════════════════════════════════ 1. 업스케일 — FINDINGS 재현
@pytest.mark.parametrize(
    "scale,max_pair_retention", [(1.5, 0.60), (2.0, 0.01)]
)
def test_coordinate_scaling_destroys_adjacency(scale, max_pair_retention):
    """좌표 스케일은 인접성을 부순다. FINDINGS 의 50% / 0% 를 재현한다."""
    verts, faces = sphere_mesh(radius=0.12)
    cells = surface_voxelize(verts, faces)

    mapped = coordinate_scale(cells, scale)
    retention = six_neighbor_pair_retention(cells, mapped)

    assert retention <= max_pair_retention, (
        f"s={scale} 에서 쌍 유지율 {retention:.3f} — FINDINGS 재현 실패"
    )


def test_coordinate_scaling_at_2x_isolates_every_voxel():
    """s=2.0 이면 **모든** 복셀이 고립된다. 디코더가 색종이 조각을 만드는 이유다."""
    verts, faces = sphere_mesh(radius=0.12)
    cells = surface_voxelize(verts, faces)

    mapped = np.unique(coordinate_scale(cells, 2.0), axis=0)
    assert six_neighbor_pair_retention(cells, coordinate_scale(cells, 2.0)) == 0.0
    assert isolated_fraction(mapped) == 1.0


@pytest.mark.parametrize("scale", [1.5, 2.0])
def test_revoxelization_keeps_the_surface_a_surface(scale):
    """★ 같은 배율에서 재복셀화는 고립 복셀을 **하나도** 만들지 않는다.

    이것이 D11 의 핵심 주장이다: 연속 메시를 다시 래스터화하면 인접성이 처음부터
    올바르게 구성되므로, 격자가 촘촘해져도 표면은 표면으로 남는다.
    """
    verts, faces = sphere_mesh(radius=0.12)
    cells = surface_voxelize(verts, faces)
    span = cells.max(axis=0) - cells.min(axis=0) + 1

    rv = revoxelize_to_extent(verts, faces, np.rint(span * scale).astype(int))

    assert isolated_fraction(rv) == 0.0, "재복셀화가 고립 복셀을 만들었다"
    assert rv.shape[0] > cells.shape[0], "해상도를 올렸는데 셀이 안 늘었다"
    got = rv.max(axis=0) - rv.min(axis=0) + 1
    assert np.all(np.abs(got - np.rint(span * scale)) <= 2), (
        f"목표 범위와 어긋난다: 목표 {np.rint(span*scale).tolist()} 실제 {got.tolist()}"
    )


# ══════════════════════ 2. 다운스케일 — D11 이 실제로 하는 방향
def test_downscaling_is_not_distinguished_by_neighbor_retention():
    """🔴 **정직한 기록.** 다운스케일에서는 6-이웃 유지율이 두 경로를 구분 못 한다.

    다운스케일은 셀을 찢는 게 아니라 뭉친다. 그래서 둘 다 고립 복셀 0% 다.
    D11 의 근거를 이 방향으로 확장해서 인용하면 안 된다.
    """
    verts, faces = cube_mesh(size=0.45)
    cells = surface_voxelize(verts, faces)
    span = cells.max(axis=0) - cells.min(axis=0) + 1
    target = np.array([16, 16, 16])

    scaled = np.unique(coordinate_scale(cells, float(min(target / span))), axis=0)
    rv = revoxelize_to_extent(verts, faces, target)

    assert isolated_fraction(scaled) == 0.0
    assert isolated_fraction(rv) == 0.0


def test_downscaling_paths_disagree_on_content():
    """다운스케일에서 두 경로가 갈리는 것은 연결성이 아니라 **내용**이다.

    좌표 스케일은 이미 양자화된 좌표를 다시 반올림해 표면 셀을 잃는다.
    두 결과를 같은 자리로 옮겨 비교하면 겹침이 크게 떨어진다.
    """
    verts, faces = cube_mesh(size=0.45)
    cells = surface_voxelize(verts, faces)
    span = cells.max(axis=0) - cells.min(axis=0) + 1
    target = np.array([16, 16, 16])

    scaled = np.unique(coordinate_scale(cells, float(min(target / span))), axis=0)
    rv = revoxelize_to_extent(verts, faces, target)

    overlap = _iou(scaled - scaled.min(axis=0), rv - rv.min(axis=0))
    assert overlap < 0.6, (
        f"두 경로가 같은 답을 낸다 (IoU {overlap:.3f}) — 이 픽스처로는 차이를 못 보인다"
    )
    assert scaled.shape[0] < rv.shape[0], "좌표 스케일이 셀을 잃지 않았다"


# ══════════════════════════════════ 3. 재복셀화 자체의 성질
def test_revoxelize_respects_target_extent():
    verts, faces = donor_mesh()
    for target in ([18, 18, 14], [24, 24, 24], [40, 30, 20]):
        rv = revoxelize_to_extent(verts, faces, target)
        got = rv.max(axis=0) - rv.min(axis=0) + 1
        # 등비 축소라 **가장 빡빡한 축**이 목표에 닿고 나머지는 그 이하다.
        assert np.all(got <= np.array(target) + 1), (
            f"목표 {target} 를 넘었다: {got.tolist()}"
        )
        assert np.any(got >= np.array(target) - 2), (
            f"목표 {target} 에 한 축도 못 닿았다: {got.tolist()}"
        )


def test_revoxelize_is_deterministic():
    verts, faces = donor_mesh()
    a = revoxelize_to_extent(verts, faces, [20, 20, 16])
    b = revoxelize_to_extent(verts, faces, [20, 20, 16])
    assert np.array_equal(a, b)


def test_revoxelize_preserves_shape_character():
    """등비 축소라 형상 비율이 유지된다 — 축마다 다른 배율을 쓰면 호박이 찌그러진다."""
    verts, faces = donor_mesh()
    full = surface_voxelize(verts, faces)
    small = revoxelize_to_extent(verts, faces, [20, 20, 20])

    def ratios(c):
        s = (c.max(axis=0) - c.min(axis=0) + 1).astype(float)
        return s / s.max()

    assert np.allclose(ratios(full), ratios(small), atol=0.12), (
        f"형상 비율이 바뀌었다: {ratios(full)} → {ratios(small)}"
    )


def test_revoxelize_rejects_bad_arguments():
    verts, faces = donor_mesh()
    with pytest.raises(ValueError, match="target_extent"):
        revoxelize_to_extent(verts, faces, [10, 10])
    with pytest.raises(ValueError, match="target_extent"):
        revoxelize_to_extent(verts, faces, [10, 0, 10])
    with pytest.raises(ValueError, match="fill"):
        revoxelize_to_extent(verts, faces, [10, 10, 10], fill=1.5)
