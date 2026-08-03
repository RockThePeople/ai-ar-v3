"""S2 관통 — 합성 픽스처로 전 구간을 한 번 통과시킨다.

효능(A) · 보존(B) · 절감(C) 가 **동시에** 성립하는지를 본다. 셋 중 하나라도 따로
재면 아무 뜻이 없다는 것이 이 프로젝트의 방법론 5조 3번이다.

    보존만 재면 아무것도 안 하는 구현이 전부 통과한다

그래서 이 파일의 마지막 테스트(`test_noop_passes_preservation_and_saving_but_fails_efficacy`)
가 **음성 대조**다. 아무것도 안 하는 구현을 같은 계측에 넣어서, 보존·절감은 통과하고
효능에서만 떨어지는지 확인한다. 그게 없으면 이 스위트는 자기를 보호하지 못한다.
"""

from __future__ import annotations

import numpy as np
import pytest

from server import metrics
from server.pipeline import (
    build_mask,
    derive_bookkeeping,
    encode_chunks,
    occupancy_to_mesh,
    package_delta,
    splice,
    surface_voxelize,
    top_region_cells,
)
from server.pipeline.delta import (
    Bookkeeping,
    audit_against_bytes,
    diff_would_have_missed,
    verify_bookkeeping,
)
from server.pipeline.splice import SpliceResult
from server.tests.fixtures import cube_mesh, snowman_mesh, sphere_mesh

from deltacontract.assemble import AssemblyError
from deltacontract.chunkbin import blob_hash, decode
from deltacontract.coords import VOXEL_RES, dilate_cells
from deltacontract.errors import BookkeepingMismatch, MaskEmpty
from deltacontract.partition import partition_mesh

# ── 픽스처 형상 상수 ───────────────────────────────────────────────────
# 머리를 감싸는 NORMALIZED bbox. 머리 구(중심 z=0.20, 반지름 0.13)를 여유 있게 덮되,
# 몸통을 물지 않는다 — 물면 보존 영역이 줄어서 절감률이 실제보다 좋아 보인다.
HEAD_BBOX = ((-0.145, -0.145, 0.065), (0.145, 0.145, 0.335))
DONOR_SIZE = 0.24
DONOR_CROP_FRACTION = 0.85
HALO = 1


# ══════════════════════════════════════════════════════════════ 헬퍼
def _chunks_of(cells: np.ndarray):
    """점유 → {chunk_key: .cbin 바이트}. 합성 디코더를 거친다."""
    verts, faces = occupancy_to_mesh(cells)
    meshes = partition_mesh(verts, faces, voxel_cells=cells)
    return encode_chunks(meshes)


def _hashes(blobs):
    return {k: blob_hash(v) for k, v in blobs.items()}


def _run_pipeline():
    """관통 1회. 결과 일체를 돌려준다."""
    base_cells = surface_voxelize(*snowman_mesh())
    donor_cells = surface_voxelize(*cube_mesh(size=DONOR_SIZE))
    mask = build_mask(bbox=HEAD_BBOX, halo=HALO)

    sp = splice(
        base_cells,
        donor_cells,
        mask,
        crop_fraction=DONOR_CROP_FRACTION,
        strict_containment=True,
    )

    parent_blobs = _chunks_of(base_cells)
    child_blobs = _chunks_of(sp.cells)

    bk = derive_bookkeeping(sp, child_blobs.keys())
    audit_against_bytes(bk, _hashes(parent_blobs), _hashes(child_blobs))
    pkg = package_delta(parent_blobs, child_blobs, bk, mask=mask, job_id="synthetic")

    report = metrics.evaluate(
        before=base_cells,
        after=sp.cells,
        mask_cells=mask.cells,            # 효능 — 사용자가 지정한 원본 마스크
        book_region_cells=mask.dilated,   # 보존 — halo 까지 팽창시킨 부기 영역
        parent_blobs=parent_blobs,
        child_blobs=pkg.blobs,            # 승계가 적용된 **최종** 세트
        book=bk.book,
        full_bytes=pkg.full_bytes,
        delta_bytes=pkg.delta_bytes,
    )
    return {
        "base": base_cells, "donor": donor_cells, "mask": mask, "splice": sp,
        "parent": parent_blobs, "child": child_blobs, "bk": bk, "pkg": pkg,
        "report": report,
    }


@pytest.fixture(scope="module")
def run():
    return _run_pipeline()


