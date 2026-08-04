"""D9 좌표 프레임 규약 — 회귀 테스트.

이 파일의 목적은 하나다: **항등 변환을 쓰면 반드시 실패한다.**

`docs/PROGRESS.md` §2 D9 는 A5000 이 48개 부호付 순열을 전수 탐색해 확정했다
(정답 IoU 0.9365 vs 차점 0.1943, 4.8배). 그 사실 자체는 GPU 위에서 실자산으로
확인된 것이고, 여기서 다시 증명하지는 않는다.

여기가 막는 것은 **조용한 드리프트**다:
  · 누가 상수를 바꾼다
  · 누가 GLB 좌표를 변환 없이 복셀 격자에 넣는다
  · 누가 `frame="glb"` 를 정상 경로에 쓴다

셋 다 예외를 내지 않는다. 마스크는 여전히 "위쪽 35%" 를 잡고 지표도 숫자를 낸다 —
다만 그 숫자가 전부 다른 물체에 대한 것이다. 그래서 문서가 아니라 테스트로 둔다
(방법론 5조 4번: 규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다).
"""

from __future__ import annotations

import numpy as np
import pytest

from server.metrics import _codes, _iou
from server.pipeline import build_mask, occupancy_to_mesh, surface_voxelize
from server.pipeline.frames import (
    GLB_TO_VOXEL,
    IDENTITY,
    VOXEL_TO_GLB,
    AxisTransform,
    all_signed_permutations,
    assert_not_identity,
    to_voxel_frame,
)
from server.tests.fixtures import (
    asymmetric_asset_glb_frame,
    asymmetric_asset_voxel_frame,
)


def _iou_of(cells_a: np.ndarray, cells_b: np.ndarray) -> float:
    return _iou(_codes(cells_a), _codes(cells_b))


def _voxelize_with(transform: AxisTransform) -> np.ndarray:
    """GLB 프레임 자산을 주어진 변환으로 복셀화한다."""
    verts, faces = asymmetric_asset_glb_frame()
    return surface_voxelize(transform.apply(verts), faces)


# ══════════════════════════════════════════════════ 1. 상수 자체
def test_d9_constant_matches_progress_md():
    """D9 문구와 상수가 글자 그대로 같은지. 상수를 바꾸면 여기서 먼저 걸린다."""
    assert GLB_TO_VOXEL.perm == (0, 2, 1)
    assert GLB_TO_VOXEL.sign == (1, -1, 1)
    assert str(GLB_TO_VOXEL) == "(x, -z, y)"


def test_transform_roundtrip_is_exact():
    """순열·부호뿐이므로 왕복이 **정확히** 항등이다 (부동소수 오차 없음)."""
    pts = np.array([[0.1, -0.2, 0.3], [-0.5, 0.5, 0.0]], dtype=np.float64)
    assert np.array_equal(VOXEL_TO_GLB.apply(GLB_TO_VOXEL.apply(pts)), pts)
    assert np.array_equal(GLB_TO_VOXEL.apply(VOXEL_TO_GLB.apply(pts)), pts)


def test_transform_preserves_integer_dtype():
    """정수 복셀 좌표가 float 로 승격되면 하류에서 조용히 반올림이 생긴다."""
    cells = np.array([[1, 2, 3]], dtype=np.int64)
    assert GLB_TO_VOXEL.apply(cells).dtype == np.int64


def test_there_are_exactly_48_signed_permutations():
    perms = all_signed_permutations()
    assert len(perms) == 48
    assert len({(p.perm, p.sign) for p in perms}) == 48
    assert any(p.perm == GLB_TO_VOXEL.perm and p.sign == GLB_TO_VOXEL.sign
               for p in perms)


