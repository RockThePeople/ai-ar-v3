"""부기(bookkeeping) — **배치에서 유도한다. diff 로 만들지 않는다.**

────────────────────────────────────────────────────────────────────────
🔴 왜 diff 가 아닌가
────────────────────────────────────────────────────────────────────────
`docs/PROGRESS.md` §4 실패 목록:

    부기를 diff 로 유도   비워진 청크 8개가 목록에서 사라지고 옛 기하가 남았다

비워진 청크에는 **새 바이트가 없다.** 그래서 "이전 해시 ≠ 현재 해시" 로 만든 목록에
나타나지 않는다. 클라이언트는 그 청크에 대해 아무 지시도 못 받고, 옛 머리 기하를
그대로 들고 남는다. 예외는 안 난다. 화면에서만 보인다.

배치에서 유도하면 이 구멍이 없다. 무엇을 비웠고 무엇을 놓았는지는 **연산을 하기
전부터 알고 있는 사실**이기 때문이다 (FINAL §0: "diff 를 하지 않는다. 편집 마스크는
생성 시점에 이미 알고 있다").

    zone1 = 기증자가 차지한 자리
    zone2 = 비운 자리 (zone1 제외)
    book  = zone1 ∪ zone2   ← 이번 연산이 새 바이트를 책임지는 청크 전체

전칭 규칙 (계약 §4.3):  ∀ c ∈ book : c ∈ chunks ∨ c ∈ removed_chunk_ids
"아무 데도 안 넣기" 는 거부된다 — 빠뜨린 것과 비었다고 알려준 것을 구분할 수 없어서다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from deltacontract.assemble import (  # type: ignore[import-not-found]
    assemble_zones,
    split_removed,
)
from deltacontract.coords import (  # type: ignore[import-not-found]
    chunk_keys_sorted,
    voxel_code,
    voxel_to_chunk,
)
from deltacontract.errors import BookkeepingMismatch  # type: ignore[import-not-found]
from deltacontract.partition import diff_chunk_sets  # type: ignore[import-not-found]

from .splice import SpliceResult

__all__ = [
    "Bookkeeping",
    "OverflowResult",
    "OverflowThresholdUnknown",
    "classify_overflow",
    "derive_bookkeeping_with_overflow",
    "audit_against_bytes",
    "derive_bookkeeping",
    "diff_would_have_missed",
    "verify_bookkeeping",
]


@dataclass(frozen=True)
class Bookkeeping:
    """배치에서 유도한 부기. `changed`/`removed` 는 산출 청크가 정해진 뒤 채워진다."""

    zone1: List[str]     # 기증자가 차지한 청크
    zone2: List[str]     # 비워진 청크 (zone1 제외)
    book: List[str]      # zone1 ∪ zone2
    changed: List[str]   # book 중 새 바이트가 나온 것
    removed: List[str]   # book 중 비어서 사라진 것

    @property
    def n_book(self) -> int:
        return len(self.book)


def derive_bookkeeping(
    spliced: SpliceResult, produced_keys: Iterable[str]
) -> Bookkeeping:
    """조립 결과 + 실제로 만들어진 청크 키 → 부기.

    Args:
        spliced:       `splice()` 결과. **여기 있는 배치 정보만** 쓴다.
        produced_keys: 결과 메시를 분할해서 실제로 바이트가 나온 청크 키 전부.

    Returns:
        Bookkeeping. `changed ∪ removed == book` 이 보장된다.
    """
    zones = assemble_zones(spliced.donor_placed, spliced.emptied)
    changed, removed = split_removed(zones["book"], produced_keys)
    bk = Bookkeeping(
        zone1=zones["zone1"],
        zone2=zones["zone2"],
        book=zones["book"],
        changed=changed,
        removed=removed,
    )
    verify_bookkeeping(bk)
    return bk


def verify_bookkeeping(bk: Bookkeeping) -> None:
    """전칭 규칙 ∀c ∈ book : c ∈ changed ∨ c ∈ removed 를 강제한다.

    `derive_bookkeeping` 이 만든 것은 구성상 항상 통과한다. 그래도 부르는 이유는,
    부기를 손으로 조립하는 호출부(3090 오케스트레이터)가 같은 함수로 자기를 검사할
    수 있어야 하기 때문이다. 규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다.
    """
    c, r = set(bk.changed), set(bk.removed)
    orphan = [k for k in bk.book if k not in c and k not in r]
    if orphan:
        raise BookkeepingMismatch(
            f"부기에 있는데 chunks 에도 removed 에도 없는 청크 {len(orphan)}개: "
            f"{orphan[:8]}{' …' if len(orphan) > 8 else ''}"
        )
    stray = sorted((c | r) - set(bk.book))
    if stray:
        raise BookkeepingMismatch(
            f"부기 밖인데 changed/removed 에 들어간 청크 {len(stray)}개: {stray[:8]}"
        )


def diff_would_have_missed(
    bk: Bookkeeping,
    previous_hashes: Mapping[str, str],
    current_hashes: Mapping[str, str],
) -> List[str]:
    """**진단용.** diff 로 부기를 만들었다면 놓쳤을 청크 키.

    실측에서 8개가 여기 걸렸다. 0 이 아니라는 것이 "배치 유도" 가 밥값을 한다는
    증거이므로, 관통 실행 때 이 값을 로그에 남긴다.
    """
    changed, _removed = diff_chunk_sets(dict(previous_hashes), dict(current_hashes))
    return sorted(set(bk.book) - set(changed.keys()))


def audit_against_bytes(
    bk: Bookkeeping,
    previous_hashes: Mapping[str, str],
    current_hashes: Mapping[str, str],
) -> None:
    """부기가 **너무 작지 않은지** 실제 바이트로 검증한다.

    부기가 크면 전송이 낭비될 뿐이지만, 부기가 작으면 클라이언트가 옛 기하를 들고
    남는다. 후자만 사고다. 그래서 한 방향만 예외로 만든다:

        바이트가 바뀌었는데 부기에 없는 청크가 하나라도 있으면 BookkeepingMismatch.

    ⚠️ 이 검사는 **합성 디코더**(voxelize.occupancy_to_mesh)처럼 기하가 복셀-국소일
       때만 성립한다. 진짜 TRELLIS 디코더는 receptive field 때문에 마스크 밖도
       미세하게 흔들려서 해시가 달라진다 — 그때는 해시가 아니라 기하 거리로 잰다.
    """
    changed, removed = diff_chunk_sets(dict(previous_hashes), dict(current_hashes))
    book = set(bk.book)
    leaked = sorted((set(changed.keys()) | set(removed)) - book)
    if leaked:
        raise BookkeepingMismatch(
            f"부기 밖에서 바이트가 바뀐 청크 {len(leaked)}개: {leaked[:8]}"
            f"{' …' if len(leaked) > 8 else ''}. 부기가 실제 변화보다 작다 — "
            "클라이언트가 옛 기하를 들고 남는다."
        )


# ══════════════ D54 — overflow 부기에 **연결성분 필터**를 건다
#
# 🔴 델타 코딩의 성패가 여기서 갈린다.
#
# 편집이 마스크 밖에 신규 복셀을 만들면 그 청크도 부기에 들어가야 한다 — 안 넣으면
# 클라이언트가 옛 기하를 들고 남는다. 그런데 실측 overflow 602복셀 중 **404 가
# 전역 리메시 잡음**이었다. 필터 없이 전부 넣으면:
#
#     80청크 / 부모 124청크 = **64.5% 가 델타에 들어간다** → 절감률이 죽는다
#
# 신호와 잡음의 차이는 **개수가 아니라 연결성**이다:
#     고립 복셀        = 잡음 (리메시가 경계에서 흔들린 것)
#     연결된 덩어리    = 실제 구조 (측면 머리는 각 300+ 복셀이고 연결돼 있다)
#
# **D29-a 의 논리를 overflow 에 그대로 적용한다** — 거기서도 "날개 조각이냐 머리냐" 를
# 개수로는 못 갈랐고 형상으로 갈랐다.
#
# ⚠️ 문턱을 **한 점으로 정하지 않는다** (D39-a). 잡음의 최대 연결성분 크기를
#    **인자로 받고**, 없으면 판정을 거부한다. 그 값은 A5000 이 잰다.
# ⚠️ **해시 비교 금지.** 점유(before/after occupancy) 비교로 유도한다 —
#    재디코딩하면 152/152 청크가 전부 다른 해시를 낸다 (§4 실패 기록).


class OverflowThresholdUnknown(RuntimeError):
    """잡음의 최대 연결성분 크기를 모른 채 overflow 를 걸러내려 했다 (D54 · D39-a).

    문턱을 한 점으로 정하면 이 프로젝트가 반복해 물린 자리로 돌아간다
    (D5 · D16 · D33 · D37 · D39 전부 같은 병). 값은 A5000 이 잰다.
    """


@dataclass(frozen=True)
class OverflowResult:
    """마스크 밖 신규 복셀의 분류 결과. **원시 개수를 항상 들고 다닌다** (D37 의 교훈)."""

    n_overflow_voxels: int          # 마스크 밖 신규 복셀 총수
    n_signal_voxels: int            # 그중 문턱 이상 덩어리에 속한 것
    n_noise_voxels: int             # 고립·소덩어리 (잡음으로 본 것)
    signal_chunks: List[str]        # 부기에 **추가되는** 청크
    noise_chunks_skipped: List[str] # 필터가 걸러낸 청크
    threshold: int                  # 쓴 문턱 (잡음 최대 연결성분 크기)
    component_sizes: List[int]      # 성분 크기 분포 — 문턱 재조정의 근거

    def describe(self) -> str:
        return (
            f"overflow {self.n_overflow_voxels}복셀 → 신호 {self.n_signal_voxels} / "
            f"잡음 {self.n_noise_voxels} (문턱 {self.threshold}) · "
            f"부기 추가 {len(self.signal_chunks)}청크 · "
            f"걸러낸 {len(self.noise_chunks_skipped)}청크 · "
            f"성분 크기 {sorted(self.component_sizes, reverse=True)[:6]}"
        )


def classify_overflow(
    before_cells,
    after_cells,
    mask,
    *,
    noise_max_component: Optional[int] = None,
) -> OverflowResult:
    """마스크 밖 신규 복셀을 **신호와 잡음으로 가른다** (D54).

    Args:
        before_cells / after_cells: 점유. **해시가 아니라 점유로 유도한다.**
        mask: `MaskResult`. `mask.dilated`(마스크 + halo) 밖을 overflow 로 본다.
        noise_max_component: 잡음의 **최대 연결성분 크기**. 문턱은 이 값 + 1 이다.
            None 이면 판정을 거부한다 — 한 점으로 정하지 않는다 (D39-a).

    Raises:
        OverflowThresholdUnknown: 잡음 크기를 모른다.
    """
    import numpy as np  # noqa: PLC0415

    if noise_max_component is None:
        raise OverflowThresholdUnknown(
            "잡음의 최대 연결성분 크기를 모른 채 overflow 를 거를 수 없다 (D54). "
            "실측 overflow 602복셀 중 404 가 전역 리메시 잡음이었고, 필터 없이 넣으면 "
            "80/124 청크(64.5%)가 델타에 끌려와 절감률이 죽는다. 반대로 문턱을 "
            "임의로 정하면 실제 구조(측면 머리 각 300+복셀)를 버릴 수 있다. "
            "그 값은 A5000 이 대조군으로 잰다."
        )
    if noise_max_component < 0:
        raise ValueError(f"잡음 성분 크기가 음수다: {noise_max_component}")

    threshold = int(noise_max_component) + 1

    b = np.asarray(before_cells, dtype=np.int64).reshape(-1, 3)
    a = np.asarray(after_cells, dtype=np.int64).reshape(-1, 3)
    edited = np.asarray(mask.dilated, dtype=np.int64).reshape(-1, 3)

    bc = set(voxel_code(b).tolist()) if b.size else set()
    ec = set(voxel_code(edited).tolist()) if edited.size else set()
    ac = voxel_code(a) if a.size else np.zeros((0,), dtype=np.int64)

    # 마스크 + halo **밖**의 **신규** 복셀만 본다.
    keep = [i for i, c in enumerate(ac.tolist()) if c not in bc and c not in ec]
    overflow = a[keep] if keep else np.zeros((0, 3), dtype=np.int64)

    signal, noise = [], []
    sizes: List[int] = []
    for comp_cells in _component_cell_groups(overflow):
        sizes.append(len(comp_cells))
        (signal if len(comp_cells) >= threshold else noise).extend(comp_cells)

    sig_keys = _chunk_keys_of(signal)
    noise_keys = sorted(set(_chunk_keys_of(noise)) - set(sig_keys))

    return OverflowResult(
        n_overflow_voxels=int(overflow.shape[0]),
        n_signal_voxels=len(signal),
        n_noise_voxels=len(noise),
        signal_chunks=sig_keys,
        noise_chunks_skipped=noise_keys,
        threshold=threshold,
        component_sizes=sizes,
    )


def _component_cell_groups(cells):
    """연결 성분별 셀 목록. `metrics.components` 는 요약만 주므로 여기서 다시 묶는다."""
    import numpy as np  # noqa: PLC0415

    from server.metrics import _NEIGHBORS_26  # noqa: PLC0415

    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.shape[0] == 0:
        return []
    codes = voxel_code(a)
    index = {int(c): i for i, c in enumerate(codes)}
    seen = np.zeros(a.shape[0], dtype=bool)
    groups = []
    for start in range(a.shape[0]):
        if seen[start]:
            continue
        seen[start] = True
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(tuple(int(v) for v in a[i]))
            nb = a[i] + _NEIGHBORS_26
            nb = nb[np.all((nb >= 0) & (nb < 64), axis=1)]
            for code in voxel_code(nb):
                j = index.get(int(code))
                if j is not None and not seen[j]:
                    seen[j] = True
                    stack.append(j)
        groups.append(members)
    return groups


def _chunk_keys_of(cells) -> List[str]:
    import numpy as np  # noqa: PLC0415

    if not len(cells):
        return []
    a = np.asarray(list(cells), dtype=np.int64).reshape(-1, 3)
    return chunk_keys_sorted(voxel_to_chunk(a))


def derive_bookkeeping_with_overflow(
    spliced,
    produced_keys,
    *,
    overflow: OverflowResult,
) -> Bookkeeping:
    """부기 = (마스크 + halo) ∪ **신호 overflow 청크** (D54).

    잡음 청크는 **넣지 않는다** — 넣으면 절감률이 죽는다. 그 판단의 근거는
    개수가 아니라 연결성이다.
    """
    base = derive_bookkeeping(spliced, produced_keys)
    if not overflow.signal_chunks:
        return base

    produced = set(produced_keys)
    book = sorted(set(base.book) | set(overflow.signal_chunks))
    changed, removed = split_removed(book, produced)
    merged = Bookkeeping(
        zone1=base.zone1,
        zone2=sorted(set(base.zone2) | (set(overflow.signal_chunks) - set(base.zone1))),
        book=book,
        changed=changed,
        removed=removed,
    )
    verify_bookkeeping(merged)
    return merged
