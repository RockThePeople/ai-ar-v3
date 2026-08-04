"""실자산 `.cbin` 세트 → 관통 1회. **파이프라인을 다시 구현하지 않는다.**

합성 픽스처(`server/tests/test_pipeline._run_pipeline`)와 **같은 함수들을 같은
순서로** 부른다. 다른 것은 입력이 구·육면체가 아니라 A5000 이 만든 실자산이라는
점뿐이다. 그래야 "합성에서는 되는데 실자산에서는 안 된다" 가 코드 차이가 아니라
데이터 차이로 좁혀진다.

────────────────────────────────────────────────────────────────────────
D9 / D9-b — 이 경로의 변환은 **항등이고, 그것이 정답이다**
────────────────────────────────────────────────────────────────────────
같은 파이프라인에 좌표 함정이 둘이고 정답이 서로 반대다:

    GLB 파일 → 복셀      `to_voxel_frame` (perm 0,2,1 / sign 1,-1,1).  항등이면 IoU 0.19
    디코더 native → 복셀  **항등**.  두 프레임이 같다 (D9-b)

`.cbin` 정점은 `to_glb` 를 거치지 않은 **디코더 native** 라 후자다. 그래서 이
모듈은 `decoder_native_to_voxel_frame()` 을 부른다 — 항등이지만 명시적으로.
생략하면 다음 세션이 여기에 GLB 용 변환을 잘못 건다.

(`voxelize.load_mesh`/`voxelize_asset` 은 GLB 용이라 여기서 쓰지 않는다.
 실자산이 GLB 로 들어오는 날에는 그 변환을 `pipeline/frames.py` 의
 `GLB_TO_VOXEL` 이 맡는다 — rev6 에서 편입됐고 `load_mesh()` 가 기본값으로
 그 변환을 적용한다. **직접 축을 만지지 마라**: 항등 변환을 쓰면 예외 없이
 IoU 0.19 대가 나오고, 지표는 전부 다른 물체에 대해 숫자를 낸다.)

────────────────────────────────────────────────────────────────────────
D11 (rev7) — 크기는 크롭이 아니라 **재복셀화**로 맞춘다
────────────────────────────────────────────────────────────────────────
base·donor 가 **각자 독립적으로** NORMALIZED 격자를 꽉 채우게 정규화된다
(W3 실측: base span 49×50×64, donor 60×60×64). 그래서 기증자를 그대로 두면
`crop ≤ 0.30` 에서만 마스크에 들어갔고, 화면에는 호박의 위쪽 뚜껑만 떴다 —
삼각눈도 톱니입도 잘려 나갔다. 지표는 통과하는데 "호박 머리" 로는 안 보였다.

`fit_donor_to_mask` 가 기증자 **메시**를 마스크 범위에 맞춰 새 cell_size 로
다시 래스터화한다. 희소 좌표를 곱하는 것이 아니므로 계약이 금지한 스케일이
아니고, 호박 전체가 들어가므로 얼굴이 살아남는다. 그래서 이 모듈은 이제
`.cbin` 에서 점유뿐 아니라 **메시**(`cbin_dir_to_mesh`)도 꺼낸다.

────────────────────────────────────────────────────────────────────────
마스크를 bbox 상수로 주지 않는다
────────────────────────────────────────────────────────────────────────
합성 픽스처는 머리 위치를 아니까 `HEAD_BBOX` 상수를 쓸 수 있었다. 실자산은
모른다. 그래서 `top_region_cells(occupancy, fraction)` 를 쓴다 — **자산의 실제
점유 구간** 기준 위쪽 일부다. 격자 전체 기준으로 자르면 자산이 치우쳤을 때
아무것도 안 잡히거나 전부 잡힌다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from deltacontract.chunkbin import blob_hash, decode  # type: ignore[import-not-found]
from deltacontract.partition import partition_mesh  # type: ignore[import-not-found]

from . import metrics
from .pipeline import (
    build_mask,
    derive_bookkeeping,
    encode_chunks,
    occupancy_to_mesh,
    package_delta,
    splice,
    surface_voxelize,
    top_region_cells,
)
from .pipeline.frames import decoder_native_to_voxel_frame
from .pipeline.delta import audit_against_bytes
from .pipeline.splice import fit_donor_to_mask

__all__ = ["cbin_dir_to_mesh", "cbin_dir_to_occupancy", "run_real_walkthrough"]

HEAD_FRACTION = 0.35   # 자산 세로 점유의 위 35% = "머리"

# D11(rev7) 이후 **1.0** 이다. 크기는 `fit_donor_to_mask` 의 재복셀화가 맞추므로
# 크롭은 더 이상 크기 조절 수단이 아니다. W3 의 0.30 은 호박 위쪽 뚜껑만 남겨
# 얼굴을 잘라 버렸다 — 그게 D11 이 신설된 이유다.
DONOR_CROP_FRACTION = 1.0
HALO = 1


def cbin_dir_to_mesh(chunk_dir: Path) -> Tuple[np.ndarray, np.ndarray]:
    """`.cbin` 디렉터리 → 이어붙인 (vertices, faces).

    청크의 `POSITION`/`INDEX` 를 정점 오프셋만 더해 잇는다. 좌표는 이미 계약의
    NORMALIZED 공간(Z-up)이라 **정규화도 회전도 하지 않는다** (D9).

    ★ D11 이후 기증자는 이 **메시** 가 필요하다. 점유 셀만 들고 있으면 다시
      복셀화할 원본이 없어서 크롭 말고는 크기를 맞출 방법이 없다.
    """
    files = sorted(Path(chunk_dir).glob("*.cbin"))
    if not files:
        raise FileNotFoundError(f".cbin 이 없다: {chunk_dir}")

    vert_parts: List[np.ndarray] = []
    face_parts: List[np.ndarray] = []
    offset = 0
    for f in files:
        mesh = decode(f.read_bytes())
        v = np.asarray(mesh.positions, dtype=np.float64)
        idx = np.asarray(mesh.indices, dtype=np.int64).reshape(-1, 3)
        vert_parts.append(v)
        face_parts.append(idx + offset)
        offset += v.shape[0]

    verts = np.concatenate(vert_parts, axis=0)
    # 🔴 D9-b — `.cbin` 정점은 **디코더 native(z-up)** 이고 그것이 곧 복셀 격자
    #    프레임이라 이 변환은 **항등**이다. 그래도 명시적으로 부른다: 항등이라
    #    생략하면 다음 세션이 여기에 `to_voxel_frame`(GLB용)을 잘못 건다.
    #    A5000 은 기하 전용 export 에서 `to_glb` 회전을 빠뜨려 이 함정에 빠졌고,
    #    놓쳤으면 잡음 바닥값이 통째로 허수가 될 뻔했다.
    verts = decoder_native_to_voxel_frame(verts)
    return verts, np.concatenate(face_parts, axis=0)


def cbin_dir_to_occupancy(chunk_dir: Path, *, oversample: float = 2.0) -> np.ndarray:
    """`.cbin` 디렉터리 → VOXEL 점유 셀 (N,3)."""
    verts, faces = cbin_dir_to_mesh(chunk_dir)
    return surface_voxelize(verts, faces, oversample=oversample)


def _chunks_of(cells: np.ndarray) -> Dict[str, bytes]:
    """점유 → {chunk_key: .cbin 바이트}. 합성 픽스처 경로와 **같은 함수들**이다."""
    verts, faces = occupancy_to_mesh(cells)
    meshes = partition_mesh(verts, faces, voxel_cells=cells)
    return encode_chunks(meshes)


def run_real_walkthrough(
    base_chunks: Path,
    donor_chunks: Path,
    *,
    head_fraction: float = HEAD_FRACTION,
    crop_fraction: float = DONOR_CROP_FRACTION,
    halo: int = HALO,
    noise_floor: Optional[float] = None,
) -> dict:
    """실자산 관통 1회. 반환 모양은 `_run_pipeline()` 과 **같다** — DebugView 가
    합성/실자산을 구분하지 않고 같은 렌더러로 그릴 수 있어야 하기 때문이다.
    """
    base_cells = cbin_dir_to_occupancy(base_chunks)
    donor_verts, donor_faces = cbin_dir_to_mesh(donor_chunks)

    # per_slice=True — 마스크를 자산 단면에 맞춘 계단 모양으로 딴다 (D11 부수 결정).
    # False 면 머리 위 허공까지 마스크가 되고, W3 실측에서 격자의 21% 였다.
    mask = build_mask(
        cells=top_region_cells(base_cells, head_fraction, per_slice=True), halo=halo
    )

    # 🔴 D11 — 크롭이 아니라 **재복셀화**로 크기를 맞춘다. 희소 좌표를 곱하는 것이
    #    아니라 연속 메시를 새 cell_size 로 래스터화하는 것이라, 계약이 금지한
    #    스케일이 아니다. 이제 호박 전체가 들어가므로 얼굴이 살아남는다.
    donor_cells, used_fill = fit_donor_to_mask(donor_verts, donor_faces, mask)

    sp = splice(
        base_cells,
        donor_cells,
        mask,
        # D11 이후 크롭은 크기 조절 수단이 아니다. 1.0 = 기증자 전체를 쓴다.
        crop_fraction=crop_fraction,
        # 🔴 D13 — 끄지 마라. W3 에서 끄고 돌렸다가 보존이 조용히 무너졌다
        #    (preservation_iou_out 0.345 / 절감 14.05%). 마스크 밖으로 나간 기증자
        #    셀은 보존(B)을 **직접** 깨는데, 예외가 안 나서 숫자만 보면 원인을
        #    알 수 없다. 켜면 못 들어갈 때 조립 단계에서 즉시 터진다.
        strict_containment=True,
    )

    parent_blobs = _chunks_of(base_cells)
    child_blobs = _chunks_of(sp.cells)

    bk = derive_bookkeeping(sp, child_blobs.keys())
    audit_against_bytes(
        bk,
        {k: blob_hash(v) for k, v in parent_blobs.items()},
        {k: blob_hash(v) for k, v in child_blobs.items()},
    )
    pkg = package_delta(parent_blobs, child_blobs, bk, mask=mask, job_id="realasset")

    report = metrics.evaluate(
        before=base_cells,
        after=sp.cells,
        mask_cells=mask.cells,             # 효능 — 사용자가 지정한 원본 마스크
        edited_region_cells=mask.dilated,  # 보존 — halo 까지 팽창시킨 편집 영역
        parent_blobs=parent_blobs,
        child_blobs=pkg.blobs,
        book=bk.book,
        full_bytes=pkg.full_bytes,
        delta_bytes=pkg.delta_bytes,
        # D13 — splice 에서 실제로 강제했다는 사실을 지표에 넘긴다. 여기서
        # True 를 적어 놓고 splice 에서 끄면 그게 곧 거짓말이 되므로 둘을 같이 본다.
        containment_enforced=True,
        # ★ 실자산의 잡음 바닥값은 **아직 없다** (D5-b). W3-A5000 이 편집 없이
        #   인코드→디코드만 왕복시켜 측정 중이다. None 을 넘기면 계측은 되고
        #   **판정만 거부된다** — 추정값을 넣어 통과시키면 "잡음인지 누출인지 못
        #   가른 채 보존됨이라고 적는" 것이 된다. 바닥값이 오면 여기만 채운다.
        noise_floor=noise_floor,
    )
    return {
        "base": base_cells, "donor": donor_cells, "mask": mask, "splice": sp,
        "parent": parent_blobs, "child": child_blobs, "bk": bk, "pkg": pkg,
        "report": report, "used_fill": used_fill,
    }