# ══════════════════════════════════ 2. ★ 항등이 실패하는 회귀 테스트
def test_identity_transform_is_rejected_explicitly():
    """`assert_not_identity` 가 항등을 막는다."""
    assert IDENTITY.is_identity
    assert not GLB_TO_VOXEL.is_identity
    assert_not_identity(GLB_TO_VOXEL, "정상 경로")
    with pytest.raises(ValueError, match="항등"):
        assert_not_identity(IDENTITY, "정상 경로")


def test_identity_transform_produces_a_different_object():
    """★★ 항등을 쓰면 **다른 물체**가 나온다. 예외는 안 난다 — 그게 위험한 이유다.

    이 테스트가 D9 의 핵심 방어다. 상수를 항등으로 바꾸면 여기서 죽는다.
    """
    reference = surface_voxelize(*asymmetric_asset_voxel_frame())

    correct = _iou_of(_voxelize_with(GLB_TO_VOXEL), reference)
    identity = _iou_of(_voxelize_with(IDENTITY), reference)

    assert correct > 0.95, f"D9 변환이 참조와 안 맞는다: IoU {correct:.4f}"
    assert identity < 0.5, f"항등이 참조와 맞아 버렸다: IoU {identity:.4f}"
    # A5000 실측은 0.9365 vs 0.1943 = 4.8배. 합성에서도 같은 자릿수의 격차가 난다.
    assert correct / max(identity, 1e-9) > 3.0


def test_d9_is_the_unique_best_of_all_48_permutations():
    """★ A5000 의 전수 탐색을 합성 픽스처로 재현한다.

    48개 중 **정답 하나만** 참조와 정확히 겹친다(IoU 1.0). 상수를 어떻게 바꿔도
    1위가 아니게 되므로 여기서 걸린다.

    ⚠️ 합성 격차는 실측보다 작다 — 1위 1.000 vs 2위 0.734(1.36배)이고, A5000 의
       실자산은 0.9365 vs 0.1943(4.8배)였다. 2위 세 개는 세로축을 맞게 잡고
       가로축만 뒤집은 순열이라, 물체가 좌우·앞뒤로 더 비대칭일수록 벌어진다.
       **이 테스트의 일은 D9 를 재증명하는 것이 아니라 드리프트를 막는 것이다** —
       근거는 A5000 의 실측이고, 여기서 픽스처를 더 비대칭으로 깎아 숫자를 키우는
       것은 대리 지표를 만드는 짓이다.
    """
    reference = surface_voxelize(*asymmetric_asset_voxel_frame())
    scored = sorted(
        ((_iou_of(_voxelize_with(t), reference), t) for t in all_signed_permutations()),
        key=lambda kv: kv[0],
        reverse=True,
    )
    best_iou, best = scored[0]
    runner_iou, runner = scored[1]

    assert (best.perm, best.sign) == (GLB_TO_VOXEL.perm, GLB_TO_VOXEL.sign), (
        f"전수 탐색 1위가 D9 상수가 아니다: {best} (IoU {best_iou:.4f})"
    )
    # 정답은 두 기술을 **정확히** 겹치게 한다 — 같은 물체를 두 프레임에서 쓴 것이니까.
    assert best_iou > 0.99, f"D9 상수인데 참조와 정확히 안 겹친다: {best_iou:.4f}"
    # 그리고 2위와 명확히 떨어져 있다 — 동점이면 탐색이 답을 고른 것이 아니다.
    assert best_iou - runner_iou > 0.2, (
        f"1위 {best_iou:.4f}({best}) 와 2위 {runner_iou:.4f}({runner}) 가 너무 가깝다"
    )


def test_to_voxel_frame_is_the_shared_entry_point():
    verts, _ = asymmetric_asset_glb_frame()
    assert np.array_equal(to_voxel_frame(verts), GLB_TO_VOXEL.apply(verts))


