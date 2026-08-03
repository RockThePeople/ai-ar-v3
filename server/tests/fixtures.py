"""합성 픽스처 — 구 · 육면체. **파일도 GPU 도 네트워크도 쓰지 않는다.**

S2 의 실자산(눈사람 · 호박)은 3090 이 확보한다. 맥북 세션이 그것을 기다리면 웨이브가
직렬화되므로, 같은 **형상 역할**을 하는 합성 자산으로 전 구간을 먼저 관통시킨다.

    눈사람(base)  ← 구 2개 (몸통 + 머리).  머리가 전체의 작은 부분이어야
                    "국소 편집" 과 "전송 절감" 이 둘 다 의미를 갖는다
    호박(donor)   ← 육면체.  구가 아니어야 마스크 안에 **없던 복셀**이 생긴다

육면체를 고른 이유는 모양이 예뻐서가 아니라, 구 표면과 겹치지 않는 셀이 반드시
생기기 때문이다. 기증자가 base 와 비슷하면 신규 복셀이 0 에 가까워지고, 그러면
효능 테스트가 통과해도 무엇을 증명했는지 알 수 없다.
"""

from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np

__all__ = ["cube_mesh", "snowman_mesh", "sphere_mesh"]


def sphere_mesh(
    center: Sequence[float] = (0.0, 0.0, 0.0),
    radius: float = 0.3,
    n_lat: int = 32,
    n_lon: int = 64,
) -> Tuple[np.ndarray, np.ndarray]:
    """UV 구. NORMALIZED 공간 (vertices (V,3), faces (F,3))."""
    c = np.asarray(center, dtype=np.float64)
    lat = np.linspace(0.0, np.pi, n_lat + 1)
    lon = np.linspace(0.0, 2.0 * np.pi, n_lon, endpoint=False)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    x = np.sin(la) * np.cos(lo)
    y = np.sin(la) * np.sin(lo)
    z = np.cos(la)
    verts = (np.stack([x, y, z], axis=-1).reshape(-1, 3) * radius) + c

    faces = []
    for i in range(n_lat):
        for j in range(n_lon):
            a = i * n_lon + j
            b = i * n_lon + (j + 1) % n_lon
            d = (i + 1) * n_lon + j
            e = (i + 1) * n_lon + (j + 1) % n_lon
            faces.append([a, d, e])
            faces.append([a, e, b])
    return verts, np.asarray(faces, dtype=np.int64)


def cube_mesh(
    center: Sequence[float] = (0.0, 0.0, 0.0), size: float = 0.3
) -> Tuple[np.ndarray, np.ndarray]:
    """축정렬 정육면체. 삼각형 12개."""
    c = np.asarray(center, dtype=np.float64)
    h = size / 2.0
    verts = np.array(
        [
            [-h, -h, -h], [h, -h, -h], [h, h, -h], [-h, h, -h],
            [-h, -h, h], [h, -h, h], [h, h, h], [-h, h, h],
        ],
        dtype=np.float64,
    ) + c
    faces = np.array(
        [
            [0, 3, 2], [0, 2, 1],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [3, 7, 6], [3, 6, 2],
            [0, 4, 7], [0, 7, 3],
            [1, 2, 6], [1, 6, 5],
        ],
        dtype=np.int64,
    )
    return verts, faces


def snowman_mesh(
    body_center: Sequence[float] = (0.0, 0.0, -0.16),
    body_radius: float = 0.22,
    head_center: Sequence[float] = (0.0, 0.0, 0.20),
    head_radius: float = 0.13,
) -> Tuple[np.ndarray, np.ndarray]:
    """구 2개를 이어 붙인 눈사람. 두 메시를 정점 오프셋으로 합칠 뿐 불리언 연산은 없다.

    내부에 잠긴 면이 남지만 상관없다 — 표면 복셀화는 점유만 보고, 잠긴 면이 만드는
    셀은 몸통·머리 표면 셀의 부분집합이다.
    """
    bv, bf = sphere_mesh(body_center, body_radius)
    hv, hf = sphere_mesh(head_center, head_radius)
    verts = np.concatenate([bv, hv], axis=0)
    faces = np.concatenate([bf, hf + bv.shape[0]], axis=0)
    return verts, faces
