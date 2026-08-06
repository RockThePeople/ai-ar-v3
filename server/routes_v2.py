"""Unity 가 부를 `/v2` 라우트 넷. **계약을 새로 짓지 않는다.**

요청·응답 모양은 전부 `contract/python/deltacontract/schemas.py` 다:
`GenerateRequest` · `EditRequest` · `JobStatus` · `PatchPackage` · `ChunkManifest`.
URI 조립은 `uris.py` 가 한다. 여기서 손으로 만드는 문자열은 없다.

────────────────────────────────────────────────────────────────────────
🔴 생성 가드는 **산출 바이트**를 본다 (W26d)
────────────────────────────────────────────────────────────────────────
전 판본은 상류의 `/v2/trellis/health` 선언을 봤는데, 그 선언이 거짓일 수 있다는 걸
실제로 겪었다 — W26 시점에 `<EDIT_HOST>` 헬스는 `contract_version 4` 를 답했고
같은 시점의 산출물은 **v3** 였다. 선언은 증거가 아니다 (D28).

지금은 `server/generate.py` 가 받은 `.cbin` **헤더를 직접 읽어** 저장 직전에 판정한다.
상류가 무엇을 선언하든 결과가 같다. `server/contractguard.py` 가 그 판정이고
`server/tests/test_contractguard.py` 가 "헬스 v4 · 산출 v3" 를 합성으로 재현해 잠근다.
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


from .contractguard import UpstreamContractMismatch  # noqa: F401  (라우트가 잡아 보고한다)


# ══════════════════════════════════════════════════════════ ① 생성
def _source_png(req: GenerateRequest, progress) -> bytes:
    """prompt → RGBA. `server/t2i.py` 가 conda 경계를 subprocess 로 넘는다.

    ⚠️ 실패하면 **자리표시를 내지 않는다.** 빈 이미지를 넘기면 그 뒤 파이프라인이
       전부 정상 동작하면서 다른 물체를 만든다 — 예외가 안 나는 실패다.
    """
    from .t2i import render_rgba

    progress(0.02, "t2i", "prompt → RGBA")
    data, timing = render_rgba(req.raw_prompt, seed=req.seed)
    progress(0.12, "t2i", f"RGBA {len(data):,}B · {timing}")
    return data


@router.post("/v2/assets", response_model=JobStatus)
def create_asset(req: GenerateRequest) -> JobStatus:
    job = JOBS.new()

    def work(progress) -> dict:
        from .generate import run_generate

        # 🔴 사전 점검(헬스 조회)을 하지 않는다. 선언은 증거가 아니다 —
        #    받은 바이트를 **저장 직전에** 검증한다 (server/generate.py).
        asset_id = f"v3-{job.job_id}"
        return run_generate(asset_id, _source_png(req, progress), req.seed, progress)

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
