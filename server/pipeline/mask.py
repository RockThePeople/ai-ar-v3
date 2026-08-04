"""편집 마스크 — bbox 산출 + halo 팽창.

────────────────────────────────────────────────────────────────────────
🔴 팽창은 반드시 **클램프 뒤에** 한다
────────────────────────────────────────────────────────────────────────
`docs/PROGRESS.md` §4 실패 목록:

    Dilate 를 클램프보다 먼저   합성 입력엔 안 나오고 실제 제스처로만 터진다

왜 순서가 문제인가. `dilate_cells()` 는 팽창시킨 뒤 격자를 벗어난 좌표를
**버린다**(`coords.py`: `expanded[np.all((expanded >= 0) & (expanded < VOXEL_RES), ...)]`).
따라서 범위 밖 셀이 입력에 섞여 있으면:

    잘못된 순서   dilate(cells) → clamp    셀 x=65 → 팽창 64..66 → 전부 버려짐
                                            ⇒ 그 셀이 마스크에서 **소리 없이 사라진다**
    올바른 순서   clamp(cells) → dilate    셀 x=65 → 63 → 팽창 62..63 → 살아남는다

합성 bbox 입력은 애초에 범위 안이라 이 차이가 안 드러난다. 라쏘 제스처는 화면
가장자리에서 범위 밖 좌표를 만들고, 그때 마스크 경계 한 줄이 통째로 빠진다.
마스크가 빠진 자리는 편집이 안 되고, 부기에도 안 잡히므로 옛 기하가 그대로 남는다.

이 모듈의 모든 진입점은 `clamp_cells()` 를 먼저 거친다. 규칙을 문서로만 적으면
지켜지지 않는다는 것이 이 프로젝트의 방법론 5조 중 4번이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    VOXEL_RES,
    bbox_to_voxel_cells,
    canonical_sort,
    chunk_keys_sorted,
    dilate_cells,
    mask_fingerprint,
    voxel_to_chunk,
)
from deltacontract.errors import MaskEmpty  # type: ignore[import-not-found]

__all__ = [
    "HALO_DEFAULT",
    "MaskResult",
    "build_mask",
    "clamp_cells",
    "top_region_cells",
]

# FINAL §8.2. halo=0 은 계약 테스트가 "불충분" 으로 못박아 둔 값이다
# (conformance: test_halo_zero_is_insufficient).
HALO_DEFAULT = 1


@dataclass(frozen=True)
class MaskResult:
    """마스크 1건의 전체 상태. 지문까지 여기서 만든다.

    `cells` 는 halo **전**, `dilated` 는 halo **후**다. 계약이 두 지문을 구분해서
    요구하므로(`coords.mask_fingerprint` docstring) 둘 다 들고 다닌다.
    """

    cells: np.ndarray
    dilated: np.ndarray
    halo: int
    fingerprint: str
    fingerprint_dilated: str
    chunk_keys: List[str] = field(default_factory=list)

    @property
    def n_cells(self) -> int:
        return int(self.cells.shape[0])

    @property
    def n_dilated(self) -> int:
        return int(self.dilated.shape[0])


def clamp_cells(cells: np.ndarray) -> np.ndarray:
    """VOXEL 셀을 [0, 64) 안으로 **끌어당긴다**. 버리지 않는다.

    버리면(=필터링하면) 마스크가 조용히 줄어든다. 끌어당기면 경계 셀이 경계에
    붙을 뿐 개수가 보존된다 — 어느 쪽도 원본과 같지는 않지만, 후자는 눈에 보이고
    전자는 안 보인다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return np.zeros((0, 3), dtype=np.int64)
    return np.clip(a, 0, VOXEL_RES - 1)


