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
    revoxelize_to_extent,
    splice,
    surface_voxelize,
    top_region_cells,
)
from server.pipeline.splice import fit_donor_to_mask
from server.pipeline.delta import (
    Bookkeeping,
    audit_against_bytes,
    diff_would_have_missed,
    verify_bookkeeping,
)
from server.pipeline.splice import SpliceResult
from server.tests.fixtures import cube_mesh, donor_mesh, snowman_mesh, sphere_mesh

from deltacontract.assemble import AssemblyError
from deltacontract.chunkbin import blob_hash, decode
from deltacontract.coords import VOXEL_RES, dilate_cells
from deltacontract.errors import BookkeepingMismatch, MaskEmpty
from deltacontract.partition import partition_mesh

# ── 픽스처 형상 상수 ───────────────────────────────────────────────────
# 머리 마스크는 점유의 위쪽 HEAD_FRACTION 을 **슬라이스별 bbox** 로 감싼다 (D11 부수).
# 예전의 단일 직육면체 bbox 는 가로로 몸통 폭까지 덮어서 마스크가 격자의 21% 였다.
HEAD_FRACTION = 0.30
HALO = 1

# 예전 방식 — 비교·회귀용으로만 남긴다.
HEAD_BBOX = ((-0.145, -0.145, 0.065), (0.145, 0.145, 0.335))

# 🔴 잡음 바닥값 (D5-b). **이 값은 실자산에 쓰면 안 된다.**
#
# 합성 경로에서 0.0 인 것은 추정이 아니라 **유도된 사실**이다: 부기 밖 청크는
# 재디코딩되지 않고 부모 바이트를 그대로 승계하므로(package.py), 재디코딩 잡음이
# 존재할 자리가 없다. 즉 "재디코딩 영역" 이 공집합이다.
#
# 실자산의 바닥값은 **편집 없이 인코드→디코드만 왕복시킨 대조군**에서 나오고,
# 그것은 W3-A5000 에 배정돼 있다. 그 전에는 어떤 값도 가정하지 않는다 —
# `test_preservation_refuses_to_judge_without_noise_floor` 가 그걸 강제한다.
# D33 — 맨 float 이 아니라 `NoiseFloor` 다. 자산 id 가 없으면 타입이 거부한다.
SYNTHETIC_ASSET_ID = "synthetic-snowman"
SYNTHETIC_NOISE_FLOOR = metrics.NoiseFloor(
    value=0.0,
    asset_id=SYNTHETIC_ASSET_ID,
    region="outside_mask",
    n_voxels=0,
)


# ══════════════════════════════════════════════════════════════ 헬퍼
def _chunks_of(cells: np.ndarray):
    """점유 → {chunk_key: .cbin 바이트}. 합성 디코더를 거친다."""
    verts, faces = occupancy_to_mesh(cells)
    meshes = partition_mesh(verts, faces, voxel_cells=cells)
    return encode_chunks(meshes)


def _hashes(blobs):
    return {k: blob_hash(v) for k, v in blobs.items()}


