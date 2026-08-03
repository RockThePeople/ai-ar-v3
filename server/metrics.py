"""D5 지표 — 효능(A) · 보존(B) · 절감(C) 를 숫자로 만든다.

`docs/PROGRESS.md` §2 D5 를 코드로 옮긴 것이다. 이 파일이 이 프로젝트가 **여섯 번
물린 자리**다. 두 가지 병이 있고 둘은 같은 병이다:

    보존만 재면    아무것도 안 하는 구현이 전부 통과한다
    지표를 잘못 고르면   제대로 도는 구현이 탈락한다

────────────────────────────────────────────────────────────────────────
🔴 전역 평균 실루엣 대칭차는 **판정 지표가 아니다** (D5 로 폐기)
────────────────────────────────────────────────────────────────────────
두 방향으로 눈이 멀었다:

  ① 국소 마스크 편집에 둔감 — 마스크가 실루엣의 18%면 나머지 82%가 평균을 희석한다
  ② 시선 방향 돌출에 둔감 — 카메라 쪽으로 튀어나온 주둥이는 외곽선을 안 건드려
     정면 대칭차가 0.16%로 **최저**였다. 옆면에서만 14.3%로 잡혔다

W1/A5000 실측: 같은 편집에 대해 전역 2.58% vs 마스크·최대뷰 14.32% — **5.5배**.
그 게이트를 그대로 뒀다면 "VoxHammer 는 형태를 못 바꾼다" 로 기록될 뻔했다.

그래서 이 모듈은 판정 지표와 참고 수치를 **이름과 상수로 분리**한다.
`GATE_METRICS` 에 없는 것은 게이트에 쓰지 않는다. 특히 `reference_*` 접두가
붙은 함수는 로그와 대화용이고, 통과/탈락을 정하지 않는다.

────────────────────────────────────────────────────────────────────────
왜 효능-필수가 "신규 복셀 수" 인가
────────────────────────────────────────────────────────────────────────
레벨2(형태 변경)의 **정의 그 자체**이기 때문이다. RePaint-lite 는 `coords` 를
고정하므로 복셀을 추가할 수 없었고(활성 복셀 100% 재샘플링으로도 실루엣 0.46%),
그래서 이 값이 구조적으로 0 이었다. 0 이면 아무것도 안 한 것이다 —
카메라 각도로도, 평균으로도 가려지지 않는다.

────────────────────────────────────────────────────────────────────────
효능과 보존은 **다른 영역**에서 잰다. 의도적이다
────────────────────────────────────────────────────────────────────────
  효능   사용자가 지정한 **원본 마스크** 안에서 잰다 — 요구한 자리가 바뀌었는가
  보존   halo 까지 팽창시킨 **부기 영역 밖**에서 잰다 — 건드리겠다고 선언하지
         않은 자리가 그대로인가

halo 를 보존 영역에 넣으면 "우리가 지우겠다고 한 자리" 를 지웠다고 탈락한다.
halo 를 효능 영역에 넣으면 마스크 경계 바깥의 변화가 효능으로 계상된다.
둘 다 틀리다. 그래서 호출부가 두 영역을 명시적으로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Sequence

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    VOXEL_RES,
    voxel_code,
)

__all__ = [
    "GATE_METRICS",
    "REFERENCE_METRICS",
    "MetricReport",
    "efficacy_iou_in_mask",
    "efficacy_new_voxels",
    "evaluate",
    "preservation_byte_identity",
    "preservation_iou_out",
    "reference_silhouette_global_mean",
    "silhouette_masked_max",
    "transfer_saving",
]

# 판정에 쓰는 지표. 이 목록 밖의 수치로 게이트를 만들지 않는다 (D5).
GATE_METRICS = (
    "efficacy_new_voxels",
    "efficacy_iou_in_mask",
    "preservation_iou_out",
    "preservation_byte_identity",
    "transfer_saving",
)

# 로그·대화용. 통과/탈락을 정하지 않는다.
REFERENCE_METRICS = (
    "reference_silhouette_global_mean",
    "silhouette_masked_max",
)


# ══════════════════════════════════════════════════════════════ 집합 도구
def _codes(cells: Optional[np.ndarray]) -> np.ndarray:
    """VOXEL 셀 → 유일 정수 코드. 집합 연산을 좌표 튜플이 아니라 정수로 한다."""
    if cells is None:
        return np.zeros((0,), dtype=np.int64)
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return np.zeros((0,), dtype=np.int64)
    return np.unique(voxel_code(a))


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    """두 복셀 집합의 IoU. **둘 다 비면 1.0** (변화 없음이 곧 완전 일치다)."""
    if a.size == 0 and b.size == 0:
        return 1.0
    inter = np.intersect1d(a, b, assume_unique=True).size
    union = np.union1d(a, b).size
    return float(inter) / float(union) if union else 1.0


# ══════════════════════════════════════════════════════════════ 효능 (A)
def efficacy_new_voxels(
    before: np.ndarray, after: np.ndarray, mask: np.ndarray
) -> int:
    """★ 효능-필수. 마스크 영역에서 **없다가 생긴** 복셀 수.

    레벨2(형태 변경)의 정의 그 자체다. 0 이면 아무것도 안 한 것이고, 그 판정은
    카메라 각도·평균·해상도 어느 것으로도 뒤집히지 않는다.

    Args:
        mask: 사용자가 지정한 **원본 마스크** (halo 전). 모듈 docstring 참고.
    """
    m = _codes(mask)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return int(np.setdiff1d(a, b, assume_unique=True).size)


def efficacy_removed_voxels(
    before: np.ndarray, after: np.ndarray, mask: np.ndarray
) -> int:
    """마스크 영역에서 **있다가 사라진** 복셀 수. 신규 복셀의 짝.

    게이트는 아니지만 같이 봐야 한다 — 추가만 있고 제거가 0 이면 기증자가 옛 기하
    위에 겹쳐 박힌 것이다 (비우기 단계가 일을 안 했다는 뜻).
    """
    m = _codes(mask)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return int(np.setdiff1d(b, a, assume_unique=True).size)


def efficacy_iou_in_mask(
    before: np.ndarray, after: np.ndarray, mask: np.ndarray
) -> float:
    """★ 효능-정도. 마스크 영역 복셀 점유 IoU(before, after).

    **낮을수록 많이 바뀐 것이다.** D5 기준선은 < 0.8.
    1.0 이면 마스크 안이 완전히 그대로다.
    """
    m = _codes(mask)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return _iou(b, a)


# ══════════════════════════════════════════════════════════════ 보존 (B)
def preservation_iou_out(
    before: np.ndarray, after: np.ndarray, mask: np.ndarray
) -> float:
    """★ 보존. 마스크 **밖** 복셀 점유 IoU. D5 기준선은 > 0.99.

    Args:
        mask: 부기 영역 = **halo 까지 팽창시킨** 마스크. 모듈 docstring 참고.
    """
    m = _codes(mask)
    b = np.setdiff1d(_codes(before), m, assume_unique=True)
    a = np.setdiff1d(_codes(after), m, assume_unique=True)
    return _iou(b, a)


def preservation_byte_identity(
    parent_blobs: Mapping[str, bytes],
    child_blobs: Mapping[str, bytes],
    book: Iterable[str],
) -> float:
    """★ 보존. 부기 밖 청크의 **바이트 동일률** [0, 1].

    분모는 부모가 갖고 있던 부기 밖 청크 수다. 자식에 없거나 바이트가 다르면
    동일하지 않은 것으로 센다.

    ⚠️ 이 지표가 100% 인 것은 디코딩이 재현적이어서가 아니라 **부모 바이트를
       승계했기 때문**이다 (`pipeline/package.py`). 재디코딩본을 쓰면 실측에서
       152/152 청크가 전부 다른 해시를 냈다 — 기하는 중앙값 0.0002 메시셀 차이였다.
       즉 이 값이 100% 가 아니면 승계 경로가 깨진 것이지 기하가 바뀐 것이 아니다.
    """
    outside = [k for k in parent_blobs if k not in set(book)]
    if not outside:
        return 1.0
    same = sum(1 for k in outside if child_blobs.get(k) == parent_blobs[k])
    return same / len(outside)


# ══════════════════════════════════════════════════════════════ 절감 (C)
def transfer_saving(full_bytes: int, delta_bytes: int) -> float:
    """★ 절감. 1 - (보낸 바이트 / 전체 재전송 바이트). [0, 1].

    ⚠️ 이 값 하나만 보면 **아무것도 안 보내는 구현이 100% 를 받는다.** 반드시
       효능 지표와 **같이** 판정한다 — 그것이 D5 와 방법론 5조 3번의 요지다.
    """
    if full_bytes <= 0:
        raise ValueError(f"full_bytes 가 0 이하다: {full_bytes}")
    if delta_bytes < 0:
        raise ValueError(f"delta_bytes 가 음수다: {delta_bytes}")
    return 1.0 - (float(delta_bytes) / float(full_bytes))


# ══════════════════════════════════════════════════════ 실루엣 (참고·보조)
def _silhouette(codes: np.ndarray, axis: int) -> np.ndarray:
    """복셀 코드 집합을 축에 수직인 평면으로 투영한 2D 불리언 마스크."""
    grid = np.zeros((VOXEL_RES, VOXEL_RES, VOXEL_RES), dtype=bool)
    if codes.size:
        x = codes // (VOXEL_RES * VOXEL_RES)
        y = (codes // VOXEL_RES) % VOXEL_RES
        z = codes % VOXEL_RES
        grid[x, y, z] = True
    return grid.any(axis=axis)


def _sym_diff_ratio(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 0.0
    return float(np.logical_xor(a, b).sum()) / float(union)


def reference_silhouette_global_mean(
    before: np.ndarray, after: np.ndarray
) -> float:
    """⚠️ **참고 수치. 게이트에 쓰지 마라** (D5 로 폐기된 지표).

    전역 실루엣 대칭차의 축 평균. 국소 편집과 시선 방향 돌출 양쪽에 둔감하다.
    로그에 남기는 이유는 과거 계측치(2.58%)와 이어 보기 위해서일 뿐이다.
    """
    b, a = _codes(before), _codes(after)
    return float(
        np.mean([_sym_diff_ratio(_silhouette(b, ax), _silhouette(a, ax))
                 for ax in (0, 1, 2)])
    )


def silhouette_masked_max(
    before: np.ndarray, after: np.ndarray, mask: np.ndarray
) -> float:
    """보조 지표. 마스크 영역에 한정한 실루엣 대칭차의 **최대뷰**. 평균이 아니다.

    D5 기준선은 > 10%. 최대뷰를 쓰는 이유는 W1 실측 때문이다 — 시선 방향으로
    튀어나온 주둥이는 정면에서 0.16%, 옆면에서 14.3% 였다. 평균을 쓰면 그 편집이
    사라진다.
    """
    m = _codes(mask)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return max(
        _sym_diff_ratio(_silhouette(b, ax), _silhouette(a, ax)) for ax in (0, 1, 2)
    )


# ══════════════════════════════════════════════════════════════ 종합
@dataclass(frozen=True)
class MetricReport:
    """한 번의 편집에 대한 전체 계측. 게이트 판정은 `gate_g2()` 가 한다."""

    efficacy_new_voxels: int
    efficacy_removed_voxels: int
    efficacy_iou_in_mask: float
    preservation_iou_out: float
    preservation_byte_identity: float
    transfer_saving: float
    reference: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "efficacy_new_voxels": self.efficacy_new_voxels,
            "efficacy_removed_voxels": self.efficacy_removed_voxels,
            "efficacy_iou_in_mask": self.efficacy_iou_in_mask,
            "preservation_iou_out": self.preservation_iou_out,
            "preservation_byte_identity": self.preservation_byte_identity,
            "transfer_saving": self.transfer_saving,
            "reference": dict(self.reference),
        }

    def gate_g2(
        self,
        *,
        min_new_voxels: int = 1,
        max_iou_in_mask: float = 0.8,
        min_iou_out: float = 0.99,
        min_byte_identity: float = 1.0,
        min_transfer_saving: float = 0.40,
    ) -> Dict[str, bool]:
        """G2 판정. **효능·보존·절감이 동시에** 성립해야 한다.

        참고 수치(`reference`)는 판정에 들어가지 않는다 — 들어가면 D5 가 폐기한
        지표가 뒷문으로 게이트에 복귀한다.
        """
        return {
            "efficacy": (
                self.efficacy_new_voxels >= min_new_voxels
                and self.efficacy_iou_in_mask < max_iou_in_mask
            ),
            "preservation": (
                self.preservation_iou_out > min_iou_out
                and self.preservation_byte_identity >= min_byte_identity
            ),
            "saving": self.transfer_saving > min_transfer_saving,
        }


def evaluate(
    *,
    before: np.ndarray,
    after: np.ndarray,
    mask_cells: np.ndarray,
    book_region_cells: np.ndarray,
    parent_blobs: Mapping[str, bytes],
    child_blobs: Mapping[str, bytes],
    book: Sequence[str],
    full_bytes: int,
    delta_bytes: int,
) -> MetricReport:
    """전 지표를 한 번에 계산한다.

    Args:
        mask_cells:        효능을 재는 영역 — 사용자가 지정한 원본 마스크.
        book_region_cells: 보존을 재는 영역의 여집합 — halo 까지 팽창시킨 마스크.
    """
    return MetricReport(
        efficacy_new_voxels=efficacy_new_voxels(before, after, mask_cells),
        efficacy_removed_voxels=efficacy_removed_voxels(before, after, mask_cells),
        efficacy_iou_in_mask=efficacy_iou_in_mask(before, after, mask_cells),
        preservation_iou_out=preservation_iou_out(before, after, book_region_cells),
        preservation_byte_identity=preservation_byte_identity(
            parent_blobs, child_blobs, book
        ),
        transfer_saving=transfer_saving(full_bytes, delta_bytes),
        reference={
            "silhouette_global_mean": reference_silhouette_global_mean(before, after),
            "silhouette_masked_max": silhouette_masked_max(before, after, mask_cells),
        },
    )