# ══════════════════════════════ 3. GLB 적재 경로가 실제로 통과하는가
def test_load_mesh_applies_the_frame(tmp_path):
    """GLB 파일을 실제로 써서 읽어 본다. 기본값이 voxel 프레임이어야 한다."""
    trimesh = pytest.importorskip("trimesh", reason="GLB 적재 경로 전용")
    from server.pipeline import load_mesh

    verts, faces = asymmetric_asset_glb_frame()
    path = tmp_path / "asset.glb"
    trimesh.Trimesh(vertices=verts, faces=faces, process=False).export(path)

    got_voxel, _ = load_mesh(str(path), frame="voxel")
    got_glb, _ = load_mesh(str(path), frame="glb")

    # 기본값은 voxel 프레임이다.
    assert np.allclose(load_mesh(str(path))[0], got_voxel)
    # 두 프레임이 실제로 다르다 — 변환이 no-op 이 아니다.
    assert not np.allclose(got_voxel, got_glb)
    assert np.allclose(got_voxel, GLB_TO_VOXEL.apply(got_glb))


def test_load_mesh_rejects_unknown_frame(tmp_path):
    pytest.importorskip("trimesh", reason="GLB 적재 경로 전용")
    from server.pipeline import load_mesh

    with pytest.raises(ValueError, match="frame"):
        load_mesh(str(tmp_path / "nope.glb"), frame="world")


# ══════════════════ 4. ★ 프레임이 틀리면 파이프라인이 통째로 무의미해진다
def test_wrong_frame_makes_the_head_mask_select_the_wrong_part():
    """★★ "조용히 전부 무의미해진다" 를 숫자로 보인다.

    머리 마스크는 복셀 격자의 **위쪽(+Z)** 을 잡는다. 프레임이 틀리면 그 자리에
    머리가 없다 — 몸통 옆구리가 잡힌다. 마스크는 정상 동작하고, 조립도 돌고,
    지표도 숫자를 낸다. 다만 전부 엉뚱한 부위에 대한 것이다.
    """
    head_bbox = ((-0.12, -0.12, 0.05), (0.12, 0.12, 0.26))
    mask = build_mask(bbox=head_bbox, halo=1)
    mask_codes = _codes(mask.cells)

    correct = _voxelize_with(GLB_TO_VOXEL)
    wrong = _voxelize_with(IDENTITY)

    def occupied_in_mask(cells):
        return np.intersect1d(_codes(cells), mask_codes, assume_unique=True).size

    n_correct = occupied_in_mask(correct)
    n_wrong = occupied_in_mask(wrong)

    # 올바른 프레임에서는 머리가 마스크 안에 실제로 있다.
    assert n_correct > 0
    # 항등에서는 같은 마스크가 전혀 다른 양의 기하를 집는다.
    assert n_wrong != n_correct
    # 그리고 두 결과의 겹침이 작다 — 다른 부위를 보고 있다는 뜻이다.
    both = _iou_of(correct, wrong)
    assert both < 0.5, f"두 프레임 결과가 너무 비슷하다 (IoU {both:.4f})"


def test_occupancy_roundtrip_is_frame_agnostic():
    """복셀 → 메시 → 복셀 왕복은 프레임을 바꾸지 않는다 (합성 디코더 자체 점검)."""
    cells = surface_voxelize(*asymmetric_asset_voxel_frame())
    again = surface_voxelize(*occupancy_to_mesh(cells))
    assert _iou_of(cells, again) > 0.99


# ══════════════════ 5. D9-b — 두 번째 좌표 함정 (방향이 반대다)
def test_to_glb_rotation_matrix_equals_voxel_to_glb():
    """★ `to_glb` 말미의 회전이 `VOXEL_TO_GLB` 와 **같은 변환**인지.

    TRELLIS 원문:  vertices @ [[1,0,0],[0,0,-1],[0,1,0]]
    이게 우리 상수와 다르면 D9 와 D9-b 중 하나가 틀린 것이다.
    """
    from server.pipeline.frames import TO_GLB_ROTATION, DECODER_NATIVE_TO_GLB

    pts = np.array(
        [[0.1, -0.2, 0.3], [-0.5, 0.5, 0.0], [1.0, 2.0, 3.0]], dtype=np.float64
    )
    assert np.array_equal(pts @ TO_GLB_ROTATION, DECODER_NATIVE_TO_GLB.apply(pts))
    assert np.array_equal(pts @ TO_GLB_ROTATION, VOXEL_TO_GLB.apply(pts))
    # (x, y, z) → (x, z, -y) 임을 직접 확인
    assert np.array_equal(pts @ TO_GLB_ROTATION, np.stack(
        [pts[:, 0], pts[:, 2], -pts[:, 1]], axis=1))


