"""생성 잡의 실체 — 상류(`<EDIT_HOST>`) 로 생성하고 **바이트를 검증한 뒤** 저장한다.

🔴 검증은 **저장 직전**이다. 사전 점검(헬스 조회)이 아니다.

사전 점검은 상류의 **선언**을 보는 것이고, 그 선언이 거짓일 수 있다는 걸 실제로 겪었다
(W26: 헬스 v4 · 산출 v3). 우리가 지켜야 하는 것은 "상류가 뭐라고 하는가" 가 아니라
"클라이언트에게 무엇을 건네는가" 이므로, **건네기 직전의 바이트**를 본다.

⇒ 상류가 무엇을 선언하든 결과가 같다. 거짓 선언은 통과하지 못한다.

⚠️ 하나라도 어긋나면 **전부 버린다** (`verify_blobs`). 일부만 저장하면 자산이 반쪽으로
   남고, 반쪽은 열리기까지 해서 "화면이 비었다" 보다 훨씬 늦게 발견된다.

⚠️ 호스트·키는 환경변수로만 받는다 (§6). 이 파일에 값이 없다.
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Dict, Optional

from deltacontract.uris import staging_chunk_uri  # type: ignore[import-not-found]

from .assetstore import STORE
from .contractguard import UpstreamContractMismatch, verify_blobs

__all__ = ["run_generate", "UpstreamUnavailable"]


class UpstreamUnavailable(RuntimeError):
    error_code = "UPSTREAM_UNAVAILABLE"


def _client():
    import httpx

    url, key = os.environ.get("BLOCKEDIT_B_URL"), os.environ.get("BLOCKEDIT_B_KEY")
    if not url or not key:
        raise UpstreamUnavailable(
            "상류 자격증명이 없다 (환경변수 미설정). 추측해서 진행하지 않는다")
    return httpx.Client(base_url=url.rstrip("/"),
                        headers={"X-Blockedit-Key": key}, timeout=60.0)


def run_generate(asset_id: str, rgba_png: bytes, seed: int, progress) -> dict:
    """RGBA → 상류 생성 → **전 청크 검증** → 저장. 성공하면 매니페스트를 낸다."""
    import time

    with _client() as c:
        progress(0.05, "submit", "상류에 제출")
        # ⚠️ 형식은 이미 도는 `_tools/submit_trellis.py` 와 **같아야 한다.** 손으로
        #    다시 정하면 두 벌이 되고, 그때부터 어느 쪽이 진짜인지 매번 확인해야 한다.
        #    meta 는 JSON 문자열 폼 필드다 (본문 JSON 이 아니다).
        import json as _json

        meta = {"asset_id": asset_id, "seed": int(seed), "image_part": "image"}
        r = c.post("/v2/trellis/generate",
                   data={"meta": _json.dumps(meta)},
                   files={"image": ("image.png", rgba_png, "image/png")})
        if r.status_code >= 400:
            raise UpstreamUnavailable(f"생성 제출 실패 {r.status_code}: {r.text[:200]}")
        job_id = r.json()["job_id"]

        progress(0.15, "structure", f"상류 잡 {job_id}")
        deadline = time.time() + 600
        payload = None
        while time.time() < deadline:
            p = c.get(f"/v2/trellis/jobs/{job_id}")
            p.raise_for_status()
            payload = p.json()
            # 🔴 완료 판정은 **응답 모양**으로 한다 (b_client._poll · submit_trellis 와 동일).
            #    state 로 판정하면 상류가 그 필드를 안 채우는 판본에서 영원히 돈다.
            if isinstance(payload, dict) and "chunks" in payload and "to_version" in payload:
                break
            st = payload.get("state")
            if st in ("failed", "cancelled"):
                raise UpstreamUnavailable(f"상류 잡 {st}: {payload.get('error')}")
            progress(0.15 + 0.35 * float(payload.get("progress") or 0),
                     payload.get("stage") or "running", payload.get("stage_detail") or "")
            time.sleep(2)
        if not (payload and "chunks" in payload):
            raise UpstreamUnavailable(f"상류 잡이 제한 시간 안에 안 끝났다: {job_id}")
        version = int(payload["to_version"])

        progress(0.55, "chunk", "청크 수신")
        entries = payload.get("chunks") or []
        blobs: Dict[str, bytes] = {}
        failed: list[str] = []
        for i, e in enumerate(entries):
            # ⚠️ 상류의 BChunkResponse 항목은 `chunk_id` 다 — 계약 매니페스트의
            #    `ChunkEntry.uri` 와 **모양이 다르다** (W3 에서 181청크 전부 404 났던 자리).
            key = e.get("chunk_id") or Path(e["uri"]).name.split(".")[0]
            # 🔴 커밋 전이라 바이트는 staging 에 있고, 경로는 **job_id(j-…)** 를 받는다.
            #    슬롯 디렉터리명(gen-…)이 아니다 — 그걸로 치면 전부 404 다.
            g = c.get(staging_chunk_uri(asset_id, job_id, key))
            if g.status_code != 200:
                failed.append(f"{key}({g.status_code})")
                continue
            data = g.content
            got = hashlib.sha256(data).hexdigest()
            if e.get("hash") and got != e["hash"]:
                failed.append(f"{key}(해시 불일치)")
                continue
            blobs[key] = data
            if i % 50 == 0:
                progress(0.55 + 0.3 * (i / max(len(entries), 1)), "chunk",
                         f"{len(blobs)}/{len(entries)}")
        if failed:
            # 부분 수신은 성공이 아니다 — 반쪽 자산은 열리기까지 해서 더 늦게 발견된다.
            raise UpstreamUnavailable(
                f"청크 {len(failed)}/{len(entries)} 개를 못 받았다 "
                f"(예: {', '.join(failed[:5])}). 부분 저장하지 않는다")

    # 🔴 여기가 가드다. 상류가 무엇을 선언했든 **바이트**로 판정한다.
    progress(0.9, "verify", "계약 판본 검증 (바이트)")
    counts = verify_blobs(blobs, where="<EDIT_HOST>")

    progress(0.95, "store", "저장")
    STORE.put_version(asset_id, version, blobs)
    return {"asset_id": asset_id, "manifest": STORE.manifest(asset_id, version),
            "stage_detail": f"청크 {len(blobs)} · 판본 {counts}"}
