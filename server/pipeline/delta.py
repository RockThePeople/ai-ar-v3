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
from typing import Dict, Iterable, List, Mapping, Tuple

from deltacontract.assemble import (  # type: ignore[import-not-found]
    assemble_zones,
    split_removed,
)
from deltacontract.errors import BookkeepingMismatch  # type: ignore[import-not-found]
from deltacontract.partition import diff_chunk_sets  # type: ignore[import-not-found]

from .splice import SpliceResult

__all__ = [
    "Bookkeeping",
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