def test_decoder_native_is_already_the_voxel_frame():
    """★★ D9-b — 디코더 native 에서는 **항등이 정답이다.** D9 와 정반대다.

    `to_glb` 를 거치지 않은 메시(기하 전용 export·디버그 덤프)는 이미 z-up 이고
    그게 복셀 격자 프레임이다. 여기에 `GLB_TO_VOXEL` 을 걸면 틀린다.

    A5000 이 이 회전을 빠뜨렸다가 소스를 읽고 잡았다 — 놓쳤으면 잡음 바닥값이
    통째로 허수가 됐다.
    """
    from server.pipeline.frames import (
        DECODER_NATIVE_TO_VOXEL,
        decoder_native_to_voxel_frame,
    )

    assert DECODER_NATIVE_TO_VOXEL.is_identity, "D9-b 는 항등이어야 한다"

    native, faces = asymmetric_asset_voxel_frame()   # 디코더 native = 복셀 프레임
    reference = surface_voxelize(native, faces)

    # 올바른 처리: 아무것도 걸지 않는다.
    right = surface_voxelize(decoder_native_to_voxel_frame(native), faces)
    assert _iou_of(right, reference) == 1.0

    # 🔴 흔한 오류: D9 변환을 native 메시에 또 건다.
    wrong = surface_voxelize(GLB_TO_VOXEL.apply(native), faces)
    assert _iou_of(wrong, reference) < 0.5, (
        f"native 에 GLB_TO_VOXEL 을 걸었는데 참조와 맞아 버렸다 "
        f"(IoU {_iou_of(wrong, reference):.4f}) — 픽스처가 대칭이라 함정을 못 잡는다"
    )


def test_two_traps_point_in_opposite_directions():
    """★ D9 와 D9-b 를 한 자리에서 대조한다. 헷갈리면 여기를 보면 된다.

        D9    GLB 파일 메시    → 항등은 **오답** (IoU 낮음)
        D9-b  디코더 native   → 항등이 **정답** (IoU 1.0)
    """
    reference = surface_voxelize(*asymmetric_asset_voxel_frame())

    glb_verts, faces = asymmetric_asset_glb_frame()
    native_verts, _ = asymmetric_asset_voxel_frame()

    d9_identity = _iou_of(surface_voxelize(IDENTITY.apply(glb_verts), faces), reference)
    d9_correct = _iou_of(surface_voxelize(GLB_TO_VOXEL.apply(glb_verts), faces), reference)
    d9b_identity = _iou_of(surface_voxelize(IDENTITY.apply(native_verts), faces), reference)

    assert d9_identity < 0.5 < d9_correct      # D9: 항등이 오답
    assert d9b_identity == 1.0                 # D9-b: 항등이 정답


def test_glb_export_roundtrip_returns_to_the_voxel_frame(tmp_path):
    """★ 안전성: native → to_glb → 파일 → `load_mesh(frame="voxel")` = 원래 프레임.

    이 왕복이 항등이어야 "GLB 로 내보냈다가 다시 읽어도 같은 물체" 가 성립한다.
    깨지면 3090 이 DebugView 에 띄운 것과 우리가 계측한 것이 다른 물체가 된다.
    """
    trimesh = pytest.importorskip("trimesh", reason="GLB 왕복 전용")
    from server.pipeline import load_mesh
    from server.pipeline.frames import DECODER_NATIVE_TO_GLB

    native, faces = asymmetric_asset_voxel_frame()
    exported = DECODER_NATIVE_TO_GLB.apply(native)      # to_glb 가 하는 일

    path = tmp_path / "roundtrip.glb"
    trimesh.Trimesh(vertices=exported, faces=faces, process=False).export(path)

    back, _ = load_mesh(str(path), frame="voxel")
    assert np.allclose(back, native, atol=1e-6), "GLB 왕복이 프레임을 바꿨다"


