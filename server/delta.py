"""D45 — 편집 결과 → **부기 · 승계 · .cbin 델타**. 3090 담당(델타 조립).

────────────────────────────────────────────────────────────────────────
이 모듈이 바로잡는 것 (D61-a)
────────────────────────────────────────────────────────────────────────
"VoxHammer 절감률 ≈ 0%" 라는 말이 돌았다. 그 값은 **승계·overflow 부기를 안 붙이고
원출력을 그대로 잰 것**이고, 우리 델타 파이프라인을 한 번도 통과한 적이 없다.

왜 원출력이 0% 로 보이는가는 간단하다. VoxHammer 는 자산을 **통째로 재디코딩**한다.
재디코딩하면 마스크 밖 기하도 부동소수점 수준에서 미세하게 흔들리고, 그러면
**모든 청크의 해시가 달라진다.** 해시만 보고 "바뀐 청크" 를 세면 전부가 바뀐 것이 되고
절감은 0 이 된다.

⚠️ 그래서 **해시 비교로 부기를 유도하지 않는다.** 부기는 **점유 비교**로 정한다:
   어느 복셀이 새로 생겼는가. 해시는 "달라졌다" 만 말하고 "무엇이 달라졌는가" 를
   말하지 않는데, 재디코딩 잡음과 실제 편집을 가르는 것이 정확히 그 차이다.

────────────────────────────────────────────────────────────────────────
부기 규칙 (D54)
────────────────────────────────────────────────────────────────────────
    부기 = (마스크 ∪ halo) 의 청크
         ∪ (마스크 밖 **신규** 복셀 중 연결성분 ≥ 문턱 인 것) 의 청크

세 가지를 지킨다:

  ① 마스크 밖 **기존** 복셀은 안 건드린다. 새로 생긴 것만 본다. 기존 복셀까지
     세면 재디코딩이 살짝 옮겨 놓은 표면이 전부 "변화" 로 잡혀 부기가 폭발한다.
  ② 연결성분 문턱이 **잡음과 신호를 가른다.** overflow 는 두 가지가 섞여 있다 —
     재디코딩 잡음(작고 흩어짐)과 마스크 밖으로 자란 실제 가지(크고 뭉침).
     필터가 없으면 잡음이 청크를 끌고 들어와 절감이 죽는다.
  ③ 문턱은 **관측된 성분 크기 분포에서** 고른다. 숫자를 먼저 정하고 데이터를
     맞추지 않는다 (§0: 숫자가 안 나올 때 파라미터를 돌려 맞추지 않는다).

🔴 필터가 너무 세면 **실제 편집 결과를 지운다.** 그래서 산출에 "무엇이 잘렸는가" 를
   같이 낸다 — 절감률만 보면 아무것도 안 보내는 구현이 100% 를 받는다(방법론 5조 3번).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from deltacontract import (  # type: ignore[import-not-found]
    chunk_key,
    dilate_cells,
    encode,
    partition_mesh,
    voxel_to_chunk,
)

from .metrics import components, inherited_byte_identity, transfer_saving

__all__ = ["Bookkeeping", "DeltaResult", "component_sizes", "build_bookkeeping", "assemble_delta"]


def _keys(cells: np.ndarray) -> set:
    """복셀 셀 → 청크 키 집합. 청크 좌표를 손으로 계산하지 않는다 (계약이 정본)."""
    if len(cells) == 0:
        return set()
    return {chunk_key(c) for c in voxel_to_chunk(np.asarray(cells, dtype=np.int64))}


def _rows(a: np.ndarray) -> np.ndarray:
    """(N,3) → 행 단위 비교용 1-D 코드. 원소 단위 비교는 D51 이 물린 자리다."""
    a = np.asarray(a, dtype=np.int64).reshape(-1, 3)
    return a[:, 0] * 4096 + a[:, 1] * 64 + a[:, 2]


def component_sizes(cells: np.ndarray) -> List[int]:
    """26-연결 성분 크기, 큰 것부터. 문턱을 고르기 **전에** 분포를 본다."""
    return [c.n_cells for c in components(np.asarray(cells, dtype=np.int64).reshape(-1, 3))]


@dataclass(frozen=True)
class Bookkeeping:
    """부기 결과 + **왜 그렇게 됐는지**."""

    book: List[str]
    mask_keys: List[str]
    overflow_keys: List[str]
    threshold: int
    kept_components: List[int]      # 문턱을 넘어 부기에 든 성분 크기
    dropped_components: List[int]   # 잡음으로 버린 성분 크기
    new_outside_cells: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((0, 3), np.int64))
    kept_cells: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((0, 3), np.int64))

    @property
    def dropped_voxels(self) -> int:
        return sum(self.dropped_components)

    @property
    def kept_voxels(self) -> int:
        return sum(self.kept_components)


def build_bookkeeping(
    base_cells: np.ndarray,
    result_cells: np.ndarray,
    mask_cells: np.ndarray,
    *,
    halo: int = 1,
    min_component: int,
) -> Bookkeeping:
    """D54 부기. `min_component` 에 **기본값을 두지 않는다.**

    기본값을 두면 다음 세션이 안 적고 지나가고, 그 순간 문턱이 근거 없이 굳는다.
    문턱을 고를 때는 `component_sizes()` 로 분포를 먼저 보라.
    """
    mask = np.asarray(mask_cells, dtype=np.int64).reshape(-1, 3)
    region = dilate_cells(mask, halo) if halo else mask
    mask_keys = _keys(region)

    # 🔴 점유 비교다. 해시 비교가 아니다 — 재디코딩하면 해시는 전부 다르다.
    new_rows = np.setdiff1d(_rows(result_cells), _rows(base_cells))
    res = np.asarray(result_cells, dtype=np.int64).reshape(-1, 3)
    is_new = np.isin(_rows(res), new_rows)
    # ⚠️ 마스크 밖 **기존** 복셀은 안 건드린다. 새로 생긴 것만 남긴다.
    outside = ~np.isin(_rows(res), _rows(region))
    new_outside = res[is_new & outside]

    kept, dropped, kept_cells = [], [], []
    for comp in components(new_outside) if len(new_outside) else []:
        (kept if comp.n_cells >= min_component else dropped).append(comp.n_cells)
    if len(new_outside):
        # 성분별 셀을 다시 얻어야 청크를 뽑을 수 있다. components() 는 요약만 내므로
        # 같은 26-연결을 셀 단위로 한 번 더 돈다 — 정의가 갈리지 않게 같은 함수를 쓴다.
        kept_cells = _cells_of_large_components(new_outside, min_component)
    kept_arr = np.asarray(kept_cells, dtype=np.int64).reshape(-1, 3)
    overflow_keys = _keys(kept_arr)

    return Bookkeeping(
        book=sorted(mask_keys | overflow_keys),
        mask_keys=sorted(mask_keys),
        overflow_keys=sorted(overflow_keys),
        threshold=int(min_component),
        kept_components=sorted(kept, reverse=True),
        dropped_components=sorted(dropped, reverse=True),
        new_outside_cells=new_outside,
        kept_cells=kept_arr,
    )


def _cells_of_large_components(cells: np.ndarray, min_size: int) -> np.ndarray:
    """26-연결 성분 중 `min_size` 이상인 것의 셀만. `components()` 와 같은 이웃 정의."""
    from .metrics import _NEIGHBORS_26, voxel_code

    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    index = {int(c): i for i, c in enumerate(voxel_code(a))}
    seen = np.zeros(a.shape[0], dtype=bool)
    out: List[np.ndarray] = []
    for start in range(a.shape[0]):
        if seen[start]:
            continue
        seen[start] = True
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            nb = a[i] + _NEIGHBORS_26
            nb = nb[np.all((nb >= 0) & (nb < 64), axis=1)]
            for code in voxel_code(nb):
                j = index.get(int(code))
                if j is not None and not seen[j]:
                    seen[j] = True
                    stack.append(j)
        if len(members) >= min_size:
            out.append(a[members])
    return np.concatenate(out, axis=0) if out else np.zeros((0, 3), dtype=np.int64)


@dataclass(frozen=True)
class DeltaResult:
    """🔴 `saving` 은 **같은 메시에서** 잰다. 섞으면 절감이 부풀려진다.

    W18 에서 실제로 밟은 함정: 델타 청크는 `runG.glb`(면 13,398)에서 인코딩하고
    승계 청크는 base `.cbin`(면 426,600)에서 그대로 물려받으면, 청크당 바이트가
    30배 차이 나서 절감이 **97.6%** 로 나온다. 그 값은 델타가 잘해서가 아니라
    **분모가 무거워서** 나온 것이다. 너무 깨끗한 숫자를 의심하라(방법론 5조 1번).

    그래서 분모는 `full_bytes` — **결과 메시 전체를 다시 보냈다면** 몇 바이트인가 —
    로 잡는다. 분자·분모가 같은 메시·같은 인코더를 지난다.
    `asset_bytes`(델타 + 실제 승계 바이트)는 자산 크기 맥락으로만 따로 낸다.
    """

    book: List[str]
    delta_bytes: int          # 보낸 것 (부기 청크, 결과 메시에서 인코딩)
    full_bytes: int           # 결과 메시를 **전부** 다시 보냈다면 (같은 메시·같은 인코더)
    inherited_bytes: int      # 승계한 부모 바이트 (자산 크기 맥락)
    asset_bytes: int          # 델타 + 승계 = 최종 자산 크기
    saving: float
    identity: float
    reencoded_identity: float
    n_delta_chunks: int
    n_inherited_chunks: int
    n_full_chunks: int
    child_blobs: Dict[str, bytes] = field(repr=False, default_factory=dict)


def assemble_delta(
    base_blobs: Dict[str, bytes],
    result_verts: np.ndarray,
    result_faces: np.ndarray,
    result_cells: np.ndarray,
    book: Sequence[str],
) -> DeltaResult:
    """부기 청크만 새로 인코딩하고 나머지는 **부모 바이트를 그대로 승계**한다.

    `reencoded_identity` 를 같이 내는 이유: 승계를 **안 했을 때** 무엇이 벌어지는지를
    같은 실행에서 보여주기 위해서다. 그 값이 곧 "재디코딩하면 해시가 전부 다르다"
    이고, 절감 0% 라는 말의 출처다. 두 숫자를 나란히 놔야 그 말이 무엇을 잰 것인지 보인다.
    """
    book = set(book)
    meshes = partition_mesh(result_verts, result_faces, voxel_cells=result_cells)
    fresh = {k: encode(m) for k, m in meshes.items()}

    child: Dict[str, bytes] = {}
    for k in set(base_blobs) | set(fresh):
        if k in book:
            if k in fresh:
                child[k] = fresh[k]          # 새로 인코딩해 보낸다
            # 부기에 들었는데 결과에 없다 = 비워진 청크. 전송 목록에서 빠진다.
        elif k in base_blobs:
            child[k] = base_blobs[k]         # 🔴 승계 — 바이트 그대로

    delta_bytes = sum(len(v) for k, v in child.items() if k in book)
    inherited_bytes = sum(len(v) for k, v in child.items() if k not in book)
    # 🔴 분모는 **결과 메시 전체**다. 승계 바이트(부모 메시)를 분모에 넣으면
    #    청크당 무게가 달라 절감이 부풀려진다 — 위 docstring 의 97.6% 가 그 값이다.
    full_bytes = sum(len(v) for v in fresh.values())

    return DeltaResult(
        book=sorted(book),
        delta_bytes=delta_bytes,
        full_bytes=full_bytes,
        inherited_bytes=inherited_bytes,
        asset_bytes=delta_bytes + inherited_bytes,
        saving=transfer_saving(full_bytes, delta_bytes) if full_bytes else 0.0,
        identity=inherited_byte_identity(base_blobs, child, book),
        # 승계 없이 전부 다시 인코딩했다면 몇 %가 부모와 바이트 동일한가.
        reencoded_identity=inherited_byte_identity(base_blobs, fresh, book),
        n_delta_chunks=sum(1 for k in child if k in book),
        n_inherited_chunks=sum(1 for k in child if k not in book),
        n_full_chunks=len(fresh),
        child_blobs=child,
    )
