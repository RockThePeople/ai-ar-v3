"""게이트 판정 — **단독 실행판**. 리포 없이 돈다 (numpy 만).

`server/metrics.py` 의 게이트 조건 중 A5000 이 실제로 쓰는 것만 뽑았다.
계측(IoU·churn·hue)은 A5000 이 자기 데이터로 하고, 여기서는 **판정 규칙**을 준다 —
규칙이 갈라지는 것이 W11 에서 실제로 일어난 일이다.

담는 것:
    D38   op 별 방향 조건       — W10 이 통과한 틈
    D33   자산·영역별 바닥값     — 맨 float 거부
    D37   halo 는 원시 개수+육안 — 비율 단독 판정 거부

⚠️ 정본은 `server/metrics.py` 다. 이 파일은 그 부분집합이고, 갈라짐은
   `server/tests/test_handoff.py` 가 대조로 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

__all__ = [
    "DIRECTION_RULES",
    "DirectionMismatch",
    "BaselineMisapplied",
    "RatioWithoutResolution",
    "NoiseFloor",
    "VoxelDelta",
    "check_direction",
    "halo_verdict",
]


class DirectionMismatch(RuntimeError):
    """편집 방향이 op 가 요구하는 것과 반대다 (D38)."""


class BaselineMisapplied(RuntimeError):
    """다른 자산·영역의 바닥값을 쓰려 했다 (D33)."""


class RatioWithoutResolution(RuntimeError):
    """표본이 작아 비율에 유효숫자가 없다 (D37)."""


@dataclass(frozen=True)
class VoxelDelta:
    new: int
    removed: int

    @property
    def net(self) -> int:
        return self.new - self.removed


@dataclass(frozen=True)
class NoiseFloor:
    """바닥값 + **어디서 쟀는가**. 맨 float 으로 다니면 자산 경계를 소리 없이 넘는다."""

    value: float
    asset_id: str
    region: str = "global"
    n_voxels: int = 0

    def __post_init__(self) -> None:
        if not str(self.asset_id).strip():
            raise BaselineMisapplied(
                "바닥값에 asset_id 가 없다 (D33). 소년상 0.0701 을 dragon-c(0.2229)에 "
                "쓰면 3.2배 과대평가된다."
            )

    def require(self, asset_id: str, region: Optional[str] = None) -> None:
        if asset_id != self.asset_id:
            raise BaselineMisapplied(
                f"바닥값은 {self.asset_id!r} 것인데 {asset_id!r} 에 쓰려 한다 (D33)."
            )
        if region is not None and region != self.region:
            raise BaselineMisapplied(
                f"바닥값은 영역 {self.region!r} 것인데 {region!r} 에 쓰려 한다 "
                "(D33/D36). halo 대역과 z 대역은 서로 다른 공간이다."
            )


DIRECTION_RULES: Dict[str, Tuple[Optional[str], Optional[Callable]]] = {
    "add":            ("신규 > 제거", lambda d: d.new > d.removed),
    "remove":         ("제거 > 신규", lambda d: d.removed > d.new),
    "replace_region": (None, None),
    "recolor":        ("기하 불변 (신규 = 제거 = 0)", lambda d: d.new == 0 and d.removed == 0),
}


def check_direction(op: str, delta: VoxelDelta) -> None:
    """op 가 요구하는 방향을 지켰는지 (D38). 어기면 예외.

    W10: 연결성분 1.000 으로 통과했는데 제거 730 > 신규 304 였다 — 머리를 먹었다.
    """
    if op not in DIRECTION_RULES:
        raise KeyError(f"op={op!r} 의 방향 규칙이 없다 (D38).")
    desc, pred = DIRECTION_RULES[op]
    if pred is None:
        return
    if not pred(delta):
        raise DirectionMismatch(
            f"op={op!r} 는 {desc} 를 요구하는데 신규 {delta.new} / 제거 "
            f"{delta.removed} (순증 {delta.net:+d}) 다 (D38)."
        )


def halo_verdict(
    *,
    n_new: int,
    n_removed: int,
    n_union: int,
    baseline: NoiseFloor,
    visual_confirmed: Optional[bool] = None,
) -> bool:
    """halo 판정 (D37). **비율 단독으로는 판정하지 않는다.**

    대역 합집합이 45~48복셀이라 복셀 하나가 2% 다 — 초과배수에 유효숫자가 없다.
    """
    if visual_confirmed is not None:
        return bool(visual_confirmed)
    resolution = 1.0 / n_union if n_union else float("inf")
    if resolution > 0.01:
        raise RatioWithoutResolution(
            f"합집합 {n_union}복셀 — 복셀 하나가 {resolution * 100:.1f}% 다 (D37). "
            f"원시 개수 신규 {n_new} / 제거 {n_removed}. 판정은 깊이맵 육안으로 한다."
        )
    return ((n_new + n_removed) / n_union) <= baseline.value
