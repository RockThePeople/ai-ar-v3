"""실자산 `.cbin` 세트 → 관통 1회. **파이프라인을 다시 구현하지 않는다.**

합성 픽스처(`server/tests/test_pipeline._run_pipeline`)와 **같은 함수들을 같은
순서로** 부른다. 다른 것은 입력이 구·육면체가 아니라 A5000 이 만든 실자산이라는
점뿐이다. 그래야 "합성에서는 되는데 실자산에서는 안 된다" 가 코드 차이가 아니라
데이터 차이로 좁혀진다.

────────────────────────────────────────────────────────────────────────
D9 — 여기서 좌표계를 변환하지 않는다
────────────────────────────────────────────────────────────────────────
GLB 는 Y-up, 복셀 격자는 Z-up 이라 그 사이를 손으로 오가면 조용히 틀린다.
**이 모듈은 GLB 를 건드리지 않는다.** `.cbin` 의 `positions` 는 이미 계약의
NORMALIZED 공간(Z-up)이고, 그대로 `surface_voxelize` 에 넣는다. 회전도 축 교환도
없다 — 할 필요가 없는 경로를 골랐다.

(`voxelize.load_mesh`/`voxelize_asset` 은 GLB 용이라 여기서 쓰지 않는다.
 실자산이 GLB 로 들어오는 날에는 그 변환을 `pipeline/frames.py` 의
 `GLB_TO_VOXEL` 이 맡는다 — rev6 에서 편입됐고 `load_mesh()` 가 기본값으로
 그 변환을 적용한다. **직접 축을 만지지 마라**: 항등 변환을 쓰면 예외 없이
 IoU 0.19 대가 나오고, 지표는 전부 다른 물체에 대해 숫자를 낸다.)

────────────────────────────────────────────────────────────────────────
⚠️ D11 미해결 — 지금 이 경로를 돌리면 기증자는 "잘린 호박" 이다
────────────────────────────────────────────────────────────────────────
base·donor 가 **각자 독립적으로** NORMALIZED 격자를 꽉 채우게 정규화된다
(W3 실측: base span 49×50×64, donor 60×60×64, 머리 마스크 span 49×50×23).
스케일은 계약이 금지하므로(6-이웃 유지율 s=2.0 → 0%) 크롭으로만 맞추는데,
실측 스윕 결과 crop ≤ 0.30 에서만 마스크에 들어간다. 그 크롭은 호박의 **위쪽
30%(뚜껑+꼭지)** 만 남기고 삼각눈·톱니입을 잘라 버린다.

⇒ 지표는 통과해도 화면에는 "호박 머리" 가 아니라 "호박 뚜껑" 이 뜬다.
   **D11 이 닫히기 전까지 재관통을 돌리지 마라** — 돌려도 같은 그림이다.
   맥북이 기증자 크기를 고치면 이 파일은 손댈 것이 없다(파라미터만 바뀐다).

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
from .pipeline.delta import audit_against_bytes

__all__ = ["cbin_dir_to_occupancy", "run_real_walkthrough"]

HEAD_FRACTION = 0.35   # 자산 세로 점유의 위 35% = "머리"

# 🔴 실측으로 정한 값이다. 합성 픽스처의 0.85 를 그대로 쓰면 기증자가 마스크에 안
#    들어가고 strict_containment 가 조립을 거부한다 (W3 스윕: 1.0/0.8/0.6/0.5/0.4
#    전부 실패, 0.30 에서 처음 들어감). ⚠️ 이 값은 **D11 이 닫히면 바뀐다** —
#    기증자를 애초에 작게 만들면 크롭을 이렇게까지 조일 이유가 없다.
DONOR_CROP_FRACTION = 0.30
HALO = 1


def cbin_dir_to_occupancy(chunk_dir: Path, *, oversample: float = 2.0) -> np.ndarray:
    """`.cbin` 디렉터리 → VOXEL 점유 셀 (N,3).

    청크를 전부 디코딩해 하나의 메시로 잇고 표면 복셀화한다. 정점 좌표는 이미
    NORMALIZED 라 **정규화도 회전도 하지 않는다** (D9).
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
    faces = np.concatenate(face_parts, axis=0)
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
    donor_cells = cbin_dir_to_occupancy(donor_chunks)

    mask = build_mask(cells=top_region_cells(base_cells, head_fraction), halo=halo)

    sp = splice(
        base_cells,
        donor_cells,
        mask,
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
        # ★ 실자산의 잡음 바닥값은 **아직 없다** (D5-b). W3-A5000 이 편집 없이
        #   인코드→디코드만 왕복시켜 측정 중이다. None 을 넘기면 계측은 되고
        #   **판정만 거부된다** — 추정값을 넣어 통과시키면 "잡음인지 누출인지 못
        #   가른 채 보존됨이라고 적는" 것이 된다. 바닥값이 오면 여기만 채운다.
        noise_floor=noise_floor,
    )
    return {
        "base": base_cells, "donor": donor_cells, "mask": mask, "splice": sp,
        "parent": parent_blobs, "child": child_blobs, "bk": bk, "pkg": pkg,
        "report": report,
    }
