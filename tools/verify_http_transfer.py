#!/usr/bin/env python3
"""HTTP 로 받은 청크를 **로컬 사본과 바이트 대조**한다 (W27①).

🔴 판정은 육안이 아니다. "화면이 같으면 전송이 맞다" 로는 **몇 청크가 조용히 빠져도
   통과**한다. W25 에서 APK 를 상대로 만든 그 대조 장치를 이번엔 **네트워크**에 건다.

    GEN_HOST=<host> python3 tools/verify_http_transfer.py [asset_id] [--local assets/moto-b]

⚠️ 호스트는 **환경변수로만** 받는다 (§7). 코드·문서·기본값에 넣지 않는다.
⚠️ 경로를 손으로 만들지 않는다 — `uris.chunk_uri()` 를 쓴다. 3090 이 접두사
   비대칭으로 물렸다 (스테이징은 job_id, 슬롯 디렉터리명은 404).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contract" / "python"))

from deltacontract import assert_contract_compatible, chunk_uri  # noqa: E402


def fetch(base: str, path: str, timeout: float = 20.0) -> bytes:
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("asset_id", nargs="?", default="v3-moto-b")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--port", default=os.environ.get("GEN_PORT", "8083"))
    ap.add_argument("--local", default="assets/moto-b/parent")
    args = ap.parse_args()

    host = os.environ.get("GEN_HOST", "")
    if not host:
        print("❌ GEN_HOST 가 없다. 값은 리포에 두지 않는다 (§7).", file=sys.stderr)
        return 2
    base = f"http://{host}:{args.port}"

    # ── ① manifest + 계약 가드
    t0 = time.perf_counter()
    man = json.loads(fetch(base, f"/v2/assets/{args.asset_id}/manifest.v{args.version}.json"))
    t_man = time.perf_counter() - t0

    contract = man.get("contract", {})
    assert_contract_compatible(contract)          # 🔴 v3 자산이면 여기서 즉시 거부된다
    chunks = man.get("chunks", {})
    print(f"manifest {len(chunks)}청크 · contract_version={contract.get('contract_version')} "
          f"· chunk_size={contract.get('chunk_size')} · {t_man*1000:.0f}ms")

    # ── ② 전 청크 수신 + 바이트 대조
    local_dir = ROOT / args.local
    ok = miss_local = mismatch = 0
    total_bytes = 0
    bad: list[str] = []

    t0 = time.perf_counter()
    for key in sorted(chunks):
        uri = chunk_uri(args.asset_id, key, args.version)      # 손으로 만들지 않는다
        blob = fetch(base, uri)
        total_bytes += len(blob)

        lp = local_dir / f"{key}.cbin"
        if not lp.exists():
            miss_local += 1
            continue
        if hashlib.sha256(blob).hexdigest() == hashlib.sha256(lp.read_bytes()).hexdigest():
            ok += 1
        else:
            mismatch += 1
            if len(bad) < 6:
                bad.append(key)
    dt = time.perf_counter() - t0

    print(f"수신 {len(chunks)}청크 · {total_bytes:,}바이트 · {dt:.2f}s "
          f"({total_bytes/dt/1024:.0f} KB/s · 청크당 {dt/max(1,len(chunks))*1000:.1f}ms)")
    print(f"바이트 일치 {ok}/{len(chunks)} · 불일치 {mismatch} · 로컬 사본 없음 {miss_local}")
    if bad:
        print(f"불일치 예: {bad}")

    if mismatch == 0 and miss_local == 0 and ok == len(chunks):
        print("✅ 전 청크 sha256 일치 — **전송 성립**")
        return 0
    print("❌ 전송이 성립하지 않았다")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
