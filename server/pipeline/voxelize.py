"""메시 ↔ 64³ occupancy.

두 방향을 모두 담는다:

    surface_voxelize()   메시 → VOXEL 셀 집합    (S2-1 의 "base 를 64³ 복셀화")
    occupancy_to_mesh()  VOXEL 셀 집합 → 메시    (합성 디코더)

`occupancy_to_mesh` 는 TRELLIS 메시 디코더의 **합성 대역(stand-in)** 이다. GPU 없이
전 구간을 관통시키기 위한 것이고, 실자산이 붙으면 이 자리에 진짜 디코더가 들어간다.

────────────────────────────────────────────────────────────────────────
🔴 왜 합성 디코더는 면을 컬링하지 않고, 왜 복셀 안쪽으로 들여쓰는가
────────────────────────────────────────────────────────────────────────
둘 다 **청크 배정을 복셀-국소로 만들기 위해서**다. 이유가 다르다.

1. 컬링 없음.
   이웃 복셀을 보고 면을 지우면, 한 복셀의 점유 변화가 이웃 복셀의 바이트를
   바꾼다. 그러면 "배치에서 유도한 부기" 가 실제 바이트 변화 집합보다 작아지고,
   마스크 밖 바이트 승계가 조용히 깨진다. 여기서는 각 복셀이 자기 6면을 항상
   그대로 내서 기하가 복셀별로 독립이 되게 한다.

2. 들여쓰기(`FACE_INSET`).
   `partition_mesh` 는 삼각형을 **무게중심이 속한 청크**에 배정한다. 복셀 경계에
   정확히 놓인 면은 무게중심이 이웃 청크의 시작점에 떨어져서, 청크 A 의 복셀이
   청크 B 의 바이트를 바꾼다. 면을 복셀 안쪽으로 조금 들여쓰면 무게중심이 복셀
   내부에 들어와 `normalized_to_chunk(centroid) == voxel_to_chunk(cell)` 이 보장된다.

⚠️ 진짜 TRELLIS 디코더는 이 두 성질을 **갖지 않는다.** sparse conv 의 receptive
   field 때문에 마스크 밖 latent 도 흔들린다. 그것이 halo(§8.2)가 존재하는 이유이고,
   실자산으로 갈아끼울 때 마스크 밖 보존은 **바이트가 아니라 기하 거리**로 재야 한다
   (`contract/python/deltacontract/assemble.py` 서두의 "해시 비교로 검증하지 마라").
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    NORMALIZED_MAX,
    NORMALIZED_MIN,
    NORMALIZED_SPAN,
    VOXEL_RES,
    assert_in_normalized_bounds,
    canonical_sort,
)

from .frames import GLB_TO_VOXEL

__all__ = [
    "FACE_INSET",
    "load_mesh",
    "normalize_to_normalized",
    "occupancy_to_mesh",
    "surface_voxelize",
    "voxelize_asset",
]

# 면을 복셀 안쪽으로 들여쓰는 비율 (복셀 한 변 기준). 모듈 docstring 2번.
# float32 저장 후에도 무게중심이 복셀 안에 남을 만큼 크고, 육안으로는 안 보일 만큼 작다.
FACE_INSET = 0.1


# ══════════════════════════════════════════════════════════════════ 적재
def load_mesh(path: str, *, frame: str = "voxel") -> Tuple[np.ndarray, np.ndarray]:
    """GLB/glTF/OBJ → (vertices (V,3) float64, faces (F,3) int64).

    🔴 **기본값이 `frame="voxel"` 이다.** GLB 는 Y-up, 복셀 격자는 Z-up 이므로
       읽은 좌표를 그대로 쓰면 안 된다 (D9). 이 함수가 GLB 를 읽는 유일한 경로이고,
       여기서 `frames.GLB_TO_VOXEL` 을 적용한다.

       `frame="glb"` 는 **진단·대조군 전용**이다. 이걸로 받은 좌표를 복셀 격자에
       넣으면 IoU 0.19 대가 나오고 예외는 안 난다.

    ⚠️ trimesh 는 **지연 import** 한다. 이 세션의 테스트는 합성 픽스처만 쓰므로
       trimesh 가 없는 환경에서도 나머지 파이프라인이 전부 돌아야 한다.
       실자산 적재는 3090 담당이다.

    스케일은 손대지 않는다 — NORMALIZED 로 옮기는 것은 `normalize_to_normalized`
    의 일이고, 그 둘을 한 함수에 섞으면 "이미 정규화된 자산" 을 두 번 정규화하는
    사고가 난다. 축 순열은 스케일이 아니므로 여기서 해도 그 문제가 없다.
    """
    if frame not in ("voxel", "glb"):
        raise ValueError(f"frame 은 'voxel'|'glb' 여야 한다: {frame!r}")
    try:
        import trimesh  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 환경 의존
        raise ImportError(
            "GLB/glTF 적재에는 trimesh 가 필요하다: pip install trimesh. "
            "합성 픽스처 경로는 trimesh 없이 돈다."
        ) from e

    obj = trimesh.load(path, force="mesh", process=False)
    verts = np.asarray(obj.vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(obj.faces, dtype=np.int64).reshape(-1, 3)
    if faces.size == 0:
        raise ValueError(f"삼각형이 없다: {path}")
    if frame == "voxel":
        verts = GLB_TO_VOXEL.apply(verts)
    return verts, faces


def normalize_to_normalized(vertices: np.ndarray) -> np.ndarray:
    """임의 스케일의 정점을 NORMALIZED [-0.5, 0.5]^3 로 옮긴다.

    긴 축을 기준으로 **등비 축소**한다. 축마다 다른 배율을 쓰면 형상이 찌그러지고,
    그 왜곡은 마스크 좌표까지 따라와서 "무엇을 쟀는지" 를 알 수 없게 만든다.
    """
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    if v.size == 0:
        raise ValueError("빈 정점 배열")
    lo, hi = v.min(axis=0), v.max(axis=0)
    center = (lo + hi) / 2.0
    extent = float(np.max(hi - lo))
    if extent <= 0:
        raise ValueError("자산의 크기가 0 이다")
    # 경계에 정확히 닿으면 normalized_to_voxel 의 클램프에 걸린다. 아주 살짝 줄인다.
    scale = (NORMALIZED_SPAN / extent) * (1.0 - 1e-9)
    return (v - center) * scale


# ══════════════════════════════════════════════════════════════ 표면 복셀화
def surface_voxelize(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    oversample: float = 2.0,
    max_batch_points: int = 4_000_000,
) -> np.ndarray:
    """NORMALIZED 메시의 **표면**을 VOXEL 셀 집합으로 만든다. (N,3) int64, canonical 순.

    부피를 채우지 않는다 — SLat 은 껍질 표현이고, 빈 내부에는 latent 이 없다
    (`docs/PROGRESS.md` §4 "solidify(부피 채움)" 실패 기록). 표면만 잡는 것이
    모델이 실제로 들고 있는 것과 같은 모양이다.

    삼각형마다 무게중심좌표 격자를 깔아 점을 뿌리고 그 점이 떨어진 셀을 모은다.
    격자 간격은 가장 긴 변을 기준으로 정하므로 결과는 **입력에만 의존**한다
    (난수·해시·순회순서 없음 → 결정적).
    """
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if f.size == 0:
        return np.zeros((0, 3), dtype=np.int64)
    if int(f.max()) >= v.shape[0]:
        raise ValueError("face 인덱스가 정점 배열 범위를 벗어난다.")
    assert_in_normalized_bounds(v, "voxelize 입력 정점")

    tri = v[f]  # (F,3,3)
    cell_size = NORMALIZED_SPAN / VOXEL_RES
    edges = np.linalg.norm(
        tri[:, [1, 2, 0], :] - tri[:, [0, 1, 2], :], axis=2
    )  # (F,3)
    longest = float(edges.max()) if edges.size else 0.0
    n = int(np.ceil(longest / cell_size * float(oversample)))
    n = max(1, min(n, 4 * VOXEL_RES))

    # 무게중심좌표 격자 (i+j+k == n)
    ii, jj = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
    keep = (ii + jj) <= n
    a = ii[keep] / n
    b = jj[keep] / n
    c = 1.0 - a - b
    bary = np.stack([a, b, c], axis=1)  # (P,3)

    out = []
    p = bary.shape[0]
    step = max(1, int(max_batch_points // max(1, p)))
    for start in range(0, tri.shape[0], step):
        chunk = tri[start : start + step]  # (B,3,3)
        pts = np.einsum("pk,bkd->bpd", bary, chunk).reshape(-1, 3)
        cells = np.floor((pts - NORMALIZED_MIN) / NORMALIZED_SPAN * VOXEL_RES)
        cells = np.clip(cells, 0, VOXEL_RES - 1).astype(np.int64)
        out.append(np.unique(cells, axis=0))

    if not out:
        return np.zeros((0, 3), dtype=np.int64)
    return canonical_sort(np.unique(np.concatenate(out, axis=0), axis=0))


def voxelize_asset(
    path: str, *, oversample: float = 2.0, frame: str = "voxel"
) -> np.ndarray:
    """GLB/glTF 파일 → VOXEL 셀 집합. 적재(D9 축 변환) → 정규화 → 표면 복셀화.

    `frame="glb"` 는 **대조군 전용**이다 — 그 결과를 정상 경로의 복셀과 비교하면
    D9 가 왜 필요한지가 숫자로 나온다 (`server/tests/test_frames.py`).
    """
    verts, faces = load_mesh(path, frame=frame)
    return surface_voxelize(
        normalize_to_normalized(verts), faces, oversample=oversample
    )


# ══════════════════════════════════════════════════════════ 합성 디코더
# 큐브 6면 · 면당 삼각형 2개. winding 은 바깥쪽(CCW).
_CUBE_CORNERS = np.array(
    [
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ],
    dtype=np.float64,
)
_CUBE_FACES = np.array(
    [
        [0, 3, 2], [0, 2, 1],   # -z
        [4, 5, 6], [4, 6, 7],   # +z
        [0, 1, 5], [0, 5, 4],   # -y
        [3, 7, 6], [3, 6, 2],   # +y
        [0, 4, 7], [0, 7, 3],   # -x
        [1, 2, 6], [1, 6, 5],   # +x
    ],
    dtype=np.int64,
)


def occupancy_to_mesh(
    cells: np.ndarray, *, inset: float = FACE_INSET
) -> Tuple[np.ndarray, np.ndarray]:
    """VOXEL 셀 집합 → NORMALIZED 메시 (vertices (V,3) float32, faces (F,3) int64).

    복셀 하나당 정육면체 하나. **이웃을 보지 않는다** (모듈 docstring 1번).
    면은 `inset` 만큼 복셀 안쪽으로 들어가 있어서, 모든 삼각형 무게중심이 자기
    복셀 안에 있다 (모듈 docstring 2번).

    정점은 복셀마다 8개씩 그대로 낸다 — 전역 중복 제거를 하지 않는 것은
    `chunkbin.canonicalize()` 가 청크 안에서 무손실 용접을 하기 때문이다.
    여기서 미리 합치면 청크 경계에서 두 청크가 같은 정점을 공유하게 되어
    "청크는 독립" 이라는 성질만 흐려진다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return (
            np.zeros((0, 3), dtype=np.float32),
            np.zeros((0, 3), dtype=np.int64),
        )
    if a.min() < 0 or a.max() >= VOXEL_RES:
        raise ValueError(
            f"셀이 [0,{VOXEL_RES}) 를 벗어난다: min={a.min()}, max={a.max()}"
        )
    if not (0.0 <= inset < 0.5):
        raise ValueError(f"inset 은 [0, 0.5) 여야 한다: {inset}")

    corners = _CUBE_CORNERS * (1.0 - 2.0 * inset) + inset  # (8,3)
    # (N,8,3) → NORMALIZED
    pts = a[:, None, :].astype(np.float64) + corners[None, :, :]
    verts = pts / VOXEL_RES * NORMALIZED_SPAN + NORMALIZED_MIN
    verts = verts.reshape(-1, 3).astype(np.float32)

    base = (np.arange(a.shape[0], dtype=np.int64) * 8)[:, None, None]
    faces = (base + _CUBE_FACES[None, :, :]).reshape(-1, 3)

    assert_in_normalized_bounds(verts, "occupancy_to_mesh 출력 정점")
    return verts, faces
