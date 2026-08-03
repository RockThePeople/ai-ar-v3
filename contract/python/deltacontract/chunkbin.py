"""
`.cbin` — 청크 1개의 이진 표현. 이 파일이 정본이고, unity/ChunkBin.cs 가 같은 포맷의
두 번째 구현이다. 둘은 conformance/ 골든 벡터로 바이트 단위 대조된다.

────────────────────────────────────────────────────────────────────────
왜 JSON 이 아닌가
────────────────────────────────────────────────────────────────────────
청크 하나가 수천~수만 정점이다. base64 JSON 은 부피가 ~1.37배 늘고 파싱이 느리며,
무엇보다 **float 를 텍스트로 왕복시키면 바이트 동일성 판정이 무의미해진다**.
델타의 전제(§4.2 "영향받지 않은 청크는 바이트 단위로 이전 버전과 동일")를 지키려면
고정폭 리틀엔디언 이진이어야 한다.

────────────────────────────────────────────────────────────────────────
레이아웃 (모두 little-endian)
────────────────────────────────────────────────────────────────────────
  off  size  type      field
    0     4  char[4]   magic = "CBN1"
    4     4  uint32    contract_version
    8     4  uint32    flags   bit0 NORMAL, bit1 COLOR, bit2 UV
   12    12  int32[3]  chunk coord (x, y, z)
   24     4  uint32    vertex_count  (V)
   28     4  uint32    index_count   (I, 3의 배수)
   32     4  uint32    voxel_count   (이 청크의 활성 SLat 복셀 수, 계측용)
   36     4  uint32    reserved = 0
  ── header = 40 bytes (4의 배수 → 이후 모든 배열이 4바이트 정렬)
   40  12V  float32[V][3]  POSITION   (NORMALIZED 공간)
    …   12V float32[V][3]  NORMAL     (flags bit0)
    …    4V uint8  [V][4]  COLOR_RGBA (flags bit1)  ← u8x4 라 정렬 유지
    …    8V float32[V][2]  TEXCOORD_0 (flags bit2)
    …    4I uint32[I]      INDEX

정렬이 4바이트로 유지되는 것은 glTF bufferView 제약(accessor byteOffset 은 컴포넌트
크기의 배수)을 만족시키기 위해서다. 3090은 이 파일을 **재포장 없이 그대로** glTF
buffer 로 참조하고 bufferView.byteOffset 만 계산하면 된다 (buffer_views() 참고).

해시: 파일 전체 바이트의 SHA-256. 헤더까지 포함하므로 청크 좌표가 다르면 해시도 다르다.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

from .coords import (
    CONTRACT_VERSION,
    POSITION_QUANT_SCALE,
    Coord,
    chunk_key,
)

MAGIC = b"CBN1"
HEADER_SIZE = 40

FLAG_NORMAL = 1 << 0
FLAG_COLOR = 1 << 1
FLAG_UV = 1 << 2


class ChunkBinError(ValueError):
    pass


@dataclass
class ChunkMesh:
    """청크 1개의 메시. 모든 배열은 canonical 순서로 정규화된 상태여야 한다.

    canonicalize() 를 거치지 않은 ChunkMesh 를 encode() 에 넣으면 예외가 난다 —
    "정규화를 깜빡해서 델타가 깨졌다"가 이 시스템에서 가장 찾기 어려운 버그라서
    실수를 타입 레벨이 아니라 런타임에서라도 잡는다.
    """

    chunk_coord: Coord
    positions: np.ndarray  # (V,3) float32
    normals: Optional[np.ndarray] = None  # (V,3) float32
    colors: Optional[np.ndarray] = None  # (V,4) uint8
    uvs: Optional[np.ndarray] = None  # (V,2) float32
    indices: np.ndarray = None  # (I,) uint32
    voxel_count: int = 0
    _canonical: bool = False

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def index_count(self) -> int:
        return int(self.indices.shape[0])

    @property
    def key(self) -> str:
        return chunk_key(self.chunk_coord)


# ══════════════════════════════════════════════════════════ canonicalize
#
# FINAL 명세 §9-1 의 요구를 정점 레벨까지 밀어붙인 것.
#
# 왜 필요한가: 메시 디코더(FlexiCubes)는 dense 64³ 큐브를 순회하며 정점을 뱉는다.
# 편집으로 **다른 청크**의 큐브가 활성/비활성 되면, 안 바뀐 영역의 정점도 전역
# 인덱스 번호가 통째로 밀린다. 기하는 같은데 인덱스만 달라지므로, 정규화 없이
# 바이트 비교를 하면 "안 바뀐 청크"가 매번 바뀐 것으로 판정된다.
#
# 정규화 후에는 청크 로컬 인덱스가 기하에서만 결정되므로 이 문제가 사라진다.


def _sort_keys(positions: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """정점 정렬 키. 1차: 양자화 위치(x,y,z), 2차: 전체 속성 raw 바이트.

    양자화는 **정렬에만** 쓰고 저장되는 값은 원본 float32 그대로다 (무손실).
    """
    q = np.rint(positions.astype(np.float64) * POSITION_QUANT_SCALE).astype(np.int64)
    # np.lexsort 는 마지막 키가 1순위. raw 바이트를 tie-break 로 쓰기 위해
    # 바이트열을 void 뷰로 바꿔 문자열 비교시킨다.
    raw_view = np.ascontiguousarray(raw).view([("b", np.uint8, raw.shape[1])]).ravel()
    order = np.lexsort((raw_view, q[:, 2], q[:, 1], q[:, 0]))
    return order


def canonicalize(
    chunk_coord: Coord,
    positions: np.ndarray,
    indices: np.ndarray,
    normals: Optional[np.ndarray] = None,
    colors: Optional[np.ndarray] = None,
    uvs: Optional[np.ndarray] = None,
    voxel_count: int = 0,
) -> ChunkMesh:
    """중복 정점 용접(무손실) → canonical 정렬 → 인덱스 재매핑 → 삼각형 정렬."""
    positions = np.ascontiguousarray(positions, dtype=np.float32).reshape(-1, 3)
    v = positions.shape[0]
    indices = np.ascontiguousarray(indices, dtype=np.uint32).ravel()
    if indices.size % 3 != 0:
        raise ChunkBinError(f"인덱스 개수가 3의 배수가 아니다: {indices.size}")
    if v and indices.size and int(indices.max()) >= v:
        raise ChunkBinError(f"인덱스 범위 초과: max={int(indices.max())}, V={v}")

    parts = [positions.view(np.uint8).reshape(v, -1)]
    if normals is not None:
        normals = np.ascontiguousarray(normals, dtype=np.float32).reshape(v, 3)
        parts.append(normals.view(np.uint8).reshape(v, -1))
    if colors is not None:
        colors = np.ascontiguousarray(colors, dtype=np.uint8).reshape(v, 4)
        parts.append(colors)
    if uvs is not None:
        uvs = np.ascontiguousarray(uvs, dtype=np.float32).reshape(v, 2)
        parts.append(uvs.view(np.uint8).reshape(v, -1))
    raw = np.concatenate(parts, axis=1) if v else np.zeros((0, 1), np.uint8)

    if v == 0:
        return ChunkMesh(
            chunk_coord=tuple(int(c) for c in chunk_coord),
            positions=positions,
            normals=None if normals is None else normals,
            colors=None if colors is None else colors,
            uvs=None if uvs is None else uvs,
            indices=indices,
            voxel_count=int(voxel_count),
            _canonical=True,
        )

    # 1) 완전히 동일한(모든 속성 바이트가 같은) 정점 용접 — 무손실.
    #    디코더가 중복 정점을 몇 개 뱉는지에 결과가 의존하지 않게 만든다.
    _, weld_first, weld_inverse = np.unique(
        raw, axis=0, return_index=True, return_inverse=True
    )
    positions = positions[weld_first]
    if normals is not None:
        normals = normals[weld_first]
    if colors is not None:
        colors = colors[weld_first]
    if uvs is not None:
        uvs = uvs[weld_first]
    raw = raw[weld_first]
    indices = weld_inverse[indices].astype(np.uint32)

    # 2) canonical 정렬
    order = _sort_keys(positions, raw)
    remap = np.empty(order.size, dtype=np.uint32)
    remap[order] = np.arange(order.size, dtype=np.uint32)
    positions = np.ascontiguousarray(positions[order])
    if normals is not None:
        normals = np.ascontiguousarray(normals[order])
    if colors is not None:
        colors = np.ascontiguousarray(colors[order])
    if uvs is not None:
        uvs = np.ascontiguousarray(uvs[order])
    indices = remap[indices]

    # 3) 삼각형 정규화: 최소 인덱스가 앞에 오도록 **회전**(스왑 아님 → winding 보존)
    tris = indices.reshape(-1, 3)
    if tris.size:
        roll = np.argmin(tris, axis=1)
        cols = (roll[:, None] + np.arange(3)[None, :]) % 3
        tris = np.take_along_axis(tris, cols, axis=1)
        # 4) 삼각형 목록 정렬 — 면 열거 순서 의존성 제거
        tris = tris[np.lexsort((tris[:, 2], tris[:, 1], tris[:, 0]))]
        # 5) 완전 중복 삼각형 제거 (디코더가 degenerate 를 중복 뱉는 경우 대비)
        keep = np.ones(tris.shape[0], dtype=bool)
        if tris.shape[0] > 1:
            keep[1:] = np.any(tris[1:] != tris[:-1], axis=1)
        tris = tris[keep]

    return ChunkMesh(
        chunk_coord=tuple(int(c) for c in chunk_coord),
        positions=positions,
        normals=normals,
        colors=colors,
        uvs=uvs,
        indices=np.ascontiguousarray(tris.ravel(), dtype=np.uint32),
        voxel_count=int(voxel_count),
        _canonical=True,
    )


# ══════════════════════════════════════════════════════════ encode / decode


def encode(mesh: ChunkMesh) -> bytes:
    if not mesh._canonical:
        raise ChunkBinError("canonicalize() 를 거치지 않은 ChunkMesh 는 인코딩할 수 없다.")
    v = mesh.vertex_count
    flags = 0
    if mesh.normals is not None:
        flags |= FLAG_NORMAL
    if mesh.colors is not None:
        flags |= FLAG_COLOR
    if mesh.uvs is not None:
        flags |= FLAG_UV

    head = struct.pack(
        "<4sIIiiiIIII",
        MAGIC,
        CONTRACT_VERSION,
        flags,
        int(mesh.chunk_coord[0]),
        int(mesh.chunk_coord[1]),
        int(mesh.chunk_coord[2]),
        v,
        mesh.index_count,
        int(mesh.voxel_count),
        0,
    )
    assert len(head) == HEADER_SIZE, len(head)

    body = [np.ascontiguousarray(mesh.positions, dtype="<f4").tobytes()]
    if mesh.normals is not None:
        body.append(np.ascontiguousarray(mesh.normals, dtype="<f4").tobytes())
    if mesh.colors is not None:
        body.append(np.ascontiguousarray(mesh.colors, dtype=np.uint8).tobytes())
    if mesh.uvs is not None:
        body.append(np.ascontiguousarray(mesh.uvs, dtype="<f4").tobytes())
    body.append(np.ascontiguousarray(mesh.indices, dtype="<u4").tobytes())
    return head + b"".join(body)


def decode(blob: bytes) -> ChunkMesh:
    if len(blob) < HEADER_SIZE:
        raise ChunkBinError(f"헤더보다 짧다: {len(blob)} bytes")
    magic, ver, flags, cx, cy, cz, v, i, voxels, _res = struct.unpack_from("<4sIIiiiIIII", blob, 0)
    if magic != MAGIC:
        raise ChunkBinError(f"magic 불일치: {magic!r}")
    if ver != CONTRACT_VERSION:
        raise ChunkBinError(f"계약 버전 불일치: file={ver}, local={CONTRACT_VERSION}")

    off = HEADER_SIZE

    def take(count: int, dtype: str, width: int) -> np.ndarray:
        nonlocal off
        nbytes = count * width
        if off + nbytes > len(blob):
            raise ChunkBinError(f"본문이 잘렸다. offset={off}, need={nbytes}, total={len(blob)}")
        arr = np.frombuffer(blob, dtype=dtype, count=count, offset=off)
        off += nbytes
        return arr

    positions = take(v * 3, "<f4", 4).reshape(v, 3)
    normals = take(v * 3, "<f4", 4).reshape(v, 3) if flags & FLAG_NORMAL else None
    colors = take(v * 4, np.uint8, 1).reshape(v, 4) if flags & FLAG_COLOR else None
    uvs = take(v * 2, "<f4", 4).reshape(v, 2) if flags & FLAG_UV else None
    indices = take(i, "<u4", 4)
    if off != len(blob):
        raise ChunkBinError(f"본문에 잉여 바이트가 있다: {len(blob) - off}")

    return ChunkMesh(
        chunk_coord=(cx, cy, cz),
        positions=positions,
        normals=normals,
        colors=colors,
        uvs=uvs,
        indices=indices,
        voxel_count=int(voxels),
        _canonical=True,
    )


def blob_hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ══════════════════════════════════════════════════════════ glTF 연동
#
# 3090은 .cbin 을 **재포장하지 않는다**. 그대로 glTF buffer 로 참조하고
# bufferView.byteOffset 만 아래 함수로 계산한다. 재포장하면 그 시점에 바이트가
# 달라져서 "안 바뀐 청크는 바이트 동일"이 깨진다.


def buffer_views(mesh_or_header: "ChunkMesh | bytes") -> Dict[str, Tuple[int, int]]:
    """{"POSITION": (byteOffset, byteLength), ...} 반환."""
    if isinstance(mesh_or_header, (bytes, bytearray)):
        m = decode(bytes(mesh_or_header))
    else:
        m = mesh_or_header
    v, i = m.vertex_count, m.index_count
    off = HEADER_SIZE
    out: Dict[str, Tuple[int, int]] = {}

    def add(name: str, nbytes: int) -> None:
        nonlocal off
        out[name] = (off, nbytes)
        off += nbytes

    add("POSITION", v * 12)
    if m.normals is not None:
        add("NORMAL", v * 12)
    if m.colors is not None:
        add("COLOR_0", v * 4)
    if m.uvs is not None:
        add("TEXCOORD_0", v * 8)
    add("INDICES", i * 4)
    return out
