"""
결정적 합성 메시. GPU 없이 세 세션 어디서나 같은 입력을 만들 수 있어야 하므로
난수는 고정 시드 PCG 로만 쓰고, 부동소수 연산은 float64 로 계산한 뒤 float32 로 내린다.

이 픽스처의 역할은 "예쁜 3D"가 아니라 **청크 경계를 확실히 가로지르는 형상**이다:
경계에 걸친 삼각형이 없으면 분할 규칙의 버그가 드러나지 않는다.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np


def torus(
    n_major: int = 48,
    n_minor: int = 24,
    r_major: float = 0.30,
    r_minor: float = 0.11,
    seed: int = 20260729,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """토러스. NORMALIZED [-0.5,0.5] 안에 들어가고 여러 청크를 가로지른다.

    Returns: (vertices (V,3) float32, faces (F,3) int64, vertex_attrs (V,6) float32)
             vertex_attrs 는 TRELLIS 관례대로 [albedo RGB, normal XYZ].
    """
    u = np.linspace(0.0, 2.0 * np.pi, n_major, endpoint=False, dtype=np.float64)
    v = np.linspace(0.0, 2.0 * np.pi, n_minor, endpoint=False, dtype=np.float64)
    uu, vv = np.meshgrid(u, v, indexing="ij")

    cx = (r_major + r_minor * np.cos(vv)) * np.cos(uu)
    cy = (r_major + r_minor * np.cos(vv)) * np.sin(uu)
    cz = r_minor * np.sin(vv)
    verts = np.stack([cx, cy, cz], axis=-1).reshape(-1, 3)

    # 법선: 튜브 중심에서 표면으로 향하는 방향
    nx = np.cos(vv) * np.cos(uu)
    ny = np.cos(vv) * np.sin(uu)
    nz = np.sin(vv)
    normals = np.stack([nx, ny, nz], axis=-1).reshape(-1, 3)

    idx = np.arange(n_major * n_minor).reshape(n_major, n_minor)
    i0 = idx
    i1 = np.roll(idx, -1, axis=0)
    i2 = np.roll(idx, -1, axis=1)
    i3 = np.roll(np.roll(idx, -1, axis=0), -1, axis=1)
    faces = np.concatenate(
        [
            np.stack([i0, i1, i3], axis=-1).reshape(-1, 3),
            np.stack([i0, i3, i2], axis=-1).reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.int64)

    rng = np.random.default_rng(seed)
    albedo = rng.random((verts.shape[0], 3))

    # ★ TRELLIS 실제 출력을 흉내낸다: 뒤 3채널은 [0,1] 로 인코딩된 법선이다
    #   (A5000 실측 — 6채널 전부 min 이 양수, 단위벡터 비율 0.00%).
    #   픽스처가 [-1,1] 단위벡터를 내놓으면 `split_trellis_vertex_attrs` 의 기본
    #   경로를 테스트하지 못하고, 실제로 빠졌던 그 버그를 그대로 놓친다.
    normals_encoded = (normals + 1.0) * 0.5

    attrs = np.concatenate([albedo, normals_encoded], axis=1).astype(np.float32)
    return verts.astype(np.float32), faces, attrs


def voxel_cells_from_mesh(vertices: np.ndarray) -> np.ndarray:
    """정점 위치로부터 대략의 VOXEL 셀 집합. voxel_count 계측 경로 검증용."""
    from deltacontract.coords import canonical_sort, normalized_to_voxel

    cells = normalized_to_voxel(np.asarray(vertices))
    return canonical_sort(np.unique(cells, axis=0))


def apply_local_edit(
    vertices: np.ndarray,
    faces: np.ndarray,
    attrs: np.ndarray,
    *,
    bbox_min=(0.15, -0.12, -0.50),
    bbox_max=(0.50, 0.12, 0.50),
    bulge: float = 0.035,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """국소 편집 시뮬레이션 — bbox 안의 정점만 바깥으로 밀어낸다.

    RePaint 를 흉내내는 게 아니라, **"마스크 밖은 비트 단위로 안 건드렸다"는
    이상적인 조건**을 만드는 것이 목적이다. 이 조건에서조차 다른 청크의 해시가
    바뀐다면 그건 모델 문제가 아니라 계약(정규화/분할) 버그다.
    """
    v = np.array(vertices, dtype=np.float32, copy=True)
    lo = np.asarray(bbox_min, dtype=np.float32)
    hi = np.asarray(bbox_max, dtype=np.float32)
    inside = np.all((v >= lo) & (v <= hi), axis=1)

    # attrs 의 뒤 3채널은 [0,1] 인코딩이므로 방향으로 쓰려면 복원해야 한다.
    n = np.asarray(attrs[:, 3:6], dtype=np.float32) * 2.0 - 1.0
    v[inside] = (v[inside].astype(np.float64) + n[inside].astype(np.float64) * bulge).astype(np.float32)
    return v, np.array(faces, copy=True), np.array(attrs, copy=True)