# ══════════════════ 6. D28 — 세 번째 좌표 함정: 격자 정본
def test_slat_coords_is_the_canonical_grid():
    """★ D28 — 격자 정본은 `slat coords`(manifest)다. 우리 복셀화는 진단용이다."""
    from server.pipeline.frames import (
        SURFACE_VOXELIZATION_ROLE,
        VOXEL_GRID_SOURCE,
        assert_slat_grid,
    )

    assert VOXEL_GRID_SOURCE == "slat_coords"
    assert SURFACE_VOXELIZATION_ROLE == "diagnostic_only"
    assert_slat_grid(VOXEL_GRID_SOURCE, "마스크")


def test_non_canonical_grid_is_rejected():
    """★★ 자체 표면 복셀화로 만든 마스크를 그대로 쓰면 거부된다.

    축이 맞아도 인덱스가 밀린다 — 실측에서 같은 자산이 3090 z=45 / A5000 z=44 로
    한 칸 어긋났고, 그 한 칸이 "목 극소점 **위**" 와 "극소점 **에서**" 를 갈랐다.
    예외는 안 나고 결과만 달라지는 종류의 실패다.
    """
    from server.pipeline.frames import GridSourceMismatch, assert_slat_grid

    with pytest.raises(GridSourceMismatch, match="slat_coords"):
        assert_slat_grid("surface_voxelize", "head3mask")
    with pytest.raises(GridSourceMismatch):
        assert_slat_grid("", "마스크")


def test_three_coordinate_traps_are_all_encoded_here():
    """★ 좌표 함정이 셋이고 전부 이 모듈에 상수·함수로 있다.

    D9   축 순열      GLB 파일 → 항등은 오답
    D9-b to_glb 회전  디코더 native → 항등이 정답
    D28  격자 정본    slat coords 가 정본, 자체 복셀화는 진단용

    문서로만 적으면 안 지켜진다는 것이 이 프로젝트의 방법론 5조 4번이다.
    """
    from server.pipeline import frames

    assert frames.GLB_TO_VOXEL.perm == (0, 2, 1)                  # D9
    assert frames.DECODER_NATIVE_TO_VOXEL.is_identity             # D9-b
    assert frames.VOXEL_GRID_SOURCE == "slat_coords"              # D28
    for fn in ("assert_not_identity", "assert_slat_grid"):
        assert callable(getattr(frames, fn)), fn


# ══════════════ 7. D35 — 다섯 번째 함정: 좌우 대칭은 X 를 못 가린다
def test_mirror_x_is_an_involution():
    from server.pipeline.frames import mirror_x

    cells = np.array([[10, 5, 5], [11, 5, 5], [0, 0, 0], [63, 63, 63]], dtype=np.int64)
    assert np.array_equal(mirror_x(mirror_x(cells)), cells)


def test_symmetrize_x_makes_the_ambiguity_harmless():
    """★★ D35 해법 — 마스크를 X 대칭으로 만들면 X 반전이 마스크를 바꾸지 않는다.

    대칭 자산(dragon-c, 날개 둘)에서 전수 탐색 1·2위 격차가 **1.01배**였고
    2위가 같은 순열의 X 반전이었다. IoU 로는 원리적으로 못 가린다 —
    그래서 **고르지 않는다. 고를 필요가 없게 만든다.**
    """
    from server.pipeline.frames import is_x_symmetric, mirror_x, symmetrize_x

    asym = np.array([[10, 5, 5], [11, 5, 5]], dtype=np.int64)
    assert not is_x_symmetric(asym)

    sym = symmetrize_x(asym)
    assert is_x_symmetric(sym)
    # 핵심: X 반전을 걸어도 **같은 마스크**다. 그래서 모호성이 무해하다.
    assert np.array_equal(np.unique(mirror_x(sym), axis=0), np.unique(sym, axis=0))
    assert sym.shape[0] >= asym.shape[0], "대칭화는 마스크를 넓힌다 (그 대가는 감수한다)"


