"""`slatmask` — 마스크 좌표의 근거가 slat 격자인지를 **함수가** 지키는가 (D28).

W8 까지 이 모듈은 수동 검증만 있었다. 자기 검사가 자기 환경에서 안 돌면 자기를
보호하지 않는다 (방법론 5조 5번) — 특히 이 모듈의 방어는 "예외가 안 나는 실패"
를 막는 것이라, 회귀해도 아무 증상이 없다.

여기서 잠그는 것 넷:
  ① 표면 복셀화 입력 거부 (선언으로)
  ② 라벨만 slat 으로 바꾼 우회 거부 (매니페스트 총계 대조로)
  ③ 절단면이 목 극소 **위**(neck+1)에 있다 (D25-a)
  ④ 상수가 정본(`frames.VOXEL_GRID_SOURCE`)과 갈라지지 않았다
"""

from __future__ import annotations

import numpy as np
import pytest

from server.pipeline.frames import VOXEL_GRID_SOURCE
from server.slatmask import (
    SLAT,
    SURFACE,
    NotSlatCoords,
    build_head3_mask,
    chunk_z_profile,
    neck_z,
    z_profile,
)


def _necked_asset() -> np.ndarray:
    """목이 있는 합성 자산. 몸통(굵음) → 목(가늘음) → 머리(중간).

    실자산을 못 쓰는 이유: slat coords 가 `.cbin` 에 없어서 3090 로컬에는
    slat 좌표가 존재하지 않는다 (D34). 그래서 형상 성질만 합성으로 잠근다.
    """
    cells = []
    for z in range(10, 64):
        r = 9 if z < 34 else (2 if z < 46 else 6)
        for x in range(-r, r + 1):
            for y in range(-r, r + 1):
                if x * x + y * y <= r * r:
                    cells.append((32 + x, 32 + y, z))
    return np.unique(np.array(cells, dtype=np.int64), axis=0)


# ══════════════════════════════════════════════════ ④ 정본과의 드리프트
def test_slat_constant_matches_frames():
    """`slatmask.SLAT` 은 `frames.VOXEL_GRID_SOURCE` 의 복사본이다.

    A5000 이 리포 없이 단독으로 돌릴 수 있게 값을 복사해 뒀다. 복사본은
    갈라지므로 그 드리프트를 여기서 잡는다 — 갈라지면 `source=SLAT` 로 부른
    호출이 `assert_slat_grid` 를 통과하지 못한다.
    """
    assert SLAT == VOXEL_GRID_SOURCE
    assert SURFACE != VOXEL_GRID_SOURCE


# ══════════════════════════════════════════════════ ① 선언으로 거부
def test_surface_source_is_rejected():
    """표면 복셀화라고 **적으면** 거부된다."""
    with pytest.raises(NotSlatCoords):
        build_head3_mask(_necked_asset(), source=SURFACE, symmetrize=False)


def test_source_has_no_default():
    """`source` 에 기본값이 없다 — 안 적으면 부를 수 없다.

    기본값이 있으면 다음 세션이 안 적고 지나가고, 그 순간 D28 이 무력해진다.
    """
    with pytest.raises(TypeError):
        build_head3_mask(_necked_asset())  # type: ignore[call-arg]


# ══════════════════════════════════════════════════ ② 라벨 우회 거부
def test_slat_label_still_checked_against_manifest():
    """`source="slat_coords"` 라고 **적어도** 총계가 다르면 거부된다.

    W8 실측이 이 경로로 잡혔다: 표면 복셀화 10,264 vs slat 정본 9,591.
    선언만 믿으면 라벨 한 줄로 방어를 우회할 수 있다.
    """
    asset = _necked_asset()
    manifest = {"voxel_count_total": len(asset) + 673, "chunks": []}
    with pytest.raises(NotSlatCoords) as e:
        build_head3_mask(asset, source=SLAT, symmetrize=False, manifest=manifest)
    assert str(len(asset)) in str(e.value)


def test_matching_manifest_passes():
    asset = _necked_asset()
    spec = build_head3_mask(
        asset, source=SLAT, symmetrize=False,
        manifest={"voxel_count_total": len(asset), "chunks": []},
    )
    assert spec.grid_source == VOXEL_GRID_SOURCE