# ══════════════════════════════════════════════════════ 1. 복셀화
def test_voxelize_produces_surface_shell_in_range():
    cells = surface_voxelize(*snowman_mesh())
    assert cells.shape[0] > 0
    assert cells.min() >= 0 and cells.max() < VOXEL_RES
    # 껍질이지 덩어리가 아니다 — 부피를 채웠다면 bbox 부피에 육박했을 것이다.
    span = cells.max(axis=0) - cells.min(axis=0) + 1
    assert cells.shape[0] < 0.5 * float(np.prod(span))


def test_voxelize_is_deterministic():
    a = surface_voxelize(*snowman_mesh())
    b = surface_voxelize(*snowman_mesh())
    assert np.array_equal(a, b)


def test_synthetic_decoder_keeps_chunk_assignment_voxel_local():
    """합성 디코더의 삼각형은 전부 자기 복셀의 청크로 간다.

    이게 깨지면 한 복셀의 변화가 이웃 청크의 바이트를 바꾸고, 배치에서 유도한
    부기가 실제 변화보다 작아진다 (voxelize.py 모듈 docstring 2번).
    """
    from deltacontract.coords import normalized_to_chunk, voxel_to_chunk

    cells = surface_voxelize(*cube_mesh(size=0.3))
    verts, faces = occupancy_to_mesh(cells)
    centroids = verts[faces].mean(axis=1)
    tri_chunk = normalized_to_chunk(centroids)
    # 삼각형 12개가 복셀 하나에 대응한다 (occupancy_to_mesh 의 배치 순서).
    expected = np.repeat(voxel_to_chunk(cells), 12, axis=0)
    assert np.array_equal(tri_chunk, expected)


# ══════════════════════════════════════════════════════ 2. 마스크 순서
def test_clamp_happens_before_dilate():
    """🔴 팽창은 클램프 **뒤에**. 순서가 바뀌면 경계 셀이 소리 없이 사라진다.

    합성 bbox 입력으로는 이 차이가 안 드러난다 — 라쏘 제스처가 만드는 범위 밖
    좌표로만 터진다. 그래서 여기서 범위 밖 셀을 직접 넣는다.
    """
    out_of_range = np.array([[VOXEL_RES + 1, 10, 10]], dtype=np.int64)

    # 올바른 순서 (build_mask): 클램프 → 팽창. 셀이 살아남는다.
    m = build_mask(out_of_range, halo=1)
    assert m.n_cells == 1
    assert np.array_equal(m.cells[0], [VOXEL_RES - 1, 10, 10])
    assert m.n_dilated == 2 * 3 * 3  # x는 격자 끝이라 한쪽만 팽창

    # 잘못된 순서: 팽창 → 클램프(=범위 밖 버리기). 셀이 통째로 사라진다.
    wrong = dilate_cells(out_of_range, 1)
    assert wrong.shape[0] == 0, (
        "dilate_cells 는 팽창 후 범위 밖을 버린다 — 그래서 클램프가 먼저여야 한다"
    )


def test_mask_in_range_is_unaffected_by_ordering():
    """범위 안 입력에서는 두 순서가 같은 답을 낸다 — 그래서 합성 입력으로는 못 잡는다."""
    cells = np.array([[10, 10, 10], [11, 10, 10]], dtype=np.int64)
    assert np.array_equal(build_mask(cells, halo=1).dilated, dilate_cells(cells, 1))


def test_empty_mask_is_rejected():
    with pytest.raises(MaskEmpty):
        build_mask(np.zeros((0, 3), dtype=np.int64), halo=1)


def test_mask_fingerprints_distinguish_halo():
    m = build_mask(bbox=HEAD_BBOX, halo=HALO)
    assert m.fingerprint != m.fingerprint_dilated
    assert m.n_dilated > m.n_cells


def test_top_region_uses_asset_extent_not_grid():
    """`find_head_bbox` 이식분: 기준은 [0,64) 전체가 아니라 자산의 점유 구간이다."""
    cells = surface_voxelize(*sphere_mesh(center=(0.0, 0.0, 0.25), radius=0.15))
    top = top_region_cells(cells, fraction=0.4)
    lo_z, hi_z = int(cells[:, 2].min()), int(cells[:, 2].max())
    assert top[:, 2].min() > lo_z          # 아래쪽은 안 잡혔다
    assert top[:, 2].max() >= hi_z         # 위쪽 끝까지 덮는다


# ══════════════════════════════════════════════════════ 3. 조립
def test_splice_rejects_fractional_offset(run):
    """스케일도 소수 이동도 없다. 래퍼가 계약의 거부를 우회하지 않는지 본다."""
    with pytest.raises(AssemblyError, match="정수"):
        splice(
            run["base"], run["donor"], run["mask"],
            crop_fraction=DONOR_CROP_FRACTION, offset=[0.5, 0, 0],
        )


