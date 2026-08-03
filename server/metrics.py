"""D5 / D5-a / D5-b 지표 — 효능(A) · 보존(B) · 절감(C) 를 숫자로 만든다.

`docs/PROGRESS.md` §2 D5·D5-a·D5-b 를 코드로 옮긴 것이다. 이 파일이 이 프로젝트가
**여섯 번 물린 자리**다. 두 가지 병이 있고 둘은 같은 병이다:

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

`GATE_METRICS` 에 없는 것은 게이트에 쓰지 않는다. `reference_*` 접두가 붙은 함수는
로그와 대화용이고 통과/탈락을 정하지 않는다.

────────────────────────────────────────────────────────────────────────
D5-a — W2/A5000 이 자기 맹점을 먼저 등록했고 6건 중 5건이 실제로 발동했다
────────────────────────────────────────────────────────────────────────
  ① 신규 복셀 수는 **삭제에 눈이 멀다**    신규 274 뒤에 제거 245 (순증 +29)
     ⇒ 신규·제거를 항상 **쌍으로** 반환한다 (`VoxelDelta`)
  ② 신규 복셀은 **공간 분포에 눈이 멀다**  273/274 가 단일 연결성분이었다.
     이게 없으면 "표면 잡음 274개" 와 구분이 안 된다
     ⇒ ★ `efficacy_largest_component` 를 **필수 지표로 승격**
  ③ 마스크 밖 IoU 는 1.0 이 될 수 없다     실측 0.853. 리메싱 잡음이다
     ⇒ 절대값 해석 금지. 잡음 바닥값 대비로만 읽는다 (D5-b)
  ④ 표면 복셀화는 신규 수를 부풀린다       마스크 **밖에서도** 신규 533/제거 478
     ⇒ ★ `churn_ratio` 로 정규화. 실측 4.97배
  ⑤ 최대뷰는 이상치에 취약                 상위 5뷰 15.3~16.5% — 단독 이상치는 아니었다

────────────────────────────────────────────────────────────────────────
D5-b — 🔴 바이트 동일률은 **승계 청크에만** 쓴다
────────────────────────────────────────────────────────────────────────
맥북의 합성 디코더는 면 컬링 없음 + 복셀 안쪽 들여쓰기라 기하가 **복셀-국소**다.
그래서 "마스크 밖 바이트 100%" 가 구조적으로 성립한다. 진짜 TRELLIS 디코더는
receptive field 때문에 그 성질이 **없다** — A5000 의 마스크 밖 IoU 0.853 이 그것이다.

    보존-A  `inherited_byte_identity`        승계 청크. 우리가 안 건드렸으니 **항진명제**.
                                             회귀 검사용이지 보존의 증거가 아니다
    보존-B  `preservation_geometry_distance` 재디코딩 영역. **이게 진짜 보존 지표**
    보존-C  `churn_ratio` 의 분모              잡음 바닥값 수준인지

🔴 보존-B 는 **잡음 바닥값(baseline) 없이 판정하지 않는다.** 바닥값은 편집 없이
   인코드→디코드만 왕복시킨 대조군에서 나오고, 그건 W3-A5000 에 배정돼 있다.
   추정값으로 통과시키면 0.853 의 13% 가 잡음인지 누출인지 못 가른 채
   "보존됨" 이라고 적게 된다. 그래서 `baseline=None` 이면 판정을 **거부**한다.
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
    "NoiseFloorUnknown",
    "PreservationDistance",
    "VoxelDelta",
    "churn_ratio",
    "efficacy_iou_in_mask",
    "efficacy_largest_component",
    "efficacy_largest_component_of_result",
    "efficacy_voxel_delta",
    "evaluate",
    "inherited_byte_identity",
    "preservation_geometry_distance",
    "preservation_iou_out",
    "reference_silhouette_global_mean",
    "silhouette_masked_max",
    "transfer_saving",
]

# 판정에 쓰는 지표. 이 목록 밖의 수치로 게이트를 만들지 않는다 (D5).
# rev6 §5 S2 의 G2 문구와 1:1 이다.
GATE_METRICS = (
    "efficacy_new_voxels",           # > 0
    "efficacy_largest_component",    # ≥ 0.8
    "churn_ratio",                   # ≥ 3.0
    "inherited_byte_identity",       # == 1.0
    "preservation_geometry_distance",  # ≤ 잡음 바닥값 (baseline 필수)
    "transfer_saving",               # > 0.40
)

# 로그·대화용. 통과/탈락을 정하지 않는다.
REFERENCE_METRICS = (
    "reference_silhouette_global_mean",
    "silhouette_masked_max",
    "efficacy_iou_in_mask",
    "efficacy_largest_component_of_result",
    "preservation_iou_out",
)


class NoiseFloorUnknown(RuntimeError):
    """잡음 바닥값 없이 보존을 판정하려 했다. 추정값으로 통과시키지 않는다."""


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


def _decode(codes: np.ndarray) -> np.ndarray:
    """정수 코드 → (N,3) 셀."""
    if codes.size == 0:
        return np.zeros((0, 3), dtype=np.int64)
    x = codes // (VOXEL_RES * VOXEL_RES)
    y = (codes // VOXEL_RES) % VOXEL_RES
    z = codes % VOXEL_RES
    return np.stack([x, y, z], axis=1).astype(np.int64)


# ══════════════════════════════════════════════════════════════ 효능 (A)
@dataclass(frozen=True)
class VoxelDelta:
    """신규·제거를 **쌍으로** 들고 다닌다 (D5-a ①).

    신규만 보면 삭제에 눈이 먼다 — A5000 실측에서 신규 274 뒤에 제거 245 가
    숨어 있었고 순증은 +29 였다. 둘을 떼어 놓으면 그 사실이 사라진다.
    """

    new: int
    removed: int

    @property
    def net(self) -> int:
        return self.new - self.removed

    @property
    def churn(self) -> int:
        """총 변화량. 방향과 무관하게 "얼마나 흔들렸나"."""
        return self.new + self.removed


def efficacy_voxel_delta(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> VoxelDelta:
    """★ 효능-필수. 영역 안의 신규·제거 복셀 수를 **쌍으로** 반환한다.

    `new > 0` 이 레벨2(형태 변경)의 정의 그 자체다. RePaint-lite 는 `coords` 를
    고정하므로 구조적으로 0 이었다. 0 이면 아무것도 안 한 것이고, 그 판정은
    카메라 각도·평균·해상도 어느 것으로도 뒤집히지 않는다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return VoxelDelta(
        new=int(np.setdiff1d(a, b, assume_unique=True).size),
        removed=int(np.setdiff1d(b, a, assume_unique=True).size),
    )