# ══════════════════════════════════════════════════ ③ 목 극소 **위**에서 절단
def test_cut_is_above_the_neck_minimum():
    """🔴 D25-a. 절단면은 극소점 **다음** 칸이다.

    극소점 자체를 포함하면 목을 물고 들어가고, 이번 게이트의 "자연스럽게"
    (= 목 연결부 품질)가 거기서 깎인다. W6 이 실제로 그 실수를 했다 —
    격자가 한 칸 밀려 있어서 "위" 가 slat 기준으로는 "에서" 였다.
    """
    asset = _necked_asset()
    nz, ncount = neck_z(z_profile(asset))
    spec = build_head3_mask(asset, source=SLAT, symmetrize=False)

    assert spec.neck_z == nz
    assert spec.box_z[0] == nz + 1, "극소점 위가 아니라 극소점에서 잘랐다"
    assert not (spec.cells[:, 2] <= nz).any(), "마스크가 목을 물고 있다"


def test_neck_is_a_cross_section_minimum_not_a_z_quantile():
    """D25. 목은 형상의 성질이지 높이의 성질이 아니다.

    합성 자산의 목 구간은 z 34–45 인데, 자산 세로 구간(10–63)의 단순 분위수는
    거기로 떨어지지 않는다. 극소를 쓰는 정의만 목을 맞춘다.
    """
    asset = _necked_asset()
    nz, _ = neck_z(z_profile(asset))
    assert 34 <= nz <= 45

    # ⚠️ 목은 **평탄한 띠**다(z 34–45 가 같은 반지름). 그래서 nz±2 와 비교하면
    #    안 된다 — 같은 띠 안이라 값이 같다. 몸통·머리와 비교해야 뜻이 맞는다.
    prof = z_profile(asset)
    body = prof[20]          # 몸통 한복판
    head = prof[55]          # 머리 한복판
    assert prof[nz] < body, "목 단면이 몸통보다 작지 않다"
    assert prof[nz] < head, "목 단면이 머리보다 작지 않다"


# ══════════════════════════════════════════════════ 체적 마스크 (D22 ②)
def test_mask_is_mostly_empty_space():
    """머리 셋이 갈라져 나올 **빈 자리**가 마스크의 대부분이어야 한다 (D22 ②)."""
    spec = build_head3_mask(_necked_asset(), source=SLAT, symmetrize=False)
    assert spec.empty_fraction > 0.5
    assert spec.empty_cells == spec.n_cells - spec.asset_cells_inside
    assert spec.asset_cells_inside > 0, "마스크가 자산을 하나도 안 덮는다"


def test_mask_widens_for_three_heads():
    """좌우로 머리 폭만큼 넓힌다 — 총 3배 폭."""
    asset = _necked_asset()
    spec = build_head3_mask(asset, source=SLAT, symmetrize=False, width_multiple=1.0)
    w = spec.head_span_xy[0]
    got = spec.box_x[1] - spec.box_x[0] + 1
    assert got >= 2 * w, f"머리 3개가 들어갈 폭이 아니다: {got} < {2 * w}"


def test_spec_carries_grid_source_and_fingerprint():
    """인계물에 격자 정본과 지문이 같이 실린다 (D27 ③)."""
    d = build_head3_mask(_necked_asset(), source=SLAT, symmetrize=False).as_dict()
    assert d["grid_source"] == VOXEL_GRID_SOURCE
    assert len(d["fingerprint"]) > 0
    assert d["empty_cells_inside"] + d["asset_cells_inside"] == d["mask_cells"]


# ══════════════════════════════════════════════════ 매니페스트 (slat 정본)
def test_chunk_z_profile_sums_to_manifest_total():
    """청크-z 프로파일은 매니페스트 총계와 일치한다.

    `.cbin` 에 복셀 좌표가 없으므로(D34), 3090 이 slat 에 대해 로컬에서 확실히
    아는 것은 이 청크 단위 개수뿐이다.
    """
    manifest = {
        "voxel_count_total": 60,
        "chunks": [
            {"chunk_id": "1_2_5", "voxel_count": 10},
            {"chunk_id": "3_4_5", "voxel_count": 20},
            {"chunk_id": "0_0_7", "voxel_count": 30},
        ],
    }
    prof = chunk_z_profile(manifest)
    assert prof == {5: 30, 7: 30}
    assert sum(prof.values()) == manifest["voxel_count_total"]