def test_splice_actually_clears_occupied_cells(run):
    """§5 S2-7 — 비우기 단계가 밥값을 하는지 자증한다.

    0 이면 옛 머리 기하가 그대로 남아 기증자가 겹쳐 박힌다. 예외는 안 난다.
    """
    sp = run["splice"]
    assert sp.n_cleared_occupied > 0
    assert sp.n_donor_outside_mask == 0
    assert sp.n_donor_overlap_kept == 0


def test_splice_result_is_base_minus_mask_plus_donor(run):
    sp, mask = run["splice"], run["mask"]
    from deltacontract.coords import voxel_code

    result = set(voxel_code(sp.cells).tolist())
    emptied = set(voxel_code(mask.dilated).tolist())
    base = set(voxel_code(run["base"]).tolist())
    placed = set(voxel_code(sp.donor_placed).tolist())
    assert result == (base - emptied) | placed


# ══════════════════════════════════════════════════════ 4. 부기
def test_bookkeeping_is_derived_from_placement_not_diff(run):
    """diff 로 만들었다면 놓쳤을 청크가 실제로 존재한다 (실측 8개의 합성 재현)."""
    bk, pkg = run["bk"], run["pkg"]
    missed = diff_would_have_missed(
        bk, _hashes(run["parent"]), _hashes(run["child"])
    )
    assert missed, (
        "diff 유도가 아무것도 안 놓쳤다면 이 픽스처는 그 함정을 재현하지 못한다"
    )
    # 놓쳤을 청크는 전부 부기에 들어 있다 — 그게 배치 유도의 값어치다.
    assert set(missed) <= set(bk.book)


def test_bookkeeping_satisfies_universal_rule(run):
    """∀ c ∈ book : c ∈ changed ∨ c ∈ removed"""
    bk = run["bk"]
    assert set(bk.changed) | set(bk.removed) == set(bk.book)
    assert not (set(bk.changed) & set(bk.removed))
    verify_bookkeeping(bk)


def test_bookkeeping_rejects_orphan_chunk():
    """부기에 있는데 어디에도 안 들어간 청크는 거부된다."""
    bad = Bookkeeping(zone1=["1_1_1"], zone2=[], book=["1_1_1"], changed=[], removed=[])
    with pytest.raises(BookkeepingMismatch):
        verify_bookkeeping(bad)


def test_no_bytes_change_outside_bookkeeping(run):
    """부기 밖에서 바이트가 바뀌면 클라이언트가 옛 기하를 들고 남는다."""
    audit_against_bytes(run["bk"], _hashes(run["parent"]), _hashes(run["child"]))


# ══════════════════════════════════════════════════════ 5. 포장
def test_package_inherits_parent_bytes_outside_bookkeeping(run):
    pkg, parent = run["pkg"], run["parent"]
    assert pkg.inherited_keys
    for key in pkg.inherited_keys:
        assert pkg.blobs[key] is parent[key], f"{key} 가 재인코딩본으로 대체됐다"


def test_package_wire_payload_is_bookkeeping_only(run):
    pkg, bk = run["pkg"], run["bk"]
    assert set(pkg.delta_blobs) == set(bk.changed)
    assert pkg.removed == sorted(bk.removed)
    assert pkg.delta_bytes < pkg.full_bytes


def test_packaged_chunks_decode(run):
    """포장된 바이트가 실제로 계약대로 디코딩된다."""
    for key, blob in run["pkg"].blobs.items():
        mesh = decode(blob)
        assert mesh.key == key
        assert mesh.vertex_count > 0


def test_manifest_carries_contract_and_mask_fingerprints(run):
    mf = run["pkg"].manifest
    assert mf["contract"]["voxel_res"] == VOXEL_RES
    assert mf["mask_fingerprint"] != mf["mask_fingerprint_dilated"]
    assert mf["halo_margin_voxels"] == HALO
    assert mf["bookkeeping"]["book"] == run["bk"].book


# ══════════════════════════════════════════════════ 6. ★ 게이트 G2
def test_efficacy_new_voxels_is_positive(run):
    """★ 효능-필수. 0 이면 아무것도 안 한 것이다 (D5)."""
    r = run["report"]
    assert r.efficacy_new_voxels > 0
    # 추가만 있고 제거가 0 이면 기증자가 옛 기하 위에 겹쳐 박힌 것이다.
    assert r.efficacy_removed_voxels > 0


def test_efficacy_iou_in_mask_shows_real_change(run):
    """★ 효능-정도. 마스크 안 IoU < 0.8 (D5)."""
    assert run["report"].efficacy_iou_in_mask < 0.8


