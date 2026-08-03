"""
전체 메시 -> 청크별 메시 분할. A5000(생성/편집 서버)이 쓰는 진입점.

────────────────────────────────────────────────────────────────────────
FINAL 명세와의 의도적 차이 — 반드시 읽을 것
────────────────────────────────────────────────────────────────────────
명세 §3.4 의사코드는 청크마다 따로 디코딩한다:

    mesh_subset = trellis.decoder.decode(chunk_slat, context=neighboring(chunk_id, chunks))

이건 TRELLIS(.1/.2) 의 실제 메시 디코더에서 성립하지 않는다. `SparseFeatures2Mesh`
는 sparse latent 를 **dense 그리드로 펼친 뒤 FlexiCubes 를 전역으로 한 번** 돌린다
(`get_dense_attrs` → `self.mesh_extractor(...)`). 청크 부분집합만 넣고 돌린 결과는
전체를 돌려서 자른 것과 같지 않다.

따라서 이 계약은 순서를 뒤집는다:

    전체 SLat 디코딩 1회  →  결과 메시를 청크로 분할

디코딩은 확산 샘플링과 달리 forward 1회라 비용이 작고, 무엇보다 "안 바뀐 청크는
바이트 동일"이라는 델타 전제를 이쪽이 훨씬 잘 지킨다. FlexiCubes 는 큐브 단위
지역 연산이므로, 입력 sdf/deform 이 같은 영역은 같은 정점을 낸다. 달라지는 건
전역 인덱스 번호뿐이고 그건 chunkbin.canonicalize() 가 흡수한다.

남는 위험은 하나다: sparse conv 디코더의 receptive field 때문에 **마스크 밖
latent 도 미세하게 흔들릴 수 있다.** 그게 §8.2 halo 와 §9-2 (hash vs epsilon)
가 존재하는 이유이고, conformance/ 의 결정성 측정이 답을 내야 하는 질문이다.

────────────────────────────────────────────────────────────────────────
삼각형 배정 규칙 (결정적)
────────────────────────────────────────────────────────────────────────
삼각형은 **무게중심이 속한 청크**에 통째로 배정된다.
  - 각 삼각형은 정확히 한 청크에 속한다 → 합집합 = 원본 메시, 누락도 중복도 없다.
  - 청크 경계에 걸친 삼각형의 정점은 양쪽 청크 로컬 버퍼에 복제된다. 정상이다.
  - 무게중심은 float 합/3 이므로 같은 입력이면 같은 값 → 배정도 결정적.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np

from .chunkbin import ChunkMesh, canonicalize
from .coords import (
    assert_in_normalized_bounds,
    chunk_key,
    normalized_to_chunk,
    voxel_to_chunk,
)


def split_trellis_vertex_attrs(
    vertex_attrs: Optional[np.ndarray],
    normal_encoding: str = "0_1",
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """TRELLIS `MeshExtractResult.vertex_attrs` (V,6) -> (albedo RGB, **단위** normal).

    `SparseFeatures2Mesh` 의 layout 은 color 채널이 6개이고 "6 channel color
    including normal map" 으로 주석되어 있다 — 앞 3채널 albedo, 뒤 3채널 normal.
    채널이 3개뿐이면 albedo 만 있는 것으로 본다.

    ────────────────────────────────────────────────────────────────
    normal_encoding — contract_version 3에서 추가. 기본값이 바뀌는 지점이다.
    ────────────────────────────────────────────────────────────────
    "normal map" 이라는 이름 그대로, 뒤 3채널은 **[0,1] 로 인코딩된** 법선이다.
    A5000 실측(2026-07-29): 6채널 전부 min 이 양수이고 max 가 1 근처다. 그대로
    쓰면 단위벡터 비율이 **0.00%**, `2n-1` 로 복원하면 83.5% 가 된다.

    이 변환을 호출부에 맡겼더니 **두 곳에서 동시에 빠졌다** — 계약 문서의 예제
    스니펫과 A5000 probe 양쪽이다. 조명이 전부 틀어지는데 예외는 안 나므로,
    세 번째 세션에서도 같은 식으로 빠질 게 확실하다. 그래서 계약이 한다.

      "0_1"  : [0,1] 인코딩으로 보고 2n-1 후 정규화 (TRELLIS 실제 출력. 기본값)
      "unit" : 이미 [-1,1] 단위벡터로 보고 정규화만
      "raw"  : 아무것도 안 함. 진단용

    albedo 는 손대지 않는다 — 실측상 이탈 0.0000% 로 이미 [0,1] 이다.
    """
    if vertex_attrs is None:
        return None, None
    a = np.asarray(vertex_attrs)
    if a.ndim != 2:
        raise ValueError(f"(V,C) 가 필요하다. got {a.shape}")
    if normal_encoding not in ("0_1", "unit", "raw"):
        raise ValueError(f"normal_encoding 은 '0_1'|'unit'|'raw' 중 하나여야 한다. got {normal_encoding!r}")

    if a.shape[1] >= 6:
        albedo, normal = a[:, 0:3], a[:, 3:6]
    elif a.shape[1] >= 3:
        return a[:, 0:3], None
    else:
        return None, None

    if normal_encoding == "0_1":
        normal = np.asarray(normal, dtype=np.float32) * 2.0 - 1.0
    if normal_encoding != "raw":
        normal = normalize_normals(normal)
    return albedo, normal


def albedo_to_rgba8(albedo: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """float albedo [0,1] -> uint8 RGBA. 양자화 규칙을 여기 한 곳에 고정한다.

    round-half-away-from-zero 가 아니라 numpy 기본 round-half-even 을 쓰면
    플랫폼별로 갈릴 수 있는 여지가 생기므로 명시적으로 floor(x*255 + 0.5) 를 쓴다.
    """
    if albedo is None:
        return None
    a = np.clip(np.asarray(albedo, dtype=np.float64), 0.0, 1.0)
    rgb = np.floor(a * 255.0 + 0.5).astype(np.uint8)
    alpha = np.full((rgb.shape[0], 1), 255, dtype=np.uint8)
    return np.concatenate([rgb, alpha], axis=1)


def normalize_normals(normals: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """길이만 1로 맞춘다. **[0,1] → [-1,1] 복원은 하지 않는다** —
    그건 `split_trellis_vertex_attrs(normal_encoding=...)` 의 책임이다.
    범위를 보고 인코딩을 추측하지 않는다. 휴리스틱은 경계 사례에서 조용히 틀린다.
    """
    if normals is None:
        return None
    n = np.asarray(normals, dtype=np.float32)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    ln = np.where(ln < 1e-12, 1.0, ln)
    return (n / ln).astype(np.float32)


def partition_mesh(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    normals: Optional[np.ndarray] = None,
    colors_rgba8: Optional[np.ndarray] = None,
    uvs: Optional[np.ndarray] = None,
    voxel_cells: Optional[np.ndarray] = None,
) -> Dict[str, ChunkMesh]:
    """NORMALIZED 공간 메시를 청크별 ChunkMesh 로 분할한다.

    Args:
        vertices: (V,3) float, NORMALIZED [-0.5, 0.5] 공간.
        faces:    (F,3) int, vertices 인덱스.
        voxel_cells: (N,3) int VOXEL 셀. 청크별 voxel_count 계측용(선택).

    Returns:
        {chunk_key: ChunkMesh}. 삼각형이 하나도 없는 청크는 포함되지 않는다
        (명세 §4.3 "활성 복셀이 없는 청크는 노드를 생성하지 않는다").
    """
    vertices = np.ascontiguousarray(vertices, dtype=np.float32).reshape(-1, 3)
    faces = np.ascontiguousarray(faces, dtype=np.int64).reshape(-1, 3)
    if faces.size and int(faces.max()) >= vertices.shape[0]:
        raise ValueError("face 인덱스가 정점 배열 범위를 벗어난다.")
    # 좌표계를 통째로 잘못 넘긴 경우를 여기서 잡는다 (복셀 인덱스, 월드 미터 등).
    # 경계 정점의 소량 이탈(±1/512)은 정상이므로 허용치를 둔다.
    assert_in_normalized_bounds(vertices)

    voxels_per_chunk: Dict[str, int] = {}
    if voxel_cells is not None and np.asarray(voxel_cells).size:
        cids = voxel_to_chunk(np.asarray(voxel_cells).reshape(-1, 3))
        keys, counts = np.unique(
            np.asarray([chunk_key(c) for c in cids]), return_counts=True
        )
        voxels_per_chunk = {str(k): int(c) for k, c in zip(keys, counts)}

    out: Dict[str, ChunkMesh] = {}
    if faces.size == 0:
        return out

    # 삼각형 무게중심 -> 청크
    centroids = vertices[faces].mean(axis=1)  # (F,3) float32
    face_chunk = normalized_to_chunk(centroids)  # (F,3) int

    # 청크별로 face 를 모은다. 청크 수는 최대 512 이므로 파이썬 루프로 충분.
    uniq, inverse = np.unique(face_chunk, axis=0, return_inverse=True)
    for ci in range(uniq.shape[0]):
        cid = tuple(int(v) for v in uniq[ci])
        sel = np.flatnonzero(inverse == ci)
        sub_faces = faces[sel]

        used = np.unique(sub_faces)
        local = np.full(vertices.shape[0], -1, dtype=np.int64)
        local[used] = np.arange(used.size, dtype=np.int64)

        out[chunk_key(cid)] = canonicalize(
            chunk_coord=cid,
            positions=vertices[used],
            indices=local[sub_faces].ravel().astype(np.uint32),
            normals=None if normals is None else np.asarray(normals)[used],
            colors=None if colors_rgba8 is None else np.asarray(colors_rgba8)[used],
            uvs=None if uvs is None else np.asarray(uvs)[used],
            voxel_count=voxels_per_chunk.get(chunk_key(cid), 0),
        )
    return out


def diff_chunk_sets(
    previous: Dict[str, str],
    current: Dict[str, str],
) -> Tuple[Dict[str, str], list[str]]:
    """이전/현재 청크 해시 맵 -> (변경된 청크, 제거된 청크 키).

    ⚠️ 이건 "무엇이 바뀌었는지 찾아내는" diff 가 **아니다**. 무엇이 바뀌었는지는
    RePaint 부기에서 이미 알고 있고(§0 핵심 설계 원칙), 이 함수는 그 지식이 실제
    바이트와 일치하는지 **검증**하는 용도다. 부기가 말한 영향 청크 집합과 이 함수
    결과가 어긋나면 결정성 가정(§9)이 깨진 것이고, 그때는 조용히 넘어가면 안 된다.
    conformance/test_determinism.py 가 정확히 이 불일치를 잡는다.
    """
    changed = {k: h for k, h in current.items() if previous.get(k) != h}
    removed = [k for k in previous if k not in current]
    return changed, sorted(removed)
