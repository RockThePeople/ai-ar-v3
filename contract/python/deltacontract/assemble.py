"""조립(assemble) — 다른 자산의 일부를 마스크 자리에 **끼워 넣는다**.

편집(RePaint)과 다른 연산이다. RePaint 는 `coords` 를 고정하고 `feats` 만 다시
뽑으므로 **복셀을 늘릴 수 없다**(`server/repaint/masked_sampler.py` 서두의
"Phase 1 제약"). 그래서 편집의 시각적 변화에는 상한이 있다 — A5000 실측
(2026-08-02): 활성 복셀 **100%** 를 순수 노이즈에서 재샘플링해도 실루엣 대칭차가
front 0.46% / left 0.70% 에 그친다.

조립은 좌표를 **직접 구성**하므로 그 제약 밖이다. 같은 자산에 선인장 상단을
끼워 넣었을 때 대칭차가 front 6.1% / left 8.0% 로, 편집 상한의 13배·11배였다.

이 모듈은 그 연산 중 **순수 계산 부분만** 담는다. 모델도 GPU 도 부르지 않는다.
A5000 이 구현하고 3090 이 검증하는 두 자리에서 같은 함수를 쓰기 위한 것이다.

────────────────────────────────────────────────────────────────────────
🔴 왜 문서가 아니라 함수인가
────────────────────────────────────────────────────────────────────────
이 프로젝트는 "규칙만 적고 함수를 안 줘서" 같은 사고를 세 번 냈다:
3.11.0 staging 경로 · 3.13.0 마스크 지문 · 그리고 배치/부기가 네 번째가 될
자리였다. 아래 세 규칙은 **전부 실측으로 발견한 함정**이고, 문서로만 적으면
두 세션이 각자 다르게 구현한다.

  1. 배치는 **정수 평행이동만** 안전하다.
     소수 이동은 `rint` 의 half-to-even 때문에 서로 다른 복셀을 한 칸으로
     뭉갠다. 실측: 눈사람 머리 4,110복셀을 +0.5 이동 → 914복셀 (78% 소실).
     +3.0 이동 → 4,110 그대로.

  2. 스케일은 **쓸 수 없는 연산**이다. 좌표를 2배 하면 이웃이 이웃이 아니게
     되고 디코더가 고립 복셀마다 조각난 표면을 만든다. 실측: 6-이웃 쌍 유지율
     s=1.5 에서 50%, s=2.0 에서 **0%**. 렌더가 색종이 조각이 된다.
     크기는 스케일이 아니라 **크롭 비율**로 고른다.

  3. 부기는 **배치에서 유도**한다. 절대 diff 로 만들지 마라.
     비워진 청크는 새 바이트가 없으므로 "변경된 청크" 목록에 나타나지 않는다.
     실측에서 diff 로 유도했더니 비워진 청크 8개가 통째로 빠졌고, 그 청크들은
     옛 머리 기하를 그대로 들고 남았다.

────────────────────────────────────────────────────────────────────────
⚠️ 해시 비교로 검증하지 마라
────────────────────────────────────────────────────────────────────────
조립은 **전체를 재디코딩**한다. 그런데 디코딩은 프로세스를 새로 띄우면 바이트가
재현되지 않는다 — 기증자 없이 base 만 다시 디코딩한 대조군에서 152/152 청크가
전부 다른 해시를 냈다. **기하 변화는 중앙값 0.0002 메시셀**이었다. 부동소수
잡음이다.

편집 경로가 "마스크 밖 바이트 100% 동일" 을 냈던 것은 재현성 때문이 아니라
**부모 바이트를 승계**했기 때문이다. 조립도 같은 승계를 해야 하고, 검증은
해시가 아니라 **대조군 대비 기하 거리**로 한다.
"""

from typing import Dict, Iterable, List, Sequence, Set, Tuple

import numpy as np

from .coords import VOXEL_RES, chunk_key, chunk_keys_sorted, voxel_to_chunk
from .errors import ContractMismatch

__all__ = [
    "AssemblyError",
    "assemble_zones",
    "crop_rows",
    "fit_offset",
    "place_cells",
    "split_removed",
]


class AssemblyError(ContractMismatch):
    """조립 전제가 깨졌다. 조용히 넘어가면 안 되는 것만 여기로 온다."""


