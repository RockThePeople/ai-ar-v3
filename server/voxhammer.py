"""형태 편집(`replace_region` · `add` · `remove`) → `<EDIT_HOST>` VoxHammer.

`recolor` 는 3090 이 로컬로 한다 (기하 불변 · GPU 불필요 · D24). 형태를 바꾸는 op 는
디코더를 돌려야 하므로 상류로 넘긴다 — `dispatch.py` 의 CONSUMERS 표가 정본이고,
여기서 그 표를 다시 쓰지 않는다.

🔴 **op 를 갈아끼우지 않는다.** 상류가 거절하면 거절로 보고한다. 자동 강등
(`replace_region` → `recolor`)은 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를
재게 만든다 (D26).

⚠️ 받은 바이트는 **저장 직전에 계약 판본을 검증한다** (`contractguard`). 상류 헬스의
   선언을 믿지 않는다 — 선언과 산출이 갈린 실례가 있다 (W26).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Dict

from deltacontract import CONTRACT_CONSTANTS, chunk_uri  # type: ignore[import-not-found]
from deltacontract.schemas import (  # type: ignore[import-not-found]
    ChunkEntry,
    ContractInfo,
    PatchPackage,
)
from deltacontract.uris import staging_chunk_uri  # type: ignore[import-not-found]

from .assetstore import STORE
from .contractguard import verify_blobs

__all__ = ["run_voxhammer_edit", "UpstreamEditFailed"]


class UpstreamEditFailed(RuntimeError):
    error_code = "UPSTREAM_EDIT_FAILED"


def _client():
    import httpx

    url, key = os.environ.get("BLOCKEDIT_B_URL"), os.environ.get("BLOCKEDIT_B_KEY")
    if not url or not key:
        raise UpstreamEditFailed("상류 자격증명이 없다 (환경변수 미설정)")
    return httpx.Client(base_url=url.rstrip("/"),
                        headers={"X-Blockedit-Key": key}, timeout=120.0)


def run_voxhammer_edit(asset_id: str, req, spec, progress) -> dict:
    """`BEditRequest` → 상류 → 청크 검증 → 새 판본 → `PatchPackage`.

    ⚠️ 상류 `/v2/trellis/edit` 는 **JSON 만** 받는다 (asset_id · base_version ·
       prompt · mask · seed · idempotency_key). **조건 이미지 슬롯이 없다** —
       조건은 텍스트 프롬프트가 전부다. 이미지 조건이 필요하면 계약 변경이다.
    """
    body = {
        "asset_id": asset_id,
        "base_version": int(req.base_version),
        # 🔴 원문을 그대로 넘긴다. 여기서 프롬프트를 다시 쓰면 상류가 무엇을 받았는지
        #    화면과 로그가 갈린다.
        "prompt": spec.target_prompt or req.raw_prompt,
        "mask": req.mask.model_dump(mode="json"),
        "seed": int(req.seed),
        "idempotency_key": req.idempotency_key,
    }
    with _client() as c:
        progress(0.2, "submit", f"<EDIT_HOST> 편집 제출 (op={spec.op})")
        r = c.post("/v2/trellis/edit", json=body)
        if r.status_code >= 400:
            raise UpstreamEditFailed(
                f"편집 제출 실패 {r.status_code}: {r.text[:300]}")
        job_id = r.json().get("job_id")
        if not job_id:
            raise UpstreamEditFailed(f"job_id 가 없다: {r.text[:200]}")

        progress(0.3, "running", f"상류 잡 {job_id}")
        deadline = time.time() + 900
        payload = None
        while time.time() < deadline:
            p = c.get(f"/v2/trellis/jobs/{job_id}")
            p.raise_for_status()
            payload = p.json()
            # 완료 판정은 **응답 모양**이다 (state 로 판정하면 그 필드를 안 채우는
            # 판본에서 영원히 돈다 — generate 경로와 같은 규약).
            if isinstance(payload, dict) and "chunks" in payload and "to_version" in payload:
                break
            st = payload.get("state")
            if st in ("failed", "cancelled"):
                raise UpstreamEditFailed(f"상류 잡 {st}: {payload.get('error')}")
            progress(0.3 + 0.4 * float(payload.get("progress") or 0),
                     payload.get("stage") or "running", payload.get("stage_detail") or "")
            time.sleep(2)
        if not (payload and "chunks" in payload):
            raise UpstreamEditFailed(f"상류 잡이 제한 시간 안에 안 끝났다: {job_id}")

        to_version = int(payload["to_version"])
        entries = payload.get("chunks") or []
        removed = list(payload.get("removed_chunk_ids") or [])

        progress(0.75, "chunk", f"청크 {len(entries)} 수신")
        blobs: Dict[str, bytes] = {}
        failed = []
        for e in entries:
            key = e.get("chunk_id") or Path(e["uri"]).name.split(".")[0]
            g = c.get(staging_chunk_uri(asset_id, job_id, key))
            if g.status_code != 200:
                failed.append(f"{key}({g.status_code})")
                continue
            if e.get("hash") and hashlib.sha256(g.content).hexdigest() != e["hash"]:
                failed.append(f"{key}(해시 불일치)")
                continue
            blobs[key] = g.content
        if failed:
            raise UpstreamEditFailed(
                f"청크 {len(failed)}/{len(entries)} 를 못 받았다 "
                f"(예: {', '.join(failed[:5])}). 부분 저장하지 않는다")

    # 🔴 가드 — 상류가 무엇을 선언했든 **바이트**로 판정한다.
    progress(0.9, "verify", "계약 판본 검증 (바이트)")
    counts = verify_blobs(blobs, where="<EDIT_HOST>")

    progress(0.95, "store", "새 판본 저장")
    STORE.put_version(asset_id, to_version, blobs)

    changed = {}
    for k, b in blobs.items():
        from deltacontract import decode  # noqa: PLC0415

        d = decode(b)
        changed[k] = ChunkEntry(
            uri=chunk_uri(asset_id, k, to_version),
            hash=hashlib.sha256(b).hexdigest(), byte_length=len(b),
            vertex_count=int(len(d.positions)), index_count=int(len(d.indices)),
            voxel_count=int(getattr(d, "voxel_count", 0) or 0), version=to_version)

    patch = PatchPackage(
        asset_id=asset_id, from_version=int(req.base_version), to_version=to_version,
        contract=ContractInfo(**CONTRACT_CONSTANTS),
        changed_chunks=changed, removed_chunk_ids=removed,
        mask_fingerprint=payload.get("mask_fingerprint"),
        mask_voxels_used=len(req.mask.voxels or []), op="edit")
    return {"patch": patch, "asset_id": asset_id,
            "manifest": STORE.manifest(asset_id, to_version),
            "stage_detail": f"청크 {len(blobs)} · 제거 {len(removed)} · 판본 {counts}"}
