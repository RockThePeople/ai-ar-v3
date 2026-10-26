"""조건 이미지 경로 (계약 3.28.0, Voxhammer-light R9 R-7): ① 조건 렌더 → ② 인페인팅 → ③ 편집.

`VHL_R9=1` 일 때 `voxhammer.run_voxhammer_edit` 가 t2i 조건 생성(`t2i.render_rgba`) 대신
이 모듈로 편집을 **제출**한다. 제출 뒤(잡 폴링·청크 수신·검증·저장)는 기존 경로 그대로다.

    ① POST <EDIT_HOST>/v2/trellis/render            → render_id · 두 해시   (BRenderRequest/Response)
       GET  <EDIT_HOST>/v2/trellis/renders/{id}/2d_render.png · 2d_mask.png
    ② POST 127.0.0.1:<INPAINT_PORT>/inpaint?wait=1    → 2d_edit (마스크 밖 = 2d_render 바이트)
    ③ POST <EDIT_HOST>/v2/trellis/edit  multipart: meta(BEditRequest) + 2d_render · 2d_mask · 2d_edit

규약 원문: Voxhammer-light `services/common/http-contract.md` §1·§3·§4 결정.

🔴 조건을 못 만들면 **조건 없이 진행하지 않는다** (D40, 고객사 회신 A4). 어느 단계든 실패하면
   사유를 붙여 잡을 실패시킨다. 옛 경로(t2i)로 몰래 떨어지지도 않는다 — 그러면 같은 요청이
   토글과 무관하게 다른 조건으로 편집되고, 결과만 봐서는 구분이 안 된다.

🔴 `RENDER_UNKNOWN`(③) 이면 ①부터 **1회만** 다시 한다(§4 결정 1). 두 번째도 같으면 보고한다.
   다시 렌더한 두 해시가 처음과 같으면 ②를 다시 돌리지 않고 처음 `2d_edit` 를 쓴다 —
   입력이 바이트 단위로 같으므로 같은 조건이다(INT4 인페인팅은 같은 시드에서도 회차마다
   조금 달라서, 다시 돌리면 오히려 조건이 바뀐다).

🔴 상류 멱등 키에 `sha256(2d_edit)` 를 넣는다(§4 결정 3). 같은 마스크·프롬프트라도 조건 이미지가
   다르면 다른 편집이다. Unity 가 보낸(또는 라우트가 유도한) 키는 3090 잡을 식별하는 데 그대로 쓰인다.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, Dict, Optional, Tuple

from deltacontract.errors import RenderUnknown  # type: ignore[import-not-found]

from .editreq import derive_idempotency_key
from .upstreamerr import parse_upstream_error, upstream_call

__all__ = ["ConditionFailed", "submit_with_condition", "inpaint_url", "log_timing"]

#: 인페인팅 시드. 정오표 E5 — 조건 이미지는 시드 0(VoxHammer `utils/inpaint.py` 와 같은 값).
#: 편집 시드(`req.seed`)와 다르다. 편집 시드는 TRELLIS 출발 잡음이다.
INPAINT_SEED = int(os.environ.get("INPAINT_SEED", "0"))

INPAINT_WHERE = "3090 인페인팅 워커"


class ConditionFailed(RuntimeError):
    """조건 이미지(①②)를 못 만들었다. 조건 없이 진행하지 않는다."""

    error_code = "CONDITION_FAILED"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def inpaint_url() -> str:
    """localhost 전용 (정오표 E4). 포트만 환경변수로 받는다."""
    return f"http://127.0.0.1:{os.environ.get('INPAINT_PORT') or 8082}"


def _render(c, asset_id: str, req, timings: Dict[str, float]) -> dict:
    """① 렌더 요청 + 파일 두 장. 응답이 선언한 해시와 받은 바이트를 대조한다."""
    body = {
        "asset_id": asset_id,
        "base_version": int(req.base_version),
        "mask": req.mask.model_dump(mode="json"),
        # None 이면 그대로 null 로 넘긴다 — A5000 이 마스크 투영 면적 최대 시점을 고른다(§4-4 수정본).
        "view": _requested_view(req),
    }
    t0 = time.perf_counter()
    r = upstream_call(lambda: c.post("/v2/trellis/render", json=body), action="조건 렌더")
    if r.status_code >= 400:
        raise parse_upstream_error(r.status_code, r.text, action="조건 렌더")
    meta = r.json()
    for k in ("render_id", "render_sha256", "mask_sha256"):
        if not meta.get(k):
            raise ConditionFailed(f"조건 렌더 응답에 {k} 가 없다: {r.text[:200]}")
    view, asked = meta.get("view"), _requested_view(req)
    if not isinstance(view, int) or not 0 <= view <= 4:
        raise ConditionFailed(f"조건 렌더 응답의 view 가 0–4 가 아니다: {view!r}")
    if asked is not None and view != asked:
        # 다른 시점을 렌더했다 — 그 렌더로 편집하면 예외 없이 다른 시점의 조건을 쓴다.
        raise ConditionFailed(f"조건 렌더의 view 가 다르다: 요청 {asked}, 응답 {view}")
    if int(meta.get("base_version", -1)) != int(req.base_version):
        # 다른 판본을 렌더했다 — 그 렌더로 편집하면 예외 없이 다른 부모를 조건으로 쓴다.
        raise ConditionFailed(
            f"조건 렌더의 base_version 이 다르다: 요청 {req.base_version}, 응답 {meta.get('base_version')}")
    t1 = time.perf_counter()
    files = {}
    for name, key in (("2d_render", "render_sha256"), ("2d_mask", "mask_sha256")):
        g = upstream_call(lambda n=name: c.get(f"/v2/trellis/renders/{meta['render_id']}/{n}.png"),
                          action=f"조건 렌더 파일({name})")
        if g.status_code != 200:
            raise parse_upstream_error(g.status_code, g.text, action=f"조건 렌더 파일({name})")
        if _sha(g.content) != meta[key]:
            raise ConditionFailed(
                f"{name}.png 바이트 해시가 렌더 응답과 다르다 (선언 {meta[key][:12]}…, "
                f"받음 {_sha(g.content)[:12]}…) — 전송 중 바뀌었다")
        files[name] = g.content
    t2 = time.perf_counter()
    timings["render_s"] = timings.get("render_s", 0.0) + (t1 - t0)
    timings["render_fetch_s"] = timings.get("render_fetch_s", 0.0) + (t2 - t1)
    timings["render_bytes"] = len(files["2d_render"]) + len(files["2d_mask"])
    return {**meta, **files}


def _requested_view(req) -> Optional[int]:
    v = getattr(req, "view", None)
    return None if v is None else int(v)


def _inpaint(render_png: bytes, mask_png: bytes, prompt: str, timings: Dict[str, float]) -> bytes:
    """② 로컬 R9 워커. 마스크 밖이 2d_render 와 바이트 동일한 `2d_edit` 를 받는다."""
    import httpx

    t0 = time.perf_counter()
    with httpx.Client(base_url=inpaint_url(), timeout=1800.0) as w:
        r = upstream_call(lambda: w.post(
            "/inpaint", params={"wait": "1"},
            data={"prompt": prompt, "seed": str(INPAINT_SEED)},
            files={"image": ("2d_render.png", render_png, "image/png"),
                   "mask": ("2d_mask.png", mask_png, "image/png")}),
            action="인페인팅", where=INPAINT_WHERE)
        if r.status_code >= 400:
            # 워커는 잡 실패를 500 + 잡 본문(error 안에 ErrorBody)으로 준다.
            try:
                body = r.json()
                err = body.get("error") if isinstance(body.get("error"), dict) else body
                text = json.dumps(err)
            except ValueError:
                text = r.text
            raise parse_upstream_error(r.status_code, text, where=INPAINT_WHERE, action="인페인팅")
        job = r.json()
        want = ((job.get("result") or {}).get("sha256") or {}).get("2d_edit")
        g = upstream_call(lambda: w.get(f"/jobs/{job['job_id']}/2d_edit.png"),
                          action="인페인팅 결과", where=INPAINT_WHERE)
        if g.status_code != 200:
            raise parse_upstream_error(g.status_code, g.text, where=INPAINT_WHERE, action="인페인팅 결과")
    edit = g.content
    if want and _sha(edit) != want:
        raise ConditionFailed(f"2d_edit 해시가 워커 보고와 다르다 ({want[:12]}… ≠ {_sha(edit)[:12]}…)")
    timings["inpaint_s"] = timings.get("inpaint_s", 0.0) + (time.perf_counter() - t0)
    spans = {s.get("name"): s.get("seconds") for s in (job.get("result") or {}).get("spans") or []}
    if spans.get("inpaint") is not None:
        timings["inpaint_worker_s"] = float(spans["inpaint"])
    return edit


def _submit(c, body: dict, cond: dict, edit_png: bytes):
    """③ multipart 편집 제출. 상류 응답을 그대로 돌려준다(오류 판정은 부르는 쪽)."""
    meta = {**body, "render_id": cond["render_id"],
            "render_sha256": cond["render_sha256"], "mask_sha256": cond["mask_sha256"]}
    return upstream_call(lambda: c.post(
        "/v2/trellis/edit", data={"meta": json.dumps(meta)},
        files={"2d_render": ("2d_render.png", cond["2d_render"], "image/png"),
               "2d_mask": ("2d_mask.png", cond["2d_mask"], "image/png"),
               "2d_edit": ("2d_edit.png", edit_png, "image/png")}),
        action="편집 제출")


def _upstream_error_code(r) -> Optional[str]:
    try:
        d = r.json()
        return d.get("error_code") if isinstance(d, dict) else None
    except ValueError:
        return None


def submit_with_condition(c, asset_id: str, req, spec, body: dict,
                          progress: Callable[..., None]) -> Tuple[object, dict]:
    """①②③ 을 하고 ③ 의 응답(성공이든 실패든)과 구간 시간을 돌려준다.

    Args:
        c: `<EDIT_HOST>` httpx 클라이언트 (voxhammer._client()).
        body: 기존 BEditRequest 본문(asset_id·base_version·prompt·mask·seed·idempotency_key).
              멱등 키는 여기서 `sha256(2d_edit)` 를 넣어 다시 만든다.
    Returns:
        (③ 응답, info). info = {timings, render_id, render_sha256, mask_sha256, edit_sha256,
        upstream_idempotency_key, render_attempts}
    """
    timings: Dict[str, float] = {}
    prompt = spec.target_prompt or req.raw_prompt
    attempts = 0
    prev: Optional[dict] = None
    edit_png: Optional[bytes] = None
    while True:
        attempts += 1
        asked = _requested_view(req)
        progress(0.1, "render", f"조건 렌더 (view {'자동' if asked is None else asked}, 시도 {attempts})")
        cond = _render(c, asset_id, req, timings)
        same = prev is not None and (cond["render_sha256"], cond["mask_sha256"]) == (
            prev["render_sha256"], prev["mask_sha256"])
        if edit_png is None or not same:
            progress(0.15, "inpaint", "조건 이미지 인페인팅 (3090 로컬)")
            edit_png = _inpaint(cond["2d_render"], cond["2d_mask"], prompt, timings)
        edit_sha = _sha(edit_png)
        key = derive_idempotency_key(asset_id, int(req.base_version), req.raw_prompt,
                                     req.mask.voxels or [], int(req.seed), condition_sha256=edit_sha)
        progress(0.2, "submit", f"<EDIT_HOST> 편집 제출 (op={spec.op}) · 조건 이미지 3장")
        t0 = time.perf_counter()
        r = _submit(c, {**body, "idempotency_key": key}, cond, edit_png)
        timings["submit_s"] = timings.get("submit_s", 0.0) + (time.perf_counter() - t0)
        timings["condition_upload_bytes"] = len(cond["2d_render"]) + len(cond["2d_mask"]) + len(edit_png)
        code = _upstream_error_code(r) if r.status_code >= 400 else None
        if code == RenderUnknown.error_code and attempts == 1:
            prev = cond
            continue                     # ①부터 1회 (§4 결정 1)
        # RENDER_MISMATCH 는 다시 보내도 안 고쳐진다 — 부르는 쪽이 사유 그대로 보고한다.
        _keep(cond, edit_png)
        info = {"timings": {k: round(v, 3) for k, v in timings.items()},
                "render_id": cond["render_id"], "render_sha256": cond["render_sha256"],
                "mask_sha256": cond["mask_sha256"], "edit_sha256": edit_sha,
                "upstream_idempotency_key": key, "render_attempts": attempts,
                "view": cond["view"], "view_requested": _requested_view(req),
                # A5000 이 렌더 응답 `renderer` 에 실어 보내는 자기 쪽 시간·시점 선택 근거(있으면 그대로)
                "render_a5000": {k: (cond.get("renderer") or {}).get(k)
                                 for k in ("timing", "view_auto", "view_areas") if (cond.get("renderer") or {}).get(k) is not None},
                "inpaint_reused": bool(same)}
        if r.status_code >= 400:
            # ③ 이 거절됐다 — 부르는 쪽이 예외로 보고한다. ①② 시간은 여기서 남긴다.
            log_timing(asset_id, req, {**info, "submit_status": r.status_code, "submit_error_code": code})
        return r, info


def _keep(cond: dict, edit_png: bytes) -> None:
    """`R9_KEEP_DIR` 가 있으면 조건 이미지 3장을 남긴다(종단 기록용). 없으면 아무것도 안 한다."""
    d = os.environ.get("R9_KEEP_DIR")
    if not d:
        return
    os.makedirs(d, exist_ok=True)
    for name, b in (("2d_render", cond["2d_render"]), ("2d_mask", cond["2d_mask"]), ("2d_edit", edit_png)):
        with open(os.path.join(d, f"{name}.png"), "wb") as f:
            f.write(b)


def log_timing(asset_id: str, req, info: dict) -> None:
    """`R9_TIMING_LOG` 가 있으면 JSONL 로 한 줄 남긴다(종단 계측용). 없으면 아무것도 안 한다.

    성공한 편집은 `voxhammer.run_voxhammer_edit` 가 ③ 의 잡 대기·청크 수신 시간을 더해 부른다.
    """
    path = os.environ.get("R9_TIMING_LOG")
    if not path:
        return
    rec = {"t": time.time(), "asset_id": asset_id, "base_version": int(req.base_version), **info}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
