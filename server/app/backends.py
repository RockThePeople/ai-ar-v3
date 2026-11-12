"""워커 호출 — 실제(A5000·3090 인페인팅·T2I) / 가짜(계약 7절). 상태 머신은 이 인터페이스만 본다.

실제 호출 순서는 내부 규약(Voxhammer-light `services/common/app-internal-v1.md`) 4·5절이다.
- 생성: 인페인팅 워커 `POST /release` → `server/t2i.py:render_rgba`(Z-Image → BiRefNet) →
  `POST /reload`(**finally**) → A5000 `POST /v2/trellis/generate`.
  Voxhammer-light R1 `reference/rtx3090/t2i_swap.py:generate_rgba` 와 같은 순서다(리포가 달라 import 하지 않는다).
- 편집: A5000 `/render` → 인페인팅 `/inpaint` → A5000 `/edit`(multipart) → 잡 폴링 → staging 청크 수신.
"""

from __future__ import annotations

import io
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from .config import Settings
from .store import ChunkStore


class StageError(Exception):
    """작업을 `failed(code)` 로 끝내는 오류. code 는 계약 6절 값이다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Progress = Callable[[str, float], None]


# ══════════════════════════════════════════════════════════ 가짜 LLM (S1)
def fake_refine(scene_prompt: str, instruction: str, reason: Optional[str] = None) -> dict:
    """규칙 기반 보정. `change X into Y` / `X → Y` 꼴이면 대상·대체를 뽑는다. 실제 LLM 은 S2."""
    target, repl, op = None, instruction.strip(), "replace"
    m = re.search(r"(?:change|turn|make|replace)\s+(.+?)\s+(?:into|to|with)\s+(.+)", instruction, re.I)
    if m:
        target, repl = m.group(1).strip(), m.group(2).strip()
    elif re.search(r"\b(remove|delete)\b|없애|제거", instruction, re.I):
        op = "remove"
    elif re.search(r"\badd\b|추가|달아", instruction, re.I):
        op = "add"
    base = (scene_prompt or "").strip().rstrip(".")
    new = f"{base}, {instruction.strip()}" if base else instruction.strip()
    if reason:
        new = f"{new} ({reason.strip()})"
    return {"target_element": target, "replacement": repl, "op": op, "new_scene_prompt": new}


def fake_classify(reason: str) -> tuple[str, str]:
    """계약 7절 가짜 모드 키워드 규칙(S1 은 실제 모드도 이 규칙)."""
    r = reason.lower()
    if any(w in r for w in ("area", "region")) or "영역" in reason:
        return "region", "선택 영역을 다시 그려 새 편집을 보내 주세요"
    if "prompt" in r or "프롬프트" in reason:
        return "prompt", "지시를 다시 해석해 보정부터 다시 합니다"
    return "quality", "같은 지시로 seed 를 바꿔 다시 그립니다"


def fake_translate(prompt: str) -> str:
    return prompt.strip()


# ══════════════════════════════════════════════════════════ 실제
def _json_or_text(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return r.text[:500]


def _fdesc(name: str, b: bytes) -> dict:
    """S9: 보낸 파일 설명(이름·바이트·sha256). 이미지 자체는 작업 폴더에 따로 남는다."""
    return {"filename": name, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}


class RealBackend:
    name = "real"

    def __init__(self, s: Settings, store: ChunkStore) -> None:
        self.s, self.store = s, store

    def _a(self, timeout: float = 120.0) -> httpx.Client:
        h = {"X-Blockedit-Key": self.s.a5000_key} if self.s.a5000_key else {}
        return httpx.Client(base_url=self.s.a5000_url, headers=h, timeout=timeout)

    def _w(self, timeout: float = 900.0) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.s.inpaint_port}", timeout=timeout)

    @staticmethod
    def _err(r: httpx.Response, code: str, what: str) -> StageError:
        try:
            b = r.json()
            msg = f"{what}: {r.status_code} {b.get('error_code')} {b.get('message')}"
        except ValueError:
            msg = f"{what}: {r.status_code} {r.text[:200]}"
        return StageError(code, msg)

    def _call(self, fn, code: str, what: str):
        try:
            return fn()
        except httpx.HTTPError as e:
            raise StageError("WORKER_UNREACHABLE", f"{what}: {type(e).__name__}: {e}") from e

    # 상태
    def health(self) -> dict:
        out = {}
        for name, url in (("a5000", f"{self.s.a5000_url}/v2/trellis/health"),
                          ("inpaint", f"http://127.0.0.1:{self.s.inpaint_port}/health")):
            try:
                r = httpx.get(url, timeout=1.5)
                out[name] = "up" if r.status_code == 200 and r.json().get("ok") else "down"
            except (httpx.HTTPError, ValueError):
                out[name] = "down"
        return out

    # 생성
    def generate(self, asset_id: str, prompt: str, seed: int, progress: Progress, trace: Optional[dict] = None) -> dict:
        from .. import t2i
        trace = trace if trace is not None else {}

        progress("t2i", 0.05)
        try:
            with self._w(timeout=600) as w:
                self._call(lambda: w.post("/release"), "WORKER_UNREACHABLE", "인페인팅 워커 release")
            try:
                png, timing = t2i.render_rgba(prompt, seed=seed)
            except t2i.T2IUnavailable as e:
                raise StageError("T2I_FAILED", str(e)) from e
        finally:
            try:
                with self._w(timeout=120) as w:
                    w.post("/reload")
            except httpx.HTTPError:
                pass   # 워커 재기동이 필요하다 — health 가 down 으로 보인다
        progress("generating", 0.3)
        meta = {"asset_id": asset_id, "seed": seed}
        with self._a(timeout=60) as c:
            r = self._call(lambda: c.post("/v2/trellis/generate", data={"meta": json.dumps(meta)},
                                          files={"image": ("input.png", png, "image/png")}),
                           "GENERATE_FAILED", "A5000 generate")
            trace["submit"] = {"t": round(self._now(), 3), "endpoint": "POST /v2/trellis/generate", "status": r.status_code,
                               "response": _json_or_text(r)}
            if r.status_code != 202:
                raise self._err(r, "GENERATE_FAILED", "A5000 generate")
            res = self._poll(c, r.json()["job_id"], "GENERATE_FAILED", progress, "generating", trace)
            progress("chunking", 0.9)
            blobs = self._fetch_version(c, asset_id, 1, res)
        raw = {"t2i": {"command": timing.get("command"), "settings": timing.get("settings"),
                       "prompt_effective": timing.get("prompt_effective"), "seed": seed},
               "a5000_generate": {"endpoint": "POST /v2/trellis/generate (multipart: meta + image)", "meta": meta,
                                  "files": {"image": _fdesc("input.png", png)}, "submit_response": r.json(),
                                  "job_result": res}}
        return {"blobs": blobs, "t2i_timing": timing, "png": png, "raw": raw}

    # 편집
    def render(self, asset_id: str, base_version: int, camera: dict, cells: List[list]) -> dict:
        body = {"asset_id": asset_id, "base_version": base_version,
                "mask": {"mode": "voxels", "voxels": cells, "halo_margin_voxels": 2, "grid_source": "slat_coords"}}
        if self.s.render_mode == "camera":
            body["camera"] = camera
        else:
            body["view"] = None   # A1 전 임시: A5000 이 마스크 투영 면적 최대 시점을 고른다
        with self._a(timeout=300) as c:
            r = self._call(lambda: c.post("/v2/trellis/render", json=body), "RENDER_FAILED", "A5000 render")
            if r.status_code != 200:
                raise self._err(r, "RENDER_FAILED", "A5000 render")
            rec = r.json()
            files = {}
            for n in ("2d_render", "2d_mask"):
                g = self._call(lambda n=n: c.get(f"/v2/trellis/renders/{rec['render_id']}/{n}.png"),
                               "RENDER_FAILED", "렌더 파일")
                if g.status_code != 200:
                    raise self._err(g, "RENDER_FAILED", f"렌더 파일 {n}")
                files[n] = g.content
        # camera 렌더 응답의 `view` 는 null 이다(A1, 계약 3.28.0 은 정수를 기대). 앱 서버는 view 를 쓰지 않는다 —
        # 다음 단계(`/edit`)는 render_id·두 PNG 만 넘기고, 클라이언트 응답(작업 객체)에는 view 가 없다.
        # 그래서 BRenderResponse 로 검증하지 않고 필요한 키만 꺼내 내부 기록에 남긴다.
        for k in ("render_id", "render_sha256", "mask_sha256"):
            if not rec.get(k):
                raise StageError("RENDER_FAILED", f"A5000 render 응답에 {k} 가 없다")
        record = {"request": {"endpoint": "POST /v2/trellis/render (JSON)", "body": body}, "response": rec,
                  "view": rec.get("view"), "camera_used": rec.get("camera_used"), "mode": self.s.render_mode,
                  "render_sha256": rec["render_sha256"], "mask_sha256": rec["mask_sha256"],
                  "timing": (rec.get("renderer") or {}).get("timing")}
        return {"render_id": rec["render_id"], "render_png": files["2d_render"], "mask_png": files["2d_mask"],
                "record": record}

    def inpaint(self, render_png: bytes, mask_png: bytes, prompt: str, seed: int, info: Optional[dict] = None) -> bytes:
        if info is not None:                              # S9: 보낸 것 그대로
            info.update({"endpoint": "POST /inpaint?wait=1 (multipart: image, mask, prompt, seed)",
                         "form": {"prompt": prompt, "seed": str(seed)},
                         "files": {"image": _fdesc("2d_render.png", render_png), "mask": _fdesc("2d_mask.png", mask_png)}})
        with self._w() as w:
            r = self._call(lambda: w.post("/inpaint", params={"wait": "1"},
                                          files={"image": ("2d_render.png", render_png, "image/png"),
                                                 "mask": ("2d_mask.png", mask_png, "image/png")},
                                          data={"prompt": prompt, "seed": str(seed)}),
                           "INPAINT_FAILED", "인페인팅")
            if r.status_code != 200:
                raise self._err(r, "INPAINT_FAILED", "인페인팅")
            g = self._call(lambda: w.get(f"/jobs/{r.json()['job_id']}/2d_edit.png"), "INPAINT_FAILED", "인페인팅 결과")
            if g.status_code != 200:
                raise self._err(g, "INPAINT_FAILED", "인페인팅 결과")
            if info is not None:
                info["worker_response"] = r.json()
                info["settings"] = ((r.json().get("result") or {}).get("settings"))
                info["settings_source"] = "워커 응답 result.settings (POST /inpaint?wait=1 응답)"
                info["received"] = _fdesc("2d_edit.png", g.content)
            return g.content

    def edit3d(self, asset_id: str, base_version: int, render: dict, edit_png: bytes, cells: List[list],
               idem: str, progress: Progress, trace: Optional[dict] = None) -> dict:
        trace = trace if trace is not None else {}
        meta = {"asset_id": asset_id, "base_version": base_version, "seed": 0, "idempotency_key": idem,
                "render_id": render["render_id"],
                "mask": {"mode": "voxels", "voxels": cells, "halo_margin_voxels": 2, "grid_source": "slat_coords"}}
        with self._a(timeout=120) as c:
            r = self._call(lambda: c.post("/v2/trellis/edit", data={"meta": json.dumps(meta)}, files={
                "2d_render": ("2d_render.png", render["render_png"], "image/png"),
                "2d_mask": ("2d_mask.png", render["mask_png"], "image/png"),
                "2d_edit": ("2d_edit.png", edit_png, "image/png")}), "EDIT_FAILED", "A5000 edit")
            # 접수는 재시도하지 않는다(S10): A5000 은 같은 idempotency_key 라도 앞 작업이 끝나 staging 이 기록되기 전에는
            # 재생하지 않고 새 작업을 만든다(services/a5000_edit/app.py replayable) → 중복 GPU 작업·같은 staging 폴더 경합
            trace["submit"] = {"t": round(self._now(), 3), "endpoint": "POST /v2/trellis/edit", "status": r.status_code,
                               "response": _json_or_text(r)}
            if r.status_code != 202:
                raise self._err(r, "EDIT_FAILED", "A5000 edit")
            jid = r.json()["job_id"]
            submit = r.json()
            res = self._poll(c, jid, "EDIT_FAILED", progress, "editing_3d", trace)
            progress("chunking", 0.9)
            blobs = {}
            for ch in res.get("chunks", []):
                k = ch["chunk_id"]
                g = self._call(lambda k=k: c.get(f"/v2/assets/{asset_id}/staging/{jid}/chunks/{k}.cbin"),
                               "EDIT_FAILED", "staging 청크")
                if g.status_code != 200:
                    raise self._err(g, "EDIT_FAILED", f"staging 청크 {k}")
                blobs[k] = g.content
        raw = {"endpoint": "POST /v2/trellis/edit (multipart: meta + 2d_render + 2d_mask + 2d_edit)", "meta": meta,
               "files": {"2d_render": _fdesc("2d_render.png", render["render_png"]),
                         "2d_mask": _fdesc("2d_mask.png", render["mask_png"]),
                         "2d_edit": _fdesc("2d_edit.png", edit_png)},
               "text": "텍스트 입력 없음(이미지 조건) — meta 에 프롬프트 필드가 없고 파일 3장만 보낸다",
               "submit_response": submit, "job_result": res}
        return {"to_version": int(res["to_version"]), "blobs": blobs,
                "removed": list(res.get("removed_chunk_ids") or []), "worker_job_id": jid,
                "stats": res.get("stats"), "raw": raw}

    def commit(self, asset_id: str, to_version: int, idem: str) -> None:
        with self._a() as c:
            r = self._call(lambda: c.post(f"/v2/trellis/assets/{asset_id}/commit",
                                          json={"to_version": to_version, "idempotency_key": idem}),
                           "EDIT_FAILED", "A5000 commit")
            if r.status_code != 200:
                raise self._err(r, "EDIT_FAILED", "A5000 commit")

    def discard(self, asset_id: str, worker_job_id: str) -> Optional[str]:
        """A2 의 `/discard`. 실패해도 앱 쪽 되돌림은 진행하고 사유를 돌려준다(기록용)."""
        try:
            with self._a() as c:
                r = c.post(f"/v2/trellis/assets/{asset_id}/discard", json={"job_id": worker_job_id})
            return None if r.status_code == 200 else f"A5000 discard {r.status_code} {r.text[:120]}"
        except httpx.HTTPError as e:
            return f"discard {type(e).__name__}"

    def delete(self, asset_id: str) -> Optional[str]:
        try:
            with self._a() as c:
                r = c.delete(f"/v2/trellis/assets/{asset_id}")
            if r.status_code == 409:
                raise StageError("ASSET_LOCKED", "A5000 에 진행 중 작업이 있다")
            return None if r.status_code in (200, 204) else f"A5000 delete {r.status_code} {r.text[:120]}"
        except httpx.HTTPError as e:
            return f"delete {type(e).__name__}"

    def slat_coords(self, asset_id: str, version: int) -> Optional[list]:
        """A5000 판본의 slat 좌표. **A5000 에 아직 이 경로가 없다**(S1 보고 요청). 없으면 None."""
        try:
            with self._a(timeout=30) as c:
                r = c.get(f"/v2/trellis/assets/{asset_id}/v{version}/slat_coords.json")
            if r.status_code != 200:
                return None
            b = r.json()
            return b.get("coords") if isinstance(b, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    def fetch_committed(self, asset_id: str, version: int) -> Dict[str, dict]:
        """A5000 커밋 판본의 manifest(샘플 가져오기용)."""
        with self._a() as c:
            r = c.get(f"/v2/trellis/assets/{asset_id}/v{version}/manifest.json")
            if r.status_code != 200:
                raise self._err(r, "INTERNAL", f"manifest {asset_id} v{version}")
            return r.json()

    def fetch_chunk(self, uri_path: str) -> bytes:
        """`/v2/assets/{a}/chunks/{k}.v{m}.cbin` → A5000 의 `/v2/trellis/assets/...` 로 받는다."""
        p = uri_path.replace("/v2/assets/", "/v2/trellis/assets/", 1)
        with self._a() as c:
            r = c.get(p)
            if r.status_code != 200:
                raise self._err(r, "INTERNAL", f"chunk {p}")
            return r.content

    # S10: 폴링 연결 오류 재시도. 간격 1·2·4·8 s 뒤 8 s 고정, 연속 실패가 60 s 를 넘으면 WORKER_UNREACHABLE.
    POLL_BACKOFF = (1.0, 2.0, 4.0, 8.0)
    POLL_GIVEUP_S = 60.0
    POLL_INTERVAL_S = 0.5
    _sleep = staticmethod(time.sleep)                   # 시험에서 바꾼다
    _now = staticmethod(time.time)

    def _poll(self, c: httpx.Client, jid: str, code: str, progress: Progress, stage: str,
              trace: Optional[dict] = None) -> dict:
        """A5000 잡 폴링. 회차마다 trace["polls"] 에 시각·HTTP 상태 또는 오류·A5000 상태를 남긴다.

        - 200: 계속(to_version 이 있으면 끝). 그 사이 실패가 있었으면 연속 실패를 초기화한다.
        - 연결 오류(ReadError·ConnectError·RemoteProtocolError·타임아웃 = httpx.TransportError)·본문 없는 5xx: 재시도.
        - 5xx 인데 A5000 오류 본문(error_code)이 있으면 = A5000 작업 실패(job_status 가 실패를 5xx 로 낸다) → 바로 실패.
        - 4xx(작업 없음 등): 바로 실패(지금과 같다).
        """
        polls = trace.setdefault("polls", []) if trace is not None else []
        t0 = self._now()
        fail_since: Optional[float] = None
        fails = 0
        n = 0
        while True:
            n += 1
            ts = self._now()
            rec: dict = {"n": n, "t": round(ts, 3)}
            polls.append(rec)
            problem = None
            try:
                r = c.get(f"/v2/trellis/jobs/{jid}")
            except httpx.TransportError as e:
                problem = rec["error"] = f"{type(e).__name__}: {e}"
            else:
                rec["status"] = r.status_code
                if r.status_code == 200:
                    b = r.json()
                    rec.update({k: b.get(k) for k in ("state", "stage", "progress") if k in b})
                    if "to_version" in b:
                        rec["state"] = rec.get("state") or "succeeded"
                        if trace is not None:
                            trace["result"] = {"t": round(ts, 3), "polls": n, "retries": sum(1 for p in polls if "retry_wait_s" in p)}
                        return b
                    if fails:
                        rec["recovered_after_s"] = round(ts - fail_since, 3)
                    fail_since, fails = None, 0
                    progress(stage, min(0.85, 0.3 + (ts - t0) / 120))
                    self._sleep(self.POLL_INTERVAL_S)
                    continue
                body = None
                try:
                    body = r.json()
                except ValueError:
                    pass
                rec["body"] = body if body is not None else r.text[:200]
                if r.status_code < 500 or (isinstance(body, dict) and body.get("error_code")):
                    if trace is not None:
                        trace["result"] = {"t": round(ts, 3), "polls": n, "failed": f"HTTP {r.status_code}"}
                    raise self._err(r, code, f"A5000 잡 {jid}")
                problem = f"HTTP {r.status_code}(오류 본문 없음)"
            fails += 1
            fail_since = fail_since if fail_since is not None else ts
            if ts - fail_since > self.POLL_GIVEUP_S:
                if trace is not None:
                    trace["result"] = {"t": round(ts, 3), "polls": n, "failed": "WORKER_UNREACHABLE",
                                       "fail_window_s": round(ts - fail_since, 3)}
                raise StageError("WORKER_UNREACHABLE", f"잡 폴링: 연속 실패 {fails}회 {ts - fail_since:.0f} s(> "
                                                       f"{self.POLL_GIVEUP_S:.0f} s), 마지막 {problem}")
            wait = self.POLL_BACKOFF[min(fails - 1, len(self.POLL_BACKOFF) - 1)]
            rec["retry_wait_s"] = wait
            self._sleep(wait)

    def _fetch_version(self, c: httpx.Client, asset_id: str, n: int, res: dict) -> Dict[str, bytes]:
        blobs = {}
        for ch in res.get("chunks", []):
            k = ch["chunk_id"]
            g = self._call(lambda k=k: c.get(f"/v2/trellis/assets/{asset_id}/chunks/{k}.v{n}.cbin"),
                           "GENERATE_FAILED", "생성 청크")
            if g.status_code != 200:
                raise self._err(g, "GENERATE_FAILED", f"생성 청크 {k}")
            blobs[k] = g.content
        return blobs


# ══════════════════════════════════════════════════════════ 가짜 (계약 7절)
class MockBackend:
    """GPU·LLM 을 부르지 않는다. 원천은 앱 저장소의 `APP_MOCK_SOURCE` 자산(판본 1..N)과 고정 PNG 3장.

    k 번째 편집은 원천 v k → v k+1 변경분을 쓰고, 원천 끝을 넘으면 마지막 변경분(v N-1 → v N)을 반복한다.
    (계약은 sample-jeep v1–v6. A2 전에는 원천이 `sample-moto` v1 + A-M1 편집 결과 v2 다 — S1 보고서.)
    """

    name = "mock"
    DUR = {"generate": 3.0, "refine": 1.0, "inpaint": 2.0, "edit3d": 2.0}

    def __init__(self, s: Settings, store: ChunkStore, fixtures: Path) -> None:
        self.s, self.store, self.fx = s, store, fixtures
        self.fail: Dict[str, str] = {}      # job_id → X-Mock-Fail 코드

    def _sleep(self, k: str) -> None:
        time.sleep(self.DUR[k] * self.s.mock_time_scale)

    def _src_versions(self) -> List[int]:
        return sorted(self.store.versions(self.s.mock_source))

    def health(self) -> dict:
        return {"a5000": "up", "inpaint": "up"}

    def generate(self, asset_id: str, prompt: str, seed: int, progress: Progress, trace: Optional[dict] = None) -> dict:
        progress("generating", 0.5)
        self._sleep("generate")
        src = self.s.mock_source
        blobs = self.store.blobs(src, 1)
        return {"blobs": blobs, "slat_coords": self.store.slat_coords(src, 1)}

    def render(self, asset_id: str, base_version: int, camera: dict, cells: List[list]) -> dict:
        return {"render_id": f"mock-{int(time.time()*1000)}", "render_png": (self.fx / "render.png").read_bytes(),
                "mask_png": (self.fx / "mask.png").read_bytes(), "record": {"mock": True}}

    def inpaint(self, render_png: bytes, mask_png: bytes, prompt: str, seed: int, info: Optional[dict] = None) -> bytes:
        if info is not None:
            info.update({"mock": True, "note": "가짜 모드 — 워커 호출 없음, 고정 edit.png", "form": {"prompt": prompt, "seed": seed}})
        self._sleep("inpaint")
        return (self.fx / "edit.png").read_bytes()

    def edit3d(self, asset_id: str, base_version: int, render: dict, edit_png: bytes, cells: List[list],
               idem: str, progress: Progress, trace: Optional[dict] = None) -> dict:
        self._sleep("edit3d")
        vs = self._src_versions()
        k = max(1, base_version)                      # 사본의 k 번째 편집 = 원천 v k → v k+1
        frm = min(k, vs[-1] - 1)
        src_dir = self.store._vdir(self.s.mock_source, frm + 1)
        blobs = {p.stem: p.read_bytes() for p in src_dir.glob("*.cbin")}
        rem = self.store._removed(self.s.mock_source, frm + 1)
        return {"to_version": base_version + 1, "blobs": blobs, "removed": sorted(rem),
                "worker_job_id": f"mock-{idem[:12]}", "stats": {"mock_source_step": [frm, frm + 1]}}

    def commit(self, asset_id: str, to_version: int, idem: str) -> None:
        return None

    def discard(self, asset_id: str, worker_job_id: str) -> Optional[str]:
        return None

    def delete(self, asset_id: str) -> Optional[str]:
        return None

    def slat_coords(self, asset_id: str, version: int) -> Optional[list]:
        """사본 v k 의 좌표 = 원천 v min(k, N) 의 좌표(편집 변경분을 원천에서 가져오므로 같은 물체다)."""
        vs = self._src_versions()
        return self.store.slat_coords(self.s.mock_source, min(int(version), vs[-1])) if vs else None
