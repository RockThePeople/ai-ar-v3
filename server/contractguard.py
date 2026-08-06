"""상류가 낸 **바이트**로 계약을 판정한다. 선언을 믿지 않는다.

────────────────────────────────────────────────────────────────────────
🔴 왜 바꿨나 — 선언과 산출이 실제로 갈렸다
────────────────────────────────────────────────────────────────────────
W26b 의 생성 가드는 상류의 `/v2/trellis/health` 가 답한 `contract.contract_version`
을 봤다. **그건 증거가 아니었다** (D28: 라벨은 선언이지 증거가 아니다).

실례가 있다. W26 시점에 `<EDIT_HOST>` 의 헬스는 이미 `contract_version 4` 를
답하고 있었는데, 같은 시점에 그 서버가 낸 `.cbin` 은 **v3** 였다 —
헤더 판본 바이트가 3 이었고 v4 클라이언트가 `ChunkBinError` 로 거부했다.
헬스만 믿는 가드는 그 상황을 **통과시킨다.**

그래서 이 모듈은 `.cbin` 헤더를 직접 읽는다. 계약이 정한 레이아웃 그대로다:

    struct.unpack_from("<4sI", blob, 0)  →  (MAGIC, contract_version)
    MAGIC = b"CBN1"  ·  판본은 **offset 4** 의 uint32

⚠️ 판정 문턱을 손으로 적지 않는다. `deltacontract.CONTRACT_VERSION` 과 비교할 뿐이라
   계약이 올라가면 이 코드는 그대로 따라간다.

⚠️ `decode()` 를 판정에 쓰지 않는다. 그건 판본이 맞을 때만 통과하므로 "왜 틀렸나"
   를 못 말한다 — 우리는 **몇 번인지**를 알아야 상류에 보고할 수 있다.
"""

from __future__ import annotations

import struct
from typing import Dict, Iterable, Mapping, Tuple

from deltacontract import CONTRACT_VERSION  # type: ignore[import-not-found]
from deltacontract.chunkbin import HEADER_SIZE, MAGIC  # type: ignore[import-not-found]

__all__ = [
    "UpstreamContractMismatch",
    "chunk_contract_version",
    "assert_chunk_contract",
    "verify_blobs",
]


class UpstreamContractMismatch(RuntimeError):
    """상류가 낸 바이트가 우리 계약이 아니다. **조용히 저장하지 않는다.**"""

    error_code = "UPSTREAM_CONTRACT_MISMATCH"


def chunk_contract_version(blob: bytes) -> int:
    """`.cbin` 바이트 → 그 파일이 **스스로 밝힌** 계약 판본.

    Raises:
        UpstreamContractMismatch: 헤더가 아니거나 magic 이 다르다.
    """
    if len(blob) < HEADER_SIZE:
        raise UpstreamContractMismatch(
            f"헤더보다 짧다: {len(blob)} bytes (헤더 {HEADER_SIZE})")
    magic, ver = struct.unpack_from("<4sI", blob, 0)
    if magic != MAGIC:
        raise UpstreamContractMismatch(
            f"magic 이 {magic!r} 다 ({MAGIC!r} 이어야 한다) — .cbin 이 아니다")
    return int(ver)


def assert_chunk_contract(blob: bytes, where: str = "") -> int:
    """판본이 다르면 거부한다. 통과하면 그 판본을 돌려준다."""
    ver = chunk_contract_version(blob)
    if ver != CONTRACT_VERSION:
        raise UpstreamContractMismatch(
            f"{where or '상류'} 가 낸 청크가 계약 v{ver} 다. 이 서버·클라이언트는 "
            f"v{CONTRACT_VERSION} 다. 그대로 내려보내면 클라이언트가 거부해 화면이 "
            f"빈다 — 저장하지 않는다"
        )
    return ver


def verify_blobs(blobs: Mapping[str, bytes], where: str = "") -> Dict[str, int]:
    """전 청크를 검사한다. **하나라도 어긋나면 전부 버린다.**

    일부만 저장하면 자산이 반쪽으로 남고, 그 반쪽은 열리기까지 해서 더 나쁘다 —
    "화면이 비었다" 보다 "일부만 이상하다" 가 훨씬 늦게 발견된다.

    Returns:
        {판본: 개수}. 전부 맞으면 `{CONTRACT_VERSION: n}` 하나뿐이다.
    """
    seen: Dict[str, int] = {}
    bad: list[Tuple[str, int]] = []
    for key, blob in blobs.items():
        try:
            ver = chunk_contract_version(blob)
        except UpstreamContractMismatch as e:
            raise UpstreamContractMismatch(f"{where}/{key}: {e}") from e
        seen[ver] = seen.get(ver, 0) + 1
        if ver != CONTRACT_VERSION and len(bad) < 5:
            bad.append((key, ver))
    if bad:
        detail = ", ".join(f"{k}=v{v}" for k, v in bad)
        raise UpstreamContractMismatch(
            f"{where or '상류'} 청크 {sum(n for v, n in seen.items() if v != CONTRACT_VERSION)}"
            f"/{len(blobs)} 개가 v{CONTRACT_VERSION} 가 아니다 (예: {detail}). "
            f"판본별 개수: {seen}. 하나라도 어긋나면 전부 버린다"
        )
    return seen
