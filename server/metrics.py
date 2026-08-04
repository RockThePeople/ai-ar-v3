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
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    VOXEL_RES,
    voxel_code,
)

__all__ = [
    "CANONICAL_PRESERVATION",
    "DISCARDED_PRESERVATION",
    "DISCARDED_PRESERVATION_WAVES",
    "DiscardedMeasurement",
    "PreservationMeasurement",
    "preservation_measurement",
    "GATE_METRICS",
    "REFERENCE_METRICS",
    "MetricReport",
    "NoiseFloorUnknown",
    "PreservationDistance",
    "VoxelDelta",
    "ContainmentNotEnforced",
    "churn_ratio",
    "efficacy_change_component",
    "efficacy_iou_in_mask",
    "efficacy_change_component",      # 🔴 D38(rev28) — 분기 op 게이트에서 강등
    "efficacy_largest_component",
    "efficacy_largest_component_of_result",
    "efficacy_voxel_delta",
    "evaluate",
    "BaselineMisapplied",
    "DRAGON_C_NOISE_FLOORS",
    "NoiseFloor",
    "AnchorRetention",
    "BRANCHING_OPS",
    "BranchTarget",
    "uses_cc_frac_gate",
    "Component",
    "Headroom",
    "change_components",
    "components",
    "head_components",
    "DIRECTION_RULES",
    "DirectionMismatch",
    "HALO_BAND_REGIONS",
    "check_direction",
    "direction_holds",
    "HaloBandResult",
    "RatioWithoutResolution",
    "SMALL_SAMPLE_VOXELS",
    "halo_band_region",
    "DRAGON_C_NECK_PROFILE",
    "EXPECT_FEATURES_REGEN",
    "VOXHAMMER_BUDGET_SECONDS",
    "VOXHAMMER_BUDGET_SECONDS_REUSED_RENDER",
    "neck_cut_z",
    "neck_minimum_z",
    "VOXHAMMER_STAGE_SECONDS",
    "HueStats",
    "hue_shift_degrees",
    "hue_stats",
    "rgb_to_hue_saturation",
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
    "efficacy_new_voxels",           # > 0  (레벨2 — 형태)
    "hue_shift_degrees",             # > 30 (레벨1 — 색. D5 · D24 recolor 경로)
    "branch_component_count",        # ≈ factor (D44) — 분기 op 의 효능 조건
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
    "efficacy_change_component",      # 🔴 D38(rev28) — 분기 op 게이트에서 강등
    "efficacy_largest_component",
    "efficacy_largest_component_of_result",
    "preservation_iou_out",
)


class ContainmentNotEnforced(RuntimeError):
    """D13 — `strict_containment=False` 로 얻은 결과로 게이트를 재려 했다.

    끄면 기증자가 마스크 밖으로 나가 보존이 조용히 무너진다 (W3/3090 실측
    preservation_iou_out 0.345 · 절감 14.05%). 옵션이 아니라 **전제**다.
    """


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


