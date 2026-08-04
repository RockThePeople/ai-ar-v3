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

__all__ = [
    "asymmetric_asset_glb_frame",
    "asymmetric_asset_voxel_frame",
    "box_mesh",
    "cube_mesh",
    "donor_mesh",
    "snowman_mesh",
    "sphere_mesh",
]


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


def box_mesh(
    center: Sequence[float] = (0.0, 0.0, 0.0),
    size: Sequence[float] = (0.3, 0.3, 0.3),
) -> Tuple[np.ndarray, np.ndarray]:
    """축정렬 직육면체. 축마다 다른 크기를 준다.

    D9 전수 탐색 픽스처에 필요하다 — 세 축의 **크기가 전부 달라야** 축을 바꾼
    순열이 원본과 안 겹친다. 정육면체를 쓰면 여러 순열이 동점이 되어 탐색이
    답을 못 고른다 (실측: 정육면체 조합에서 1위 1.000 vs 2위 0.908).
    """
    c = np.asarray(center, dtype=np.float64)
    h = np.asarray(size, dtype=np.float64) / 2.0
    if h.shape != (3,):
        raise ValueError(f"size 는 길이 3 이어야 한다: {np.shape(size)}")
    signs = np.array(
        [
            [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
            [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
        ],
        dtype=np.float64,
    )
    verts = signs * h + c
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


def cube_mesh(
    center: Sequence[float] = (0.0, 0.0, 0.0), size: float = 0.3
) -> Tuple[np.ndarray, np.ndarray]:
    """축정렬 정육면체. `box_mesh` 의 등방 특수형."""
    return box_mesh(center, (size, size, size))


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


# ══════════════════════════════════════════════════ D9 좌표 프레임 픽스처
#
# 같은 **물리적 물체**를 두 프레임에서 각각 손으로 기술한다. 한쪽을 다른 쪽에
# 변환을 적용해서 만들면 순환 논증이 되므로, 둘 다 아래 물리적 서술에서 직접 쓴다.
#
#     물체   큰 상자(몸통) 위에 중간 상자(머리), 머리의 **앞쪽·오른쪽**으로 뻗은 주둥이
#     방향   위=GLB +Y / VOXEL +Z        앞=GLB +Z / VOXEL **-Y**       오른쪽=둘 다 +X
#
# 세 부분의 크기가 전부 다르고 주둥이가 x·앞뒤 양쪽으로 치우쳐 있어서, 48개 부호付
# 순열 중 **정답 하나만** 두 기술을 정확히 겹치게 만든다. 대칭인 물체를 쓰면 여러
# 순열이 동점이 되어 전수 탐색이 답을 못 고른다.

# 물리적 치수 — 폭(좌우) · 앞뒤 · 높이. **셋이 전부 다르다.**
#   몸통  폭 0.36 · 앞뒤 0.22 · 높이 0.30      아래
#   머리  폭 0.20 · 앞뒤 0.14 · 높이 0.18      위
#   주둥이 폭 0.10 · 앞뒤 0.26 · 높이 0.10     머리 앞·오른쪽으로 크게 돌출
#
# 주둥이가 커야 하는 이유: 앞뒤 뒤집기(x,z,y)와 좌우 뒤집기(-x,-z,y)를 구분하는 것이
# 이 돌출부뿐이다. 작게 두면 그 순열들이 IoU 0.935 로 따라붙어 전수 탐색의 격차가
# 1.07배까지 좁아진다 (실측). A5000 이 실자산에서 4.8배를 얻은 것은 실제 물체가
# 그만큼 비대칭이기 때문이다.

def asymmetric_asset_glb_frame():
    """GLB(Y-up) 프레임에서 기술한 비대칭 물체.

    GLB 축 = (폭, 높이, 앞뒤) = (x, y, z).
    """
    parts = [
        #          (폭,   높이,  앞뒤)
        box_mesh((0.00, -0.10, 0.00), (0.36, 0.30, 0.22)),   # 몸통 — 아래(-Y)
        box_mesh((0.00, 0.15, 0.00), (0.20, 0.18, 0.14)),    # 머리 — 위(+Y)
        box_mesh((0.06, 0.15, 0.17), (0.10, 0.10, 0.26)),    # 주둥이 — 앞(+Z)·오른쪽(+X)
    ]
    return _concat(parts)


def asymmetric_asset_voxel_frame():
    """VOXEL(Z-up) 프레임에서 기술한 **같은** 물체.

    VOXEL 축 = (폭, 앞뒤, 높이) = (x, y, z). 위 → +Z, 앞 → **-Y**, 오른쪽 → +X.
    D9 변환을 적용한 적이 없다 — 위와 같은 물리적 치수에서 직접 좌표를 썼다.
    """
    parts = [
        #          (폭,   앞뒤,  높이)
        box_mesh((0.00, 0.00, -0.10), (0.36, 0.22, 0.30)),   # 몸통 — 아래(-Z)
        box_mesh((0.00, 0.00, 0.15), (0.20, 0.14, 0.18)),    # 머리 — 위(+Z)
        box_mesh((0.06, -0.17, 0.15), (0.10, 0.26, 0.10)),   # 주둥이 — 앞(-Y)·오른쪽(+X)
    ]
    return _concat(parts)


def _concat(parts):
    verts, faces, off = [], [], 0
    for v, f in parts:
        verts.append(v)
        faces.append(f + off)
        off += v.shape[0]
    return np.concatenate(verts, axis=0), np.concatenate(faces, axis=0)


def donor_mesh(radius: float = 0.30, n_lat: int = 24, n_lon: int = 48,
               ribs: int = 8, rib_depth: float = 0.16):
    """기증자 — **호박** 대역. 세로 골이 파인 납작한 구.

    정육면체가 아니라 이걸 쓰는 이유는 D11 때문이다. D11 이 고치려는 증상은
    "게이트는 통과하는데 호박으로 안 보인다" 이므로, 픽스처도 **특징이 있는 형상**
    이어야 재복셀화가 그 특징을 살리는지 볼 수 있다. 정육면체는 해상도를 반으로
    줄여도 정육면체라서 아무것도 검증하지 못한다.

    골(rib)은 경도 방향 코사인으로 반지름을 흔들어 만든다. 납작한 비율(z 0.78)까지
    합치면 구·정육면체 어느 것과도 다른 실루엣이 나온다.
    """
    c = np.zeros(3)
    lat = np.linspace(0.0, np.pi, n_lat + 1)
    lon = np.linspace(0.0, 2.0 * np.pi, n_lon, endpoint=False)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    r = radius * (1.0 - rib_depth * (0.5 + 0.5 * np.cos(ribs * lo)) * np.sin(la))
    x = r * np.sin(la) * np.cos(lo)
    y = r * np.sin(la) * np.sin(lo)
    z = r * np.cos(la) * 0.78          # 납작하게
    verts = np.stack([x, y, z], axis=-1).reshape(-1, 3) + c

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
