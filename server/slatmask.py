"""마스크를 **slat 격자에서** 만든다 (D28). 표면 복셀화를 근거로 쓰지 않는다.

────────────────────────────────────────────────────────────────────────
🔴 왜 이 파일이 생겼나 — 같은 용을 두 세션이 다르게 봤다
────────────────────────────────────────────────────────────────────────
    3090 (표면 복셀화)   복셀 10,264 · 목 극소 z=45:32
    A5000 (slat coords)  복셀  9,591 · 목 극소 z=44:32 · z=45:28

청크 수(124)도 형상도 같은데 **인덱스가 한 칸 밀렸고 셀 수가 +673 늘었다.**
두 증상은 한 원인이다: `surface_voxelize` 는 디코딩된 **메시를 다시 래스터화**한다.
메시는 slat 셀 경계를 넘나들므로 원래 없던 셀이 붙고, 그 번짐이 프로파일을
한 칸 밀어 놓는다.

**정본은 slat coords 다.** VoxHammer 가 그 공간에서 동작하기 때문이다. 표면
복셀화는 진단용(그림 그리기)이고 마스크 좌표의 근거가 될 수 없다.

측정된 오차 (dragon-c, 청크-z 단위 — 매니페스트가 slat 정본):
    cz  slat  surface  차이
     0  1125     1187   +62      4  1429  1454   +25
     1  2327     2627  +300      5   563   592   +29   ← 목이 있는 띠
     2  1410     1550  +140      6   609   637   +28
     3  1840     1922   +82      7   288   295    +7
    합 9591    10264  +673 (+7.0%)

경계 근처에서 한 칸 밀리기에 충분하다. 그리고 이번 게이트의 "자연스럽게" 는
**목 연결부 품질**이라, 마스크를 극소점 **에서** 자르면(위가 아니라) 목을 물고
들어간다 — 그게 W6 에서 실제로 일어난 일이다.

────────────────────────────────────────────────────────────────────────
그래서 규칙이 아니라 **함수**로 막는다
────────────────────────────────────────────────────────────────────────
방법론 5조 4번: "규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다."
`build_head3_mask` 는 `source` 를 **명시적으로** 요구하고, `"surface"` 면 거부한다.
기본값을 두지 않는다 — 기본값이 있으면 다음 세션이 안 적고 지나간다.

⚠️ `.cbin` 에서 slat coords 를 복원할 수 없다. 포맷 실측(chunkbin.encode):
   본문 = positions + normals + colors + uvs + indices. **복셀 좌표는 없다** —
   헤더의 `voxel_count` 뿐이다. 정점 비닝(10,154)도 무게중심 비닝(10,071)도
   9,591 을 재현하지 못했다(청크별 일치 0/124, 26/124). slat coords 는
   **A5000 에서 받아야 한다.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    CHUNK_SIZE,
    VOXEL_RES,
    mask_fingerprint,
)

__all__ = [
    "SLAT",
    "SURFACE",
    "HeadMaskSpec",
    "NotSlatCoords",
    "chunk_z_profile",
    "neck_z",
    "build_head3_mask",
    "z_profile",
]

SLAT = "slat"
SURFACE = "surface"


class NotSlatCoords(ValueError):
    """표면 복셀화 좌표로 마스크를 만들려 했다 (D28).

    거부하는 이유: 표면 복셀화는 예외를 내지 않고 **조용히 한 칸 밀린 마스크**를
    만든다. 그 마스크로 잰 지표는 전부 다른 영역에 대한 숫자다 — D9 에서 이미
    본 실패 모양이고, 그때도 예외는 안 났다.
    """


@dataclass(frozen=True)
class HeadMaskSpec:
    """머리 마스크 사양. 격자 정본이 무엇이었는지를 **같이 들고 다닌다.**"""

    cells: np.ndarray
    neck_z: int
    neck_count: int
    box_x: Tuple[int, int]
    box_y: Tuple[int, int]
    box_z: Tuple[int, int]
    head_span_xy: Tuple[int, int]
    asset_cells_inside: int
    grid_source: str
    fingerprint: str

    @property
    def n_cells(self) -> int:
        return int(self.cells.shape[0])

    @property
    def grid_fraction(self) -> float:
        return self.n_cells / float(VOXEL_RES**3)

    @property
    def empty_cells(self) -> int:
        """D22② 의 핵심 — 머리 셋이 갈라져 나올 **빈 자리**."""
        return self.n_cells - self.asset_cells_inside

    @property
    def empty_fraction(self) -> float:
        return self.empty_cells / float(self.n_cells) if self.n_cells else 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "grid_source": self.grid_source,
            "neck_z": self.neck_z,
            "neck_count": self.neck_count,
            "box_x": list(self.box_x),
            "box_y": list(self.box_y),
            "box_z": list(self.box_z),
            "head_span_xy": list(self.head_span_xy),
            "mask_cells": self.n_cells,
            "mask_pct_of_grid": round(100.0 * self.grid_fraction, 3),
            "asset_cells_inside": self.asset_cells_inside,
            "empty_cells_inside": self.empty_cells,
            "empty_fraction": round(self.empty_fraction, 4),
            "fingerprint": self.fingerprint,
        }


def z_profile(cells: np.ndarray) -> Dict[int, int]:
    """z 슬라이스별 셀 수. 목 잘록함을 찾는 근거다 (D25)."""
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    zs, counts = np.unique(a[:, 2], return_counts=True)
    return {int(z): int(c) for z, c in zip(zs, counts)}


def chunk_z_profile(manifest: Dict) -> Dict[int, int]:
    """매니페스트 → 청크-z 별 slat 복셀 수. **slat 정본이다.**

    `.cbin` 에 복셀 좌표가 없으므로, slat 에 대해 로컬에서 확실히 아는 것은
    청크 단위 개수뿐이다 (8복셀 두께). 복셀 단위 프로파일은 A5000 이 줘야 한다.
    """
    prof: Dict[int, int] = {}
    for c in manifest["chunks"]:
        cz = int(str(c["chunk_id"]).split("_")[2])
        prof[cz] = prof.get(cz, 0) + int(c["voxel_count"])
    return prof


def neck_z(profile: Dict[int, int], *, search_from: float = 0.55) -> Tuple[int, int]:
    """목 잘록함의 z. **z 분위수가 아니라 단면 극소다** (D25).

    D25 가 생긴 이유: "머리 영역" 을 z 분위수로 정하면 날개가 섞인다 — W6 에서
    실제로 겪었다(마스크가 격자의 14.5% 로 부풀었다). 목은 형상의 성질이지
    높이의 성질이 아니다.

    Args:
        search_from: 자산 세로 구간의 이 지점 **위**에서만 극소를 찾는다.
                     아래쪽(다리·꼬리 사이)의 극소를 목으로 착각하지 않기 위한 것.
    """
    if not profile:
        raise ValueError("프로파일이 비었다")
    zs = sorted(profile)
    lo, hi = zs[0], zs[-1]
    floor = lo + (hi - lo) * search_from
    cand = {z: c for z, c in profile.items() if z >= floor and z <= hi - 4}
    if not cand:
        raise ValueError(f"극소 후보가 없다 (구간 {floor:.1f}–{hi - 4})")
    z = min(cand, key=lambda k: (cand[k], k))
    return int(z), int(cand[z])


def build_head3_mask(
    cells: np.ndarray,
    *,
    source: str,
    width_multiple: float = 1.0,
    manifest: Optional[Dict] = None,
) -> HeadMaskSpec:
    """slat 점유 → 머리 3개용 **체적** 마스크 (D22②).

    Args:
        cells:  (N,3) **slat 좌표**. 표면 복셀화 결과를 넣으면 거부된다.
        source: 반드시 `SLAT`. 기본값을 두지 않는다 — 기본값이 있으면 다음
                세션이 안 적고 지나가고, 그 순간 D28 이 무력해진다.
        width_multiple: 머리 폭의 몇 배씩 좌우로 넓힐지. 1.0 = 좌우 각각 머리
                폭만큼 → 총 3배 폭 (머리 3개 자리).
        manifest: 있으면 슬랫 총계와 대조해 입력이 정말 slat 인지 한 번 더 본다.

    Raises:
        NotSlatCoords: `source` 가 slat 이 아니거나, 매니페스트 총계와 어긋난다.
    """
    if source != SLAT:
        raise NotSlatCoords(
            f"source={source!r} — 마스크 좌표의 근거는 slat coords 여야 한다 (D28). "
            "표면 복셀화는 메시를 다시 래스터화하므로 셀이 번지고 프로파일이 "
            "한 칸 밀린다 (dragon-c 실측 +673셀 / +7.0%). 예외가 안 나므로 "
            "그 마스크로 잰 지표는 조용히 다른 영역에 대한 숫자가 된다."
        )

    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        raise ValueError("점유가 비었다")

    if manifest is not None:
        expected = int(manifest["voxel_count_total"])
        if a.shape[0] != expected:
            raise NotSlatCoords(
                f"셀 수가 매니페스트와 다르다: 입력 {a.shape[0]} vs slat 정본 {expected}. "
                "표면 복셀화 결과일 가능성이 높다 (D28)."
            )

    prof = z_profile(a)
    nz, ncount = neck_z(prof)

    # 🔴 목 **위**를 머리로 잡는다. 극소점 자체를 포함하면 목을 물고 들어가고,
    #    이번 게이트의 "자연스럽게"(= 목 연결부 품질)가 그 자리에서 깎인다.
    zlo, zhi = nz + 1, int(a[:, 2].max())
    head = a[a[:, 2] >= zlo]
    if head.size == 0:
        raise ValueError(f"목(z={nz}) 위에 머리가 없다")

    xlo, xhi = int(head[:, 0].min()), int(head[:, 0].max())
    ylo, yhi = int(head[:, 1].min()), int(head[:, 1].max())
    w = xhi - xlo + 1
    d = yhi - ylo + 1

    ex_lo = max(0, xlo - int(round(w * width_multiple)))
    ex_hi = min(VOXEL_RES - 1, xhi + int(round(w * width_multiple)))

    grid = np.zeros((VOXEL_RES, VOXEL_RES, VOXEL_RES), dtype=bool)
    grid[ex_lo : ex_hi + 1, ylo : yhi + 1, zlo : zhi + 1] = True
    mcells = np.argwhere(grid).astype(np.int64)

    codes_a = (a[:, 0] * VOXEL_RES + a[:, 1]) * VOXEL_RES + a[:, 2]
    codes_m = (mcells[:, 0] * VOXEL_RES + mcells[:, 1]) * VOXEL_RES + mcells[:, 2]
    inside = int(np.intersect1d(codes_a, codes_m).size)

    return HeadMaskSpec(
        cells=mcells,
        neck_z=nz,
        neck_count=ncount,
        box_x=(ex_lo, ex_hi),
        box_y=(ylo, yhi),
        box_z=(zlo, zhi),
        head_span_xy=(w, d),
        asset_cells_inside=inside,
        grid_source=SLAT,
        fingerprint=mask_fingerprint(mcells),
    )
