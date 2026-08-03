"""조립 — 마스크 자리를 비우고 기증자를 끼워 넣는다.

`contract/python/deltacontract/assemble.py` 의 **얇은 래퍼**다. 크롭·배치·부기 규칙은
전부 계약이 소유하고, 여기서는 그 함수들을 정해진 순서로 부르고 계측만 붙인다.

────────────────────────────────────────────────────────────────────────
스케일 인자는 없다. 앞으로도 없다
────────────────────────────────────────────────────────────────────────
좌표를 배로 늘리면 이웃이 이웃이 아니게 되고 디코더가 고립 복셀마다 조각난 표면을
만든다 — 실측 6-이웃 유지율 s=1.5 → 50%, s=2.0 → **0%**. 소수 평행이동도 같은 병이다
(+0.5 에서 4,110 → 914복셀, 78% 소실).

  크기는 `donor_crop_fraction` 으로만 고른다.
  위치는 정수 평행이동으로만 고른다.

둘 다 `place_cells()` 가 런타임에 거부한다. 이 래퍼가 우회로를 만들지 않는다.

────────────────────────────────────────────────────────────────────────
점유 교집합 로그 (§5 S2-7)
────────────────────────────────────────────────────────────────────────
"비우기" 단계가 실제로 몇 셀을 지웠는지를 `SpliceResult.n_cleared_occupied` 로 낸다.
이 값이 0 이면 비우기가 아무 일도 안 한 것이고, 그러면 옛 기하가 그대로 남는다 —
결과는 "호박이 눈사람 머리에 겹쳐 박힌" 모양이 된다. 숫자가 없으면 이 실패는
육안으로만 발견되고, 그때는 이미 늦다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from deltacontract.assemble import (  # type: ignore[import-not-found]
    AssemblyError,
    crop_rows,
    fit_offset,
    place_cells,
)
from deltacontract.coords import (  # type: ignore[import-not-found]
    canonical_sort,
    voxel_code,
)

from .mask import MaskResult

__all__ = ["SpliceResult", "splice"]


@dataclass(frozen=True)
class SpliceResult:
    """조립 1회의 결과와 그 과정에서 나온 계측치."""

    cells: np.ndarray            # 결과 점유 (N,3)
    donor_placed: np.ndarray     # 배치된 기증자 셀 (M,3) — 부기의 zone1
    emptied: np.ndarray          # 비운 영역 (마스크+halo) — 부기의 zone2 원천
    offset: list                 # 적용된 정수 평행이동
    crop_fraction: float

    n_base: int                  # 원본 점유 셀 수
    n_donor_cropped: int         # 크롭 후 기증자 셀 수
    n_cleared_occupied: int      # 🔴 비우기가 실제로 지운 점유 셀 수 (§5 S2-7)
    n_donor_outside_mask: int    # 기증자가 마스크 밖으로 삐져나간 셀 수
    n_donor_overlap_kept: int    # 기증자가 보존 영역과 겹친 셀 수

    @property
    def n_result(self) -> int:
        return int(self.cells.shape[0])


def _codes(cells: np.ndarray) -> np.ndarray:
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return np.zeros((0,), dtype=np.int64)
    return voxel_code(a)


def splice(
    base_cells: np.ndarray,
    donor_cells: np.ndarray,
    mask: MaskResult,
    *,
    crop_fraction: float = 1.0,
    crop_axis: int = 2,
    crop_keep: str = "top",
    offset: Optional[Sequence[int]] = None,
    seat_axis: int = 2,
    strict_containment: bool = False,
) -> SpliceResult:
    """마스크 자리를 비우고 기증자를 끼워 넣는다.

    Args:
        base_cells:  (N,3) 대상 자산의 VOXEL 점유.
        donor_cells: (M,3) 기증자 자산의 VOXEL 점유.
        mask:        `build_mask()` 결과. `mask.dilated` 가 비울 영역이다.
        crop_fraction: 기증자에서 가져올 비율. **크기 조절은 이것뿐이다.**
        offset:      정수 평행이동. None 이면 `fit_offset` 이 관례 배치를 계산한다.
        strict_containment: True 면 기증자가 마스크 밖으로 나갈 때 예외를 던진다.
                     마스크 밖으로 나간 기증자 셀은 **보존(B)을 직접 깨므로**,
                     보존을 게이트로 재는 실험에서는 켜라.

    Returns:
        SpliceResult. 결과 점유는 canonical 순이다.
    """
    base = np.asarray(base_cells, dtype=np.int64).reshape(-1, 3)
    donor = np.asarray(donor_cells, dtype=np.int64).reshape(-1, 3)
    if base.size == 0:
        raise AssemblyError("base 점유가 비었다")
    if donor.size == 0:
        raise AssemblyError("donor 점유가 비었다")

    # 1) 기증자 크롭 — 크기는 여기서만 정해진다 (스케일 없음)
    keep_rows = crop_rows(donor, crop_fraction, axis=crop_axis, keep=crop_keep)
    donor_crop = donor[keep_rows]
    if donor_crop.shape[0] == 0:
        raise AssemblyError(
            f"크롭 결과가 비었다 (fraction={crop_fraction}, axis={crop_axis}, "
            f"keep={crop_keep!r})"
        )

    # 2) 배치 — 정수 평행이동만. place_cells 가 소수·범위이탈·중복을 전부 거부한다.
    #    ⚠️ offset 을 int() 로 **강제변환하지 않는다.** 0.5 를 0 으로 잘라 버리면
    #       계약의 거부가 우회되고, 호출부는 자기가 요청한 것과 다른 배치를 받는다.
    #       소수가 들어오면 여기서 터지는 것이 맞다 (실측 +0.5 에서 4,110→914복셀).
    off = list(fit_offset(donor_crop, mask.cells, seat_axis=seat_axis)) \
        if offset is None else list(offset)
    placed = place_cells(donor_crop, off)

    # 3) 비우기 — 마스크+halo 영역의 점유를 지운다
    emptied = np.asarray(mask.dilated, dtype=np.int64).reshape(-1, 3)
    base_c = _codes(base)
    empt_c = _codes(emptied)
    cleared_hit = np.isin(base_c, empt_c)
    n_cleared_occupied = int(cleared_hit.sum())
    kept = base[~cleared_hit]

    # 4) 합치기
    placed_c = _codes(placed)
    kept_c = _codes(kept)
    n_donor_outside_mask = int((~np.isin(placed_c, empt_c)).sum())
    n_donor_overlap_kept = int(np.isin(placed_c, kept_c).sum())

    if strict_containment and n_donor_outside_mask:
        raise AssemblyError(
            f"기증자 {n_donor_outside_mask}셀이 마스크 밖으로 나갔다. 그대로 두면 "
            "'마스크 밖 불변' 이 정의상 깨진다 — 크롭 비율을 낮추거나 마스크를 넓혀라."
        )

    result = np.concatenate([kept, placed], axis=0) if placed.size else kept
    result = np.unique(result, axis=0) if result.size else result.reshape(0, 3)

    return SpliceResult(
        cells=canonical_sort(result),
        donor_placed=canonical_sort(placed),
        emptied=canonical_sort(emptied),
        offset=off,
        crop_fraction=float(crop_fraction),
        n_base=int(base.shape[0]),
        n_donor_cropped=int(donor_crop.shape[0]),
        n_cleared_occupied=n_cleared_occupied,
        n_donor_outside_mask=n_donor_outside_mask,
        n_donor_overlap_kept=n_donor_overlap_kept,
    )