def test_empty_occupancy_is_rejected():
    with pytest.raises(ValueError):
        build_head3_mask(np.zeros((0, 3), dtype=np.int64), source=SLAT, symmetrize=False)


# ══════════════════ D28-a / D35-a — W12 에서 실제로 넣은 것
#
# W11 에서 A5000 이 받은 판본에는 이 셋이 **전부 없었다**:
#     grid_source= · require_slat_grid() · is_x_symmetric()
# sha256 은 일치했는데 API 는 없었다. `server/provenance.py` 가 그 상황을 잡는다.

def test_symmetrize_has_no_default():
    """★ `source` 와 같은 이유로 기본값을 두지 않는다 (D35-a).

    기본값이 있으면 다음 세션이 안 적고 지나가고, 그 순간 D35 가 무력해진다.
    """
    import inspect

    sig = inspect.signature(build_head3_mask)
    assert sig.parameters["symmetrize"].default is inspect.Parameter.empty
    assert sig.parameters["source"].default is inspect.Parameter.empty


def test_require_slat_grid_exists_and_rejects_surface():
    """★★ W11 부재 API ①. 문자열도 spec 도 받는다."""
    from server.slatmask import NotSlatCoords, require_slat_grid

    require_slat_grid(SLAT)
    spec = build_head3_mask(_necked_asset(), source=SLAT, symmetrize=False)
    spec.require_slat_grid()
    require_slat_grid(spec)

    with pytest.raises(NotSlatCoords, match="slat_coords"):
        require_slat_grid(SURFACE, "head3mask")


def test_is_x_symmetric_exists_and_symmetrize_makes_it_true():
    """★★ W11 부재 API ②③. 대칭화가 실제로 대칭을 만드는가."""
    from server.slatmask import NotXSymmetric, is_x_symmetric

    asset = _necked_asset()
    plain = build_head3_mask(asset, source=SLAT, symmetrize=False)
    sym = build_head3_mask(asset, source=SLAT, symmetrize=True)

    assert is_x_symmetric(sym.cells)
    assert sym.x_symmetric
    sym.require_x_symmetric()
    assert sym.x_symmetry_cost == 1.0          # 이미 대칭이면 더 안 넓어진다

    # 대칭화하지 않은 마스크는 자산 위치에 따라 깨질 수 있다 — 그때 거부된다.
    if not plain.x_symmetric:
        with pytest.raises(NotXSymmetric, match="D35-a"):
            plain.require_x_symmetric()
        assert sym.n_cells >= plain.n_cells


def test_offset_asset_breaks_symmetry_without_symmetrize():
    """★★ D35-a — W10 의 대칭은 **우연이었다.** 치우친 자산에서 깨진다."""
    from server.slatmask import NotXSymmetric

    # 격자 중심(31.5)에서 벗어난 자산.
    cells = []
    for z in range(10, 60):
        r = 8 if z < 40 else (2 if z < 46 else 5)
        for x in range(18 - r, 18 + r + 1):
            for y in range(30 - r, 30 + r + 1):
                cells.append((x, y, z))
    asset = np.array(sorted(set(cells)), dtype=np.int64)

    plain = build_head3_mask(asset, source=SLAT, symmetrize=False)
    assert not plain.x_symmetric
    assert plain.x_symmetry_cost > 1.0
    with pytest.raises(NotXSymmetric):
        plain.require_x_symmetric()

    sym = build_head3_mask(asset, source=SLAT, symmetrize=True)
    assert sym.x_symmetric
    sym.require_x_symmetric()


def test_as_dict_carries_the_symmetry_facts():
    """인계 매니페스트가 대칭 여부를 들고 다녀야 한다 (D27③)."""
    d = build_head3_mask(_necked_asset(), source=SLAT, symmetrize=True).as_dict()
    assert d["grid_source"] == SLAT
    assert d["x_symmetric"] is True
    assert "x_symmetry_cost" in d
