"""Unity 가 부를 `/v2` 라우트 넷. **계약을 새로 짓지 않는다.**

요청·응답 모양은 전부 `contract/python/deltacontract/schemas.py` 다:
`GenerateRequest` · `EditRequest` · `JobStatus` · `PatchPackage` · `ChunkManifest`.
URI 조립은 `uris.py` 가 한다. 여기서 손으로 만드는 문자열은 없다.

────────────────────────────────────────────────────────────────────────
🔴 생성 경로는 **세워 두고 켜지 않는다**
────────────────────────────────────────────────────────────────────────
A5000 `:8082` 가 아직 계약 v3 를 낸다 (W26 실측: `contract_version 3 · chunk_size 8`,
v4 클라이언트가 `ChunkBinError: file=3, local=4` 로 거부). 그대로 연결하면 앱 화면이
**통째로 빈다** — 맥북이 앱에서 겪은 그 예외와 같은 것이다.

그래서 생성 잡은 상류 계약을 **먼저 확인하고**, v3 면 `failed` +
`error_code=UPSTREAM_CONTRACT_MISMATCH` 로 멈춘다. 조용히 v3 청크를 내려보내지 않는다.
A5000 이 v4 로 올라가면 이 검사는 **저절로 통과한다** — 판본을 코드에 안 박았다.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Response

from deltacontract import CONTRACT_VERSION  # type: ignore[import-not-found]
from deltacontract.schemas import (  # type: ignore[import-not-found]
    EditRequest,
    GenerateRequest,
    JobStatus,
)

from .assetstore import STORE, AssetNotFound, VersionNotFound
from .jobs import JOBS

router = APIRouter()


class UpstreamContractMismatch(RuntimeError):
    error_code = "UPSTREAM_CONTRACT_MISMATCH"


def _upstream_contract_version() -> Optional[int]:
    """A5000 이 지금 내는 계약 판본. 모르면 None — **추측하지 않는다.**"""
    url, key = os.environ.get("BLOCKEDIT_B_URL"), os.environ.get("BLOCKEDIT_B_KEY")
    if not url or not key:
        return None
    try:
        import httpx

        r = httpx.get(f"{url.rstrip('/')}/v2/trellis/health",
                      headers={"X-Blockedit-Key": key}, timeout=10.0)
        r.raise_for_status()
        c = r.json().get("contract") or {}
        v = c.get("contract_version")
        return int(v) if isinstance(v, int) else None
    except Exception:                                    # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════ ① 생성
@router.post("/v2/assets", response_model=JobStatus)
def create_asset(req: GenerateRequest) -> JobStatus:
    job = JOBS.new()

    def work(progress) -> dict:
        progress(0.05, "contract", "상류 계약 판본 확인")
        up = _upstream_contract_version()
        if up is None:
            raise UpstreamContractMismatch(
                "상류(생성 백엔드) 계약 판본을 확인할 수 없다. 자격증명이 없거나 "
                "응답하지 않는다 — 추측해서 진행하지 않는다")
        if up != CONTRACT_VERSION:
            raise UpstreamContractMismatch(
                f"상류가 계약 v{up} 를 낸다. 이 서버·클라이언트는 v{CONTRACT_VERSION} 다. "
                f"그대로 내려보내면 클라이언트가 청크를 거부해 화면이 빈다 — 멈춘다")
        # v4 가 되면 여기서 실제 생성으로 이어진다. 지금은 도달하지 않는다.
        raise UpstreamContractMismatch(
            "상류 계약은 맞는데 생성 배선이 아직 없다 — 다음 웨이브다")

    JOBS.run(job.job_id, work)
    return job


# ══════════════════════════════════════════════════════════ ② 편집
@router.post("/v2/assets/{asset_id}/edits", response_model=JobStatus)
def create_edit(asset_id: str, req: EditRequest) -> JobStatus:
    """마스크는 `EditMask` 다 — **복셀 좌표 목록**이고 `grid_source` 는 생략 불가다.

    ⚠️ 여기서 `grid_source` 를 채워 넣지 않는다 (D28-a). 스키마가 거부하게 둔다 —
       서버가 지어낸 격자가 정본을 참칭하는 순간이 그 자리다.
    """
    try:
        STORE.manifest(asset_id, req.base_version)
    except AssetNotFound as e:
        raise HTTPException(404, str(e)) from e
    except VersionNotFound as e:
        raise HTTPException(409, str(e)) from e

    job = JOBS.new(asset_id=asset_id, idempotency_key=req.idempotency_key)
    if job.state != "queued":            # 같은 키의 잡을 재사용했다
        return job

    def work(progress) -> dict:
        from .editrun import run_edit

        return run_edit(asset_id, req, progress)

    JOBS.run(job.job_id, work)
    return job


# ══════════════════════════════════════════════════════════ ③ 폴링
@router.get("/v2/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    job = JOBS.get(job_id)
    if job is None:
        # ⚠️ 잡은 인메모리다. 서버가 재시작하면 사라진다 — "실패" 가 아니라 "없다" 다.
        raise HTTPException(404, f"모르는 잡이다: {job_id} (서버 재시작 시 잡은 사라진다)")
    return job


# ══════════════════════════════════════════════════════════ ④ 청크 전송
@router.get("/v2/assets/{asset_id}/chunks/{name}")
def get_chunk(asset_id: str, name: str) -> Response:
    """`{key}.v{n}.cbin`. 경로 조립은 계약(`uris.py`)이 정한 모양 그대로다."""
    from deltacontract.uris import parse_chunk_uri  # type: ignore[import-not-found]

    try:
        _aid, key, version = parse_chunk_uri(f"/v2/assets/{asset_id}/chunks/{name}")
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(400, f"청크 URI 형식이 아니다: {name} ({e})") from e
    try:
        blob = STORE.chunk(asset_id, key, version)
    except (AssetNotFound, VersionNotFound) as e:
        raise HTTPException(404, str(e)) from e
    return Response(blob, media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


@router.get("/v2/assets/{asset_id}/manifest.v{version}.json")
def get_manifest(asset_id: str, version: int):
    """매니페스트. `contract` 에 chunk_size·chunk_grid_res 가 실려 나간다."""
    try:
        return STORE.manifest(asset_id, version)
    except (AssetNotFound, VersionNotFound) as e:
        raise HTTPException(404, str(e)) from e