# 26-이웃. 표면 복셀은 대각으로만 이어지는 자리가 흔해서 6-이웃을 쓰면 한 덩어리가
# 여러 조각으로 갈라진다 — 그러면 "단일 연결성분" 이 과소평가된다.
_NEIGHBORS_26 = np.array(
    [
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    ],
    dtype=np.int64,
)


def _largest_component_size(cells: np.ndarray) -> int:
    """26-연결 최대 성분의 셀 수."""
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.shape[0] == 0:
        return 0
    codes = voxel_code(a)
    index = {int(c): i for i, c in enumerate(codes)}
    seen = np.zeros(a.shape[0], dtype=bool)
    best = 0
    for start in range(a.shape[0]):
        if seen[start]:
            continue
        seen[start] = True
        stack = [start]
        size = 0
        while stack:
            i = stack.pop()
            size += 1
            nb = a[i] + _NEIGHBORS_26
            nb = nb[np.all((nb >= 0) & (nb < VOXEL_RES), axis=1)]
            for code in voxel_code(nb):
                j = index.get(int(code))
                if j is not None and not seen[j]:
                    seen[j] = True
                    stack.append(j)
        best = max(best, size)
    return best


def efficacy_largest_component(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """★ 효능-필수 (D5-a ② 로 승격). 신규 복셀의 **최대 연결성분 / 신규 총수**. [0,1].

    A5000 실측 273/274 = 0.996. 이 지표가 없으면 "머리에 호박이 하나 생겼다" 와
    "표면에 잡음이 274개 흩뿌려졌다" 가 **같은 숫자**로 나온다. 신규 복셀 수만으로는
    구분이 원리적으로 불가능하다.

    신규 복셀이 0 이면 0.0 을 돌려준다 — 나눌 것이 없고, 어차피 효능-필수에서
    이미 떨어진다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    new_codes = np.setdiff1d(a, b, assume_unique=True)
    if new_codes.size == 0:
        return 0.0
    return _largest_component_size(_decode(new_codes)) / float(new_codes.size)


def efficacy_largest_component_of_result(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """진단용. **결과 점유 전체**(신규 + 유지)의 최대 연결성분 비율.

    🔴 왜 이 진단이 필요한가 — W3/맥북이 찾은 것.

    `efficacy_largest_component`(rev6 G2 지표)는 **신규 복셀만** 본다. 가산 편집
    (VoxHammer 주둥이)에서는 그게 맞다 — 빈 공간에 덩어리가 하나 생기니까
    273/274 = 0.996 이 나온다.

    그런데 **치환** 편집(assemble)에서는 기증자 껍질이 옛 껍질과 교차한다.
    교차한 셀은 before 에도 있으므로 "신규" 가 아니고, 그 링이 신규 집합에서
    빠지면서 **한 덩어리인 껍질이 두 조각으로 갈린다.** 합성 실측:

        배치된 기증자 껍질의 최대성분비   1.000   ← 실제로는 한 덩어리다
        결과 점유 전체의 최대성분비       1.000
        신규 복셀만의 최대성분비          0.731   ← rev6 G2 지표. 0.8 미달

    즉 이 지표는 assemble 경로를 구조적으로 불리하게 잰다. D5 가 고친 것과 **같은
    종류의 병**이다 — 지표를 잘못 고르면 제대로 도는 구현이 탈락한다.
    판정 기준을 바꾸는 것은 Chat 의 몫이므로, 여기서는 게이트를 rev6 문구대로 두고
    이 진단을 나란히 낸다.
    """
    m = _codes(region)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    if a.size == 0:
        return 0.0
    return _largest_component_size(_decode(a)) / float(a.size)


def efficacy_iou_in_mask(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """마스크 영역 복셀 점유 IoU. **낮을수록 많이 바뀐 것이다.**

    ⚠️ rev6 G2 에서는 판정 조건이 **아니다** (참고). D5 본문의 "< 0.8" 은 남아
       있지만 G2 문구가 신규복셀·연결성분·churn 셋으로 정리됐다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return _iou(b, a)


# ══════════════════════════════════════════════════════ 잡음 정규화 (D5-a ④)
def churn_ratio(
    before: np.ndarray, after: np.ndarray, mask_region: np.ndarray
) -> float:
    """★ 마스크 **안** churn 밀도 / 마스크 **밖** churn 밀도. A5000 실측 4.97배.

    왜 필요한가. 표면 복셀화는 리메싱만으로도 복셀을 만들고 지운다 — A5000 실측에서
    마스크 **밖에서도** 신규 533 / 제거 478 이 나왔다. 원시 개수만 보면 마스크 밖이
    더 크게 나오고("밖이 더 많이 바뀌었다"), 그건 영역 크기가 다르기 때문이지
    누출이 아니다.

    정규화: 각 영역의 **편집 전 점유 복셀 수**로 나눈다.

        density(R) = churn(R) / |before ∩ R|
        ratio      = density(mask) / density(outside)

    ⚠️ 이 정규화는 A5000 의 4.97 을 재현하도록 고른 것이다 (신규/제거 개수와
       TRELLIS 총 복셀 7,125 로부터 역산하면 영역별 점유 수 기준이 맞는다).
       **W3-A5000 이 자기 계산과 대조해 확인해야 한다** — 다르면 여기를 고친다.

    마스크 밖 churn 이 0 이면 `inf` 를 돌려준다 (완전 국소). 합성 디코더가
    구조적으로 그 상태다 — 실자산에서는 절대 안 나온다.
    """
    m = _codes(mask_region)
    b_all, a_all = _codes(before), _codes(after)

    b_in = np.intersect1d(b_all, m, assume_unique=True)
    a_in = np.intersect1d(a_all, m, assume_unique=True)
    b_out = np.setdiff1d(b_all, m, assume_unique=True)
    a_out = np.setdiff1d(a_all, m, assume_unique=True)

    churn_in = (
        np.setdiff1d(a_in, b_in, assume_unique=True).size
        + np.setdiff1d(b_in, a_in, assume_unique=True).size
    )
    churn_out = (
        np.setdiff1d(a_out, b_out, assume_unique=True).size
        + np.setdiff1d(b_out, a_out, assume_unique=True).size
    )

    density_in = churn_in / float(max(1, b_in.size))
    density_out = churn_out / float(max(1, b_out.size))
    if density_out == 0.0:
        return float("inf") if density_in > 0.0 else 0.0
    return density_in / density_out


# ══════════════════════════════════════════════════════════════ 보존 (B)
def inherited_byte_identity(
    parent_blobs: Mapping[str, bytes],
    child_blobs: Mapping[str, bytes],
    book: Iterable[str],
) -> float:
    """보존-A. **승계 청크**의 바이트 동일률 [0,1]. (구 `preservation_byte_identity`)

    🔴 **이것은 보존의 증거가 아니라 항진명제다** (D5-b). 부기 밖 청크는 부모
       바이트를 그대로 물려주므로 100% 가 나오는 것이 당연하다. 100% 가 아니면
       **승계 경로가 깨진 것**이지 기하가 바뀐 것이 아니다 — 그래서 회귀 검사로만 쓴다.

    재디코딩되는 영역의 진짜 보존은 `preservation_geometry_distance` 가 잰다.
    이름을 분리한 이유가 그것이다: 예전 이름(`preservation_byte_identity`)은 이
    값이 보존을 증명한다고 오독하게 만들었다.
    """
    outside = [k for k in parent_blobs if k not in set(book)]
    if not outside:
        return 1.0
    same = sum(1 for k in outside if child_blobs.get(k) == parent_blobs[k])
    return same / len(outside)


@dataclass(frozen=True)
class PreservationDistance:
    """보존-B 계측치 + 판정. 판정은 잡음 바닥값이 있어야만 나온다."""

    distance: float
    baseline: Optional[float]

    @property
    def passes(self) -> bool:
        if self.baseline is None:
            raise NoiseFloorUnknown(
                f"재디코딩 영역 기하 거리 {self.distance:.4f} 를 판정할 잡음 바닥값이 "
                "없다. 바닥값은 **편집 없이 인코드→디코드만 왕복시킨 대조군**에서 "
                "나오고 W3-A5000 에 배정돼 있다 (D5-b). 추정값을 넣어 통과시키면 "
                "'잡음인지 누출인지 못 가른 채 보존됨이라고 적는' 것이 된다."
            )
        return self.distance <= self.baseline


def preservation_geometry_distance(
    before: np.ndarray,
    after: np.ndarray,
    edited_region: np.ndarray,
    *,
    baseline: Optional[float] = None,
) -> PreservationDistance:
    """★ 보존-B. 재디코딩 영역(= 편집 영역 **밖**)의 기하 거리 = 1 - IoU.

    A5000 실측 마스크 밖 IoU 0.853 → 거리 0.147. **절대값으로 해석하지 마라**
    (D5-a ③): 그 0.147 이 순수 리메싱 잡음인지 일부 실제 누출인지는 대조군
    없이는 못 가른다.

    Args:
        edited_region: 우리가 건드리겠다고 선언한 영역 (halo 까지 팽창시킨 마스크).
                       이 **밖**에서 잰다.
        baseline: 잡음 바닥값. None 이면 계측은 되지만 `.passes` 가 거부한다.
    """
    m = _codes(edited_region)
    b = np.setdiff1d(_codes(before), m, assume_unique=True)
    a = np.setdiff1d(_codes(after), m, assume_unique=True)
    return PreservationDistance(distance=1.0 - _iou(b, a), baseline=baseline)


def preservation_iou_out(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """참고. 마스크 밖 복셀 IoU. `1 - preservation_geometry_distance` 다.

    ⚠️ D5-a ③ — **1.0 이 될 수 없다** (실측 0.853). 절대값 문턱(> 0.99)으로
       판정하지 마라. rev6 G2 에서 이 조건은 기하 거리 ≤ 바닥값으로 대체됐다.
    """
    m = _codes(region)
    b = np.setdiff1d(_codes(before), m, assume_unique=True)
    a = np.setdiff1d(_codes(after), m, assume_unique=True)
    return _iou(b, a)


# ══════════════════════════════════════════════════════════════ 절감 (C)
def transfer_saving(full_bytes: int, delta_bytes: int) -> float:
    """★ 절감. 1 - (보낸 바이트 / 전체 재전송 바이트). [0,1].

    ⚠️ 이 값 하나만 보면 **아무것도 안 보내는 구현이 100% 를 받는다.** 반드시
       효능 지표와 **같이** 판정한다 — 방법론 5조 3번.
    """
    if full_bytes <= 0:
        raise ValueError(f"full_bytes 가 0 이하다: {full_bytes}")
    if delta_bytes < 0:
        raise ValueError(f"delta_bytes 가 음수다: {delta_bytes}")
    return 1.0 - (float(delta_bytes) / float(full_bytes))


# ══════════════════════════════════════════════════════ 실루엣 (참고·보조)
def _silhouette(codes: np.ndarray, axis: int) -> np.ndarray:
    grid = np.zeros((VOXEL_RES, VOXEL_RES, VOXEL_RES), dtype=bool)
    if codes.size:
        cells = _decode(codes)
        grid[cells[:, 0], cells[:, 1], cells[:, 2]] = True
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

    로그에 남기는 이유는 과거 계측치(2.58%)와 이어 보기 위해서일 뿐이다.
    """
    b, a = _codes(before), _codes(after)
    return float(
        np.mean([_sym_diff_ratio(_silhouette(b, ax), _silhouette(a, ax))
                 for ax in (0, 1, 2)])
    )


def silhouette_masked_max(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """보조 지표. 마스크 한정 실루엣 대칭차의 **최대뷰**. 평균이 아니다.

    최대뷰를 쓰는 이유는 W1 실측 때문이다 — 시선 방향으로 튀어나온 주둥이는
    정면에서 0.16%, 옆면에서 14.3% 였다. 평균을 쓰면 그 편집이 사라진다.
    D5-a ⑤: 상위 5뷰가 15.3~16.5% 로 근접해 단독 이상치는 아니었다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    return max(
        _sym_diff_ratio(_silhouette(b, ax), _silhouette(a, ax)) for ax in (0, 1, 2)
    )


# ══════════════════════════════════════════════════════════════ 종합
@dataclass(frozen=True)
class MetricReport:
    """한 번의 편집에 대한 전체 계측. 게이트 판정은 `gate_g2()` 가 한다."""

    delta_in_mask: VoxelDelta
    delta_outside: VoxelDelta
    efficacy_largest_component: float
    churn_ratio: float
    inherited_byte_identity: float
    preservation: PreservationDistance
    transfer_saving: float
    reference: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "efficacy_new_voxels": self.delta_in_mask.new,
            "efficacy_removed_voxels": self.delta_in_mask.removed,
            "efficacy_net_voxels": self.delta_in_mask.net,
            "outside_new_voxels": self.delta_outside.new,
            "outside_removed_voxels": self.delta_outside.removed,
            "efficacy_largest_component": self.efficacy_largest_component,
            "churn_ratio": self.churn_ratio,
            "inherited_byte_identity": self.inherited_byte_identity,
            "preservation_geometry_distance": self.preservation.distance,
            "preservation_baseline": self.preservation.baseline,
            "transfer_saving": self.transfer_saving,
            "reference": dict(self.reference),
        }

    def gate_g2(
        self,
        *,
        visual_confirmed: Optional[bool] = None,
        min_new_voxels: int = 1,
        min_largest_component: float = 0.8,
        min_churn_ratio: float = 3.0,
        min_inherited_identity: float = 1.0,
        min_transfer_saving: float = 0.40,
    ) -> Dict[str, object]:
        """G2 판정 — `docs/PROGRESS.md` rev6 §5 S2 의 문구와 **1:1** 이다.

            ★ 효능   DebugView 에서 호박 머리가 육안으로 보인다 (사용자 확인)
                     AND 마스크 내 신규 복셀 > 0
                     AND 최대 연결성분 / 신규 ≥ 0.8
                     AND churn(안)/churn(밖) ≥ 3.0
            ★ 보존   승계 청크 바이트 동일률 100%
                     AND 재디코딩 영역 기하 거리 ≤ 잡음 바닥값
            ★ 절감   전송 절감 > 40%

        `visual_confirmed` 는 **코드가 만들 수 없는 사실**이다 (원칙 7: 육안
        산출물이 없으면 미검증). None 이면 숫자 조건만 채워지고 `"efficacy"` 는
        `None` 이 된다 — 통과도 실패도 아닌 **미결**이다. 그 상태로 G2 를 닫지 마라.

        Raises:
            NoiseFloorUnknown: 잡음 바닥값 없이 보존을 판정하려 할 때 (D5-b).
        """
        numeric_efficacy = (
            self.delta_in_mask.new >= min_new_voxels
            and self.efficacy_largest_component >= min_largest_component
            and self.churn_ratio >= min_churn_ratio
        )
        if visual_confirmed is None:
            efficacy: Optional[bool] = None if numeric_efficacy else False
        else:
            efficacy = numeric_efficacy and bool(visual_confirmed)

        return {
            "efficacy": efficacy,
            "efficacy_numeric": numeric_efficacy,
            "visual_confirmed": visual_confirmed,
            "preservation": (
                self.inherited_byte_identity >= min_inherited_identity
                and self.preservation.passes  # baseline 없으면 여기서 거부
            ),
            "saving": self.transfer_saving > min_transfer_saving,
        }


def evaluate(
    *,
    before: np.ndarray,
    after: np.ndarray,
    mask_cells: np.ndarray,
    edited_region_cells: np.ndarray,
    parent_blobs: Mapping[str, bytes],
    child_blobs: Mapping[str, bytes],
    book: Sequence[str],
    full_bytes: int,
    delta_bytes: int,
    noise_floor: Optional[float] = None,
) -> MetricReport:
    """전 지표를 한 번에 계산한다.

    Args:
        mask_cells:          효능을 재는 영역 — 사용자가 지정한 원본 마스크.
        edited_region_cells: 건드리겠다고 선언한 영역 — halo 까지 팽창시킨 마스크.
                             보존은 이 **밖**에서 잰다.
        noise_floor:         잡음 바닥값. None 이면 보존 **판정**이 거부된다 (계측은 됨).
    """
    return MetricReport(
        delta_in_mask=efficacy_voxel_delta(before, after, mask_cells),
        delta_outside=_delta_outside(before, after, edited_region_cells),
        efficacy_largest_component=efficacy_largest_component(
            before, after, mask_cells
        ),
        churn_ratio=churn_ratio(before, after, edited_region_cells),
        inherited_byte_identity=inherited_byte_identity(
            parent_blobs, child_blobs, book
        ),
        preservation=preservation_geometry_distance(
            before, after, edited_region_cells, baseline=noise_floor
        ),
        transfer_saving=transfer_saving(full_bytes, delta_bytes),
        reference={
            "silhouette_global_mean": reference_silhouette_global_mean(before, after),
            "silhouette_masked_max": silhouette_masked_max(before, after, mask_cells),
            "efficacy_iou_in_mask": efficacy_iou_in_mask(before, after, mask_cells),
            "efficacy_largest_component_of_result":
                efficacy_largest_component_of_result(before, after, mask_cells),
            "preservation_iou_out": preservation_iou_out(
                before, after, edited_region_cells
            ),
        },
    )


def _delta_outside(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> VoxelDelta:
    """편집 영역 **밖**의 신규·제거. D5-a ④ 의 "밖에서도 533/478" 을 항상 보이게 한다."""
    m = _codes(region)
    b = np.setdiff1d(_codes(before), m, assume_unique=True)
    a = np.setdiff1d(_codes(after), m, assume_unique=True)
    return VoxelDelta(
        new=int(np.setdiff1d(a, b, assume_unique=True).size),
        removed=int(np.setdiff1d(b, a, assume_unique=True).size),
    )