# ══════════════════════════════════════════════════════════════════ 크롭
def crop_rows(cells: np.ndarray, fraction: float, axis: int = 2,
              keep: str = "top") -> np.ndarray:
    """기증자에서 가져올 부분을 고르는 **행 마스크** (N,) bool.

    크기 조절은 스케일이 아니라 이걸로 한다 (모듈 docstring 2번).
    경계는 자산의 **실제 점유 구간**을 기준으로 자른다 — [0,64) 전체를 기준으로
    자르면 자산이 한쪽에 치우쳤을 때 아무것도 안 잡히거나 전부 잡힌다.

    Args:
        cells:    (N,3) int VOXEL 좌표.
        fraction: 0 < f <= 1. 가져올 비율.
        axis:     0=x 1=y 2=z. 기본 z(상단축).
        keep:     "top" 이면 큰 쪽, "bottom" 이면 작은 쪽.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if not (0.0 < fraction <= 1.0):
        raise AssemblyError(f"fraction 은 (0,1] 이어야 한다: {fraction}")
    if axis not in (0, 1, 2):
        raise AssemblyError(f"axis 는 0/1/2 여야 한다: {axis}")
    if a.size == 0:
        return np.zeros(0, dtype=bool)
    lo, hi = int(a[:, axis].min()), int(a[:, axis].max())
    span = hi - lo
    if keep == "top":
        return a[:, axis] >= lo + span * (1.0 - fraction)
    if keep == "bottom":
        return a[:, axis] <= lo + span * fraction
    raise AssemblyError(f"keep 은 'top'/'bottom' 이어야 한다: {keep!r}")


# ══════════════════════════════════════════════════════════════════ 배치
def place_cells(cells: np.ndarray, offset: Sequence[int]) -> np.ndarray:
    """기증자 셀을 정수 평행이동해서 놓는다. 스케일은 **없다**.

    셋을 전부 여기서 거부한다 — 하나라도 놓치면 조용히 틀린 자산이 나온다:
      · 소수 offset      (모듈 docstring 1번, 78% 복셀 소실)
      · [0,64) 이탈       디코더가 받는 격자를 벗어난다
      · 이동 후 중복      정수 이동은 단사여서 원래 중복이 없으면 안 생긴다.
                         생겼다면 입력에 이미 중복이 있었다는 뜻이다

    Returns:
        (N,3) int64. 입력과 **같은 순서**다 — feats 를 같은 순서로 붙일 수 있어야
        한다. 정렬은 호출부가 `canonical_order` 로 따로 한다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    off = np.asarray(offset)
    if off.shape != (3,):
        raise AssemblyError(f"offset 은 길이 3 이어야 한다: {off.shape}")
    if not np.all(np.equal(np.mod(off.astype(np.float64), 1.0), 0.0)):
        raise AssemblyError(
            f"offset 은 **정수**여야 한다: {off.tolist()}. 소수 이동은 rint 의 "
            "half-to-even 때문에 서로 다른 복셀을 한 칸으로 뭉갠다 "
            "(실측 +0.5 에서 4,110→914).")
    out = a + off.astype(np.int64)
    if out.size and (out.min() < 0 or out.max() >= VOXEL_RES):
        raise AssemblyError(
            f"배치가 [0,{VOXEL_RES}) 를 벗어난다: "
            f"min={out.min(axis=0).tolist()} max={out.max(axis=0).tolist()}. "
            "offset 을 줄이거나 크롭 비율을 낮춰라 — **잘라내지 마라**, "
            "자르면 형상이 바뀐다.")
    if out.shape[0] and np.unique(out, axis=0).shape[0] != out.shape[0]:
        raise AssemblyError(
            "배치 후 중복 좌표가 생겼다. 정수 이동은 단사이므로 이건 입력에 "
            "이미 중복이 있었다는 뜻이다 — 스케일이 섞여 들어갔는지 확인해라.")
    return out