def efficacy_change_component(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> float:
    """★ 효능-필수 (D12, rev7). **(신규 ∪ 제거)** 의 최대 연결성분 비율. [0,1].

    ────────────────────────────────────────────────────────────────────
    왜 "신규만" 에서 "신규 ∪ 제거" 로 바꿨나
    ────────────────────────────────────────────────────────────────────
    D5-a ② 의 원래 정의는 **가산 편집 전용**이었다. VoxHammer 가 빈 공간에 주둥이를
    더한 경우에는 신규 복셀이 곧 변화 전체라 273/274 = 0.996 이 나온다.

    치환 편집(assemble)에서는 기증자 껍질이 옛 껍질과 교차한다. 교차한 셀은
    before 에도 있어 "신규" 가 아니고, 그 링이 신규 집합에서 빠지면서 **한 덩어리인
    껍질이 두 조각으로 갈린다.** W3/맥북 실측:

        배치된 기증자 껍질   1.000   ← 실제로는 한 덩어리
        결과 점유 전체       1.000
        신규 복셀만          0.731   ← 옛 정의. 문턱 0.8 미달

    편집은 더하든 치환하든 **하나의 응집된 변화**여야 한다. 신규와 제거를 합치면
    교차 링이 제거 쪽에 들어와 구멍이 메워지고, 두 경로가 같은 정의로 잡힌다.
    문턱은 0.8 단일 유지 — 경로별 분리는 rev7 에서 기각됐다(그게 대리 지표를 만든다).

    변화가 전혀 없으면 0.0 이다. no-op 이 여기서 떨어져야 하므로 1.0 이 아니다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    changed = np.union1d(
        np.setdiff1d(a, b, assume_unique=True),
        np.setdiff1d(b, a, assume_unique=True),
    )
    if changed.size == 0:
        return 0.0
    return _largest_component_size(_decode(changed)) / float(changed.size)


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

    ────────────────────────────────────────────────────────────────────
    D17 (rev8) — 정의 확정: **합집합 정규화 ≡ 1 − IoU**
    ────────────────────────────────────────────────────────────────────
        churn_rate(R) = (|new| + |removed|) / |(before ∪ after) ∩ R|  ≡  1 − IoU(R)
        churn_ratio   = (1 − IoU_in) / (1 − IoU_out)

    A5000 실측 = 0.7300 / 0.1469 = **4.97**.

    채택 이유  ① 항등적으로 1−IoU 라 IoU 만 있으면 재현된다 (별도 상태 불필요)
               ② 유계 [0,1]        ③ before/after 대칭

    기각한 것 — **내가 W3 에서 제안했던 before-점유 정규화**(= 7.459).
        무계다. 마스크 안에서 1.188 > 1 이 나와 "비율" 로 해석할 수 없고,
        before 가 빈 영역에서 발산한다. 결론(≫1)은 같았지만 정의가 틀렸다.
    오답      원시 개수비 0.5134 — 영역 크기를 무시한다.

    마스크 밖 churn 이 0 이면 `inf` (완전 국소). 합성 디코더가 구조적으로 그
    상태다 — 실자산에서는 절대 안 나온다.
    """
    m = _codes(mask_region)
    b_all, a_all = _codes(before), _codes(after)

    iou_in = _iou(
        np.intersect1d(b_all, m, assume_unique=True),
        np.intersect1d(a_all, m, assume_unique=True),
    )
    iou_out = _iou(
        np.setdiff1d(b_all, m, assume_unique=True),
        np.setdiff1d(a_all, m, assume_unique=True),
    )

    churn_in = 1.0 - iou_in
    churn_out = 1.0 - iou_out
    if churn_out == 0.0:
        return float("inf") if churn_in > 0.0 else 0.0
    return churn_in / churn_out


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
    """보존-B 계측치 + 판정.

    ────────────────────────────────────────────────────────────────────
    D16 (rev8) — 절대 문턱이 아니라 **바닥값 대비 초과배수**로 판정한다
    ────────────────────────────────────────────────────────────────────
    마스크 밖 IoU 0.853 은 "0.99 미만이라 실패" 가 아니라 "바닥값 0.9299 대비
    **2.10배 초과**라 실패" 다. 절대 IoU 문턱(0.99)은 리메싱 잡음 때문에 **어떤
    디코더로도 달성 불가능**하다 — 항진적으로 실패하므로 지표가 죽는다.

    임계는 **왕복 잡음(0.0701) 기준**이지 디코더 분산(0.0003) 기준이 아니다.
    분산 기준으로 잡으면 모든 것이 유의해져서 역시 지표가 죽는다.

    D5·D11·D12 와 같은 병이다 — **문턱이 측정 대상의 물리를 반영하지 않으면
    거짓말한다.**
    """

    distance: float
    baseline: Optional["NoiseFloor"] = None
    max_excess_ratio: float = 1.0
    #: 이 거리를 어느 자산·영역에서 쟀는가. 바닥값과 대조된다 (D33).
    asset_id: Optional[str] = None
    region: Optional[str] = None

    def __post_init__(self) -> None:
        # 🔴 D33 — 맨 float 바닥값은 받지 않는다. 어느 자산에서 잰 것인지가
        #    사라지고, 사라지면 반드시 잘못 재사용된다 (소년상 → 용, 3.2배).
        if self.baseline is not None and not isinstance(self.baseline, NoiseFloor):
            raise BaselineMisapplied(
                f"바닥값이 {type(self.baseline).__name__} 이다 — `NoiseFloor` 여야 한다 "
                "(D33). 자산 id 와 영역이 없는 바닥값은 받지 않는다."
            )

    @property
    def excess_ratio(self) -> float:
        """거리 / 바닥값. 1.0 이면 왕복 잡음과 같은 수준이라는 뜻이다.

        A5000 실측 VoxHammer 경로 = 0.1469 / 0.0701 = **2.10배**.
        assemble 경로(strict_containment=True) = 0.0 / 바닥값 = **0.0배**.
        """
        if self.baseline is None:
            raise NoiseFloorUnknown(_NO_BASELINE_MSG.format(d=self.distance))
        # D33 — 다른 자산·영역의 바닥값을 쓰려 하면 여기서 걸린다.
        if self.asset_id is not None:
            self.baseline.require_same_asset(self.asset_id, self.region)
        if self.baseline.value == 0.0:
            # 재디코딩 영역이 공집합이면 바닥값도 0 이다. 거리도 0 이어야 성립.
            return 0.0 if self.distance == 0.0 else float("inf")
        return self.distance / self.baseline.value

    @property
    def passes(self) -> bool:
        return self.excess_ratio <= self.max_excess_ratio

    @property
    def is_small_sample(self) -> bool:
        """바닥값 표본이 작은가. 판정 옆에 반드시 같이 적는다 (D33).

        목 대역은 100복셀뿐이라 분산이 크다 — 분산이 큰 값과 작은 값을 같은
        문턱으로 재면 안 된다.
        """
        return self.baseline is not None and self.baseline.is_small_sample


_NO_BASELINE_MSG = (
    "재디코딩 영역 기하 거리 {d:.4f} 를 판정할 잡음 바닥값이 없다. 바닥값은 "
    "**편집 없이 인코드→디코드만 왕복시킨 대조군**에서 나온다 (D5-b · D14). "
    "추정값을 넣어 통과시키면 '잡음인지 누출인지 못 가른 채 보존됨이라고 적는' "
    "것이 된다."
)


def preservation_geometry_distance(
    before: np.ndarray,
    after: np.ndarray,
    edited_region: np.ndarray,
    *,
    baseline: Optional["NoiseFloor"] = None,
    max_excess_ratio: float = 1.0,
    asset_id: Optional[str] = None,
    region: Optional[str] = None,
) -> PreservationDistance:
    """★ 보존-B. 재디코딩 영역(= 편집 영역 **밖**)의 기하 거리 = 1 - IoU.

    A5000 실측 마스크 밖 IoU 0.853 → 거리 0.147. **절대값으로 해석하지 마라**
    (D5-a ③): 그 0.147 이 순수 리메싱 잡음인지 일부 실제 누출인지는 대조군
    없이는 못 가른다.

    Args:
        edited_region: 우리가 건드리겠다고 선언한 영역 (halo 까지 팽창시킨 마스크).
                       이 **밖**에서 잰다.
        baseline: 잡음 바닥값. None 이면 계측은 되지만 `.passes` 가 거부한다.
        max_excess_ratio: D16 — 바닥값 대비 몇 배까지 허용할지. 기본 1.0 은
            rev6 G2 의 "기하 거리 ≤ 잡음 바닥값" 을 배수로 옮긴 것이다.
            ⚠️ 정확한 배수는 Chat 이 정할 사안이다 (보고에 올렸다). 알려진 두
            데이터점(assemble 0.0배 · VoxHammer 2.10배)은 (0, 2.10) 안의 어떤
            값을 써도 같은 판정을 낸다.
    """
    m = _codes(edited_region)
    b = np.setdiff1d(_codes(before), m, assume_unique=True)
    a = np.setdiff1d(_codes(after), m, assume_unique=True)
    return PreservationDistance(
        distance=1.0 - _iou(b, a),
        baseline=baseline,
        max_excess_ratio=max_excess_ratio,
        asset_id=asset_id,
        region=region,
    )


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
    efficacy_change_component: float
    churn_ratio: float
    inherited_byte_identity: float
    preservation: PreservationDistance
    transfer_saving: float
    #: 마스크 안 **변화(신규 ∪ 제거)의 연결 성분 수**. 분기 op 게이트의 조건이다 (D44).
    n_change_components: int = 0
    reference: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "efficacy_new_voxels": self.delta_in_mask.new,
            "efficacy_removed_voxels": self.delta_in_mask.removed,
            "efficacy_net_voxels": self.delta_in_mask.net,
            "outside_new_voxels": self.delta_outside.new,
            "outside_removed_voxels": self.delta_outside.removed,
            "n_change_components": self.n_change_components,
            "efficacy_change_component_ref": self.efficacy_change_component,
            "churn_ratio": self.churn_ratio,
            "inherited_byte_identity": self.inherited_byte_identity,
            "preservation_geometry_distance": self.preservation.distance,
            "preservation_baseline": (
                None if self.preservation.baseline is None
                else self.preservation.baseline.describe()
            ),
            "preservation_excess_ratio": (
                None if self.preservation.baseline is None
                else self.preservation.excess_ratio
            ),
            "transfer_saving": self.transfer_saving,
            "reference": dict(self.reference),
        }

    def gate_g2(
        self,
        *,
        op: Optional[str] = None,
        target: Optional["BranchTarget"] = None,
        visual_confirmed: Optional[bool] = None,
        min_new_voxels: int = 1,
        min_change_component: float = 0.8,
        min_churn_ratio: float = 3.0,
        min_inherited_identity: float = 1.0,
        min_transfer_saving: float = 0.40,
    ) -> Dict[str, object]:
        """G2 판정 — `docs/PROGRESS.md` rev6 §5 S2 의 문구와 **1:1** 이다.

            ★ 효능   DebugView 에서 호박 머리가 육안으로 보인다 (사용자 확인)
                     AND 마스크 내 신규 복셀 > 0
                     AND (신규 ∪ 제거)의 최대 연결성분 ≥ 0.8      ← D12 (rev7)
                     AND churn(안)/churn(밖) ≥ 3.0
                     AND **op 별 방향 조건**                      ← D38 (rev17)

        🔴 `op` 를 주지 않으면 방향 조건이 **검사되지 않는다.** W10 이 정확히 그
        상태로 통과했다 (연결성분 1.000 인데 제거 730 > 신규 304 — 머리를 먹었다).
        게이트를 재는 경로는 반드시 `op` 를 넘긴다.
            ★ 보존   승계 청크 바이트 동일률 100%
                     AND 재디코딩 영역 기하 거리 ≤ 바닥값 × max_excess_ratio  ← D16
            ★ 절감   전송 절감 > 40%

        `visual_confirmed` 는 **코드가 만들 수 없는 사실**이다 (원칙 7: 육안
        산출물이 없으면 미검증). None 이면 숫자 조건만 채워지고 `"efficacy"` 는
        `None` 이 된다 — 통과도 실패도 아닌 **미결**이다. 그 상태로 G2 를 닫지 마라.

        Raises:
            NoiseFloorUnknown: 잡음 바닥값 없이 보존을 판정하려 할 때 (D5-b).
        """
        # `target` 이 오면 그 안의 op 가 정본이다 (LLM 스펙이 곧 목표다).
        if target is not None and op is None:
            op = target.op

        # D38 — 방향 조건. op 를 안 주면 검사되지 않는다는 사실을 결과에 남긴다.
        direction_ok: Optional[bool] = None
        if op is not None:
            direction_ok = direction_holds(op, self.delta_in_mask)

        # D44 — 분기 op 는 **성분 수 ≈ factor** 로 판정한다.
        component_count_ok: Optional[bool] = None
        if target is not None:
            component_count_ok = target.component_count_ok(self.n_change_components)

        # 🔴 D38(rev28) — 분기 op 에서는 cc_frac 을 쓰지 않는다.
        #    갈라지는 것이 목적이라 이 지표는 **반대 방향**이다
        #    (runG 0.537 로 탈락했는데 역대 최고 결과였다).
        cc_frac_ok = (
            self.efficacy_change_component >= min_change_component
            if uses_cc_frac_gate(op) else True
        )

        numeric_efficacy = (
            self.delta_in_mask.new >= min_new_voxels
            and cc_frac_ok
            and self.churn_ratio >= min_churn_ratio
            and direction_ok is not False
            and component_count_ok is not False
        )
        if visual_confirmed is None:
            efficacy: Optional[bool] = None if numeric_efficacy else False
        else:
            efficacy = numeric_efficacy and bool(visual_confirmed)

        return {
            "efficacy": efficacy,
            "efficacy_numeric": numeric_efficacy,
            "direction_ok": direction_ok,          # None = op 미지정 → 미검사 (D38)
            "component_count_ok": component_count_ok,   # None = 목표 미지정 (D44)
            "cc_frac_gated": uses_cc_frac_gate(op),     # False = 분기 op (D38 rev28)
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
    containment_enforced: bool,
    noise_floor: Optional["NoiseFloor"] = None,
    max_excess_ratio: float = 1.0,
    asset_id: Optional[str] = None,
    region: Optional[str] = None,
) -> MetricReport:
    """전 지표를 한 번에 계산한다.

    Args:
        mask_cells:          효능을 재는 영역 — 사용자가 지정한 원본 마스크.
        edited_region_cells: 건드리겠다고 선언한 영역 — halo 까지 팽창시킨 마스크.
                             보존은 이 **밖**에서 잰다.
        containment_enforced: 🔴 D13 — `SpliceResult.strict_containment` 를 그대로 넘겨라.
                             False 면 계측조차 하지 않고 거부한다. 마스크 밖으로 나간
                             기증자는 보존을 **정의상** 깨므로, 그 상태의 숫자는
                             게이트로서 아무 뜻이 없다.
        noise_floor:         잡음 바닥값 `NoiseFloor`. **맨 float 은 받지 않는다** (D33) —
                             자산 id 가 없으면 소년상 값이 용에 쓰이고 3.2배 과대평가된다.
                             None 이면 보존 **판정**이 거부된다 (계측은 됨).
        asset_id/region:     이 계측이 어느 자산·영역인가. 주면 바닥값과 대조한다.

    Raises:
        ContainmentNotEnforced: `containment_enforced=False` 일 때.
    """
    if not containment_enforced:
        raise ContainmentNotEnforced(
            "strict_containment=False 로 얻은 결과로는 게이트를 잴 수 없다 (D13). "
            "기증자가 마스크 밖으로 나가면 보존이 정의상 깨진다 — W3/3090 실측 "
            "preservation_iou_out 0.345 · 절감 14.05%, 켜면 1.000000."
        )
    return MetricReport(
        delta_in_mask=efficacy_voxel_delta(before, after, mask_cells),
        delta_outside=_delta_outside(before, after, edited_region_cells),
        efficacy_change_component=efficacy_change_component(
            before, after, mask_cells
        ),
        n_change_components=len(change_components(before, after, mask_cells)),
        churn_ratio=churn_ratio(before, after, edited_region_cells),
        inherited_byte_identity=inherited_byte_identity(
            parent_blobs, child_blobs, book
        ),
        preservation=preservation_geometry_distance(
            before, after, edited_region_cells,
            baseline=noise_floor, max_excess_ratio=max_excess_ratio,
            asset_id=asset_id, region=region,
        ),
        transfer_saving=transfer_saving(full_bytes, delta_bytes),
        reference={
            "silhouette_global_mean": reference_silhouette_global_mean(before, after),
            "silhouette_masked_max": silhouette_masked_max(before, after, mask_cells),
            "efficacy_iou_in_mask": efficacy_iou_in_mask(before, after, mask_cells),
            "efficacy_largest_component_new_only":
                efficacy_largest_component(before, after, mask_cells),
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


# ══════════════════════════════════════════ 색 (D5 레벨1 · D24 recolor 경로)
@dataclass(frozen=True)
class HueStats:
    """색상 통계. **평균 채도를 같이 들고 다닌다** — 이유는 아래.

    🔴 회색의 hue 는 의미가 없다. (148,148,148) 의 색상각은 정의되지 않고,
    (148,147,147) 처럼 아주 살짝 치우친 값은 **잡음이 각도를 결정한다.**
    흰 눈사람의 "hue 58.3°" 가 정확히 그 상태다 — 그 숫자 자체는 신뢰할 수 없다.

    그래도 D5 레벨1 판정("마스크 안 평균 hue 이동 > 30°")은 성립한다. 편집 **후**가
    주황(채도 높음)이라 이동량이 크게 나오기 때문이다. 다만 `before` 가
    무채색이면 "무엇에서" 30° 움직였는지는 말할 수 없으므로, 그 사실을
    `is_achromatic` 로 표면에 올린다. 숨기면 다음 세션이 58.3° 를 실측치로 인용한다.
    """

    mean_hue_deg: float
    mean_saturation: float
    n: int

    #: 이 아래면 색상각을 신뢰하지 않는다. HSV 채도 기준.
    ACHROMATIC_SATURATION = 0.10

    @property
    def is_achromatic(self) -> bool:
        return self.mean_saturation < self.ACHROMATIC_SATURATION


def rgb_to_hue_saturation(rgb: np.ndarray):
    """(N,3) uint8 RGB → (hue 도[0,360), HSV 채도[0,1])."""
    a = np.asarray(rgb, dtype=np.float64).reshape(-1, 3) / 255.0
    if a.shape[0] == 0:
        return np.zeros((0,)), np.zeros((0,))
    mx = a.max(axis=1)
    mn = a.min(axis=1)
    d = mx - mn
    hue = np.zeros_like(mx)
    nz = d > 1e-12
    r, g, b = a[:, 0], a[:, 1], a[:, 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        is_r = nz & (mx == r)
        is_g = nz & (mx == g) & ~is_r
        is_b = nz & ~is_r & ~is_g
        hue[is_r] = ((g[is_r] - b[is_r]) / d[is_r]) % 6.0
        hue[is_g] = (b[is_g] - r[is_g]) / d[is_g] + 2.0
        hue[is_b] = (r[is_b] - g[is_b]) / d[is_b] + 4.0
    hue = (hue * 60.0) % 360.0
    sat = np.where(mx > 1e-12, d / np.maximum(mx, 1e-12), 0.0)
    return hue, sat


def hue_stats(rgb: np.ndarray) -> HueStats:
    """색상의 **원형 평균**. 산술 평균을 쓰면 0°/359° 가 180° 로 나온다."""
    hue, sat = rgb_to_hue_saturation(rgb)
    n = int(hue.shape[0])
    if n == 0:
        return HueStats(mean_hue_deg=float("nan"), mean_saturation=0.0, n=0)
    rad = np.deg2rad(hue)
    mean = float(np.rad2deg(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360.0)
    return HueStats(mean_hue_deg=mean, mean_saturation=float(sat.mean()), n=n)


def hue_shift_degrees(before_rgb: np.ndarray, after_rgb: np.ndarray) -> float:
    """★ D5 레벨1. 마스크 안 평균 hue 이동(도). 문턱 > 30°.

    **원형 거리**다 — 350° 와 10° 의 차이는 340° 가 아니라 20° 다. 최대 180°.
    W6/3090 실측: 58.3° → 27.0°, 이동 **31.3°** (문턱 통과).
    """
    b, a = hue_stats(before_rgb), hue_stats(after_rgb)
    if b.n == 0 or a.n == 0:
        return 0.0
    diff = abs(a.mean_hue_deg - b.mean_hue_deg) % 360.0
    return float(min(diff, 360.0 - diff))


# ══════════════════════════ D33 — 잡음 바닥값은 **자산별·영역별**이다
#
# ★★ W8/A5000 실측이 이걸 바꿨다. 소년상 하나로 잰 0.0701 을 용에 그대로 쓰면
#    halo 초과배수가 **3배 이상 과대평가**되고 "누출 심각" 이라는 오판이 나온다.
#
#     자산        영역           before복셀   1−IoU
#     소년상      (전역)              —       0.0701
#     dragon-c    전역              8,000     0.2229   ← 소년상의 3.2배
#     dragon-c    몸통 z<44         7,181     0.2225
#     dragon-c    ★ 목 z44–47         100     0.1538   ← W9 halo 의 분모
#     dragon-c    머리 z≥48           719     0.2358
#     dragon-c    디코더 분산           —     0.0017   (소년상 0.0003 의 5.7배)
#
# 전역 0.2229 를 목에 쓰면 과소평가, 0.0701 을 쓰면 3배 과대평가다. **영역 분해가 필수다.**
#
# ⚠️ 목 대역은 **100복셀뿐**이다. 분산이 큰 값과 작은 값을 같은 문턱으로 재지 않는다 —
#    그래서 표본 크기를 값과 함께 들고 다니고, 판정할 때 표면에 올린다.
#
# ⇒ 그래서 **자산 id 없는 바닥값은 타입이 거부한다.** float 하나로 다니면
#   어느 자산·어느 영역에서 잰 것인지가 사라지고, 사라지면 반드시 잘못 재사용된다.


class BaselineMisapplied(RuntimeError):
    """다른 자산·다른 영역에서 잰 바닥값을 그대로 쓰려 했다 (D33)."""


#: 이 아래면 표본이 작아 분산이 크다고 본다. 목 대역(100복셀)이 기준점이다.
SMALL_SAMPLE_VOXELS = 200


@dataclass(frozen=True)
class NoiseFloor:
    """잡음 바닥값 + **어디서 어떻게 쟀는가**. float 하나로 다니지 않는다.

    이름이 아니라 **타입**으로 강제하는 이유: 맨 float 은 자산 경계를 소리 없이
    넘어간다. 소년상 0.0701 을 용에 쓰면 3배 과대평가인데 예외도 안 난다.
    """

    value: float                 # 1 − IoU 무차원
    asset_id: str                # 🔴 필수. 어느 자산에서 쟀는가
    region: str = "global"       # 어느 영역인가 (global / neck / body / head …)
    n_voxels: int = 0            # 표본 크기 — 분산 해석에 필요하다
    decoder_variance: Optional[float] = None   # 같은 자산의 디코더 분산 (유의성 하한)
    #: 🔴 의사(pseudo) 마스크로 잰 잠정값인가. 실마스크가 오면 재산출해야 한다 (D36).
    provisional: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not self.asset_id.strip():
            raise BaselineMisapplied(
                "바닥값에 asset_id 가 없다 (D33). 잡음 바닥값은 자산별이다 — "
                "소년상 0.0701 을 dragon-c(0.2229)에 쓰면 3.2배 과대평가되고 "
                "'누출 심각' 이라는 오판이 나온다."
            )
        if not (self.value == self.value) or self.value < 0.0:
            raise BaselineMisapplied(f"바닥값이 음수이거나 NaN 이다: {self.value}")
        if self.n_voxels < 0:
            raise BaselineMisapplied(f"표본 크기가 음수다: {self.n_voxels}")

    @property
    def is_small_sample(self) -> bool:
        """표본이 작아 분산이 큰가. 목 대역(100복셀)이 여기 걸린다."""
        return 0 < self.n_voxels < SMALL_SAMPLE_VOXELS

    def require_same_asset(self, asset_id: str, region: Optional[str] = None) -> None:
        """다른 자산·영역에 쓰려 하면 거부한다. **이 클래스의 존재 이유다.**"""
        if asset_id != self.asset_id:
            raise BaselineMisapplied(
                f"바닥값은 {self.asset_id!r} 에서 잰 것인데 {asset_id!r} 에 쓰려 한다 "
                f"(D33). 실측 격차 3.2배 — 자산이 바뀌면 **다시 재라**."
            )
        if region is not None and region != self.region:
            # D36 — halo 대역과 z 대역은 **서로 다른 공간**이다. 겹치지 않는다.
            crossing_space = (region in HALO_BAND_REGIONS) != self.is_halo_band
            extra = (
                " 🔴 halo 대역과 z 대역은 서로 다른 공간이다 (D36) — "
                "목 0.1538 을 halo-1(0.0222) 분모로 쓰면 약 7배 과소평가한다."
                if crossing_space else
                " dragon-c 안에서도 목 0.1538 vs 머리 0.2358 로 1.5배 차이다."
            )
            raise BaselineMisapplied(
                f"바닥값은 영역 {self.region!r} 에서 잰 것인데 {region!r} 에 쓰려 한다 "
                f"(D33/D36).{extra}"
            )

    @property
    def is_halo_band(self) -> bool:
        return self.region in HALO_BAND_REGIONS

    def describe(self) -> str:
        parts = [f"{self.asset_id}/{self.region} 1−IoU={self.value:.4f}"]
        if self.n_voxels:
            parts.append(f"표본 {self.n_voxels}복셀" + (" ⚠️분산 큼" if self.is_small_sample else ""))
        if self.decoder_variance is not None:
            parts.append(f"디코더분산 {self.decoder_variance:.4f}")
        if self.provisional:
            parts.append("⚠️잠정(의사 마스크)")
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)


# ══════════════════════════ D36 — halo 분모는 **대역 전용**이다
#
# 🔴 rev14 의 "halo 분모는 목 대역 0.1538" 이 **틀렸다.** 목 z44–47 과
#    "halo 바깥 0–2 vox 대역" 은 **서로 다른 공간**이다. 겹치지 않는다.
#
#     halo   대역 셀   before   신규/제거   합집합   대역 전용 바닥값
#      1      2,516      44       1 / 0       45       **0.0222**
#      2      2,694      41       4 / 0       45       **0.0889**
#      3      2,876      43       5 / 2       48       **0.1458**
#     (참고) 목 z44–47   100      17 / 1     100         0.1538
#
# ⇒ 0.1538 을 halo-1 분모로 쓰면 **약 7배 과소평가**한다.
#   W8 의 "0.0701 → 0.2229 (3.2배 과대평가)" 에 이어 **분모 오류 두 번째**이고,
#   이번엔 **반대 방향**이다. 그래서 region 을 자산과 같은 급으로 강제한다 —
#   "neck" 과 "halo_band_1" 은 다른 region 이고, 섞으면 타입이 거부한다.
#
# ⚠️ 위 값은 **의사(pseudo) 마스크** 기준이다. 실마스크가 오면 재산출해야 한다 —
#    그 사실도 값과 함께 들고 다닌다 (`provisional`).

#: halo 대역 region 이름. 목·머리·몸통 z대역과 **다른 공간**이다 (D36).
HALO_BAND_REGIONS = ("halo_band_1", "halo_band_2", "halo_band_3")


def halo_band_region(halo: int) -> str:
    """halo 폭 → region 이름. 오타로 다른 공간을 가리키는 것을 막는다."""
    if halo not in (1, 2, 3):
        raise BaselineMisapplied(
            f"halo 대역 바닥값은 1·2·3 만 잰 상태다: halo={halo} (D36). "
            "재려면 A5000 이 그 폭으로 다시 재야 한다 — 없는 값을 보간하지 마라."
        )
    return f"halo_band_{halo}"


# W8/A5000 실측. **참조용 상수이지 기본값이 아니다** — 다른 자산·영역에 쓰면 타입이 거부한다.
DRAGON_C_NOISE_FLOORS = {
    # z 대역 (마스크 안/밖 보존용)
    "global": NoiseFloor(0.2229, "dragon-c", "global", 8000, decoder_variance=0.0017),
    "body":   NoiseFloor(0.2225, "dragon-c", "body",   7181, decoder_variance=0.0017),
    "neck":   NoiseFloor(0.1538, "dragon-c", "neck",    100, decoder_variance=0.0017),
    "head":   NoiseFloor(0.2358, "dragon-c", "head",    719, decoder_variance=0.0017),
    # 🔴 halo 대역 — 위 z 대역과 **다른 공간**이다. 분모로 쓸 것은 이쪽이다 (D36).
    # 🔴 D36-a — **실마스크** 실측값. W9 의사 마스크 값(0.0222/0.0889/0.1458)은 폐기.
    #    halo-1 이 **10배** 틀렸다. 그대로 썼으면 초과배수가 10배 과대평가됐다 —
    #    A5000 이 미리 등록한 맹점 D 가 크게 발동한 사례다.
    "halo_band_1": NoiseFloor(
        0.2222, "dragon-c", "halo_band_1", 226,
        decoder_variance=0.0017,
        note="실마스크 · 신규 189 / 제거 37 · W9 의사값 0.0222 의 10.0배",
    ),
    "halo_band_2": NoiseFloor(
        0.2100, "dragon-c", "halo_band_2", 218,
        decoder_variance=0.0017,
        note="실마스크 · 신규 155 / 제거 63 · W9 의사값 0.0889 의 2.4배",
    ),
    "halo_band_3": NoiseFloor(
        0.1681, "dragon-c", "halo_band_3", 229,
        decoder_variance=0.0017,
        note="실마스크 · 신규 146 / 제거 83 · W9 의사값 0.1458 의 1.2배",
    ),
}

#: W9 의사 마스크 잠정치. **폐기됐다** — 재사용 금지. 대조 기록으로만 남긴다 (D36-a).
DISCARDED_PSEUDO_HALO_FLOORS = {"halo_band_1": 0.0222, "halo_band_2": 0.0889,
                                "halo_band_3": 0.1458}


# ══════════════ 🔴🔴 D51 — W10~W13 의 보존 수치가 **전부 무효**다
#
# `edit_pipeline.py:543` 이 `torch.isin` 을 2D 텐서에 썼다. 그건 **원소 단위**(평탄화
# 값 집합)지 **행(x,y,z) 멤버십이 아니다.** 어떤 복셀의 x·y·z 값이 삭제집합 **어딘가에**
# 각각 등장하기만 하면 삭제로 쳤다.
#
#     보존 복셀   VoxHammer  13 / 8,511 (**0.15%**)   →   행 단위 정답  7,608 (**89%**)
#
#   ★ W12 에서 살아남은 13개는 **값 63 을 포함한 복셀뿐**이다 — 그 마스크에 없던 유일한
#     값이다. **공간과 아무 상관이 없다.** 되붙일 것이 사실상 없었다.
#
# ⇒ **이것이 W10~W13 의 "마스크 밖 3.5배 초과" 를 그대로 설명한다.** 누출이 아니라
#   보존이 꺼져 있었던 것이다. D33/D36 의 초과배수 비교도 전부 이 위에서 쟀다.
#
# 🔴 이것은 **NoiseFloor 타입이 잡은 D33-a 와 같은 종류**의 오염이다. 다만 훨씬 크다.
#   D33-a 는 "다른 자산의 분모" 였고 이건 "분자가 통째로 거짓" 이었다. 그래서 같은
#   처방을 쓴다 — **맨 float 으로 돌아다니지 못하게 타입으로 막고, 폐기를 값에 붙인다.**
#
# ⚠️ 바닥값(DRAGON_C_NOISE_FLOORS) 자체는 **무편집 재디코딩**으로 잰 것이라 D51 경로를
#    타지 않는다. 무효인 것은 **그 분모에 대고 잰 W10~W13 의 분자와 초과배수**다.
#    다만 이 구분은 내가 A5000 의 측정 스크립트를 직접 못 봤다 — 확인이 필요하다.


class DiscardedMeasurement(RuntimeError):
    """D51 로 무효화된 W10~W13 의 보존 수치를 쓰려 했다."""


@dataclass(frozen=True)
class PreservationMeasurement:
    """마스크 밖 보존 실측 1건. **어느 웨이브 · 유효한가**를 값과 함께 들고 다닌다.

    맨 float 으로 두면 다음 세션이 0.7753 을 인용한다 — 그 숫자는 편집 결과가 아니라
    **보존이 꺼져 있었다는 사실**을 잰 것이다.
    """

    wave: str
    outside_iou_complement: float     # 마스크 밖 1 − IoU
    floor: NoiseFloor                 # 분모 (같은 자산·같은 영역이어야 한다)
    preserved_voxels: Optional[int] = None
    source_voxels: Optional[int] = None
    #: A5000 이 **보고한** 초과배수. 계산값과 다르면 분모가 다른 것이다 (아래 참조).
    reported_excess_ratio: Optional[float] = None
    discarded: bool = False
    discard_reason: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.discarded and not self.discard_reason:
            raise ValueError("폐기 표시에는 이유가 있어야 한다 — 이유 없는 폐기는 잊힌다")

    @property
    def excess_ratio(self) -> float:
        """바닥값 대비 초과배수. **폐기된 값에서는 거부한다.**"""
        self.require_valid()
        if self.floor.value <= 0.0:
            raise BaselineMisapplied(f"바닥값이 0 이다: {self.floor.describe()}")
        return self.outside_iou_complement / self.floor.value

    @property
    def preservation_rate(self) -> Optional[float]:
        if self.preserved_voxels is None or not self.source_voxels:
            return None
        return self.preserved_voxels / self.source_voxels

    def require_consistent_ratio(self, tolerance: float = 0.02) -> None:
        """보고된 초과배수와 **분모로 다시 계산한 값**이 맞는지 본다.

        🔴 runG 가 여기 걸린다: 0.2399 / 0.2231 = **1.08** 인데 보고는 **1.16** 이다.
           1.16 을 얻으려면 분모가 0.2068 이어야 한다. 즉 runG 는 **다른 분모**로
           쟀다 — 마스크가 W13→W14 로 바뀌었으니 '마스크 밖'이라는 영역 자체가
           달라졌고, D33 의 규칙대로라면 **바닥값도 그 마스크로 다시 재야 한다.**

        어느 쪽이 맞는지는 내가 정할 수 없다 (A5000 이 잰다). 그래서 **조용히 한쪽을
        고르지 않고 예외로 올린다.** 분모를 결과에 맞춰 고르는 것이 이 프로젝트가
        여섯 번 물린 그 병이다.
        """
        if self.reported_excess_ratio is None:
            return
        computed = self.outside_iou_complement / self.floor.value
        if abs(computed - self.reported_excess_ratio) > tolerance:
            implied = self.outside_iou_complement / self.reported_excess_ratio
            raise BaselineMisapplied(
                f"{self.wave}: 보고 초과배수 {self.reported_excess_ratio:.2f}× 인데 "
                f"바닥값 {self.floor.describe()} 으로 계산하면 {computed:.2f}× 다. "
                f"보고값이 맞으려면 분모가 {implied:.4f} 여야 한다 — 마스크가 바뀌면 "
                f"'마스크 밖' 영역이 달라지므로 바닥값도 그 마스크로 다시 재야 한다 "
                f"(D33). 어느 쪽이 맞는지는 A5000 이 잰다."
            )

    def require_valid(self) -> None:
        """폐기된 수치를 판정에 쓰면 **예외를 던진다** (D51).

        bool 을 돌려주면 호출부가 검사하고도 무시할 수 있고, 그게 곧 조용한 실패다.
        """
        if self.discarded:
            raise DiscardedMeasurement(
                f"{self.wave} 의 보존 수치({self.outside_iou_complement:.4f})는 "
                f"무효다 (D51): {self.discard_reason} "
                f"유효한 정본은 {sorted(CANONICAL_PRESERVATION)} 이다."
            )

    def describe(self) -> str:
        head = f"{self.wave} 마스크밖 1−IoU={self.outside_iou_complement:.4f}"
        if self.discarded:
            return f"🔴 폐기 {head} — {self.discard_reason}"
        rate = self.preservation_rate
        tail = f" · 보존 {self.preserved_voxels}/{self.source_voxels} ({rate:.0%})" if rate else ""
        return f"{head} ({self.excess_ratio:.2f}×){tail}{' · ' + self.note if self.note else ''}"


#: W15 재측정 기준 전역 바닥값. W8 의 0.2229 와 0.0002 차 — 같은 자산·같은 영역이다.
DRAGON_C_OUTSIDE_FLOOR_W15 = NoiseFloor(
    0.2231, "dragon-c", "global", 8000, decoder_variance=0.0017,
    note="W15 재측정 · D51 수정 후 초과배수의 분모",
)

#: 🔴 **폐기된** 수치. 지우지 않고 남기는 이유는, 지우면 다음 세션이 다시 잰 것으로
#  착각하고 인용하기 때문이다. 쓰려고 하면 `require_valid()` 가 막는다.
DISCARDED_PRESERVATION = {
    "W13": PreservationMeasurement(
        wave="W13", outside_iou_complement=0.7753,
        floor=DRAGON_C_OUTSIDE_FLOOR_W15,
        preserved_voxels=13, source_voxels=8511,
        discarded=True,
        discard_reason=(
            "torch.isin 이 행이 아니라 원소 단위라 보존이 13/8,511 = 0.15% 였다. "
            "'마스크 밖 3.48배 초과' 는 누출이 아니라 보존이 꺼져 있었다는 뜻이다. "
            "생존한 13개는 값 63 을 포함한 복셀뿐 — 공간과 무관하다."
        ),
    ),
}
#: W10~W12 도 같은 결함 위에서 쟀다. 수치는 A5000 기록에만 있고 여기 옮기지 않는다 —
#  옮기면 인용되기 때문이다. 이름만 남긴다.
DISCARDED_PRESERVATION_WAVES = ("W10", "W11", "W12", "W13")

#: ★ **새 정본** (W15, D51 수정 후). runG 가 프로젝트 최고 보존이다.
CANONICAL_PRESERVATION = {
    "runF": PreservationMeasurement(
        wave="runF", outside_iou_complement=0.2737,
        floor=DRAGON_C_OUTSIDE_FLOOR_W15,
        preserved_voxels=7608, source_voxels=8511,
        reported_excess_ratio=1.23,
        note="D51 수정만 · W13 마스크 그대로 (변수 통제) · overflow 1,025/99청크",
    ),
    "runG": PreservationMeasurement(
        wave="runG", outside_iou_complement=0.2399,
        floor=DRAGON_C_OUTSIDE_FLOOR_W15,
        preserved_voxels=7608, source_voxels=8511,
        reported_excess_ratio=1.16,   # 🔴 0.2399/0.2231 = 1.08 과 불일치 — 분모 확인 필요
        note="+W14 마스크 · direction_ok TRUE 최초(849/500) · overflow 602/80청크 · 성분 3",
    ),
}


def preservation_measurement(key: str) -> PreservationMeasurement:
    """이름으로 실측을 꺼낸다. 폐기된 것을 꺼내면 **꺼낼 때가 아니라 쓸 때** 막힌다 —
    폐기 사실을 읽히게 하려면 객체가 손에 들어와야 하기 때문이다."""
    if key in CANONICAL_PRESERVATION:
        return CANONICAL_PRESERVATION[key]
    if key in DISCARDED_PRESERVATION:
        return DISCARDED_PRESERVATION[key]
    if key in DISCARDED_PRESERVATION_WAVES:
        raise DiscardedMeasurement(
            f"{key} 의 보존 수치는 D51 로 무효이고 이 리포에 옮기지 않았다. "
            f"유효한 정본은 {sorted(CANONICAL_PRESERVATION)} 이다."
        )
    raise KeyError(f"모르는 실측: {key!r}")


# ══════════════════════════ D31-a — VoxHammer 1회 예산 (게이트 문턱)
#
# W1 의 200초는 **캐시가 더워진 상태**의 수치였다. 실측(W8/A5000)으로 분해하면:
#
#     slat→GLB      46.4s
#     150뷰 렌더     80.4s
#     features      24.5s
#     편집          ~200s
#     ─────────────────────
#     렌더·피처 있음  ~305s      slat 에서 시작  ~350s
#
# ⇒ G4 의 "1회 < 300초" 는 **캐시 상태를 정상으로 오인한 문턱**이었다. 400초로 정정한다.
#   문턱이 실제 비용보다 낮으면 정상 실행이 예산 초과로 기록되고, 그건 D5·D11·D12·D16 과
#   같은 병이다 — **문턱이 측정 대상의 물리를 반영하지 않으면 거짓말한다.**
#
# 문서에만 두면 다음 세션이 300 을 인용한다. 상수로 둔다.

#: G4 의 편집 1회 벽시계 문턱(초). D31-a 로 300 → 400 정정.
VOXHAMMER_BUDGET_SECONDS = 400.0

#: 참고 — 단계별 실측(초). 재사용으로 아낄 수 있는 항목을 눈에 보이게 둔다.
VOXHAMMER_STAGE_SECONDS = {
    "slat_to_glb": 46.4,
    "render_150_views": 80.4,   # w8/render/ 재사용 시 절약 가능
    "features": 24.5,           # 🔴 D31-b — 재사용해도 **검사는 끄지 않는다** (아래)
    "edit": 200.0,
}

# ── D31-b — features 재사용은 검사를 **끄지 않는다** ─────────────────────
#
# D10 래퍼가 `features.npz` 의 mtime 을 검증하는 이유는 W1 1회차가 **7월자 낡은
# features 로 돌면서 "completed successfully" 를 찍었기** 때문이다
# (`extract_feature.py:125` 의 맨 except 가 OOM 을 삼켰다).
#
# 재사용이 그 검증과 충돌한다고 해서 검증을 끄면, 정확히 그 사고가 다시 난다.
# ⇒ `EXPECT_FEATURES_REGEN=1` 을 **유지**하는 것이 정본이다. 재생성 비용 24.5초는
#   "낡은 캐시로 돌고도 성공이라고 적는" 위험보다 싸다.

#: D31-b — features 재사용 시에도 재생성 기대 플래그를 유지한다.
EXPECT_FEATURES_REGEN = True

#: D31-b — features 를 재생성하는 정상 경로의 편집 예산(초). 렌더만 재사용한다.
VOXHAMMER_BUDGET_SECONDS_REUSED_RENDER = 270.0

# ── D25-b — 목 극소 z 정정 ────────────────────────────────────────────────
#
# A5000 W7 원문:  z=42:108 → 43:60 → 44:32 → **45:28** → 46:30 → 47:28 → 48:40 → 49:75
# 🔴 Chat 이 rev14 에 "극소 z=44(32복셀)" 로 잘못 요약했다. 진짜 극소는 **z=45(28)** 이다.
#    D25-a(절단 = 극소 위 = neck+1)를 적용하면 절단은 **z=46**. rev14 전제대로면 z=45 —
#    **한 칸 차이로 목을 문다.**
#
# 상수로 두는 이유: 요약 오류가 실제 결정을 바꾼 것이 이번이 세 번째다
# (인계 경로 · rev 미도달 · 극소 z). 문서 문장은 다시 잘못 요약될 수 있다.

#: dragon-c 의 z별 복셀 수 (A5000 W7 실측). 극소를 코드가 직접 고르게 한다.
DRAGON_C_NECK_PROFILE = {42: 108, 43: 60, 44: 32, 45: 28, 46: 30, 47: 28, 48: 40, 49: 75}


def neck_minimum_z(profile=None) -> int:
    """목 극소 z. **표에서 직접 고른다** — 요약을 믿지 않는다 (D25-b).

    동점이면 낮은 z 를 고른다 (z=45:28 과 z=47:28 이 동점이지만 극소는 45 다 —
    47 은 머리로 올라가는 구간이라 목이 아니다).
    """
    prof = DRAGON_C_NECK_PROFILE if profile is None else profile
    return min(sorted(prof), key=lambda z: prof[z])


def neck_cut_z(profile=None) -> int:
    """절단 z = 극소 **위** 한 칸 (D25-a). dragon-c 에서는 46 이다."""
    return neck_minimum_z(profile) + 1


# ══════════════════ D37 — halo 판정: 비율을 버리고 **원시 개수 + 육안**으로
#
# 🔴 지표를 버리는 **두 번째** 경우다 (첫 번째는 D5 전역 실루엣). 둘 다 같은 병이다:
#    **지표가 측정 대상의 물리를 반영하지 못한다.**
#
# 사실: halo 대역 합집합이 **45~48복셀**뿐이고 바닥값 분자가 **신규 1개**(halo-1)다.
#       ⇒ **복셀 하나가 대역의 2%** 다. 초과배수를 소수점으로 논할 수 없다.
#       0.0222 라는 값은 "1/45" 이고, 신규가 2개가 되면 그대로 2배가 된다.
#
# ⇒ 원시 개수를 **반드시 병기**하고, 표본이 작으면 **비율 단독 판정을 거부**한다.
#   "목이 자연스럽게 이어지는가" 는 근본적으로 육안 질문이다 (D19 의 선례).
#   육안은 `visual_confirmed` 와 같은 방식으로 **코드가 만들 수 없게** 둔다.


class RatioWithoutResolution(RuntimeError):
    """표본이 작아 비율에 유효숫자가 없다. 비율 단독으로 판정하지 않는다 (D37)."""


@dataclass(frozen=True)
class HaloBandResult:
    """halo 대역 계측. **원시 개수가 1급 시민이다** — 비율은 참고값이다 (D37)."""

    halo: int
    n_new: int                    # ★ 원시 개수. 이것이 판정의 근거다
    n_removed: int
    n_band_cells: int             # 대역 전체 셀 수 (분모의 분모)
    n_union: int                  # before ∪ after 합집합 — 비율의 분모
    baseline: Optional[NoiseFloor] = None
    #: 육안 확인. 🔴 코드가 만들 수 없다 — 사람이 넣는다 (D19 · D37).
    visual_confirmed: Optional[bool] = None

    @property
    def n_churn(self) -> int:
        return self.n_new + self.n_removed

    @property
    def ratio(self) -> float:
        """1 − IoU 상당의 비율. **참고값이다.** 판정에 단독으로 쓰지 마라."""
        return self.n_churn / self.n_union if self.n_union else 0.0

    @property
    def voxel_resolution(self) -> float:
        """복셀 **하나**가 비율에서 차지하는 몫. 0.02 면 유효숫자가 없다는 뜻이다."""
        return 1.0 / self.n_union if self.n_union else float("inf")

    @property
    def has_ratio_resolution(self) -> bool:
        """비율을 소수점으로 논할 만한 해상도가 있는가.

        복셀 하나가 대역의 1% 를 넘으면 없다고 본다 — 실측 2% 가 그 상태다.
        """
        return self.n_union > 0 and self.voxel_resolution <= 0.01

    @property
    def excess_ratio(self) -> float:
        """바닥값 대비 초과배수. **"약 N배" 수준의 참고값이다** (D37)."""
        if self.baseline is None:
            raise NoiseFloorUnknown(_NO_BASELINE_MSG.format(d=self.ratio))
        self.baseline.require_same_asset(
            self.baseline.asset_id, halo_band_region(self.halo)
        )
        if self.baseline.value == 0.0:
            return 0.0 if self.ratio == 0.0 else float("inf")
        return self.ratio / self.baseline.value

    def verdict(self, *, allow_ratio_only: bool = False) -> bool:
        """halo 판정. **비율 단독으로는 판정하지 않는다** (D37).

        Raises:
            RatioWithoutResolution: 표본이 작아 비율에 유효숫자가 없는데
                육안 확인도 없다. `allow_ratio_only=True` 로 우회할 수 있지만,
                그건 "해상도가 없는 줄 알면서 쓴다" 는 선언이다.
            NoiseFloorUnknown: 바닥값이 없다.
        """
        if self.visual_confirmed is not None:
            return bool(self.visual_confirmed)
        if not self.has_ratio_resolution and not allow_ratio_only:
            raise RatioWithoutResolution(
                f"halo-{self.halo} 대역 합집합이 {self.n_union}복셀뿐이라 복셀 하나가 "
                f"{self.voxel_resolution * 100:.1f}% 다 — 초과배수에 유효숫자가 없다 (D37). "
                f"원시 개수는 신규 {self.n_new} / 제거 {self.n_removed} 다. "
                "판정은 깊이맵 육안으로 한다 (`visual_confirmed`). "
                "'목이 자연스럽게 이어지는가' 는 근본적으로 육안 질문이다."
            )
        return self.excess_ratio <= 1.0

    def describe(self) -> str:
        """★ 원시 개수를 **항상** 앞에 둔다. 비율은 뒤에 참고로 붙인다 (D37)."""
        parts = [
            f"halo-{self.halo}: 신규 {self.n_new} / 제거 {self.n_removed}"
            f" (합집합 {self.n_union}복셀 · 대역 {self.n_band_cells}셀)"
        ]
        if not self.has_ratio_resolution:
            parts.append(
                f"⚠️ 복셀 1개 = {self.voxel_resolution * 100:.1f}% — 비율에 유효숫자 없음"
            )
        parts.append(f"참고 비율 {self.ratio:.4f}")
        if self.baseline is not None:
            try:
                parts.append(f"참고 약 {self.excess_ratio:.1f}배")
            except (NoiseFloorUnknown, BaselineMisapplied):
                pass
        parts.append(
            "육안 미확인" if self.visual_confirmed is None
            else ("육안 통과" if self.visual_confirmed else "육안 실패")
        )
        return " · ".join(parts)


# ══════════════════ D38 — 효능 게이트의 **방향 조건**
#
# 🔴 W10 에서 게이트가 **파괴를 통과시켰다.**
#     최대 연결성분 1.000 ≥ 0.8 → 통과. 그런데 제거 730 > 신규 304 였고
#     전체 복셀이 8,000 → 6,744 로 줄었다. **머리를 만든 게 아니라 먹었다.**
#
# 원인은 지표가 틀린 게 아니라 **묻는 질문이 틀렸다**:
#     "최대 연결성분" 은 "한 덩어리인가" 이지 **"옳은 방향인가" 가 아니다.**
#
# D5-a ① 은 "신규·제거를 **쌍으로 보고**하라" 였다. 보고 요구였을 뿐 **게이트 조건이
# 아니었다.** 그 틈으로 실패가 통과했다. 여기서 게이트로 승격한다.
#
# 게이트를 고치는 **네 번째** 경우다:
#     D5  전역 실루엣 폐기 · D12 연결성분 정의 · D37 halo 비율 폐기 · D38 방향 조건


class DirectionMismatch(RuntimeError):
    """편집 방향이 op 가 요구하는 것과 반대다 (D38)."""


#: op → 방향 조건. `None` 은 "방향 무관" 이다 (D26 매핑표와 짝).
#: 값은 (설명, 판정함수(VoxelDelta) -> bool).
def _dir_add(d: "VoxelDelta") -> bool:
    return d.new > d.removed


def _dir_remove(d: "VoxelDelta") -> bool:
    return d.removed > d.new


def _dir_unchanged(d: "VoxelDelta") -> bool:
    return d.new == 0 and d.removed == 0


DIRECTION_RULES = {
    "add":            ("신규 > 제거", _dir_add),
    "remove":         ("제거 > 신규", _dir_remove),
    "replace_region": (None, None),          # 방향 무관 — 부피 비율은 참고값
    "recolor":        ("기하 불변 (신규 = 제거 = 0)", _dir_unchanged),
}


def check_direction(op: str, delta: "VoxelDelta") -> None:
    """op 가 요구하는 **방향**을 지켰는지 (D38). 어기면 예외를 던진다.

    ⚠️ bool 을 돌려주지 않는다 — 호출부가 검사하고도 무시할 수 있으면 그게 곧
       조용한 실패다 (`dispatch.check_supported` 와 같은 이유).

    Raises:
        DirectionMismatch: 방향이 반대다.
        KeyError: 모르는 op — 방향 규칙 없이 게이트를 통과시키지 않는다.
    """
    if op not in DIRECTION_RULES:
        raise KeyError(
            f"op={op!r} 의 방향 규칙이 없다 (D38). op 를 추가했으면 방향도 정해라 — "
            "규칙 없는 op 를 통과시키면 W10 이 반복된다."
        )
    description, predicate = DIRECTION_RULES[op]
    if predicate is None:
        return
    if not predicate(delta):
        raise DirectionMismatch(
            f"op={op!r} 는 {description} 를 요구하는데 신규 {delta.new} / "
            f"제거 {delta.removed} (순증 {delta.net:+d}) 다 (D38). "
            "최대 연결성분은 '한 덩어리인가' 를 볼 뿐 '옳은 방향인가' 를 보지 않는다 — "
            "W10 에서 연결성분 1.000 으로 통과한 결과가 실제로는 머리를 **먹은** 것이었다."
        )


def direction_holds(op: str, delta: "VoxelDelta") -> bool:
    """`check_direction` 의 비예외 판본. **보고용이다** — 게이트에는 예외판을 쓴다."""
    try:
        check_direction(op, delta)
    except DirectionMismatch:
        return False
    return True


# ══════════════════ D39 — 앵커 잔존율. **문턱은 정하지 않는다**
#
# W10 가설: 마스크가 원 부위의 자산 셀을 **전부** 지우면 편집이 아니라 **재생성**이 된다.
# 실측은 마스크 안 자산 955 / 빈공간 91.79% 였고 결과는 머리가 사라졌다.
#
# ⚠️ **데이터가 한 점뿐이다.** 문턱을 정하지 않는다 — 한 점으로 문턱을 만드는 것이
#    이 프로젝트가 반복해 물린 모양이다 (D5 · D16 · D33 · D37 전부 같은 병).
#    값을 내고 **병기**만 한다. 문턱은 점이 몇 개 모인 뒤에 정한다.


@dataclass(frozen=True)
class AnchorRetention:
    """마스크 안에 남은 **원 부위 자산 셀**의 비율. 문턱 없음 (D39)."""

    n_asset_in_mask: int      # 마스크 안 자산(점유) 셀
    n_mask_cells: int         # 마스크 전체 셀
    #: 원 부위(예: 머리)의 자산 셀 수. 알 수 없으면 None.
    n_region_asset: Optional[int] = None

    @property
    def empty_fraction(self) -> float:
        """마스크 중 빈 공간 비율. W10 실측 0.9179."""
        if self.n_mask_cells <= 0:
            return 0.0
        return 1.0 - (self.n_asset_in_mask / self.n_mask_cells)

    @property
    def retention(self) -> Optional[float]:
        """마스크 안 자산 셀 / 원 부위 자산 셀. 분모를 모르면 None."""
        if not self.n_region_asset:
            return None
        return self.n_asset_in_mask / self.n_region_asset

    def describe(self) -> str:
        parts = [
            f"마스크 안 자산 {self.n_asset_in_mask} / 마스크 {self.n_mask_cells}셀"
            f" (빈공간 {self.empty_fraction * 100:.2f}%)"
        ]
        r = self.retention
        parts.append(f"앵커 잔존율 {r * 100:.1f}%" if r is not None else "앵커 잔존율 —(분모 미상)")
        parts.append("⚠️ 문턱 없음 — 데이터 1점 (D39)")
        return " · ".join(parts)


# ══════════════════ D29-a — "머리" 는 **절단면 위로 뻗는 성분**이다
#
# 🔴 성분 개수만 세면 날개 조각이 머리로 잡힌다. W11 반례:
#    z 44–46 에 걸친 조각이 하나 있었는데 **절단면 위로 뻗지 않았다** —
#    옆으로만 퍼진 날개 파편이었다. 개수로는 "머리 하나" 로 세어진다.
#
# ⇒ 성분마다 **z 범위와 x 중심**을 함께 반환하고, "머리" 를 형상 조건으로 정의한다:
#      절단면(neck_cut_z) 위로 **뻗어 올라가는** 성분만 머리로 센다.
#
# ⚠️ "위로 뻗는다" 의 기준은 절단면 위 두께다. 한 칸만 걸친 것은 뻗은 것이 아니다.


@dataclass(frozen=True)
class Component:
    """연결 성분 하나. **개수가 아니라 형상**을 들고 다닌다 (D29-a)."""

    n_cells: int
    z_min: int
    z_max: int
    x_center: float
    y_center: float

    @property
    def z_span(self) -> int:
        return self.z_max - self.z_min + 1

    def height_above(self, cut_z: int) -> int:
        """절단면 **위로** 뻗은 높이(칸). 절단면 자체는 세지 않는다."""
        return max(0, self.z_max - cut_z)

    def rises_above(self, cut_z: int, *, min_thickness: int = 2) -> bool:
        """절단면 위로 **뻗어 올라가는가** (D29-a).

        `z_max > cut_z` 만으로는 부족하다 — 절단면 바로 위 한 칸만 걸친 조각도
        그 조건을 만족한다. W11 의 날개 파편(z 44–46, 절단면 45)이 정확히 그
        경우였고, 개수로 세면 "머리 하나" 가 된다.

        ⚠️ 절단면 **자체**를 두께에 넣으면 안 된다. 넣으면 그 날개가 두께 2 로
           통과한다 (46 − max(44,45) + 1 = 2). 위로 뻗은 높이만 센다.
        """
        return self.height_above(cut_z) >= min_thickness

    def describe(self) -> str:
        return (
            f"{self.n_cells}셀 · z {self.z_min}–{self.z_max}(두께 {self.z_span})"
            f" · x중심 {self.x_center:.1f}"
        )


def components(cells: np.ndarray) -> List[Component]:
    """26-연결 성분을 **형상 정보와 함께** 나열한다. 큰 것부터."""
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.shape[0] == 0:
        return []
    codes = voxel_code(a)
    index = {int(c): i for i, c in enumerate(codes)}
    seen = np.zeros(a.shape[0], dtype=bool)
    out: List[Component] = []
    for start in range(a.shape[0]):
        if seen[start]:
            continue
        seen[start] = True
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            nb = a[i] + _NEIGHBORS_26
            nb = nb[np.all((nb >= 0) & (nb < VOXEL_RES), axis=1)]
            for code in voxel_code(nb):
                j = index.get(int(code))
                if j is not None and not seen[j]:
                    seen[j] = True
                    stack.append(j)
        m = a[members]
        out.append(Component(
            n_cells=len(members),
            z_min=int(m[:, 2].min()), z_max=int(m[:, 2].max()),
            x_center=float(m[:, 0].mean()), y_center=float(m[:, 1].mean()),
        ))
    return sorted(out, key=lambda c: c.n_cells, reverse=True)


def head_components(
    cells: np.ndarray, cut_z: int, *, min_cells: int = 1, min_thickness: int = 2
) -> List[Component]:
    """"머리" 로 셀 성분만 (D29-a). 절단면 위로 뻗는 것만 남는다.

    ⚠️ 개수만 세는 판정은 날개 조각을 머리로 센다 — W11 이 그 반례다.
    """
    return [
        c for c in components(cells)
        if c.n_cells >= min_cells and c.rises_above(cut_z, min_thickness=min_thickness)
    ]


# ══════════════════ D41 — headroom. **문턱은 정하지 않는다** (D39-a)
#
# dragon-c 는 z 0–62 로 세로 여유가 **1칸**뿐이다. 머리를 위로 뻗게 하려면
# 자랄 자리가 있어야 하는데 격자 경계가 막고 있다 — 그게 실패 가설 중 하나다.
#
# ⚠️ 점이 부족하다. 값을 내고 **병기**만 한다 (D39-a).


@dataclass(frozen=True)
class Headroom:
    """자산 bbox 와 격자 경계 사이 여유. 문턱 없음 (D41 · D39-a)."""

    lo: Tuple[int, int, int]
    hi: Tuple[int, int, int]

    @classmethod
    def from_cells(cls, cells: np.ndarray) -> "Headroom":
        a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
        if a.size == 0:
            raise ValueError("점유가 비었다 — headroom 을 잴 수 없다")
        mn, mx = a.min(axis=0), a.max(axis=0)
        return cls(
            lo=tuple(int(v) for v in mn),
            hi=tuple(int(VOXEL_RES - 1 - v) for v in mx),
        )

    @property
    def up(self) -> int:
        """위쪽(+z) 여유. 머리가 자랄 자리다."""
        return self.hi[2]

    @property
    def minimum(self) -> int:
        return min(min(self.lo), min(self.hi))

    def describe(self) -> str:
        return (
            f"headroom 위 {self.up} · x {self.lo[0]}/{self.hi[0]}"
            f" · y {self.lo[1]}/{self.hi[1]} · z {self.lo[2]}/{self.hi[2]}"
            " · ⚠️ 문턱 없음 (D41 · D39-a)"
        )


# ══════════ D38(rev28) — `largest_cc_frac` 을 **분기 op 게이트에서 뺀다**
#
# 🔴 증거 4건. 이 지표는 분기 생성과 **반대 방향**이고, 통과/탈락이 성공/실패와
#    아무 상관이 없다:
#
#     실행    largest_cc_frac   게이트   실제
#     W10        1.000          통과     🔴 **파괴** (제거 730 > 신규 304)
#     W12        0.733          탈락     성공 쪽
#     W13        0.845          통과     —
#     runG       0.537          탈락     🔴 **역대 최고 결과**
#
# 머리를 셋으로 만들면 변화가 셋으로 갈라지므로 최대 연결성분 비율은 **반드시**
# 떨어진다. 문턱이 높을수록 잘 갈라진 결과가 더 심하게 탈락한다 — 지표가
# 측정 대상의 물리와 **정확히 반대**다.
#
# ⇒ 분기 op(add + factor)의 게이트는 **변화 성분 수 ≈ factor** (D44) AND
#   `direction_ok` 다. `cc_frac` 은 `REFERENCE_METRICS` 로 강등한다.
#
# ⚠️ 다른 op 에서는 유지한다 (W16 판단):
#     replace_region / recolor / remove 는 **하나의 응집된 변화**를 기대하므로
#     "한 덩어리인가" 가 여전히 옳은 질문이다. D12 가 그 자리에서는 맞았다.
#     분기(add)만 예외다 — 갈라지는 것이 목적인 유일한 op 이기 때문이다.
#
# 게이트를 고치는 여섯 번째 경우다 (D5 · D12 · D37 · D38 · D44 · 그리고 이것).

#: 변화가 **갈라지는 것이 목적**인 op. 여기서는 `cc_frac` 을 게이트에 쓰지 않는다.
BRANCHING_OPS = frozenset({"add"})


def uses_cc_frac_gate(op: Optional[str]) -> bool:
    """이 op 의 게이트에 `largest_cc_frac` 을 쓰는가 (D38 rev28).

    `op` 가 None 이면 **쓰지 않는다** — 무엇을 만들려 했는지 모르는 채로
    "한 덩어리인가" 를 묻는 것은 W10 이 통과한 그 상태다.
    """
    return op is not None and op not in BRANCHING_OPS


@dataclass(frozen=True)
class BranchTarget:
    """분기 목표 — LLM 이 낸 `{op, factor}` 를 게이트가 받는다 (D44).

    ★ **LLM 출력이 게이트에 쓰이는 첫 자리**다. D23(좌표 금지)은 유지된다 —
      `factor` 는 좌표가 아니라 **개수**다.
    """

    op: str
    factor: Optional[float] = None
    #: 성분 수가 목표와 몇 개까지 어긋나도 되는지. 1 이면 3±1 을 허용한다.
    tolerance: int = 1

    @property
    def expected_components(self) -> Optional[int]:
        if self.op not in BRANCHING_OPS or self.factor is None:
            return None
        return int(round(self.factor))

    def component_count_ok(self, n_components: int) -> Optional[bool]:
        """변화 성분 수가 목표와 맞는가. 목표가 없으면 None (미검사)."""
        want = self.expected_components
        if want is None:
            return None
        return abs(n_components - want) <= self.tolerance

    def describe(self, n_components: int) -> str:
        want = self.expected_components
        if want is None:
            return f"성분 {n_components}개 · 목표 없음(op={self.op})"
        return (
            f"성분 {n_components}개 vs 목표 {want}개 (±{self.tolerance}) → "
            f"{'통과' if self.component_count_ok(n_components) else '탈락'}"
        )


def change_components(
    before: np.ndarray, after: np.ndarray, region: np.ndarray
) -> List[Component]:
    """마스크 안 **변화(신규 ∪ 제거)** 의 연결 성분. 분기 판정의 근거다 (D44).

    `efficacy_change_component` 는 이 성분들의 **최대 비율**을 냈다. 분기 op 에서는
    그 비율이 반대 방향이므로(D38 rev28) **개수**를 쓴다 — 머리를 셋으로 만들면
    변화도 셋으로 갈라진다.
    """
    m = _codes(region)
    b = np.intersect1d(_codes(before), m, assume_unique=True)
    a = np.intersect1d(_codes(after), m, assume_unique=True)
    changed = np.union1d(
        np.setdiff1d(a, b, assume_unique=True),
        np.setdiff1d(b, a, assume_unique=True),
    )
    return components(_decode(changed))