def _run_pipeline():
    """관통 1회. 결과 일체를 돌려준다.

    D11(rev7) 이후 기증자는 **크롭하지 않는다.** 마스크의 복셀 범위에 맞춰 메시를
    다시 복셀화하므로 호박 전체가 들어간다 — 뚜껑만 남던 문제가 여기서 사라진다.
    마스크도 `per_slice=True` 로 머리를 계단 모양으로 감싼다.
    """
    base_cells = surface_voxelize(*snowman_mesh())
    head_cells = top_region_cells(base_cells, fraction=HEAD_FRACTION)
    mask = build_mask(head_cells, halo=HALO)

    donor_verts, donor_faces = donor_mesh()
    donor_cells, used_fill = fit_donor_to_mask(donor_verts, donor_faces, mask)

    sp = splice(
        base_cells,
        donor_cells,
        mask,
        crop_fraction=1.0,          # 🔴 D11 — 크롭으로 크기를 맞추지 않는다
        strict_containment=True,    # D13 — 전제
    )

    parent_blobs = _chunks_of(base_cells)
    child_blobs = _chunks_of(sp.cells)

    bk = derive_bookkeeping(sp, child_blobs.keys())
    audit_against_bytes(bk, _hashes(parent_blobs), _hashes(child_blobs))
    pkg = package_delta(parent_blobs, child_blobs, bk, mask=mask, job_id="synthetic")

    report = metrics.evaluate(
        before=base_cells,
        after=sp.cells,
        mask_cells=mask.cells,              # 효능 — 사용자가 지정한 원본 마스크
        edited_region_cells=mask.dilated,   # 보존 — halo 까지 팽창시킨 편집 영역
        parent_blobs=parent_blobs,
        child_blobs=pkg.blobs,              # 승계가 적용된 **최종** 세트
        book=bk.book,
        full_bytes=pkg.full_bytes,
        delta_bytes=pkg.delta_bytes,
        containment_enforced=sp.strict_containment,
        noise_floor=SYNTHETIC_NOISE_FLOOR,
        asset_id=SYNTHETIC_ASSET_ID,
        region="outside_mask",
    )
    return {
        "base": base_cells, "donor": donor_cells, "mask": mask, "splice": sp,
        "parent": parent_blobs, "child": child_blobs, "bk": bk, "pkg": pkg,
        "report": report, "used_fill": used_fill, "head_cells": head_cells,
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
            crop_fraction=1.0, offset=[0.5, 0, 0],
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


# ══════════════════════════════════════════════════ 6. ★ 게이트 G2 (rev6)
def test_efficacy_reports_new_and_removed_as_a_pair(run):
    """★ 효능-필수 (D5-a ①). 신규만 보면 삭제에 눈이 먼다.

    A5000 실측에서 신규 274 뒤에 제거 245 가 숨어 있었고 순증은 +29 였다.
    제거가 0 이면 기증자가 옛 기하 **위에 겹쳐 박힌** 것이다 (비우기가 일을 안 했다).
    """
    d = run["report"].delta_in_mask
    assert d.new > 0
    assert d.removed > 0
    assert d.churn == d.new + d.removed


def test_d12_fixes_the_assemble_fragmentation_that_w3_found():
    """★ D12 회귀 — W3 이 찾은 파편화를 새 정의가 실제로 고치는지.

    W3 구성(넓은 bbox 마스크 + 크롭한 정육면체 기증자)을 그대로 재현한다.
    그 구성에서 기증자 껍질은 **한 덩어리**인데 "신규만" 정의는 0.8 을 못 넘겼다:

        배치된 기증자 껍질   1.000   ← 실제로는 한 덩어리
        결과 점유 전체       1.000
        신규 복셀만          0.731   ← 옛 정의. 문턱 미달

    원인은 구조적이다 — 기증자 껍질이 옛 껍질과 교차하고, 교차한 셀은 before 에도
    있어 "신규" 가 아니다. 그 링이 빠지며 껍질이 두 조각으로 갈린다.

    D12 는 제거를 합쳐서 그 링을 메운다. 같은 입력에서 문턱을 넘어야 한다.
    """
    base_cells = surface_voxelize(*snowman_mesh())
    mask = build_mask(bbox=HEAD_BBOX, halo=HALO)      # W3 의 넓은 bbox 마스크
    donor_cells = surface_voxelize(*cube_mesh(size=0.24))
    sp = splice(base_cells, donor_cells, mask, crop_fraction=0.85)

    new_only = metrics.efficacy_largest_component(base_cells, sp.cells, mask.cells)
    d12 = metrics.efficacy_change_component(base_cells, sp.cells, mask.cells)
    donor_blob = metrics._largest_component_size(sp.donor_placed) / len(sp.donor_placed)

    assert donor_blob == 1.0, "배치된 기증자는 한 덩어리여야 한다"
    assert 0.7 < new_only < 0.8, f"W3 실측이 재현되지 않는다: {new_only:.3f}"
    assert d12 >= 0.8, (
        f"D12 정의가 파편화를 못 고쳤다: 신규만 {new_only:.3f} → 신규∪제거 {d12:.3f}"
    )


def test_d12_holds_on_the_current_pipeline(run):
    """현재(D11) 파이프라인에서도 D12 가 성립한다.

    마스크를 좁히고 기증자를 재복셀화한 뒤에는 옛 정의도 0.99 대로 올라온다 —
    파편화가 **넓은 마스크 + 크롭**이 만든 것이었다는 방증이다. D12 는 두 구성
    모두에서 성립한다.
    """
    r = run["report"]
    assert r.efficacy_change_component == 1.0
    assert r.reference["efficacy_largest_component_new_only"] > 0.98
    assert r.reference["efficacy_largest_component_of_result"] == 1.0


def test_churn_ratio(run):
    """★ 효능-필수 (D5-a ④). churn(안)/churn(밖) ≥ 3.0.

    ⚠️ 합성 디코더는 마스크 밖 churn 이 **구조적으로 0** 이라 `inf` 가 나온다.
       실자산에서는 절대 안 나온다 (A5000 실측 4.97배). 그래서 이 통과는
       "국소성이 증명됐다" 가 아니라 "합성 경로에서는 정의상 완전 국소" 라는 뜻이다.
    """
    r = run["report"]
    assert r.churn_ratio >= 3.0
    assert r.delta_outside.churn == 0, "합성 디코더인데 마스크 밖이 흔들렸다"
    assert r.churn_ratio == float("inf")


def test_inherited_byte_identity_is_a_regression_check_not_proof(run):
    """★ 보존-A (D5-b). 승계 청크 바이트 동일률 100%.

    🔴 이것은 **항진명제**다 — 부기 밖은 부모 바이트를 그대로 물려주니까.
       100% 가 아니면 승계 경로가 깨진 것이지 기하가 바뀐 것이 아니다.
    """
    assert run["report"].inherited_byte_identity == 1.0


def test_preservation_geometry_distance(run):
    """★ 보존-B (D5-b). 재디코딩 영역 기하 거리 ≤ 잡음 바닥값.

    합성 경로에서는 재디코딩 영역이 **공집합**이므로 거리가 0.0 이고 판정이 자명하다.
    실자산에서는 A5000 이 대조군으로 바닥값을 만들어야 판정이 가능하다.
    """
    p = run["report"].preservation
    assert p.distance == 0.0
    assert p.baseline == SYNTHETIC_NOISE_FLOOR
    assert p.passes is True


def test_preservation_refuses_to_judge_without_noise_floor(run):
    """★★ 잡음 바닥값 없이는 **판정을 거부**한다 (D5-b).

    추정값으로 통과시키면 0.853 의 13% 가 잡음인지 누출인지 못 가른 채
    "보존됨" 이라고 적게 된다. 계측은 되지만 판정은 안 된다.
    """
    r = run["report"]
    bare = metrics.preservation_geometry_distance(
        run["base"], run["splice"].cells, run["mask"].dilated
    )
    assert bare.distance == r.preservation.distance  # 계측은 된다
    assert bare.baseline is None
    with pytest.raises(metrics.NoiseFloorUnknown, match="잡음 바닥값"):
        _ = bare.passes


def test_gate_g2_refuses_without_noise_floor(run):
    """게이트도 같은 이유로 거부한다 — 뒷문이 없어야 규칙이 지켜진다."""
    bare = metrics.evaluate(
        before=run["base"], after=run["splice"].cells,
        mask_cells=run["mask"].cells, edited_region_cells=run["mask"].dilated,
        parent_blobs=run["parent"], child_blobs=run["pkg"].blobs, book=run["bk"].book,
        full_bytes=run["pkg"].full_bytes, delta_bytes=run["pkg"].delta_bytes,
        containment_enforced=True, noise_floor=None,
    )
    with pytest.raises(metrics.NoiseFloorUnknown):
        bare.gate_g2()


def test_transfer_saving(run):
    """★ 절감. 전송 절감 > 40%."""
    assert run["report"].transfer_saving > 0.40


def test_gate_g2_matches_rev6_wording(run):
    """G2 판정이 rev6 §5 S2 문구와 1:1 인지.

    ★ 효능 = 육안 확인 AND 신규>0 AND 연결성분≥0.8 AND churn비≥3.0
    ★ 보존 = 승계 바이트 100% AND 기하거리 ≤ 바닥값
    ★ 절감 = 절감 > 40%

    rev7 에서 D11(재복셀화)·D12(신규 ∪ 제거)가 들어간 뒤 **숫자 조건 셋이 전부**
    성립한다. 육안 확인만 남았고 그건 코드가 만들 수 없다 (원칙 7).
    """
    r = run["report"]
    gate = r.gate_g2()

    assert r.delta_in_mask.new > 0
    assert r.efficacy_change_component >= 0.8
    assert r.churn_ratio >= 3.0
    assert gate["efficacy_numeric"] is True
    assert gate["preservation"] is True
    assert gate["saving"] is True


def test_gate_g2_visual_confirmation_semantics():
    """★ 원칙 7 — 육안 산출물이 없으면 미검증이다.

    게이트 **로직**만 본다 (픽스처 수치와 무관하게). 숫자가 다 맞아도
    `visual_confirmed` 가 없으면 효능은 **미결(None)** 이다 — 통과도 실패도 아니다.
    코드가 만들 수 없는 사실을 코드가 통과시키지 않는다.
    """
    passing = metrics.MetricReport(
        delta_in_mask=metrics.VoxelDelta(new=500, removed=400),
        delta_outside=metrics.VoxelDelta(new=0, removed=0),
        efficacy_change_component=0.99,
        churn_ratio=5.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(
            distance=0.0,
            baseline=metrics.NoiseFloor(0.0, "synthetic", "global", 0),
        ),
        transfer_saving=0.68,
    )
    assert passing.gate_g2()["efficacy"] is None, "육안 확인 없이 효능이 판정됐다"
    assert passing.gate_g2()["visual_confirmed"] is None
    assert passing.gate_g2(visual_confirmed=True)["efficacy"] is True
    assert passing.gate_g2(visual_confirmed=False)["efficacy"] is False

    # 숫자가 안 되면 육안과 무관하게 False 다 (미결이 아니다).
    failing = metrics.MetricReport(
        delta_in_mask=metrics.VoxelDelta(new=0, removed=0),
        delta_outside=metrics.VoxelDelta(new=0, removed=0),
        efficacy_change_component=0.0,
        churn_ratio=0.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(
            distance=0.0,
            baseline=metrics.NoiseFloor(0.0, "synthetic", "global", 0),
        ),
        transfer_saving=1.0,
    )
    assert failing.gate_g2()["efficacy"] is False


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
        mask_cells=mask.cells, edited_region_cells=mask.dilated,
        parent_blobs=parent_blobs, child_blobs=pkg.blobs, book=bk.book,
        full_bytes=pkg.full_bytes, delta_bytes=pkg.delta_bytes,
        containment_enforced=True, noise_floor=SYNTHETIC_NOISE_FLOOR,
        asset_id=SYNTHETIC_ASSET_ID, region="outside_mask",
    )

    # 보존·절감은 만점으로 통과한다.
    assert report.inherited_byte_identity == 1.0
    assert report.preservation.distance == 0.0
    assert report.preservation.passes is True
    assert report.transfer_saving == 1.0

    # 효능은 반드시 떨어진다 — 신규 복셀도, 연결성분도, churn 비도 전부.
    assert report.delta_in_mask.new == 0
    assert report.delta_in_mask.removed == 0
    assert report.efficacy_change_component == 0.0
    assert report.churn_ratio == 0.0

    gate = report.gate_g2(visual_confirmed=True)  # 육안까지 통과했다고 쳐도
    assert gate["preservation"] is True, "no-op 은 보존을 통과해야 한다 (그게 요점이다)"
    assert gate["saving"] is True, "no-op 은 절감을 통과해야 한다 (그게 요점이다)"
    assert gate["efficacy"] is False, (
        "🔴 no-op 이 효능을 통과했다. 효능 지표가 아무것도 막지 못한다는 뜻이다."
    )


