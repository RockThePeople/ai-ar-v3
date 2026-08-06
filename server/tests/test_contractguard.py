"""`contractguard` — **선언이 아니라 바이트**로 판정하는가 (D28).

W26b 의 가드는 상류 헬스의 선언을 봤고, 그 선언이 실제로 거짓이었다 (헬스 v4 ·
산출 v3). 그 상황을 **합성으로 재현해** 지금 가드가 막는지 잠근다.
"""

from __future__ import annotations

import struct

import pytest

from deltacontract import CONTRACT_VERSION
from deltacontract.chunkbin import HEADER_SIZE, MAGIC

from server.contractguard import (
    UpstreamContractMismatch,
    assert_chunk_contract,
    chunk_contract_version,
    verify_blobs,
)


def _blob(version: int, magic: bytes = MAGIC) -> bytes:
    """헤더만 있는 최소 `.cbin` 유사 바이트. 판정에 본문은 필요 없다."""
    return struct.pack("<4sIIiiiIIII", magic, version, 0, 0, 0, 0, 0, 0, 0, 0)


def test_reads_version_from_header_not_from_any_declaration():
    assert chunk_contract_version(_blob(CONTRACT_VERSION)) == CONTRACT_VERSION
    assert chunk_contract_version(_blob(3)) == 3          # 옛 판본도 **읽어낸다**


def test_v3_bytes_are_rejected_even_if_upstream_declares_v4():
    """🔴 실제로 겪은 상황이다 — 헬스는 v4, 산출은 v3.

    가드가 헬스를 안 보므로 선언이 무엇이든 결과가 같아야 한다.
    """
    with pytest.raises(UpstreamContractMismatch) as e:
        assert_chunk_contract(_blob(3), where="<EDIT_HOST>")
    assert "v3" in str(e.value) and f"v{CONTRACT_VERSION}" in str(e.value)


def test_current_version_passes():
    assert assert_chunk_contract(_blob(CONTRACT_VERSION)) == CONTRACT_VERSION


def test_not_a_cbin_is_rejected():
    with pytest.raises(UpstreamContractMismatch, match="magic"):
        chunk_contract_version(_blob(CONTRACT_VERSION, magic=b"XXXX"))
    with pytest.raises(UpstreamContractMismatch, match="짧다"):
        chunk_contract_version(b"CBN1")


def test_one_bad_chunk_discards_the_whole_set():
    """일부만 저장하면 자산이 반쪽으로 남는다 — 그게 더 늦게 발견된다."""
    blobs = {f"{i}_0_0": _blob(CONTRACT_VERSION) for i in range(9)}
    blobs["9_0_0"] = _blob(3)
    with pytest.raises(UpstreamContractMismatch) as e:
        verify_blobs(blobs, where="<EDIT_HOST>")
    assert "9_0_0=v3" in str(e.value)


def test_all_good_returns_counts():
    blobs = {f"{i}_0_0": _blob(CONTRACT_VERSION) for i in range(4)}
    assert verify_blobs(blobs) == {CONTRACT_VERSION: 4}


def test_threshold_is_not_hardcoded():
    """판정 문턱을 손으로 적지 않았다 — 계약 상수를 그대로 쓴다."""
    import inspect

    import server.contractguard as g

    src = inspect.getsource(g)
    assert "CONTRACT_VERSION" in src
    assert " == 4" not in src and " != 4" not in src