def test_a_centred_mask_is_already_symmetric():
    """D22② 의 "좌우로 머리 폭만큼 넓힌다" 를 따르면 자연히 대칭이 된다."""
    from server.pipeline.frames import is_x_symmetric
    from deltacontract.coords import VOXEL_RES, dense_cells

    c = VOXEL_RES // 2
    centred = dense_cells(np.array([c - 6, 20, 40]), np.array([c + 6, 24, 44]))
    assert is_x_symmetric(centred)


def test_x_mirror_of_a_symmetric_shape_is_indistinguishable():
    """🔴 함정 자체를 재현한다 — 대칭 형상은 X 반전과 IoU 가 같다.

    이게 전수 탐색이 dragon-c 에서 실패한 이유다. 검사가 무능한 게 아니라
    **정보가 없다.** 비대칭이 필요하면 fiducial 이 필요하다.
    """
    from server.pipeline.frames import mirror_x
    from deltacontract.coords import VOXEL_RES, dense_cells

    c = VOXEL_RES // 2
    sym = dense_cells(np.array([c - 8, 20, 40]), np.array([c + 8, 24, 44]))
    assert _iou_of(sym, mirror_x(sym)) == 1.0, "대칭 형상인데 IoU 가 1 이 아니다"

    asym = dense_cells(np.array([c + 2, 20, 40]), np.array([c + 18, 24, 44]))
    assert _iou_of(asym, mirror_x(asym)) < 1.0, "비대칭이면 구분이 된다"


# ══════════════════ 8. D35-a — X 대칭은 **우연히** 성립했다
def test_x_symmetry_was_accidental_in_w10():
    """★★ D35-a — 모듈은 `[xlo-w, xhi+w]` 를 클램프할 뿐 격자 중심 대칭을
    **강제하지 않는다.** W10 은 머리가 중앙에 있어 합이 63 이 됐을 뿐이다.
    """
    from deltacontract.coords import VOXEL_RES, dense_cells
    from server.pipeline.frames import assert_x_symmetric, is_x_symmetric, x_symmetry_cost

    c = VOXEL_RES // 2
    centred = dense_cells(np.array([c - 8, 20, 40]), np.array([c + 8, 24, 44]))
    assert is_x_symmetric(centred)
    assert x_symmetry_cost(centred) == 1.0        # 이미 대칭 — 넓어지지 않는다
    assert_x_symmetric(centred, "centred")

    # 자산이 치우치면 그대로 깨진다.
    offset = dense_cells(np.array([4, 20, 40]), np.array([20, 24, 44]))
    assert not is_x_symmetric(offset)
    assert x_symmetry_cost(offset) == pytest.approx(2.0)   # 반대편 허공까지 덮는다
    with pytest.raises(ValueError, match="D35-a"):
        assert_x_symmetric(offset, "head3mask")


def test_symmetry_cost_warns_before_widening():
    """대칭화 비용을 **미리** 재고 나서 쓸지 판단한다."""
    from deltacontract.coords import VOXEL_RES, dense_cells
    from server.pipeline.frames import symmetrize_x, x_symmetry_cost, is_x_symmetric

    offset = dense_cells(np.array([4, 20, 40]), np.array([20, 24, 44]))
    cost = x_symmetry_cost(offset)
    fixed = symmetrize_x(offset)
    assert is_x_symmetric(fixed)
    assert fixed.shape[0] == pytest.approx(offset.shape[0] * cost)