def build_mask(
    cells: Optional[np.ndarray] = None,
    *,
    bbox: Optional[Sequence[Sequence[float]]] = None,
    halo: int = HALO_DEFAULT,
) -> MaskResult:
    """마스크를 만든다. 🔴 **클램프 → 팽창** 순서가 여기 한 곳에 고정돼 있다.

    Args:
        cells: (N,3) VOXEL 셀. 라쏘 결과 등. 범위 밖 좌표가 섞여 있어도 된다 —
               그게 이 함수가 존재하는 이유다.
        bbox:  (bbox_min, bbox_max) NORMALIZED AABB. `cells` 와 택일.
        halo:  26-이웃 팽창 반경.

    Raises:
        MaskEmpty: 마스크가 비었다. 조용히 빈 편집을 돌리면 "아무것도 안 했는데
                   성공했다" 가 되고, 그것이 이 프로젝트가 여섯 번 물린 모양이다.
    """
    if (cells is None) == (bbox is None):
        raise ValueError("cells 와 bbox 중 정확히 하나를 줘야 한다")
    if halo < 0:
        raise ValueError(f"halo 는 음수일 수 없다: {halo}")

    if bbox is not None:
        # bbox_to_voxel_cells 는 내부에서 이미 [0,64] 로 클램프한 뒤 dense 를 만든다.
        raw = bbox_to_voxel_cells(bbox[0], bbox[1])
    else:
        raw = np.asarray(cells, dtype=np.int64).reshape(-1, 3)

    # ── 순서 고정 지점 ───────────────────────────────────────────────
    clamped = clamp_cells(raw)                       # 1) 먼저 클램프
    clamped = np.unique(clamped, axis=0) if clamped.size else clamped
    if clamped.shape[0] == 0:
        raise MaskEmpty("마스크가 비었다 (클램프 후 셀 0개)")
    dilated = dilate_cells(clamped, halo)            # 2) 그 다음 팽창
    # ─────────────────────────────────────────────────────────────────

    return MaskResult(
        cells=canonical_sort(clamped),
        dilated=dilated,
        halo=int(halo),
        fingerprint=mask_fingerprint(clamped),
        fingerprint_dilated=mask_fingerprint(dilated),
        chunk_keys=chunk_keys_sorted(voxel_to_chunk(dilated)) if dilated.size else [],
    )


def top_region_cells(
    occupancy: np.ndarray,
    fraction: float = 0.35,
    *,
    axis: int = 2,
    per_slice: bool = True,
) -> np.ndarray:
    """자산 점유의 **위쪽 `fraction`** 을 덮는 마스크 셀. `find_head_bbox` 이식분.

    기준은 [0,64) 전체가 아니라 **자산의 실제 점유 구간**이다 — 전체 격자를 기준으로
    자르면 자산이 한쪽에 치우쳤을 때 아무것도 안 잡히거나 전부 잡힌다
    (`assemble.crop_rows` 와 같은 이유).

    ────────────────────────────────────────────────────────────────────
    🔴 `per_slice` — D11 부수 결정. 기본값이 True 다
    ────────────────────────────────────────────────────────────────────
    False 면 가로 방향으로 점유 bbox **전체**를 덮는 단일 직육면체가 나온다.
    눈사람은 머리가 몸통보다 훨씬 좁아서, 그러면 머리 위 허공까지 마스크가 된다 —
    W3 실측에서 마스크가 격자의 **21%(56,350셀)** 였다. 마스크가 그만큼 크면
    "국소 편집" 이라는 전제 자체가 성립하지 않고, 전송 절감도 과대평가된다.

    True 면 `axis` 방향 **슬라이스마다** 그 슬라이스의 점유 bbox 를 따로 잡는다.
    결과는 형상을 계단 모양으로 감싸는 마스크다.

    ⚠️ 대가: 마스크가 형상에 의존하게 된다. 자산이 다르면 마스크도 다르므로
       **자산 간 계측을 직접 비교하지 마라.** 같은 자산의 before/after 비교는 무관하다.
    """
    a = np.asarray(occupancy, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        raise MaskEmpty("점유가 비었다 — 마스크를 만들 수 없다")
    if not (0.0 < fraction <= 1.0):
        raise ValueError(f"fraction 은 (0,1] 이어야 한다: {fraction}")
    if axis not in (0, 1, 2):
        raise ValueError(f"axis 는 0/1/2 여야 한다: {axis}")

    from deltacontract.coords import dense_cells  # noqa: PLC0415

    lo = a.min(axis=0)
    hi = a.max(axis=0)
    span = int(hi[axis] - lo[axis])
    cut = int(np.ceil(lo[axis] + span * (1.0 - fraction)))
    if hi[axis] + 1 <= cut:
        raise MaskEmpty(f"위쪽 {fraction:.0%} 영역이 비었다")

    if not per_slice:
        box_lo, box_hi = lo.copy(), hi.copy() + 1
        box_lo[axis] = cut
        return dense_cells(box_lo, box_hi)

    others = [i for i in (0, 1, 2) if i != axis]
    parts = []
    for s in range(cut, int(hi[axis]) + 1):
        sl = a[a[:, axis] == s]
        if sl.shape[0] == 0:
            continue  # 그 높이에 아무것도 없으면 마스크도 없다
        box_lo = np.empty(3, dtype=np.int64)
        box_hi = np.empty(3, dtype=np.int64)
        box_lo[axis], box_hi[axis] = s, s + 1
        for i in others:
            box_lo[i] = int(sl[:, i].min())
            box_hi[i] = int(sl[:, i].max()) + 1
        parts.append(dense_cells(box_lo, box_hi))

    if not parts:
        raise MaskEmpty(f"위쪽 {fraction:.0%} 영역이 비었다")
    return canonical_sort(np.unique(np.concatenate(parts, axis=0), axis=0))
