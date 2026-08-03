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

# 🔴 잡음 바닥값 (D5-b). **이 값은 실자산에 쓰면 안 된다.**
#
# 합성 경로에서 0.0 인 것은 추정이 아니라 **유도된 사실**이다: 부기 밖 청크는
# 재디코딩되지 않고 부모 바이트를 그대로 승계하므로(package.py), 재디코딩 잡음이
# 존재할 자리가 없다. 즉 "재디코딩 영역" 이 공집합이다.
#
# 실자산의 바닥값은 **편집 없이 인코드→디코드만 왕복시킨 대조군**에서 나오고,
# 그것은 W3-A5000 에 배정돼 있다. 그 전에는 어떤 값도 가정하지 않는다 —
# `test_preservation_refuses_to_judge_without_noise_floor` 가 그걸 강제한다.
SYNTHETIC_NOISE_FLOOR = 0.0


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
        mask_cells=mask.cells,              # 효능 — 사용자가 지정한 원본 마스크
        edited_region_cells=mask.dilated,   # 보존 — halo 까지 팽창시킨 편집 영역
        parent_blobs=parent_blobs,
        child_blobs=pkg.blobs,              # 승계가 적용된 **최종** 세트
        book=bk.book,
        full_bytes=pkg.full_bytes,
        delta_bytes=pkg.delta_bytes,
        noise_floor=SYNTHETIC_NOISE_FLOOR,
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


def test_largest_component_penalizes_the_assemble_path(run):
    """🔴 **W3 발견.** rev6 G2 의 "최대 연결성분 / 신규 ≥ 0.8" 이 assemble 을 떨어뜨린다.

    이 테스트는 통과를 주장하지 않는다 — **실측을 기록한다.** 픽스처를 깎아
    0.8 을 넘기는 것은 대리 지표를 만드는 짓이라 하지 않았다.

    원인은 구조적이다. 기증자 껍질이 옛 껍질과 교차하고, 교차한 셀은 before 에도
    있으므로 "신규" 가 아니다. 그 링이 빠지면서 **한 덩어리인 껍질이 갈라진다.**

        배치된 기증자 껍질의 최대성분비   1.000   ← 실제로는 한 덩어리
        결과 점유 전체의 최대성분비       1.000
        신규 복셀만의 최대성분비          0.731   ← G2 지표. 0.8 미달

    A5000 의 273/274 = 0.996 은 **가산** 편집(빈 공간에 주둥이)이라 이 현상이 없다.
    즉 이 문턱은 VoxHammer 경로에 맞춰져 있고 assemble 경로에는 안 맞는다.
    D5 가 고친 것과 같은 종류의 병이다 — 판정 기준 변경은 Chat 의 몫이다.
    """
    r = run["report"]
    sp = run["splice"]

    gate_metric = r.efficacy_largest_component
    diagnostic = r.reference["efficacy_largest_component_of_result"]
    donor_blob = metrics._largest_component_size(sp.donor_placed) / len(sp.donor_placed)

    assert 0.7 < gate_metric < 0.8, f"실측이 바뀌었다: {gate_metric:.3f}"
    assert diagnostic == 1.0, "결과 점유는 한 덩어리여야 한다"
    assert donor_blob == 1.0, "배치된 기증자는 한 덩어리여야 한다"

    # 지표가 잡음을 걸러내는 능력 자체는 살아 있다 (아래 음성 대조가 그걸 본다).
    assert gate_metric > 0.5, "그렇다고 잡음 수준까지 떨어지지는 않는다"


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
        noise_floor=None,
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

    보존·절감은 성립한다. 효능은 **연결성분 조건 하나 때문에** 떨어진다 —
    위 `test_largest_component_penalizes_the_assemble_path` 가 그 이유다.
    이걸 통과로 적으면 G2 를 잘못 닫는 것이므로 실측대로 둔다.
    """
    r = run["report"]
    gate = r.gate_g2()

    assert gate["preservation"] is True
    assert gate["saving"] is True

    # 효능의 세 숫자 조건 중 둘은 성립하고 하나가 안 된다.
    assert r.delta_in_mask.new > 0
    assert r.churn_ratio >= 3.0
    assert r.efficacy_largest_component < 0.8
    assert gate["efficacy_numeric"] is False


def test_gate_g2_visual_confirmation_semantics():
    """★ 원칙 7 — 육안 산출물이 없으면 미검증이다.

    게이트 **로직**만 본다 (픽스처 수치와 무관하게). 숫자가 다 맞아도
    `visual_confirmed` 가 없으면 효능은 **미결(None)** 이다 — 통과도 실패도 아니다.
    코드가 만들 수 없는 사실을 코드가 통과시키지 않는다.
    """
    passing = metrics.MetricReport(
        delta_in_mask=metrics.VoxelDelta(new=500, removed=400),
        delta_outside=metrics.VoxelDelta(new=0, removed=0),
        efficacy_largest_component=0.99,
        churn_ratio=5.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(distance=0.0, baseline=0.0),
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
        efficacy_largest_component=0.0,
        churn_ratio=0.0,
        inherited_byte_identity=1.0,
        preservation=metrics.PreservationDistance(distance=0.0, baseline=0.0),
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
        noise_floor=SYNTHETIC_NOISE_FLOOR,
    )

    # 보존·절감은 만점으로 통과한다.
    assert report.inherited_byte_identity == 1.0
    assert report.preservation.distance == 0.0
    assert report.preservation.passes is True
    assert report.transfer_saving == 1.0

    # 효능은 반드시 떨어진다 — 신규 복셀도, 연결성분도, churn 비도 전부.
    assert report.delta_in_mask.new == 0
    assert report.delta_in_mask.removed == 0
    assert report.efficacy_largest_component == 0.0
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
