"""`/app/v1` 라우터(app-api 1.0.1 3절)와 앱 저장소의 `/v2` 청크·manifest·staging·slat 좌표(3.5절)."""

from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from deltacontract import CONTRACT_VERSION  # type: ignore[import-not-found]

from . import models as M
from .config import APP_API
from .engine import ACTIVE, AppError
from .store import AssetNotFound, VersionNotFound

QR_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_CELLS = 65536


def err_response(e: AppError) -> JSONResponse:
    body = {"error_code": e.code, "message": e.message}
    if e.detail is not None:
        body["detail"] = e.detail
    return JSONResponse(body, status_code=e.status)


def build(app_state) -> tuple[APIRouter, APIRouter]:
    """app_state: .settings .db .store .engine .backend"""
    S = app_state
    r = APIRouter(prefix="/app/v1")
    v2 = APIRouter()

    # ── 공통 ───────────────────────────────────────────────────────
    def device(req: Request) -> str:
        if S.settings.shared_token and req.headers.get("X-App-Token") != S.settings.shared_token:
            raise AppError(403, "NOT_OWNER", "X-App-Token 이 맞지 않는다")
        k = (req.headers.get("X-Device-Key") or "").strip()
        if not 1 <= len(k) <= 200:
            raise AppError(400, "INVALID_REQUEST", "X-Device-Key 헤더가 필요하다(1–200자)")
        return k

    async def body(req: Request, model):
        try:
            raw = await req.body()
            return model.model_validate_json(raw or b"{}")
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            loc = ".".join(str(x) for x in first.get("loc", []))
            raise AppError(400, "INVALID_REQUEST", f"본문 형식 오류: {loc} {first.get('msg', '')}".strip())

    def mock_fail(req: Request) -> Optional[str]:
        return req.headers.get("X-Mock-Fail") if S.settings.mock else None

    def job_obj(j: dict, dev: Optional[str]) -> dict:
        now = S.engine.clock()
        end = j.get("finished_at") or now
        o = M.JobObj(
            job_id=j["job_id"], kind=j["kind"], asset_id=j["asset_id"], qr_id=j.get("qr_id"),
            is_mine=(dev is not None and dev == j["device"]), state=j["state"], stage=j["stage"],
            progress=float(j["progress"]), queue_pos=S.engine.queue_pos(j["job_id"]),
            created_at=j["created_at"], updated_at=j["updated_at"], elapsed_s=round(end - j["created_at"], 3),
            attempt=j.get("attempt"), max_attempts=j.get("max_attempts"),
            refine=j.get("refine"), preview=j.get("preview"), review=j.get("review"), result=j.get("result"),
            expires_at=j.get("expires_at"), error_code=j.get("error_code"), error_message=j.get("error_message"))
        return o.model_dump(mode="json")

    # ── health · samples ───────────────────────────────────────────
    @r.get("/health")
    def health():
        w = S.backend.health()
        ad = S.engine.llm
        if S.settings.mock:
            llm = "mock"                                  # 계약 7절 가짜 모드
        elif ad.name != "claude":
            llm = "missing"                               # 키 없음 → 규칙 기반
        else:
            llm = "error" if ad.last_ok is False else "configured"
        return M.HealthResp(ok=True, app_api=APP_API, contract_version=int(CONTRACT_VERSION), mock=S.settings.mock,
                            workers=M.Workers(a5000=w.get("a5000", "down"), inpaint=w.get("inpaint", "down"),
                                              llm=llm)).model_dump(mode="json")

    @r.get("/samples")
    def samples():
        rows = S.db.q("SELECT * FROM assets WHERE is_sample=1 ORDER BY created_at")
        out = []
        for a in rows:
            try:
                vs = sorted(S.store.versions(a["asset_id"]))
            except AssetNotFound:
                continue
            out.append(M.Sample(name=a["sample_name"] or a["asset_id"], asset_id=a["asset_id"], versions=vs,
                                note=a["sample_note"] or ""))
        return M.SamplesResp(samples=out).model_dump(mode="json")

    # ── QR ─────────────────────────────────────────────────────────
    def check_qr(qr_id: str) -> None:
        if not QR_RE.match(qr_id):
            raise AppError(400, "INVALID_QR", f"qr_id 형식 위반: {qr_id!r}")

    @r.get("/qr/{qr_id}")
    def qr_get(qr_id: str, req: Request):
        dev = device(req)
        check_qr(qr_id)
        a = S.db.asset_by_qr(qr_id)
        if a is None:
            raise AppError(404, "QR_EMPTY", "QR 에 묶인 자산이 없다")
        act = S.engine.active_job(a["asset_id"])
        v = int(a["version"])
        rng = S.engine.rate_range(a["asset_id"], dev) if v > 0 else None
        return M.QrResp(
            qr_id=qr_id, asset_id=a["asset_id"], version=v, is_creator=(a["creator"] == dev),
            scene_prompt=a["scene_prompt"] if v > 0 else None,
            manifest_uri=f"/v2/assets/{a['asset_id']}/manifest.v{v}.json" if v > 0 else None,
            slat_coords_uri=f"/v2/assets/{a['asset_id']}/slat_coords.v{v}.json" if v > 0 else None,
            active_job=(M.ActiveJob(job_id=act["job_id"], kind=act["kind"], state=act["state"],
                                    is_mine=act["device"] == dev) if act else None),
            can_rate=rng is not None, rate_range=rng).model_dump(mode="json")

    @r.post("/qr/{qr_id}/asset", status_code=202)
    async def qr_create(qr_id: str, req: Request):
        dev = device(req)
        check_qr(qr_id)
        b = await body(req, M.CreateAssetReq)
        j = S.engine.create_asset(qr_id, dev, b.prompt, b.seed, mock_fail(req))
        return M.CreateResp(job_id=j["job_id"], asset_id=j["asset_id"]).model_dump(mode="json")

    @r.delete("/qr/{qr_id}", status_code=204)
    def qr_delete(qr_id: str, req: Request):
        dev = device(req)
        check_qr(qr_id)
        S.engine.delete_qr(qr_id, dev)
        return Response(status_code=204)

    # ── 편집 ───────────────────────────────────────────────────────
    @r.post("/assets/{asset_id}/edits", status_code=202)
    async def edit_create(asset_id: str, req: Request):
        dev = device(req)
        b = await body(req, M.EditReq)                       # 1. 입력 형식
        prob = M.camera_problem(b.camera)
        if prob:
            raise AppError(400, "INVALID_CAMERA", prob)
        cells = []
        for c in b.volume_cells:
            if len(c) != 3:
                raise AppError(400, "INVALID_REQUEST", "volume_cells 원소는 [i, j, k]")
            cells.append(tuple(min(63, max(0, int(x))) for x in c))
        cells = sorted(set(cells))
        if not cells:
            raise AppError(400, "MASK_EMPTY", "편집 볼륨 셀이 없다")
        if len(cells) > MAX_CELLS:
            raise AppError(400, "MASK_TOO_LARGE", f"편집 볼륨 셀 {len(cells)} > {MAX_CELLS}")
        j = S.engine.create_edit(asset_id, dev, b.base_version, b.instruction,   # 2–5. 404 → 403 → 잠금 → 판본
                                 b.camera.model_dump(mode="json"), [list(c) for c in cells], mock_fail(req))
        return M.EditCreated(job_id=j["job_id"]).model_dump(mode="json")

    @r.get("/jobs/{job_id}")
    def job_get(job_id: str, req: Request):
        dev = req.headers.get("X-Device-Key")
        j = S.engine.get(job_id)
        if j is None:
            raise AppError(404, "JOB_NOT_FOUND", f"작업 없음: {job_id}")
        return job_obj(j, dev)

    @r.post("/jobs/{job_id}/review2d")
    async def review2d(job_id: str, req: Request):
        dev = device(req)
        b = await body(req, M.Review2DReq)
        return job_obj(S.engine.review2d(job_id, dev, b.accept, b.reason), dev)

    @r.post("/jobs/{job_id}/review3d")
    async def review3d(job_id: str, req: Request):
        dev = device(req)
        b = await body(req, M.Review3DReq)
        return job_obj(S.engine.review3d(job_id, dev, b.keep), dev)

    @r.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, req: Request):
        dev = device(req)
        return job_obj(S.engine.cancel(job_id, dev), dev)

    @r.get("/jobs/{job_id}/preview/{attempt}/{name}.png")
    def preview(job_id: str, attempt: int, name: str):
        if name not in ("render", "edit", "mask") or S.engine.get(job_id) is None:
            raise AppError(404, "JOB_NOT_FOUND", "미리보기 없음")
        p = S.settings.data_dir / "jobs" / job_id / str(int(attempt)) / f"{name}.png"
        if not p.is_file():
            raise AppError(404, "JOB_NOT_FOUND", "미리보기 없음")
        return FileResponse(p, media_type="image/png")

    # ── 평가 ───────────────────────────────────────────────────────
    @r.post("/assets/{asset_id}/ratings", status_code=201)
    async def rate(asset_id: str, req: Request):
        dev = device(req)
        b = await body(req, M.RatingReq)
        return M.RatingResp(**S.engine.rate(asset_id, dev, b.stars)).model_dump(mode="json")

    @r.get("/ratings/{rating_id}.json")
    def rating_file(rating_id: str):
        if not re.fullmatch(r"rt-[0-9a-f]{12}", rating_id):
            raise AppError(404, "INVALID_REQUEST", "평가 없음")
        p = S.settings.data_dir / "ratings" / f"{rating_id}.json"
        if not p.is_file():
            raise AppError(404, "INVALID_REQUEST", "평가 없음")
        return FileResponse(p, media_type="application/json")

    # ── /v2 (계약 3.28.0 경로, 앱 저장소) ───────────────────────────
    @v2.get("/v2/assets/{asset_id}/manifest.v{version}.json")
    def manifest(asset_id: str, version: int):
        try:
            return S.store.manifest(asset_id, version).model_dump(mode="json")
        except (AssetNotFound, VersionNotFound) as e:
            raise AppError(404, "ASSET_NOT_FOUND", str(e))

    @v2.get("/v2/assets/{asset_id}/chunks/{name}")
    def chunk(asset_id: str, name: str):
        m = re.fullmatch(r"(-?\d+_-?\d+_-?\d+)\.v(\d+)\.cbin", name)
        if not m:
            raise AppError(400, "INVALID_REQUEST", f"청크 이름 형식: {name}")
        try:
            b = S.store.chunk(asset_id, m.group(1), int(m.group(2)))
        except (AssetNotFound, VersionNotFound) as e:
            raise AppError(404, "ASSET_NOT_FOUND", str(e))
        return Response(b, media_type="application/octet-stream")

    @v2.get("/v2/assets/{asset_id}/staging/{job_id}/chunks/{name}")
    def staging_chunk(asset_id: str, job_id: str, name: str):
        m = re.fullmatch(r"(-?\d+_-?\d+_-?\d+)\.cbin", name)
        if not m:
            raise AppError(400, "INVALID_REQUEST", f"청크 이름 형식: {name}")
        p = S.store.staging_dir(asset_id, job_id) / f"{m.group(1)}.cbin"
        if not p.is_file():
            raise AppError(404, "JOB_NOT_FOUND", "staging 청크 없음(커밋·폐기 뒤 포함)")
        return Response(p.read_bytes(), media_type="application/octet-stream")

    @v2.get("/v2/assets/{asset_id}/slat_coords.v{version}.json")
    def slat_coords(asset_id: str, version: int):
        from ..editreq import build_slat_coords_payload
        coords = S.store.slat_coords(asset_id, version)
        if coords is None:
            try:
                S.store._vdir(asset_id, version)
            except (AssetNotFound, VersionNotFound) as e:
                raise AppError(404, "ASSET_NOT_FOUND", str(e))
            src = S.db.asset(asset_id)
            up = (src["source"] if src and src["source"] and src["source"].startswith("a5000:") else None)
            coords = S.backend.slat_coords(up[6:] if up else asset_id, version)
            if coords is None:
                raise AppError(404, "ASSET_NOT_FOUND",
                               f"{asset_id} v{version} 의 slat 좌표가 없다(A5000 에서 받지 못함). 표면으로 대신 채우지 않는다")
            S.store.put_slat_coords(asset_id, version, coords)
        return build_slat_coords_payload(asset_id, version, coords)

    return r, v2


def job_is_active(j: dict) -> bool:
    return j["state"] in ACTIVE


__all__ = ["build", "err_response", "json"]