def fit_offset(donor_cells: np.ndarray, target_cells: np.ndarray,
               seat_axis: int = 2) -> List[int]:
    """관례적 배치를 **정수로** 계산한다: 나머지 축은 중심 정렬, `seat_axis` 는
    기증자 바닥을 대상의 바닥에 앉힌다.

    "접합 단면을 맞추도록 스케일" 은 못 한다(스케일이 표면을 부순다). 대신
    **앉히고 크롭 비율로 크기를 고른다.** 반올림은 여기 한 곳에서만 일어난다.
    """
    d = np.asarray(donor_cells, dtype=np.int64).reshape(-1, 3)
    t = np.asarray(target_cells, dtype=np.int64).reshape(-1, 3)
    if d.size == 0 or t.size == 0:
        raise AssemblyError("fit_offset: 빈 셀 집합")
    off = [0, 0, 0]
    for ax in (0, 1, 2):
        if ax == seat_axis:
            off[ax] = int(t[:, ax].min() - d[:, ax].min())
        else:
            dc = (int(d[:, ax].min()) + int(d[:, ax].max())) / 2.0
            tc = (int(t[:, ax].min()) + int(t[:, ax].max())) / 2.0
            off[ax] = int(round(tc - dc))
    # 격자 안으로 **정수만큼** 되민다. 앉히는 축은 대상 바닥에 맞추려다 위로
    # 넘치기 쉽다 (기증자 상단 40% 가 목 높이에서 시작하면 z=66 이 된다).
    # ⚠️ 자르지 않는다 — 자르면 형상이 바뀌어 무엇을 쟀는지 알 수 없게 된다.
    #    안 들어가면 여기서 실패시켜 크롭 비율을 줄이게 만든다.
    for ax in (0, 1, 2):
        lo = int(d[:, ax].min()) + off[ax]
        hi = int(d[:, ax].max()) + off[ax]
        if hi - lo >= VOXEL_RES:
            raise AssemblyError(
                f"축{ax} 의 기증자 extent {hi - lo + 1} 이 격자 {VOXEL_RES} 보다 "
                "크다. 어떤 평행이동으로도 안 들어간다 — 크롭 비율을 줄여라.")
        if hi >= VOXEL_RES:
            off[ax] -= hi - (VOXEL_RES - 1)
        elif lo < 0:
            off[ax] -= lo
    return off


# ══════════════════════════════════════════════════════════════════ 부기
def assemble_zones(donor_placed: np.ndarray,
                   emptied_cells: np.ndarray) -> Dict[str, List[str]]:
    """부기를 **배치에서** 유도한다 (모듈 docstring 3번).

    ⚠️ `diff_chunk_sets` 로 만들지 마라. 비워진 청크는 새 바이트가 없어서 diff 에
       나타나지 않고, 그러면 옛 기하를 들고 남는다 (실측 8청크 유실).

    Args:
        donor_placed:  (N,3) `place_cells` 를 통과한 기증자 셀.
        emptied_cells: (M,3) 대상에서 **비운** 셀 (마스크+halo). 겹쳐 넣는
                       방식이면 빈 배열을 준다.

    Returns:
        {"zone1": [...], "zone2": [...], "book": [...]}  — 전부 정렬된 청크 키.
        zone1 = 기증자가 차지한 자리 · zone2 = 비워진 자리(zone1 제외)
        book  = zone1 ∪ zone2 = **이번 연산이 새 바이트를 책임지는 청크 전체**
    """
    z1 = _chunk_cells(donor_placed)
    z2 = _chunk_cells(emptied_cells)
    k1 = set(chunk_keys_sorted(z1)) if len(z1) else set()
    k2 = set(chunk_keys_sorted(z2)) if len(z2) else set()
    k2 -= k1
    # ⚠️ 정렬은 **사전순이 아니라 canonical(Morton)** 이다. affected_chunks 가
    #    chunk_keys_sorted 를 쓰므로 여기서 sorted() 를 쓰면 같은 집합이 다른
    #    순서로 나가고, 두 목록을 리스트로 비교하는 쪽에서 조용히 어긋난다.
    return {"zone1": _order(k1), "zone2": _order(k2), "book": _order(k1 | k2)}


def _order(keys):
    from .coords import parse_chunk_key
    return chunk_keys_sorted([parse_chunk_key(k) for k in keys]) if keys else []


def split_removed(book: Iterable[str],
                  produced: Iterable[str]) -> Tuple[List[str], List[str]]:
    """부기를 (바이트가 나온 것, 비어서 사라진 것)으로 가른다.

    계약 §4.3 전칭 규칙: ∀ c ∈ book : c ∈ chunks ∨ c ∈ removed_chunk_ids.
    "아무 데도 안 넣기" 는 3090 이 BOOKKEEPING_MISMATCH 로 거부한다 — 빠뜨린 것과
    비었다고 알려준 것을 구분할 수 없기 때문이다.
    """
    b, p = set(book), set(produced)
    return sorted(b & p), sorted(b - p)


def _chunk_cells(cells) -> np.ndarray:
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    return voxel_to_chunk(a) if a.size else a.reshape(0, 3)