def test_noise_only_edit_fails_the_largest_component_gate():
    """★★ 두 번째 음성 대조 (D5-a ②) — **흩뿌려진 잡음**을 걸러내는지.

    신규 복셀 수만 보면 "호박 머리 하나" 와 "표면 잡음 수백 개" 가 구분되지 않는다.
    A5000 이 등록한 맹점이고, 273/274 라는 실측이 그것을 메웠다.

    여기서는 마스크 안에 **서로 떨어진 복셀들**을 흩뿌려 놓는다. 신규 복셀 수는
    많지만 최대 연결성분 비율이 낮아야 하고, 그래서 효능에서 떨어져야 한다.
    """
    base_cells = surface_voxelize(*snowman_mesh())
    mask = build_mask(bbox=HEAD_BBOX, halo=HALO)

    # 마스크 안에서 3칸 간격으로 셀을 흩뿌린다 — 26-이웃으로 서로 안 닿는다.
    m = mask.cells
    scattered = m[(m[:, 0] % 3 == 0) & (m[:, 1] % 3 == 0) & (m[:, 2] % 3 == 0)]
    assert scattered.shape[0] > 20, "픽스처가 잡음을 충분히 못 만든다"

    noisy = np.unique(np.concatenate([base_cells, scattered], axis=0), axis=0)

    delta = metrics.efficacy_voxel_delta(base_cells, noisy, mask.cells)
    largest = metrics.efficacy_largest_component(base_cells, noisy, mask.cells)

    assert delta.new > 20, "신규 복셀 수 자체는 많다 — 그래서 이 지표만으론 못 거른다"
    assert largest < 0.8, (
        f"🔴 흩뿌린 잡음이 연결성분 게이트를 통과했다 ({largest:.3f}). "
        "그러면 '호박 머리' 와 '표면 잡음' 을 구분하지 못한다."
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


# ══════════════════════════════════ 8. D11 부수 — 마스크 축소
def test_per_slice_mask_is_much_smaller_than_bbox_mask():
    """★ D11 부수 — 슬라이스별 bbox 가 마스크를 실제로 좁히는지.

    단일 직육면체 bbox 는 가로로 **몸통 폭까지** 덮는다. 눈사람은 머리가 몸통보다
    훨씬 좁으므로 머리 위 허공이 통째로 마스크가 된다. 마스크가 크면 "국소 편집"
    전제가 무너지고 전송 절감도 과대평가된다 (W3/3090 실측 격자의 21%).
    """
    base = surface_voxelize(*snowman_mesh())
    flat = top_region_cells(base, fraction=HEAD_FRACTION, per_slice=False)
    stair = top_region_cells(base, fraction=HEAD_FRACTION, per_slice=True)

    assert stair.shape[0] < flat.shape[0] * 0.5, (
        f"슬라이스별 bbox 가 마스크를 못 좁혔다: {flat.shape[0]} → {stair.shape[0]}"
    )
    # 계단 마스크는 평면 마스크의 부분집합이다 — 새 셀을 만들어내지 않는다.
    assert set(metrics._codes(stair).tolist()) <= set(metrics._codes(flat).tolist())


def test_per_slice_mask_still_covers_the_head_occupancy():
    """좁히더라도 머리 점유는 전부 덮어야 한다 — 안 덮으면 옛 기하가 남는다."""
    base = surface_voxelize(*snowman_mesh())
    stair = top_region_cells(base, fraction=HEAD_FRACTION, per_slice=True)
    cut = int(stair[:, 2].min())

    head_occ = base[base[:, 2] >= cut]
    assert set(metrics._codes(head_occ).tolist()) <= set(metrics._codes(stair).tolist())


# ══════════════════════════════════ 9. D13 — strict_containment 는 전제
def test_splice_defaults_to_strict_containment():
    """D13 — 기본값이 True 다. 켜는 것을 잊을 수 있으면 전제가 아니다."""
    import inspect

    sig = inspect.signature(splice)
    assert sig.parameters["strict_containment"].default is True


def test_gate_refuses_results_obtained_without_containment(run):
    """★★ D13 — `strict_containment=False` 로 얻은 결과로는 게이트를 잴 수 없다.

    끄면 기증자가 마스크 밖으로 나가 보존이 조용히 무너진다
    (W3/3090 실측 preservation_iou_out 0.345 · 절감 14.05%, 켜면 1.000000).
    계측조차 하지 않고 거부한다 — 숫자가 나오면 누군가는 그걸 쓴다.
    """
    with pytest.raises(metrics.ContainmentNotEnforced, match="D13"):
        metrics.evaluate(
            before=run["base"], after=run["splice"].cells,
            mask_cells=run["mask"].cells, edited_region_cells=run["mask"].dilated,
            parent_blobs=run["parent"], child_blobs=run["pkg"].blobs,
            book=run["bk"].book, full_bytes=run["pkg"].full_bytes,
            delta_bytes=run["pkg"].delta_bytes,
            containment_enforced=False, noise_floor=SYNTHETIC_NOISE_FLOOR,
            asset_id=SYNTHETIC_ASSET_ID,
        )


def test_splice_result_records_containment(run):
    """게이트가 물어볼 수 있도록 결과가 그 사실을 들고 다닌다."""
    assert run["splice"].strict_containment is True


# ══════════════════════════════════ 10. D11 — 크롭 없이 들어간다
def test_donor_fits_the_mask_without_cropping(run):
    """★ D11 의 목표. 기증자가 **크롭 없이** 마스크에 들어간다.

    예전에는 crop ≤ 0.30 에서만 들어가서 화면에 뜨는 것이 호박 위쪽 30%(뚜껑+꼭지)
    뿐이었다. 재복셀화 후에는 crop_fraction=1.0 으로 전체가 들어간다.
    """
    sp, mask = run["splice"], run["mask"]

    assert sp.crop_fraction == 1.0, "크롭으로 크기를 맞추고 있다"
    assert sp.n_donor_outside_mask == 0
    assert run["used_fill"] >= 0.9, f"자동 축소가 크게 걸렸다: {run['used_fill']}"

    donor_span = run["donor"].max(axis=0) - run["donor"].min(axis=0) + 1
    mask_span = mask.cells.max(axis=0) - mask.cells.min(axis=0) + 1
    assert np.all(donor_span <= mask_span + 2), (
        f"기증자 span {donor_span.tolist()} 이 마스크 span {mask_span.tolist()} 를 넘는다"
    )
    # 그리고 마스크를 실제로 채운다 — 한 축이라도 닿아야 "맞춘" 것이다.
    assert np.any(donor_span >= mask_span - 2)


# ══════════════════════════════ 11. D17 — churn_ratio 정의 확정
def test_churn_ratio_is_identical_to_one_minus_iou():
    """★ D17 — `churn_rate(R) = (|new|+|removed|) / |(before ∪ after) ∩ R|` 이
    항등적으로 `1 − IoU(R)` 임을 확인한다. 이 항등성이 정의 채택의 이유다.
    """
    base = surface_voxelize(*snowman_mesh())
    mask = build_mask(top_region_cells(base, fraction=HEAD_FRACTION), halo=HALO)
    donor, _ = fit_donor_to_mask(*donor_mesh(), mask)
    after = splice(base, donor, mask, crop_fraction=1.0).cells

    m = metrics._codes(mask.dilated)
    b = np.intersect1d(metrics._codes(base), m, assume_unique=True)
    a = np.intersect1d(metrics._codes(after), m, assume_unique=True)

    new = np.setdiff1d(a, b, assume_unique=True).size
    removed = np.setdiff1d(b, a, assume_unique=True).size
    union = np.union1d(a, b).size

    assert abs((new + removed) / union - (1.0 - metrics._iou(b, a))) < 1e-12


def test_churn_ratio_is_bounded_unlike_the_rejected_definition():
    """★ D17 — 기각된 before-점유 정규화가 왜 무계인지 숫자로 남긴다.

    내가 W3 에서 제안했던 정의는 마스크 안에서 1.188 > 1 을 냈다 (A5000 실측).
    "비율" 로 해석할 수 없고 before 가 빈 영역에서 발산한다. 합집합 정규화는
    항상 [0,1] 이다.
    """
    base = surface_voxelize(*snowman_mesh())
    mask = build_mask(top_region_cells(base, fraction=HEAD_FRACTION), halo=HALO)
    donor, _ = fit_donor_to_mask(*donor_mesh(), mask)
    after = splice(base, donor, mask, crop_fraction=1.0).cells

    m = metrics._codes(mask.dilated)
    b = np.intersect1d(metrics._codes(base), m, assume_unique=True)
    a = np.intersect1d(metrics._codes(after), m, assume_unique=True)
    new = np.setdiff1d(a, b, assume_unique=True).size
    removed = np.setdiff1d(b, a, assume_unique=True).size

    union_norm = (new + removed) / np.union1d(a, b).size
    before_norm = (new + removed) / max(1, b.size)      # 기각된 정의

    assert 0.0 <= union_norm <= 1.0
    assert before_norm > union_norm, "두 정의가 같은 값을 내면 이 테스트는 무의미하다"


def test_churn_ratio_reproduces_a5000_arithmetic():
    """A5000 이 보고한 4.97 이 D17 정의에서 나오는지 산술로 확인한다.

    IoU_in 0.2700 · IoU_out 0.8531 → (1−0.2700)/(1−0.8531) = 0.7300/0.1469 = 4.97
    """
    assert abs((1 - 0.2700) / (1 - 0.8531) - 4.97) < 0.01


# ══════════════════════════════ 12. D16 — 바닥값 대비 초과배수
def test_preservation_uses_excess_ratio_not_absolute_threshold():
    """★ D16 — 절대 IoU 0.99 문턱은 항진적으로 실패한다. 배수로 판정한다.

    A5000 실측을 그대로 넣는다: 바닥값 0.0701(= 1−0.9299), VoxHammer 거리
    0.1469(= 1−0.8531) → **2.10배**. 절대 문턱 0.99 였다면 바닥값 자체도
    (1−0.9299=0.0701 > 0.01) 실패했을 것이다 — 어떤 디코더로도 통과 불가다.
    """
    floor = metrics.NoiseFloor(1.0 - 0.9299, "boy-statue", "global", 0)
    voxhammer = metrics.PreservationDistance(
        distance=1.0 - 0.8531, baseline=floor, max_excess_ratio=1.0
    )
    assert abs(voxhammer.excess_ratio - 2.10) < 0.02
    assert voxhammer.passes is False

    # 바닥값 자체는 정의상 1.00배 — 통과해야 한다.
    roundtrip = metrics.PreservationDistance(
        distance=floor.value, baseline=floor, max_excess_ratio=1.0
    )
    assert roundtrip.excess_ratio == 1.0
    assert roundtrip.passes is True

    # 🔴 옛 절대 문턱(IoU > 0.99)이었다면 바닥값조차 실패한다 — 항진적 실패.
    assert (1.0 - floor.value) < 0.99


def test_preservation_still_refuses_without_baseline():
    """D16 이 들어와도 baseline-없으면-거부는 유지된다 (D5-b)."""
    p = metrics.PreservationDistance(distance=0.1469, baseline=None)
    with pytest.raises(metrics.NoiseFloorUnknown, match="바닥값"):
        _ = p.excess_ratio
    with pytest.raises(metrics.NoiseFloorUnknown):
        _ = p.passes


def test_synthetic_preservation_is_zero_excess(run):
    """합성 경로는 재디코딩 영역이 공집합이라 거리 0.0 · 초과배수 0.0 이다."""
    p = run["report"].preservation
    assert p.distance == 0.0
    assert p.excess_ratio == 0.0
    assert p.passes is True


def test_assemble_path_beats_voxhammer_on_preservation():
    """★ D3(관통 우선)의 정당성 — assemble 경로가 보존에서 압도적이다.

    assemble(strict_containment=True) 마스크 밖 IoU 1.000000 → 초과배수 0.0
    VoxHammer                          마스크 밖 IoU 0.8531   → 초과배수 2.10
    """
    floor = metrics.NoiseFloor(1.0 - 0.9299, "boy-statue", "global", 0)
    assemble = metrics.PreservationDistance(distance=0.0, baseline=floor)
    voxhammer = metrics.PreservationDistance(distance=1.0 - 0.8531, baseline=floor)
    assert assemble.passes and not voxhammer.passes


# ══════════════════════════ 13. D33 — 바닥값은 자산별·영역별이다
def test_bare_float_baseline_is_rejected():
    """★★ D33 — 맨 float 바닥값은 타입이 거부한다.

    float 하나로 다니면 **어느 자산에서 잰 것인지가 사라진다.** 사라지면
    반드시 잘못 재사용된다 — 소년상 0.0701 을 dragon-c(0.2229)에 쓰면 3.2배
    과대평가되고 "누출 심각" 이라는 오판이 나온다. 예외는 안 난다.
    """
    with pytest.raises(metrics.BaselineMisapplied, match="NoiseFloor"):
        metrics.PreservationDistance(distance=0.2, baseline=0.0701)


def test_noise_floor_requires_an_asset_id():
    for bad in ("", "   "):
        with pytest.raises(metrics.BaselineMisapplied, match="asset_id"):
            metrics.NoiseFloor(0.0701, bad)


def test_baseline_from_another_asset_is_rejected():
    """★★ 자산 경계를 넘는 재사용이 **정확히** 막히는지."""
    boy = metrics.NoiseFloor(0.0701, "boy-statue", "global", 0)
    p = metrics.PreservationDistance(distance=0.20, baseline=boy, asset_id="dragon-c")
    with pytest.raises(metrics.BaselineMisapplied, match="3.2배"):
        _ = p.excess_ratio


def test_baseline_from_another_region_is_rejected():
    """같은 자산 안에서도 목 0.1538 vs 머리 0.2358 로 1.5배 차이다."""
    neck = metrics.DRAGON_C_NOISE_FLOORS["neck"]
    p = metrics.PreservationDistance(
        distance=0.20, baseline=neck, asset_id="dragon-c", region="head"
    )
    with pytest.raises(metrics.BaselineMisapplied, match="영역"):
        _ = p.excess_ratio


def test_small_sample_is_surfaced_next_to_the_verdict():
    """★ 목 대역은 100복셀뿐이다 — 분산이 큰 값과 작은 값을 같은 문턱으로 재지 않는다.

    표본 크기를 숨기면 다음 세션이 0.1538 을 전역 0.2229 와 같은 신뢰도로 쓴다.
    """
    neck = metrics.DRAGON_C_NOISE_FLOORS["neck"]
    assert neck.n_voxels == 100
    assert neck.is_small_sample

    p = metrics.PreservationDistance(
        distance=0.20, baseline=neck, asset_id="dragon-c", region="neck"
    )
    assert p.is_small_sample, "판정 옆에 표본 경고가 안 붙는다"
    assert "분산 큼" in neck.describe()

    # 전역은 표본이 크다 — 같은 문턱으로 재도 되는 쪽이다.
    assert not metrics.DRAGON_C_NOISE_FLOORS["global"].is_small_sample


def test_dragon_c_floors_match_the_measured_table():
    """D33 표 A 를 그대로 못박는다. 값이 바뀌면 여기서 걸린다."""
    f = metrics.DRAGON_C_NOISE_FLOORS
    assert f["global"].value == pytest.approx(0.2229)
    assert f["neck"].value == pytest.approx(0.1538)
    assert f["head"].value == pytest.approx(0.2358)
    assert f["body"].value == pytest.approx(0.2225)
    # 소년상 대비 3.2배 — 그대로 쓰면 안 되는 이유.
    assert f["global"].value / 0.0701 == pytest.approx(3.18, abs=0.05)
    # 디코더 분산도 5.7배 (소년상 0.0003).
    assert f["global"].decoder_variance == pytest.approx(0.0017)


def test_same_asset_and_region_passes_through():
    """거부가 과잉이면 정상 경로까지 막는다."""
    neck = metrics.DRAGON_C_NOISE_FLOORS["neck"]
    p = metrics.PreservationDistance(
        distance=0.10, baseline=neck, asset_id="dragon-c", region="neck"
    )
    assert p.excess_ratio == pytest.approx(0.10 / 0.1538, rel=1e-6)
    assert p.passes is True


def test_voxhammer_budget_is_corrected_to_400s():
    """D31-a — G4 의 "1회 < 300초" 는 캐시 상태를 정상으로 오인한 문턱이었다."""
    assert metrics.VOXHAMMER_BUDGET_SECONDS == 400.0
    stages = metrics.VOXHAMMER_STAGE_SECONDS
    assert sum(stages.values()) == pytest.approx(351.3, abs=0.5)
    # 문턱이 실비용보다 낮으면 정상 실행이 예산 초과로 기록된다.
    assert sum(stages.values()) > 300.0
    assert sum(stages.values()) < metrics.VOXHAMMER_BUDGET_SECONDS


# ══════════════════ 14. D36 — halo 분모는 **대역 전용**이다
def test_halo_bands_are_a_different_space_from_z_bands():
    """★★ 목 z대역과 halo 대역은 **서로 다른 공간**이다 (D36).

    rev14 는 "halo 분모 = 목 대역 0.1538" 이라고 적었는데 틀렸다. 대역 전용
    실측은 halo-1 에서 **0.0222** 다 — 목 값을 쓰면 **약 7배 과소평가**한다.
    W8 의 "0.0701 → 0.2229 (3.2배 과대평가)" 에 이은 **분모 오류 두 번째**이고,
    이번엔 반대 방향이다.
    """
    f = metrics.DRAGON_C_NOISE_FLOORS
    # 값 자체는 D36-a 에서 실마스크 값으로 교체됐다 (아래 D36-a 테스트가 잠근다).
    # 여기서 잠그는 것은 **두 공간이 다르다**는 사실이다.
    assert f["halo_band_1"].value != f["neck"].value
    assert f["halo_band_1"].region != f["neck"].region

    for name in metrics.HALO_BAND_REGIONS:
        assert f[name].is_halo_band
    for name in ("global", "body", "neck", "head"):
        assert not f[name].is_halo_band


def test_neck_baseline_cannot_be_used_as_a_halo_denominator():
    """★★ **D36 의 핵심.** 목 바닥값을 halo 분모로 넘기면 거부된다."""
    neck = metrics.DRAGON_C_NOISE_FLOORS["neck"]
    r = metrics.HaloBandResult(
        halo=1, n_new=1, n_removed=0, n_band_cells=2516, n_union=45, baseline=neck
    )
    with pytest.raises(metrics.BaselineMisapplied) as exc:
        _ = r.excess_ratio
    assert "다른 공간" in str(exc.value)
    assert "7배" in str(exc.value)


def test_halo_band_region_rejects_unmeasured_widths():
    """안 잰 폭을 보간하지 않는다 — 없는 값을 있는 척하지 않는다."""
    assert metrics.halo_band_region(2) == "halo_band_2"
    for bad in (0, 4, 10):
        with pytest.raises(metrics.BaselineMisapplied, match="halo"):
            metrics.halo_band_region(bad)


def test_provisional_flag_exists_and_is_now_cleared():
    """잠정값이라는 사실은 값과 함께 다녀야 한다 (D36) — 그리고 실측이 오면 내린다.

    W9 halo 값은 `provisional=True` 였고, W11 에서 실마스크 값으로 교체되면서
    플래그가 내려갔다 (D36-a). 플래그 자체가 없으면 "잠정" 과 "확정" 이 구분되지
    않고, 그러면 의사값이 확정값으로 인용된다 — 실제로 10배 틀린 값이었다.
    """
    probe = metrics.NoiseFloor(0.1, "x", "global", 10, provisional=True)
    assert "잠정" in probe.describe()

    for name in metrics.HALO_BAND_REGIONS:
        assert not metrics.DRAGON_C_NOISE_FLOORS[name].provisional, name


# ══════════════════ 15. ★★ D37 — 비율 단독 판정 거부
def test_ratio_alone_is_refused_when_the_sample_is_tiny():
    """★★ **D37.** 복셀 하나가 대역의 2% 면 초과배수에 유효숫자가 없다.

    halo-1 대역 합집합이 45복셀이고 바닥값 분자가 신규 1개다. 신규가 2개가 되면
    비율이 그대로 2배가 된다 — 소수점으로 논할 수 없다.
    지표를 버리는 **두 번째** 경우다 (첫 번째는 D5 전역 실루엣).
    """
    r = metrics.HaloBandResult(
        halo=1, n_new=1, n_removed=0, n_band_cells=2516, n_union=45,
        baseline=metrics.DRAGON_C_NOISE_FLOORS["halo_band_1"],
    )
    assert not r.has_ratio_resolution
    assert r.voxel_resolution == pytest.approx(1 / 45)

    with pytest.raises(metrics.RatioWithoutResolution) as exc:
        r.verdict()
    msg = str(exc.value)
    assert "유효숫자" in msg
    assert "신규 1" in msg          # 원시 개수를 오류 메시지에 실어 보낸다
    assert "육안" in msg


def test_raw_counts_come_first_in_the_report():
    """★ 원시 개수가 **1급 시민**이다. 비율은 뒤에 참고로만 붙는다 (D37)."""
    r = metrics.HaloBandResult(
        halo=1, n_new=1, n_removed=0, n_band_cells=2516, n_union=45,
        baseline=metrics.DRAGON_C_NOISE_FLOORS["halo_band_1"],
    )
    text = r.describe()
    assert text.index("신규 1") < text.index("참고 비율"), "비율이 개수보다 앞에 있다"
    assert "유효숫자 없음" in text
    assert "육안 미확인" in text
    assert r.n_churn == 1


def test_visual_confirmation_decides_and_code_cannot_make_it():
    """★ 육안은 `visual_confirmed` 와 같은 방식으로 코드가 만들 수 없게 둔다 (D19·D37)."""
    base = dict(halo=1, n_new=1, n_removed=0, n_band_cells=2516, n_union=45,
                baseline=metrics.DRAGON_C_NOISE_FLOORS["halo_band_1"])

    assert metrics.HaloBandResult(**base, visual_confirmed=True).verdict() is True
    assert metrics.HaloBandResult(**base, visual_confirmed=False).verdict() is False
    # 기본값은 미결이다 — 자동으로 True 가 되지 않는다.
    assert metrics.HaloBandResult(**base).visual_confirmed is None


def test_ratio_only_requires_an_explicit_opt_in_and_is_knife_edge():
    """우회는 가능하되 **선언해야** 한다 — "해상도가 없는 줄 알면서 쓴다".

    ★ 그리고 이 테스트가 D37 을 그대로 실증한다. 같은 데이터(신규 1 / 합집합 45)에서
      실제 비율은 1/45 = 0.022222… 인데 기록된 바닥값은 반올림된 0.0222 다.
      초과배수가 **1.001배** — 문턱 1.0 의 어느 쪽인지가 반올림으로 갈린다.
      "약 1배" 는 맞는 말이고 "1.001 > 1.0 이므로 실패" 는 무의미한 말이다.
    """
    floor = metrics.NoiseFloor(0.0222, "dragon-c", "halo_band_1", 45)
    r = metrics.HaloBandResult(
        halo=1, n_new=1, n_removed=0, n_band_cells=2516, n_union=45, baseline=floor
    )
    # 우회 자체는 동작한다 — 예외 없이 bool 이 나온다.
    assert isinstance(r.verdict(allow_ratio_only=True), bool)

    # 🔴 그 bool 이 무의미한 이유: 문턱이 반올림 오차 안에 있다.
    #    실제 비율은 1/45 = 0.022222… 인데 기록된 바닥값은 0.0222 다.
    assert r.excess_ratio == pytest.approx(1.0, abs=0.01)
    assert abs(r.excess_ratio - 1.0) < r.voxel_resolution, (
        "초과배수와 문턱의 차이가 복셀 1개가 만드는 변화보다도 작다 — "
        "이 비율로는 통과/실패를 가를 수 없다 (D37)"
    )
    # 신규가 하나만 늘어도 비율이 2배가 된다. 그게 '해상도 없음' 의 뜻이다.
    two = metrics.HaloBandResult(
        halo=1, n_new=2, n_removed=0, n_band_cells=2516, n_union=45, baseline=floor
    )
    assert two.excess_ratio == pytest.approx(2.0, abs=0.02)


def test_a_large_band_would_have_ratio_resolution():
    """거부가 과잉이면 표본이 큰 경우까지 막는다 — 그건 지표를 죽이는 것이다."""
    big = metrics.HaloBandResult(
        halo=1, n_new=30, n_removed=10, n_band_cells=20000, n_union=4000,
        baseline=metrics.DRAGON_C_NOISE_FLOORS["halo_band_1"],
    )
    assert big.has_ratio_resolution
    assert isinstance(big.verdict(), bool)     # 예외 없이 판정된다


# ══════════════════ 16. D25-b · D31-b
def test_neck_minimum_is_read_from_the_table_not_a_summary():
    """★ D25-b — 진짜 극소는 z=45(28복셀)이고 절단은 z=46 이다.

    Chat 이 rev14 에 "극소 z=44(32)" 로 잘못 요약했고, 한 칸 차이로 목을 문다.
    요약 오류가 실제 결정을 바꾼 것이 세 번째라 **표에서 직접 고르게** 했다.
    """
    assert metrics.DRAGON_C_NECK_PROFILE[44] == 32
    assert metrics.DRAGON_C_NECK_PROFILE[45] == 28
    assert metrics.neck_minimum_z() == 45
    assert metrics.neck_cut_z() == 46


def test_features_regeneration_check_is_not_disabled():
    """★ D31-b — 재사용이 검증과 충돌한다고 검증을 끄지 않는다.

    W1 1회차가 7월자 낡은 features 로 돌면서 "completed successfully" 를 찍었다.
    끄면 정확히 그 사고가 다시 난다. 재생성 24.5초가 그 위험보다 싸다.
    """
    assert metrics.EXPECT_FEATURES_REGEN is True
    assert metrics.VOXHAMMER_BUDGET_SECONDS_REUSED_RENDER == 270.0
    # 렌더만 재사용하는 정상 경로는 400초 문턱 안에 넉넉히 들어온다.
    assert metrics.VOXHAMMER_BUDGET_SECONDS_REUSED_RENDER < metrics.VOXHAMMER_BUDGET_SECONDS


# ══════════════════ 17. ★★ D38 — 방향 조건 (W10 이 통과한 틈)
def test_w10_destruction_would_now_fail_the_add_gate():
    """★★ **W10 재현.** 게이트가 파괴를 통과시킨 그 데이터를 그대로 넣는다.

    최대 연결성분 1.000 ≥ 0.8 로 통과했는데 제거 730 > 신규 304 였다.
    전체 복셀 8,000 → 6,744. **머리를 만든 게 아니라 먹었다.**
    """
    w10 = metrics.VoxelDelta(new=304, removed=730)
    assert w10.net == -426

    with pytest.raises(metrics.DirectionMismatch) as exc:
        metrics.check_direction("add", w10)
    msg = str(exc.value)
    assert "신규 > 제거" in msg
    assert "304" in msg and "730" in msg
    assert not metrics.direction_holds("add", w10)


def test_pure_destruction_fails_add():
    """★★ 음성 대조 — 순수 파괴(신규 0 / 제거 다수)가 add 에서 **반드시** 떨어진다."""
    pure = metrics.VoxelDelta(new=0, removed=500)
    assert not metrics.direction_holds("add", pure)
    with pytest.raises(metrics.DirectionMismatch):
        metrics.check_direction("add", pure)
    # 같은 데이터가 remove 에서는 통과한다 — 방향이 op 에 달렸다는 뜻이다.
    metrics.check_direction("remove", pure)


@pytest.mark.parametrize(
    "op, delta, ok",
    [
        ("add",            metrics.VoxelDelta(500, 100), True),
        ("add",            metrics.VoxelDelta(100, 500), False),
        ("add",            metrics.VoxelDelta(300, 300), False),   # 동점도 실패
        ("remove",         metrics.VoxelDelta(100, 500), True),
        ("remove",         metrics.VoxelDelta(500, 100), False),
        ("replace_region", metrics.VoxelDelta(100, 500), True),    # 방향 무관
        ("replace_region", metrics.VoxelDelta(500, 100), True),
        ("recolor",        metrics.VoxelDelta(0, 0),     True),    # 기하 불변
        ("recolor",        metrics.VoxelDelta(1, 0),     False),
        ("recolor",        metrics.VoxelDelta(0, 1),     False),
    ],
)
def test_direction_rules_per_op(op, delta, ok):
    """D26 매핑표와 짝을 이루는 방향 조건 전수."""
    assert metrics.direction_holds(op, delta) is ok


def test_unknown_op_is_not_waved_through():
    """방향 규칙 없는 op 를 통과시키면 W10 이 반복된다."""
    with pytest.raises(KeyError, match="방향 규칙"):
        metrics.check_direction("resize", metrics.VoxelDelta(10, 0))


def test_every_op_has_a_direction_rule():
    """`llm.OPS` 에 op 를 추가하면 방향도 정해야 한다."""
    from server.llm import OPS

    assert set(metrics.DIRECTION_RULES) == set(OPS)


def test_gate_g2_checks_direction_when_op_is_given(run):
    """★ 게이트에 op 를 넘기면 방향이 검사된다."""
    r = run["report"]
    # 합성 픽스처는 치환(replace_region)이라 방향 무관이다.
    assert r.gate_g2(op="replace_region")["direction_ok"] is True
    # 같은 결과를 add 라고 주장하면 — 신규 528 / 제거 540 이라 떨어진다.
    assert r.delta_in_mask.removed > r.delta_in_mask.new
    g = r.gate_g2(op="add")
    assert g["direction_ok"] is False
    assert g["efficacy_numeric"] is False


def test_gate_g2_records_that_direction_was_not_checked(run):
    """🔴 op 를 안 주면 방향이 **검사되지 않는다** — 그 사실이 결과에 남는다.

    W10 이 정확히 그 상태로 통과했다. None 을 True 로 읽으면 안 된다.
    """
    g = run["report"].gate_g2()
    assert g["direction_ok"] is None


# ══════════════════ 18. D39 — 앵커 잔존율 (문턱 없음)
def test_anchor_retention_reports_without_a_threshold():
    """★ D39 — 값을 내고 **병기**만 한다. 문턱은 정하지 않는다.

    데이터가 한 점뿐이다. 한 점으로 문턱을 만드는 것이 이 프로젝트가 반복해
    물린 모양이다 (D5 · D16 · D33 · D37 전부 같은 병).
    """
    w10 = metrics.AnchorRetention(n_asset_in_mask=955, n_mask_cells=11632)
    assert w10.empty_fraction == pytest.approx(0.9179, abs=0.001)
    assert w10.retention is None            # 분모를 모르면 None — 추측하지 않는다
    text = w10.describe()
    assert "91.79%" in text
    assert "문턱 없음" in text


def test_anchor_retention_with_a_known_region():
    r = metrics.AnchorRetention(
        n_asset_in_mask=955, n_mask_cells=11632, n_region_asset=1900
    )
    assert r.retention == pytest.approx(955 / 1900)
    assert "앵커 잔존율" in r.describe()


def test_anchor_retention_has_no_pass_fail_method():
    """문턱이 없다는 것을 **구조로** 남긴다 — verdict/passes 가 없어야 한다."""
    for name in ("verdict", "passes", "ok", "gate"):
        assert not hasattr(metrics.AnchorRetention, name), name


# ══════════════════ 19. D36-a — 실마스크 바닥값으로 교체
def test_halo_floors_are_the_real_mask_measurements():
    """★ W9 의사값은 **폐기**됐다. halo-1 이 10배 틀렸다."""
    f = metrics.DRAGON_C_NOISE_FLOORS
    assert f["halo_band_1"].value == pytest.approx(0.2222)
    assert f["halo_band_2"].value == pytest.approx(0.2100)
    assert f["halo_band_3"].value == pytest.approx(0.1681)

    # provisional 이 내려갔다 — 더 이상 잠정치가 아니다.
    for name in metrics.HALO_BAND_REGIONS:
        assert not f[name].provisional, name

    # 폐기된 의사값과의 격차. halo-1 이 정확히 10배다.
    old = metrics.DISCARDED_PSEUDO_HALO_FLOORS
    assert f["halo_band_1"].value / old["halo_band_1"] == pytest.approx(10.0, abs=0.05)


def test_discarded_pseudo_floors_are_not_usable_as_baselines():
    """폐기값은 **NoiseFloor 가 아니라 맨 숫자**로 남긴다 — 실수로 못 쓰게."""
    for v in metrics.DISCARDED_PSEUDO_HALO_FLOORS.values():
        assert isinstance(v, float)
        assert not isinstance(v, metrics.NoiseFloor)


# ══════════════════ 20. ★★ D29-a — "머리" 는 절단면 위로 뻗는 성분이다
def _wing_and_head():
    """W11 반례 픽스처 — 날개 조각(z 44–46)과 진짜 머리(z 46–58)."""
    wing = [(x, 30, z) for x in range(8, 16) for z in range(44, 47)]
    head = [(x, 30, z) for x in range(30, 34) for z in range(46, 59)]
    return np.array(sorted(set(wing + head)), dtype=np.int64)


def test_components_carry_shape_not_just_count():
    """★ 성분마다 z 범위·x 중심을 함께 낸다 — 개수만으로는 못 가른다 (D29-a)."""
    comps = metrics.components(_wing_and_head())
    assert len(comps) == 2

    head, wing = comps                      # 큰 것부터
    assert (head.z_min, head.z_max) == (46, 58)
    assert (wing.z_min, wing.z_max) == (44, 46)
    assert head.x_center > wing.x_center
    assert "z 46–58" in head.describe()


def test_wing_fragment_is_not_counted_as_a_head():
    """★★ **W11 반례.** 날개 조각은 절단면 위로 뻗지 않으므로 머리가 아니다.

    개수로만 세면 이 조각이 "머리 하나" 로 잡힌다. 실제로 z 44–46 짜리 조각이
    있었고 위로 뻗지 않았다.
    """
    a = _wing_and_head()
    heads = metrics.head_components(a, cut_z=45)

    assert len(heads) == 1, [c.describe() for c in heads]
    assert heads[0].z_max == 58
    # 날개는 절단면 위로 **1칸**만 걸쳤다 — 뻗은 것이 아니다.
    wing = [c for c in metrics.components(a) if c.z_max == 46][0]
    assert wing.height_above(45) == 1
    assert not wing.rises_above(45)


def test_cut_plane_itself_does_not_count_as_thickness():
    """🔴 절단면을 두께에 넣으면 날개가 통과한다 — 실제로 그렇게 짰다가 잡혔다.

    46 − max(44,45) + 1 = 2 로 문턱을 만족해 버린다. 위로 뻗은 높이만 센다.
    """
    wing = metrics.Component(n_cells=24, z_min=44, z_max=46, x_center=11.5, y_center=30.0)
    assert wing.height_above(45) == 1
    assert not wing.rises_above(45, min_thickness=2)
    assert wing.rises_above(45, min_thickness=1)     # 문턱 1 이면 통과한다


def test_three_heads_are_counted_when_they_actually_rise():
    """머리 셋이 진짜로 위로 뻗으면 셋으로 센다 (D22 레벨2 판정의 형태)."""
    cells = []
    for cx in (16, 32, 48):
        cells += [(cx + dx, 30, z) for dx in (-1, 0, 1) for z in range(46, 58)]
    heads = metrics.head_components(np.array(cells, dtype=np.int64), cut_z=45)
    assert len(heads) == 3
    assert sorted(round(c.x_center) for c in heads) == [16, 32, 48]


# ══════════════════ 21. D41 — headroom (문턱 없음)
def test_headroom_reports_without_a_threshold():
    """★ dragon-c 는 z 0–62 로 위쪽 여유가 **1칸**뿐이다. 값만 낸다 (D41 · D39-a)."""
    a = np.array([[1, 2, 0], [60, 61, 62]], dtype=np.int64)
    hr = metrics.Headroom.from_cells(a)
    assert hr.up == 1                      # 63 - 62
    assert hr.lo == (1, 2, 0)
    assert hr.minimum == 0
    assert "문턱 없음" in hr.describe()


def test_headroom_has_no_pass_fail_method():
    """문턱이 없다는 것을 **구조로** 남긴다 (D39-a)."""
    for name in ("verdict", "passes", "ok", "gate"):
        assert not hasattr(metrics.Headroom, name), name


def test_headroom_rejects_empty_occupancy():
    with pytest.raises(ValueError, match="비었다"):
        metrics.Headroom.from_cells(np.zeros((0, 3), dtype=np.int64))


# ══════════════ 22. ★★ D38(rev28) — cc_frac 을 분기 op 게이트에서 뺀다
def test_cc_frac_evidence_shows_it_is_backwards_for_branching():
    """★★ 증거 4건. 통과/탈락이 성공/실패와 **아무 상관이 없다.**

        W10  1.000  통과 → 🔴 파괴 (제거 730 > 신규 304)
        W12  0.733  탈락 → 성공 쪽
        W13  0.845  통과
        runG 0.537  탈락 → 🔴 역대 최고 결과

    머리를 셋으로 만들면 변화가 셋으로 갈라지므로 최대 연결성분 비율은 **반드시**
    떨어진다. 지표가 측정 대상의 물리와 정확히 반대다.
    """
    assert not metrics.uses_cc_frac_gate("add")
    assert "efficacy_change_component" in metrics.REFERENCE_METRICS
    assert "efficacy_change_component" not in metrics.GATE_METRICS
    assert "branch_component_count" in metrics.GATE_METRICS


def test_cc_frac_stays_for_non_branching_ops():
    """★ 다른 op 에서는 유지한다 (W16 판단).

    replace_region / recolor / remove 는 **하나의 응집된 변화**를 기대하므로
    "한 덩어리인가" 가 여전히 옳은 질문이다. 갈라지는 것이 목적인 op 는 add 뿐이다.
    """
    assert metrics.BRANCHING_OPS == frozenset({"add"})
    for op in ("replace_region", "recolor", "remove"):
        assert metrics.uses_cc_frac_gate(op), op
    # op 를 모르면 쓰지 않는다 — W10 이 통과한 그 상태로 돌아가지 않는다.
    assert not metrics.uses_cc_frac_gate(None)


def test_rung_passes_the_new_branch_gate():
    """★★ **runG 재현.** 성분 3 ≈ factor 3 · direction TRUE → **통과해야 한다.**

    옛 게이트는 cc_frac 0.537 로 탈락시켰다 — 역대 최고 결과였는데.
    """
    target = metrics.BranchTarget(op="add", factor=3.0)
    assert target.expected_components == 3
    assert target.component_count_ok(3) is True
    assert "통과" in target.describe(3)

    report = metrics.MetricReport(
        delta_in_mask=metrics.VoxelDelta(new=900, removed=400),   # direction TRUE
        delta_outside=metrics.VoxelDelta(0, 0),
        efficacy_change_component=0.537,                          # 🔴 옛 문턱 미달
        churn_ratio=5.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(
            distance=0.0, baseline=metrics.NoiseFloor(0.0, "dragon-c", "global", 0)
        ),
        transfer_saving=0.6,
        n_change_components=3,
    )
    g = report.gate_g2(target=target)
    assert g["direction_ok"] is True
    assert g["component_count_ok"] is True
    assert g["cc_frac_gated"] is False, "분기 op 인데 cc_frac 이 게이트에 남아 있다"
    assert g["efficacy_numeric"] is True, "runG 가 여전히 탈락한다"


def test_w10_destruction_still_fails_the_new_branch_gate():
    """★★ 문턱을 뺐다고 **파괴까지 통과시키면 안 된다.** 방향 조건이 잡는다."""
    report = metrics.MetricReport(
        delta_in_mask=metrics.VoxelDelta(new=304, removed=730),   # W10
        delta_outside=metrics.VoxelDelta(0, 0),
        efficacy_change_component=1.000,                          # 옛 게이트는 통과시켰다
        churn_ratio=5.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(
            distance=0.0, baseline=metrics.NoiseFloor(0.0, "dragon-c", "global", 0)
        ),
        transfer_saving=0.6,
        n_change_components=3,
    )
    g = report.gate_g2(target=metrics.BranchTarget(op="add", factor=3.0))
    assert g["direction_ok"] is False
    assert g["efficacy_numeric"] is False


def test_component_count_tolerance_and_absence():
    t = metrics.BranchTarget(op="add", factor=3.0, tolerance=1)
    assert [t.component_count_ok(n) for n in (1, 2, 3, 4, 5)] == \
        [False, True, True, True, False]
    # 분기 op 가 아니거나 factor 가 없으면 목표가 없다 — 미검사(None).
    assert metrics.BranchTarget(op="replace_region", factor=3.0).component_count_ok(1) is None
    assert metrics.BranchTarget(op="add", factor=None).component_count_ok(1) is None


def test_change_components_counts_branches(run):
    """`change_components` 가 실제 파이프라인에서 성분을 센다."""
    n = len(metrics.change_components(
        run["base"], run["splice"].cells, run["mask"].cells
    ))
    assert n >= 1
    assert run["report"].n_change_components == n


# ══════════════ 23. ★★ D54 — overflow 부기 연결성분 필터
def _mask_at(center, half=4, halo=1):
    from deltacontract.coords import dense_cells

    c = np.asarray(center, dtype=np.int64)
    return build_mask(dense_cells(c - half, c + half + 1), halo=halo)


def test_overflow_refuses_without_a_measured_noise_size():
    """★★ 문턱을 **한 점으로 정하지 않는다** (D39-a). 값이 없으면 판정을 거부한다."""
    from server.pipeline.delta import OverflowThresholdUnknown, classify_overflow

    mask = _mask_at((32, 32, 32))
    base = np.array([[32, 32, 32]], dtype=np.int64)
    with pytest.raises(OverflowThresholdUnknown, match="A5000"):
        classify_overflow(base, base, mask)


def test_pure_noise_does_not_grow_the_bookkeeping():
    """★★ **음성 대조.** 고립 복셀만 있는 입력에서 부기가 늘지 않는가.

    실측 overflow 602복셀 중 404 가 전역 리메시 잡음이었다. 필터 없이 넣으면
    80/124 청크(64.5%)가 델타에 끌려와 절감률이 죽는다.
    """
    from server.pipeline.delta import classify_overflow

    mask = _mask_at((32, 32, 32))
    base = np.array([[32, 32, 32]], dtype=np.int64)

    # 서로 26-이웃이 안 되게 4칸 간격으로 흩뿌린 고립 복셀 (전형적 리메시 잡음).
    noise = np.array(
        [[x, y, z] for x in range(4, 60, 8) for y in range(4, 60, 8) for z in (8, 56)],
        dtype=np.int64,
    )
    after = np.unique(np.concatenate([base, noise], axis=0), axis=0)

    r = classify_overflow(base, after, mask, noise_max_component=1)
    assert r.n_overflow_voxels == noise.shape[0]
    assert r.n_signal_voxels == 0, r.describe()
    assert r.n_noise_voxels == noise.shape[0]
    assert r.signal_chunks == [], "잡음이 부기를 늘렸다 — 절감률이 죽는다"
    assert r.noise_chunks_skipped, "걸러낸 청크가 기록되지 않는다"
    assert max(r.component_sizes) == 1


def test_connected_structure_is_kept_as_signal():
    """★ 연결된 덩어리는 **실제 구조**다 (측면 머리는 각 300+ 복셀, 연결됨)."""
    from server.pipeline.delta import classify_overflow

    mask = _mask_at((32, 32, 32))
    base = np.array([[32, 32, 32]], dtype=np.int64)

    blob = np.array(
        [[x, y, z] for x in range(8, 16) for y in range(8, 16) for z in range(8, 16)],
        dtype=np.int64,
    )
    isolated = np.array([[50, 50, 50], [58, 20, 40]], dtype=np.int64)
    after = np.unique(np.concatenate([base, blob, isolated], axis=0), axis=0)

    r = classify_overflow(base, after, mask, noise_max_component=1)
    assert r.n_signal_voxels == blob.shape[0]
    assert r.n_noise_voxels == isolated.shape[0]
    assert r.signal_chunks, "연결된 구조가 부기에서 빠졌다 — 옛 기하가 남는다"
    # 걸러낸 청크와 신호 청크가 겹치지 않는다.
    assert not (set(r.signal_chunks) & set(r.noise_chunks_skipped))


def test_threshold_is_noise_size_plus_one():
    """문턱은 잡음의 **최대** 연결성분 크기 + 1 이다 — 경계를 명확히 한다."""
    from server.pipeline.delta import classify_overflow

    mask = _mask_at((32, 32, 32))
    base = np.array([[32, 32, 32]], dtype=np.int64)
    pair = np.array([[8, 8, 8], [8, 8, 9]], dtype=np.int64)      # 크기 2 덩어리
    after = np.unique(np.concatenate([base, pair], axis=0), axis=0)

    assert classify_overflow(base, after, mask, noise_max_component=1).threshold == 2
    # 잡음 최대가 1이면 크기 2 는 신호다.
    assert classify_overflow(base, after, mask, noise_max_component=1).n_signal_voxels == 2
    # 잡음 최대가 2면 같은 덩어리가 잡음이 된다.
    assert classify_overflow(base, after, mask, noise_max_component=2).n_signal_voxels == 0


def test_overflow_ignores_cells_inside_the_mask_and_halo():
    """마스크 + halo 안은 이미 부기에 있다 — overflow 로 이중 계상하지 않는다."""
    from server.pipeline.delta import classify_overflow

    mask = _mask_at((32, 32, 32), half=4, halo=1)
    base = np.array([[0, 0, 0]], dtype=np.int64)
    inside = mask.dilated[:20]
    after = np.unique(np.concatenate([base, inside], axis=0), axis=0)

    r = classify_overflow(base, after, mask, noise_max_component=1)
    assert r.n_overflow_voxels == 0
    assert r.signal_chunks == []


def test_overflow_uses_occupancy_not_hashes():
    """⚠️ 해시 비교 금지 — 재디코딩하면 152/152 청크가 전부 다른 해시를 낸다."""
    import inspect

    from server.pipeline import delta as delta_mod

    src = inspect.getsource(delta_mod.classify_overflow)
    assert "hash" not in src.lower(), "overflow 분류가 해시를 본다"


def test_bookkeeping_grows_only_by_signal_chunks(run):
    """★ 부기 = (마스크 + halo) ∪ **신호 overflow 청크**."""
    from server.pipeline.delta import (
        classify_overflow,
        derive_bookkeeping_with_overflow,
    )

    sp, mask = run["splice"], run["mask"]
    child = run["child"]

    blob = np.array(
        [[x, y, 5] for x in range(2, 10) for y in range(2, 10)], dtype=np.int64
    )
    after = np.unique(np.concatenate([sp.cells, blob], axis=0), axis=0)
    ov = classify_overflow(sp.cells, after, mask, noise_max_component=1)
    assert ov.signal_chunks

    produced = set(child) | set(ov.signal_chunks)
    merged = derive_bookkeeping_with_overflow(sp, produced, overflow=ov)
    assert set(ov.signal_chunks) <= set(merged.book)
    assert set(run["bk"].book) <= set(merged.book)
    assert set(merged.changed) | set(merged.removed) == set(merged.book)


def test_noise_only_overflow_leaves_bookkeeping_unchanged(run):
    """★★ **음성 대조 (부기 수준).** 잡음만이면 부기가 그대로다."""
    from server.pipeline.delta import (
        classify_overflow,
        derive_bookkeeping_with_overflow,
    )

    sp, mask = run["splice"], run["mask"]
    noise = np.array([[2, 2, 2], [60, 60, 60], [2, 60, 30]], dtype=np.int64)
    after = np.unique(np.concatenate([sp.cells, noise], axis=0), axis=0)

    ov = classify_overflow(sp.cells, after, mask, noise_max_component=1)
    assert ov.signal_chunks == []

    merged = derive_bookkeeping_with_overflow(sp, run["child"].keys(), overflow=ov)
    assert merged.book == run["bk"].book, "잡음이 부기를 늘렸다"


# ══════════════ 24. ★★ D51 — W10~W13 보존 수치 폐기
def test_discarded_preservation_refuses_to_be_used():
    """★★ 폐기된 수치는 **쓸 때 예외를 던진다.** bool 이면 무시당한다."""
    m = metrics.DISCARDED_PRESERVATION["W13"]
    assert m.discarded and m.discard_reason
    assert m.preservation_rate < 0.002, "13/8,511 = 0.15%"
    with pytest.raises(metrics.DiscardedMeasurement, match="D51"):
        _ = m.excess_ratio
    with pytest.raises(metrics.DiscardedMeasurement):
        m.require_valid()
    assert "폐기" in m.describe()


def test_discarded_waves_are_named_not_quoted():
    """W10~W12 는 수치를 옮기지 않는다 — 옮기면 인용된다. 이름만 남기고 거부한다."""
    assert metrics.DISCARDED_PRESERVATION_WAVES == ("W10", "W11", "W12", "W13")
    for wave in ("W10", "W11", "W12"):
        with pytest.raises(metrics.DiscardedMeasurement, match="무효"):
            metrics.preservation_measurement(wave)


def test_canonical_preservation_is_rung_and_runf():
    """★ 새 정본. runG 가 역대 최고 보존(1.16×)이다."""
    runf = metrics.preservation_measurement("runF")
    rung = metrics.preservation_measurement("runG")
    assert round(runf.excess_ratio, 2) == 1.23
    runf.require_consistent_ratio()          # 분모와 보고가 일치한다
    assert rung.excess_ratio < runf.excess_ratio, "runG 가 더 나아야 한다"
    for m in (runf, rung):
        m.require_valid()
        assert round(m.preservation_rate, 2) == 0.89, "13/8,511 → 7,608/8,511"


def test_rung_ratio_does_not_match_the_recorded_floor():
    """★★ **불일치를 덮지 않는다.** runG 보고 1.16× vs 계산 1.08×.

    1.16 을 얻으려면 분모가 0.2068 이어야 한다. 마스크가 W13→W14 로 바뀌면
    '마스크 밖' 영역 자체가 달라지므로 바닥값도 그 마스크로 다시 재야 한다 (D33).
    분모를 결과에 맞춰 고르는 것이 이 프로젝트가 여섯 번 물린 병이다 — 그래서
    한쪽을 조용히 고르지 않고 예외로 올린다.
    """
    rung = metrics.CANONICAL_PRESERVATION["runG"]
    assert round(rung.excess_ratio, 2) == 1.08
    with pytest.raises(metrics.BaselineMisapplied, match="0.2068"):
        rung.require_consistent_ratio()


def test_discarded_and_canonical_differ_by_a_factor_of_three():
    """★ 폐기값 3.48× vs 정본 1.16× — 무엇이 오염됐는지 크기로 보인다."""
    bad = metrics.DISCARDED_PRESERVATION["W13"]
    good = metrics.CANONICAL_PRESERVATION["runG"]
    naive = bad.outside_iou_complement / bad.floor.value    # 검사를 우회한 계산
    assert round(naive, 2) == 3.47 or round(naive, 2) == 3.48
    assert naive / good.excess_ratio > 2.9


def test_preservation_measurement_requires_a_reason_to_discard():
    with pytest.raises(ValueError, match="이유"):
        metrics.PreservationMeasurement(
            wave="X", outside_iou_complement=0.5,
            floor=metrics.DRAGON_C_OUTSIDE_FLOOR_W15, discarded=True,
        )


def test_w15_floor_is_the_same_asset_and_region():
    """분모는 재측정해도 같은 자산·같은 영역이어야 한다 (D33)."""
    f = metrics.DRAGON_C_OUTSIDE_FLOOR_W15
    f.require_same_asset("dragon-c", "global")
    assert abs(f.value - metrics.DRAGON_C_NOISE_FLOORS["global"].value) < 0.001
    with pytest.raises(metrics.BaselineMisapplied):
        f.require_same_asset("snowman")