def test_preservation_outside_mask(run):
    """★ 보존. 마스크 밖 IoU > 0.99 AND 바이트 동일률 100%."""
    r = run["report"]
    assert r.preservation_iou_out > 0.99
    assert r.preservation_byte_identity == 1.0


def test_transfer_saving(run):
    """★ 절감. 전송 절감 > 40%."""
    assert run["report"].transfer_saving > 0.40


def test_gate_g2_all_three_hold_together(run):
    """(A)(B)(C) 가 **처음으로 함께** 성립하는지. 이 셋이 동시에 나와야 의미가 있다."""
    gate = run["report"].gate_g2()
    assert gate == {"efficacy": True, "preservation": True, "saving": True}


# ══════════════════════════════════════════════ 7. ★★ 음성 대조
def test_noop_passes_preservation_and_saving_but_fails_efficacy():
    """★★ 음성 대조 — 이 스위트가 자기를 보호하는지 확인한다.

    아무것도 안 바꾸는 구현을 같은 계측에 통과시킨다. 기대하는 답:

        보존  통과 ← 아무것도 안 건드렸으니 당연하다
        절감  통과 ← 아무것도 안 보냈으니 100% 다
        효능  실패 ← 신규 복셀이 0 이다

    보존·절감만 재는 게이트였다면 이 구현이 **만점으로 통과**한다. 지난 프로젝트에서
    (B)(C)가 "통과" 했던 것이 정확히 이 상태였다 (`docs/PROGRESS.md` §1 rev3 인식).
    효능 지표가 이걸 떨어뜨리지 못하면 게이트 전체가 무의미하다.
    """
    base_cells = surface_voxelize(*snowman_mesh())
    mask = build_mask(bbox=HEAD_BBOX, halo=HALO)
    empty = np.zeros((0, 3), dtype=np.int64)

    # 아무것도 안 하는 "편집": 결과 = 원본, 놓은 것도 비운 것도 없다.
    noop = SpliceResult(
        cells=base_cells, donor_placed=empty, emptied=empty, offset=[0, 0, 0],
        crop_fraction=1.0, n_base=int(base_cells.shape[0]), n_donor_cropped=0,
        n_cleared_occupied=0, n_donor_outside_mask=0, n_donor_overlap_kept=0,
    )

    parent_blobs = _chunks_of(base_cells)
    child_blobs = _chunks_of(noop.cells)
    bk = derive_bookkeeping(noop, child_blobs.keys())
    assert bk.book == [], "아무것도 안 했으면 부기도 비어 있어야 한다"

    pkg = package_delta(parent_blobs, child_blobs, bk, mask=mask)
    report = metrics.evaluate(
        before=base_cells, after=noop.cells,
        mask_cells=mask.cells, book_region_cells=mask.dilated,
        parent_blobs=parent_blobs, child_blobs=pkg.blobs, book=bk.book,
        full_bytes=pkg.full_bytes, delta_bytes=pkg.delta_bytes,
    )

    # 보존·절감은 만점으로 통과한다.
    assert report.preservation_iou_out == 1.0
    assert report.preservation_byte_identity == 1.0
    assert report.transfer_saving == 1.0

    # 효능은 반드시 떨어진다.
    assert report.efficacy_new_voxels == 0
    assert report.efficacy_iou_in_mask == 1.0

    gate = report.gate_g2()
    assert gate["preservation"] is True, "no-op 은 보존을 통과해야 한다 (그게 요점이다)"
    assert gate["saving"] is True, "no-op 은 절감을 통과해야 한다 (그게 요점이다)"
    assert gate["efficacy"] is False, (
        "🔴 no-op 이 효능을 통과했다. 효능 지표가 아무것도 막지 못한다는 뜻이다."
    )


def test_noop_is_invisible_to_the_discarded_global_metric():
    """폐기된 전역 실루엣 지표가 왜 게이트에서 빠졌는지를 숫자로 남긴다 (D5).

    진짜 편집에서도 전역 평균은 작게 나온다. 마스크 한정 최대뷰가 그것보다
    훨씬 크다는 것이 D5 의 실측(2.58% vs 14.32%)이 말한 바다.
    """
    run = _run_pipeline()
    ref = run["report"].reference
    assert ref["silhouette_masked_max"] > ref["silhouette_global_mean"], (
        "마스크 한정 최대뷰가 전역 평균보다 크지 않다면 이 픽스처는 D5 의 상황을 "
        "재현하지 못한다"
    )
    assert "silhouette_global_mean" not in metrics.GATE_METRICS
