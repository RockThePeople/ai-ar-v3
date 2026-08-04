"""D24 — 색 편집(레벨1)은 **별도 경로**다. 복셀 격자를 아예 경유하지 않는다.

────────────────────────────────────────────────────────────────────────
왜 S2 관통 경로로는 색이 안 되는가
────────────────────────────────────────────────────────────────────────
S2 는 `.cbin → 표면복셀화 → occupancy → occupancy_to_mesh → 재인코딩` 이다.
그런데 **`occupancy_to_mesh` 는 정점과 면만 낸다** — 색 채널이 없다. 그 경로를
타는 순간 색이 통째로 사라진다.

⚠️ 색은 자산에 **실제로 있다.** W6/3090 실측: base `flags=0b0011`(COLOR|NORMAL),
   눈사람 (148,147,147) · 호박 (209,148,71). **버리는 건 우리 경로다** —
   버그가 아니라 S2 가 기하만 다루기로 한 설계 선택의 대가다.

────────────────────────────────────────────────────────────────────────
그래서 이 경로는 복셀을 안 거친다
────────────────────────────────────────────────────────────────────────
    마스크에 걸린 청크만 디코드 → 정점 색 교체 → 재인코드
    나머지는 부모 바이트 승계

이 경로의 강점은 속도가 아니라 **기하가 원본 그대로**라는 것이다. 복셀화·재메싱을
안 하므로 `positions` 와 `indices` 가 바이트 단위로 보존된다 (W6/3090 실측
기하 바이트 53/53 · hue 58.3°→27.0° 이동 31.3° · 절감 70.46%).
`server/tests/test_recolor.py` 가 그 바이트 동일성을 못박는다.

────────────────────────────────────────────────────────────────────────
🔴 `canonicalize()` 를 다시 부르면 안 된다
────────────────────────────────────────────────────────────────────────
`chunkbin.canonicalize` 의 정점 정렬 키는 (양자화 위치, **전체 속성 raw 바이트**)다.
색을 바꾸면 그 tie-break 바이트가 바뀌므로, 재정규화하면 **정점 순서가 달라지고
positions·indices 바이트가 통째로 바뀐다** — 이 경로의 유일한 강점이 사라진다.

`decode()` 는 이미 `_canonical=True` 로 돌려주므로, 색만 갈아끼우고 그대로
`encode()` 하면 된다. 이 모듈은 `canonicalize` 를 부르지 않는다.

⚠️ `decode()` 가 주는 배열은 원본 바이트 위의 **읽기 전용 뷰**다. 제자리에서
   쓰려 하면 `ValueError: assignment destination is read-only` 가 난다 — 복사한다.

────────────────────────────────────────────────────────────────────────
부기 — 여기서는 diff 함정이 없다
────────────────────────────────────────────────────────────────────────
`assemble` 에서 부기를 diff 로 유도하면 안 됐던 이유는 **비워진 청크가 새 바이트를
안 내서 목록에서 사라지기** 때문이었다 (실측 8청크 유실). 색 편집은 기하를 안
건드리므로 청크가 비워지지도, 생겨나지도 않는다 — 그 실패 모양이 존재하지 않는다.
그래도 부기는 **마스크(=배치)에서** 유도한다. 실제로 정점이 하나도 안 걸린 청크만
제외하며, 그 제외는 `n_vertices_recolored == 0` 이라는 **사실**이지 diff 가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from deltacontract.chunkbin import (  # type: ignore[import-not-found]
    ChunkBinError,
    decode,
    encode,
)
from deltacontract.coords import (  # type: ignore[import-not-found]
    chunk_key,
    normalized_to_voxel,
    voxel_code,
    voxel_to_chunk,
)

from .delta import Bookkeeping, verify_bookkeeping
from .mask import MaskResult
from .package import DeltaPackage, package_delta

__all__ = ["RecolorResult", "recolor_asset", "recolor_chunk", "sample_colors"]


class RecolorError(ChunkBinError):
    """색 편집 전제가 깨졌다 (색 채널 없음 등)."""


def _rgba(color: Sequence[int]) -> Tuple[np.ndarray, bool]:
    """(r,g,b) 또는 (r,g,b,a) → uint8 배열. 3성분이면 알파를 **보존**한다."""
    a = np.asarray(color, dtype=np.int64).reshape(-1)
    if a.size not in (3, 4):
        raise RecolorError(f"색은 RGB 또는 RGBA 여야 한다: {a.tolist()}")
    if a.min() < 0 or a.max() > 255:
        raise RecolorError(f"색 성분이 [0,255] 밖이다: {a.tolist()}")
    return a.astype(np.uint8), a.size == 3


def _mask_codes(mask: MaskResult) -> np.ndarray:
    cells = np.asarray(mask.cells, dtype=np.int64).reshape(-1, 3)
    return np.unique(voxel_code(cells)) if cells.size else np.zeros((0,), np.int64)


def recolor_chunk(
    blob: bytes,
    color: Sequence[int],
    *,
    mask_codes: Optional[np.ndarray] = None,
) -> Tuple[bytes, int]:
    """청크 하나의 정점 색을 바꾼다. **기하 바이트는 손대지 않는다.**

    Args:
        mask_codes: 마스크 셀의 정수 코드. None 이면 청크의 **모든** 정점을 칠한다.
                    주면 그 셀에 떨어지는 정점만 칠한다.

    Returns:
        (새 바이트, 실제로 색이 바뀐 정점 수). 0 이면 바이트가 원본과 동일하다.
    """
    mesh = decode(blob)
    if mesh.colors is None:
        raise RecolorError(
            "이 청크에는 COLOR 채널이 없다 (flags 에 COLOR 비트가 없음). "
            "색 편집은 색을 가진 자산에만 쓴다 — 기하만 있는 자산은 S2 경로다."
        )

    rgba, keep_alpha = _rgba(color)
    # decode 가 준 배열은 원본 바이트 위의 읽기 전용 뷰다. 복사해야 쓴다.
    colors = np.array(mesh.colors, dtype=np.uint8, copy=True)

    if mask_codes is None:
        sel = np.ones(colors.shape[0], dtype=bool)      # 청크 전체를 칠한다
    elif mask_codes.size == 0:
        sel = np.zeros(colors.shape[0], dtype=bool)     # 빈 마스크 → 아무것도 안 칠한다
    else:
        cells = normalized_to_voxel(np.asarray(mesh.positions, dtype=np.float64))
        sel = np.isin(voxel_code(cells), mask_codes)

    if not sel.any():
        return blob, 0

    new = colors.copy()
    new[sel, :3] = rgba[:3]
    if not keep_alpha:
        new[sel, 3] = rgba[3]
    n_changed = int((new != colors).any(axis=1).sum())
    if n_changed == 0:
        return blob, 0

    mesh.colors = new
    return encode(mesh), n_changed


@dataclass(frozen=True)
class RecolorResult:
    """색 편집 1회의 결과. 패키징은 `pipeline/package.py` 를 그대로 쓴다."""

    package: DeltaPackage
    bookkeeping: Bookkeeping
    n_chunks_in_mask: int          # 마스크가 걸린 청크 수 (배치에서 유도)
    n_chunks_recolored: int        # 실제로 정점이 걸려 바이트가 바뀐 청크 수
    n_vertices_recolored: int
    color: Tuple[int, ...]

    @property
    def blobs(self) -> Dict[str, bytes]:
        return self.package.blobs

    @property
    def transfer_saving(self) -> float:
        from server.metrics import transfer_saving  # noqa: PLC0415

        return transfer_saving(self.package.full_bytes, self.package.delta_bytes)


def recolor_asset(
    parent_blobs: Mapping[str, bytes],
    mask: MaskResult,
    color: Sequence[int],
    *,
    job_id: Optional[str] = None,
) -> RecolorResult:
    """★ D24 경로. 마스크에 걸린 정점만 색을 바꾸고 나머지는 부모 바이트를 승계한다.

    복셀 격자를 경유하지 않으므로 **기하가 바이트 단위로 보존된다.**

    ⚠️ 마스크는 halo 를 쓰지 않는다. halo 는 디코더의 receptive field 때문에
       존재하는데(§8.2), 이 경로는 디코더를 안 돌린다 — 색은 정점 단위로 정확히
       갈아끼워지므로 경계 완충이 필요 없다. `mask.cells` 를 쓴다.
    """
    codes = _mask_codes(mask)
    if codes.size == 0:
        raise RecolorError("마스크가 비었다 — 칠할 곳이 없다")

    # 부기는 **마스크(배치)에서** 유도한다. 모듈 docstring 참고.
    in_mask = {
        chunk_key(c) for c in voxel_to_chunk(np.asarray(mask.cells).reshape(-1, 3))
    }
    candidates = sorted(in_mask & set(parent_blobs))

    recolored: Dict[str, bytes] = {}
    changed: List[str] = []
    n_vertices = 0
    for key in candidates:
        new_blob, n = recolor_chunk(parent_blobs[key], color, mask_codes=codes)
        if n:
            recolored[key] = new_blob
            changed.append(key)
            n_vertices += n

    # `package_delta` 는 child 를 **결과 전체 세트**로 본다 (승계 전). 색 편집은
    # 바뀐 청크만 만들므로 나머지는 부모 것을 그대로 얹어 전체 세트를 만든다.
    # 이렇게 넘겨야 "부기 밖에서 청크가 사라졌다" 검사가 제 일을 한다 —
    # 검사를 우회하려고 그 가드를 끄지 않는다.
    child_blobs: Dict[str, bytes] = {**parent_blobs, **recolored}

    bk = Bookkeeping(
        zone1=sorted(changed),   # 색이 바뀐 자리
        zone2=[],                # 비워진 자리 없음 — 기하를 안 건드린다
        book=sorted(changed),
        changed=sorted(changed),
        removed=[],              # 색 편집은 청크를 없애지 않는다
    )
    verify_bookkeeping(bk)

    pkg = package_delta(parent_blobs, child_blobs, bk, mask=mask, job_id=job_id)
    rgba, _ = _rgba(color)
    return RecolorResult(
        package=pkg,
        bookkeeping=bk,
        n_chunks_in_mask=len(candidates),
        n_chunks_recolored=len(changed),
        n_vertices_recolored=n_vertices,
        color=tuple(int(v) for v in rgba.tolist()),
    )


def sample_colors(
    blobs: Mapping[str, bytes],
    mask: Optional[MaskResult] = None,
    *,
    keys: Optional[Iterable[str]] = None,
) -> np.ndarray:
    """색 계측용 표본. (N,3) uint8 RGB.

    `mask` 를 주면 그 셀에 떨어지는 정점만 모은다. 색이 없는 청크는 건너뛴다 —
    계측은 있는 것만 보면 되고, 없는 것은 `recolor_chunk` 가 따로 거부한다.
    """
    codes = _mask_codes(mask) if mask is not None else None
    out = []
    for key in (keys if keys is not None else blobs):
        mesh = decode(blobs[key])
        if mesh.colors is None:
            continue
        rgb = np.asarray(mesh.colors, dtype=np.uint8)[:, :3]
        if codes is not None and codes.size:
            cells = normalized_to_voxel(np.asarray(mesh.positions, dtype=np.float64))
            rgb = rgb[np.isin(voxel_code(cells), codes)]
        if rgb.size:
            out.append(rgb)
    return np.concatenate(out, axis=0) if out else np.zeros((0, 3), dtype=np.uint8)
