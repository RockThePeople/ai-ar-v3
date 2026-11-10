"""작업 상태 머신 (app-api 1.0.1 4.1) — 실제·가짜 모드가 같은 코드를 지난다.

- GPU 단계(생성·보정+렌더+인페인팅·인페인팅 재시도·3D 편집)는 **단일 큐 스레드**가 차례로 돈다.
  `queue_pos` 는 그 큐에서의 순번이다.
- 자산당 진행 중 작업 1건(잠금). 끝 상태와 `needs_redraw` 는 잠금을 풀었다.
- `tick(now)` 이 10분 실행 제한(TIMEOUT)과 30분 3D 확인 자동 discard 를 처리한다. 시계는 주입할 수 있다(시험).
- 재기동: 실행 상태(queued 포함)였던 작업은 `failed(SERVER_RESTART)`. 2D·3D 확인 대기는 그대로 이어진다
  (3D 확인은 expires_at 이 지나면 discard).
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .backends import StageError, fake_classify, fake_refine, fake_translate
from .config import Settings
from .store import ChunkStore, Db

ACTIVE = {"queued", "generating", "refining", "inpainting", "awaiting_2d_review", "editing_3d", "awaiting_3d_review"}
RUNNING = {"queued", "generating", "refining", "inpainting", "editing_3d"}
TERMINAL = {"committed", "discarded", "cancelled", "failed", "needs_redraw"}


class AppError(Exception):
    """HTTP 오류 응답(계약 1절 모양)."""

    def __init__(self, status: int, code: str, message: str, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Engine:
    def __init__(self, s: Settings, db: Db, store: ChunkStore, backend, clock: Callable[[], float] = time.time,
                 start_thread: bool = True) -> None:
        self.s, self.db, self.store, self.be, self.clock = s, db, store, backend, clock
        self.lock = threading.RLock()
        self.jobs: Dict[str, dict] = {}
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.order: List[str] = []          # 큐 순서(대기 중 job_id)
        self.running: Optional[str] = None
        self._recover()
        if start_thread:
            threading.Thread(target=self._loop, daemon=True, name="gpu-queue").start()
            threading.Thread(target=self._ticker, daemon=True, name="ticker").start()

    # ── 영속 ───────────────────────────────────────────────────────
    def _recover(self) -> None:
        for j in self.db.jobs("state NOT IN ('committed','discarded','cancelled','failed','needs_redraw')"):
            if j["state"] in RUNNING:
                self._finish(j, "failed", code="SERVER_RESTART", msg="서버 재기동으로 실행 중 작업이 중단됐다", save=False)
                if j["kind"] == "generate":
                    self._drop_unborn(j)
            self.jobs[j["job_id"]] = j
            self.db.save_job(j)

    def _save(self, j: dict) -> None:
        j["updated_at"] = self.clock()
        self.db.save_job(j)

    def get(self, job_id: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(job_id)
        if j is not None:
            return j
        rows = self.db.jobs("job_id=?", (job_id,), 1)
        return rows[0] if rows else None

    def active_job(self, asset_id: str) -> Optional[dict]:
        with self.lock:
            for j in self.jobs.values():
                if j["asset_id"] == asset_id and j["state"] in ACTIVE:
                    return j
        return None

    # ── 공통 전이 ──────────────────────────────────────────────────
    def _set(self, j: dict, state: Optional[str] = None, stage: Optional[str] = None, progress: Optional[float] = None,
             **kw) -> None:
        with self.lock:
            if state and state != j["state"]:
                j["state"] = state
                j["state_since"] = self.clock()
                j["history"].append({"t": self.clock(), "state": state, "stage": stage or j["stage"]})
            if stage:
                j["stage"] = stage
            if progress is not None:
                j["progress"] = float(progress)
            j.update(kw)
            self._save(j)

    def _finish(self, j: dict, state: str, code: Optional[str] = None, msg: Optional[str] = None, save: bool = True) -> None:
        j["state"], j["state_since"] = state, self.clock()
        j["history"].append({"t": self.clock(), "state": state, "code": code})
        j["finished_at"] = self.clock()
        if code:
            j["error_code"], j["error_message"] = code, msg
        j["expires_at"] = None
        if save:
            self._save(j)

    def _new_job(self, kind: str, asset_id: str, device: str, qr_id: Optional[str], **kw) -> dict:
        t = self.clock()
        j = {"job_id": new_id("j"), "kind": kind, "asset_id": asset_id, "qr_id": qr_id, "device": device,
             "state": "queued", "stage": "queued", "progress": 0.0, "created_at": t, "updated_at": t,
             "state_since": t, "attempt": 1 if kind == "edit" else None,
             "max_attempts": self.s.max_attempts if kind == "edit" else None,
             "refine": None, "preview": None, "review": None, "result": None, "expires_at": None,
             "error_code": None, "error_message": None, "history": [{"t": t, "state": "queued"}],
             "attempts": [], "mock_fail": None, "finished_at": None, **kw}
        with self.lock:
            self.jobs[j["job_id"]] = j
        self._save(j)
        return j

    def _enqueue(self, j: dict, task: str) -> None:
        with self.lock:
            self.order.append(j["job_id"])
        self.q.put((task, j["job_id"]))

    def queue_pos(self, job_id: str) -> Optional[int]:
        with self.lock:
            return self.order.index(job_id) if job_id in self.order else None

    # ── 생성 ───────────────────────────────────────────────────────
    def create_asset(self, qr_id: str, device: str, prompt: str, seed: int, mock_fail: Optional[str] = None) -> dict:
        with self.lock:
            if self.db.asset_by_qr(qr_id) is not None:
                raise AppError(409, "QR_TAKEN", "이미 자산이 있거나 생성 중이다")
            asset_id = f"a-{uuid.uuid4().hex[:8]}"
            self.db.x("INSERT INTO assets(asset_id,qr_id,creator,scene_prompt,version,created_at,source) VALUES(?,?,?,?,0,?,?)",
                      (asset_id, qr_id, device, None, self.clock(), "generate"))
            j = self._new_job("generate", asset_id, device, qr_id, prompt=prompt, seed=seed, mock_fail=mock_fail)
        self._enqueue(j, "generate")
        return j

    def _drop_unborn(self, j: dict) -> None:
        """생성 실패 → QR 을 다시 비운다(판본 0 인 자산 행 삭제)."""
        a = self.db.asset(j["asset_id"])
        if a is not None and a["version"] == 0:
            self.db.x("DELETE FROM assets WHERE asset_id=?", (j["asset_id"],))
            self.store.delete_asset(j["asset_id"])

    def _run_generate(self, j: dict) -> None:
        self._set(j, "generating", "t2i", 0.0)
        prompt_en = fake_translate(j["prompt"])
        out = self.be.generate(j["asset_id"], prompt_en, int(j["seed"]), self._progress(j))
        if j["state"] != "generating":
            return                                        # TIMEOUT 등으로 이미 끝났다
        self._set(j, stage="chunking", progress=0.95)
        self.store.put_version(j["asset_id"], 1, out["blobs"])
        if out.get("slat_coords"):
            self.store.put_slat_coords(j["asset_id"], 1, out["slat_coords"])
        self.db.x("UPDATE assets SET version=1, scene_prompt=? WHERE asset_id=?", (prompt_en, j["asset_id"]))
        self.db.x("INSERT OR REPLACE INTO commits VALUES(?,?,?,?)", (j["asset_id"], 1, j["job_id"], self.clock()))
        j["result"] = {"to_version": 1, "manifest_uri": f"/v2/assets/{j['asset_id']}/manifest.v1.json"}
        j["progress"] = 1.0
        self._finish(j, "committed")

    # ── 편집 ───────────────────────────────────────────────────────
    def create_edit(self, asset_id: str, device: str, base_version: int, instruction: str, camera: dict,
                    cells: List[list], mock_fail: Optional[str] = None) -> dict:
        """검사 순서(1.0.1 3.3): 입력(라우트) → 404 → 403 → 잠금 → 판본."""
        with self.lock:
            a = self.db.asset(asset_id)
            if a is None or a["version"] == 0 and not a["is_sample"]:
                raise AppError(404, "ASSET_NOT_FOUND", f"자산 없음: {asset_id}")
            if a["is_sample"]:
                raise AppError(403, "READ_ONLY", "샘플 자산은 읽기 전용이다")
            act = self.active_job(asset_id)
            if act is not None:
                if act["state"] == "awaiting_3d_review":
                    raise AppError(409, "UNCOMMITTED_PENDING", "3D 확인을 기다리는 판본이 있다", {"job_id": act["job_id"]})
                raise AppError(409, "ASSET_LOCKED", "진행 중 작업이 있다", {"job_id": act["job_id"]})
            if int(base_version) != int(a["version"]):
                raise AppError(409, "VERSION_CONFLICT", f"최신 판본은 v{a['version']}",
                               {"latest_version": int(a["version"])})
            j = self._new_job("edit", asset_id, device, a["qr_id"], base_version=int(base_version),
                              instruction=instruction, camera=camera, cells=cells, mock_fail=mock_fail,
                              idem=hashlib.sha256(f"{asset_id}|{base_version}|{uuid.uuid4().hex}".encode()).hexdigest()[:32])
        self._enqueue(j, "refine")
        return j

    def _scene(self, asset_id: str) -> str:
        a = self.db.asset(asset_id)
        return (a["scene_prompt"] if a else "") or ""

    def _run_refine(self, j: dict, reason: Optional[str] = None) -> None:
        self._set(j, "refining", "refining", 0.1)
        self._mock_gate(j)
        if self.be.name == "mock":
            time.sleep(self.be.DUR["refine"] * self.s.mock_time_scale)
        j["refine"] = fake_refine(self._scene(j["asset_id"]), j["instruction"], reason)
        self._save(j)
        self._run_render_inpaint(j, new_render=True)

    def _run_render_inpaint(self, j: dict, new_render: bool) -> None:
        if j["state"] not in ("refining", "inpainting", "queued"):
            return
        self._mock_gate(j)
        d = self._preview_dir(j, j["attempt"])
        if new_render or not j.get("render"):
            self._set(j, "inpainting", "rendering", 0.1)
            r = self.be.render(j["asset_id"], j["base_version"], j["camera"], j["cells"])
            rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "render.png").write_bytes(r["render_png"])
            (rd / "mask.png").write_bytes(r["mask_png"])
            j["render"] = {"render_id": r["render_id"], "record": r.get("record")}
        self._set(j, "inpainting", "inpainting", 0.3)
        rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
        rpng, mpng = (rd / "render.png").read_bytes(), (rd / "mask.png").read_bytes()
        seed = 0 + j["attempt"] - 1                       # 내부 규약 5절: 기본 seed + attempt − 1
        edit = self.be.inpaint(rpng, mpng, j["refine"]["new_scene_prompt"], seed)
        if j["state"] != "inpainting":
            return
        d.mkdir(parents=True, exist_ok=True)
        (d / "render.png").write_bytes(rpng)
        (d / "mask.png").write_bytes(mpng)
        (d / "edit.png").write_bytes(edit)
        base = f"/app/v1/jobs/{j['job_id']}/preview/{j['attempt']}"
        j["preview"] = {"attempt": j["attempt"], "render_url": f"{base}/render.png",
                        "edit_url": f"{base}/edit.png", "mask_url": f"{base}/mask.png"}
        j["attempts"].append({"attempt": j["attempt"], "seed": seed, "prompt": j["refine"]["new_scene_prompt"],
                              "render_id": j["render"]["render_id"], "t": self.clock()})
        self._set(j, "awaiting_2d_review", "inpainting", 1.0)

    def _run_edit3d(self, j: dict) -> None:
        self._set(j, "editing_3d", "editing_3d", 0.05)
        self._mock_gate(j)
        d = self._preview_dir(j, j["attempt"])
        render = {"render_id": j["render"]["render_id"], "render_png": (d / "render.png").read_bytes(),
                  "mask_png": (d / "mask.png").read_bytes()}
        out = self.be.edit3d(j["asset_id"], j["base_version"], render, (d / "edit.png").read_bytes(), j["cells"],
                             j["idem"], self._progress(j))
        if j["state"] != "editing_3d":
            return
        self._set(j, stage="chunking", progress=0.95)
        entries = self.store.put_staging(j["asset_id"], j["job_id"], out["to_version"], out["blobs"])
        j["worker_job_id"] = out["worker_job_id"]
        j["edit_stats"] = out.get("stats")
        j["result"] = {"base_version": j["base_version"], "to_version": out["to_version"], "chunks": entries,
                       "removed_chunk_ids": out["removed"]}
        j["expires_at"] = self.clock() + self.s.review3d_s
        self._set(j, "awaiting_3d_review", "chunking", 1.0)

    # ── 리뷰 ───────────────────────────────────────────────────────
    def _own(self, job_id: str, device: str) -> dict:
        j = self.get(job_id)
        if j is None:
            raise AppError(404, "JOB_NOT_FOUND", f"작업 없음: {job_id}")
        if j["device"] != device:
            raise AppError(403, "NOT_OWNER", "작업을 만든 기기만 할 수 있다")
        return j

    def review2d(self, job_id: str, device: str, accept: bool, reason: Optional[str]) -> dict:
        with self.lock:
            j = self._own(job_id, device)
            if j["state"] != "awaiting_2d_review":
                raise AppError(409, "WRONG_STATE", f"지금 상태: {j['state']}")
            if accept:
                self._set(j, "editing_3d", "editing_3d", 0.0)
                self._enqueue(j, "edit3d")
                return j
            if not (reason or "").strip():
                raise AppError(400, "INVALID_REQUEST", "거절에는 reason 이 필요하다")
            j["_reject_stage"] = True
            self._set(j, stage="classifying")
            cat, guide = fake_classify(reason)
            j["review"] = {"reason": reason, "category": cat, "guidance": guide}
            if j["attempt"] >= self.s.max_attempts:
                self._finish(j, "failed", "RETRY_LIMIT", "2D 거절 3회")
                return j
            if cat == "region":
                self._finish(j, "needs_redraw")
                return j
            j["attempt"] += 1
            if cat == "prompt":
                self._set(j, "refining", "refining", 0.0)
                self._enqueue(j, "refine_again")
            else:
                self._set(j, "inpainting", "inpainting", 0.0)
                self._enqueue(j, "inpaint_again")
            return j

    def review3d(self, job_id: str, device: str, keep: bool) -> dict:
        with self.lock:
            j = self._own(job_id, device)
            if j["state"] != "awaiting_3d_review":
                raise AppError(409, "WRONG_STATE", f"지금 상태: {j['state']}")
            if keep:
                self._commit(j)
            else:
                self._discard(j, "사용자 되돌림")
            return j

    def cancel(self, job_id: str, device: str) -> dict:
        with self.lock:
            j = self._own(job_id, device)
            if j["state"] != "awaiting_2d_review":
                raise AppError(409, "WRONG_STATE", f"지금 상태: {j['state']}")
            self._finish(j, "cancelled")
            return j

    def _commit(self, j: dict) -> None:
        to = int(j["result"]["to_version"])
        try:
            self.be.commit(j["asset_id"], to, j["idem"])
        except StageError as e:
            raise AppError(503 if e.code == "WORKER_UNREACHABLE" else 500, e.code, str(e))
        blobs = self.store.staging_blobs(j["asset_id"], j["job_id"])
        self.store.put_version(j["asset_id"], to, blobs, j["result"]["removed_chunk_ids"])
        self.store.drop_staging(j["asset_id"], j["job_id"])    # 판본 경로로 옮겼다(해시 같음)
        coords = self.be.slat_coords(j["asset_id"], to)
        if coords:
            self.store.put_slat_coords(j["asset_id"], to, coords)
        new_scene = (j.get("refine") or {}).get("new_scene_prompt")
        self.db.x("UPDATE assets SET version=?, scene_prompt=COALESCE(?, scene_prompt) WHERE asset_id=?",
                  (to, new_scene, j["asset_id"]))
        self.db.x("INSERT OR REPLACE INTO commits VALUES(?,?,?,?)", (j["asset_id"], to, j["job_id"], self.clock()))
        j["result"]["manifest_uri"] = f"/v2/assets/{j['asset_id']}/manifest.v{to}.json"
        self._finish(j, "committed")

    def _discard(self, j: dict, why: str) -> None:
        note = self.be.discard(j["asset_id"], j.get("worker_job_id") or "")
        self.store.drop_staging(j["asset_id"], j["job_id"])
        j["discard_note"] = f"{why}{' · ' + note if note else ''}"
        self._finish(j, "discarded")

    # ── 시간 ───────────────────────────────────────────────────────
    def tick(self, now: Optional[float] = None) -> None:
        t = self.clock() if now is None else now
        with self.lock:
            for j in list(self.jobs.values()):
                if j["state"] in RUNNING - {"queued"} and t - j["state_since"] > self.s.timeout_s:
                    self._finish(j, "failed", "TIMEOUT", "실행 10분 초과")
                    if j["kind"] == "generate":
                        self._drop_unborn(j)
                elif j["state"] == "awaiting_3d_review" and j["expires_at"] and t >= j["expires_at"]:
                    self._discard(j, "30분 자동 폐기")

    def _ticker(self) -> None:
        while True:
            time.sleep(5)
            try:
                self.tick()
            except Exception:                 # noqa: BLE001
                traceback.print_exc()

    # ── 큐 ─────────────────────────────────────────────────────────
    def _progress(self, j: dict):
        def p(stage: str, frac: float) -> None:
            if j["state"] in RUNNING:
                self._set(j, stage=stage, progress=frac)
        return p

    def _mock_gate(self, j: dict) -> None:
        if j.get("mock_fail"):
            code = j["mock_fail"]
            j["mock_fail"] = None
            raise StageError(code, f"X-Mock-Fail 로 강제 실패: {code}")

    def _preview_dir(self, j: dict, attempt: int) -> Path:
        return self.s.data_dir / "jobs" / j["job_id"] / str(attempt)

    def run_task(self, task: str, job_id: str) -> None:
        with self.lock:
            if job_id in self.order:
                self.order.remove(job_id)
            j = self.jobs.get(job_id)
            self.running = job_id
        try:
            if j is None or j["state"] in TERMINAL:
                return
            if task == "generate":
                self._mock_gate(j)
                self._run_generate(j)
            elif task == "refine":
                self._run_refine(j)
            elif task == "refine_again":
                self._run_refine(j, reason=(j.get("review") or {}).get("reason"))
            elif task == "inpaint_again":
                self._run_render_inpaint(j, new_render=False)
            elif task == "edit3d":
                self._run_edit3d(j)
        except StageError as e:
            if j["state"] not in TERMINAL:
                self._finish(j, "failed", e.code, str(e))
                if j["kind"] == "generate":
                    self._drop_unborn(j)
        except Exception as e:                    # noqa: BLE001
            traceback.print_exc()
            if j and j["state"] not in TERMINAL:
                code = {"generate": "GENERATE_FAILED", "refine": "RENDER_FAILED", "refine_again": "RENDER_FAILED",
                        "inpaint_again": "INPAINT_FAILED", "edit3d": "EDIT_FAILED"}[task]
                self._finish(j, "failed", code, f"{type(e).__name__}: {e}")
                if j["kind"] == "generate":
                    self._drop_unborn(j)
        finally:
            with self.lock:
                self.running = None

    def _loop(self) -> None:
        while True:
            task, jid = self.q.get()
            self.run_task(task, jid)

    def drain(self) -> None:
        """시험용: 큐에 쌓인 것을 현재 스레드에서 모두 처리한다(start_thread=False 일 때)."""
        while not self.q.empty():
            task, jid = self.q.get()
            self.run_task(task, jid)

    # ── 삭제·평가 ──────────────────────────────────────────────────
    def delete_qr(self, qr_id: str, device: str) -> None:
        with self.lock:
            a = self.db.asset_by_qr(qr_id)
            if a is None:
                raise AppError(404, "QR_EMPTY", "자산 없음")
            if a["creator"] != device:
                raise AppError(403, "NOT_CREATOR", "생성자만 삭제할 수 있다")
            act = self.active_job(a["asset_id"])
            if act is not None:
                raise AppError(409, "ASSET_LOCKED", "진행 중 작업이 있다", {"job_id": act["job_id"]})
            aid = a["asset_id"]
            try:
                note = self.be.delete(aid)
            except StageError as e:
                raise AppError(409, e.code, str(e))
            self.store.delete_asset(aid)
            for jj in self.db.jobs("asset_id=?", (aid,)):
                self.jobs.pop(jj["job_id"], None)
                d = self.s.data_dir / "jobs" / jj["job_id"]
                if d.is_dir():
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
            self.db.x("DELETE FROM jobs WHERE asset_id=?", (aid,))
            self.db.x("DELETE FROM commits WHERE asset_id=?", (aid,))
            self.db.x("DELETE FROM assets WHERE asset_id=?", (aid,))
            self.db.x("UPDATE ratings SET asset_deleted=1 WHERE asset_id=?", (aid,))
            if note:   # 앱 쪽 삭제는 진행한다. A5000 쪽이 남았을 수 있다는 기록
                print(f"[delete] {aid}: {note}", flush=True)
                (self.s.data_dir / "logs").mkdir(parents=True, exist_ok=True)
                with open(self.s.data_dir / "logs" / "worker-notes.log", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": self.clock(), "op": "delete", "asset_id": aid, "note": note},
                                       ensure_ascii=False) + "\n")

    def rate_range(self, asset_id: str, device: str) -> Optional[List[int]]:
        """이 기기의 직전 평가 이후(없으면 생성 이후) 커밋된 편집 판본 범위. 없으면 None."""
        last = self.db.q("SELECT MAX(v_to) m FROM ratings WHERE asset_id=? AND device=?", (asset_id, device))[0]["m"]
        lo = int(last) + 1 if last is not None else 2
        vs = [r["version"] for r in self.db.q(
            "SELECT version FROM commits WHERE asset_id=? AND version>=? AND version>=2 ORDER BY version", (asset_id, lo))]
        return [vs[0], vs[-1]] if vs else None

    def rate(self, asset_id: str, device: str, stars: int) -> dict:
        with self.lock:
            a = self.db.asset(asset_id)
            if a is None or (a["version"] == 0 and not a["is_sample"]):
                raise AppError(404, "ASSET_NOT_FOUND", f"자산 없음: {asset_id}")
            if a["is_sample"]:
                raise AppError(403, "READ_ONLY", "샘플 자산은 평가하지 않는다")
            rng = self.rate_range(asset_id, device)
            if rng is None:
                raise AppError(409, "NOTHING_TO_RATE", "직전 평가 이후 커밋된 편집이 없다")
            rid = new_id("rt")
            fb = {"agent": "vhl-edit-service", "value": stars * 20, "valueDecimals": 0, "tag1": "starred",
                  "tag2": "", "endpoint": "/app/v1", "asset_id": asset_id, "version_range": rng,
                  "stars": stars, "client": device, "created_at": self.clock()}
            raw = json.dumps(fb, ensure_ascii=False, sort_keys=True).encode()
            p = self.s.data_dir / "ratings" / f"{rid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
            h = hashlib.sha256(raw).hexdigest()
            out = {"rating_id": rid, "asset_id": asset_id, "stars": stars, "value": stars * 20, "decimals": 0,
                   "tag1": "starred", "version_range": rng, "feedback_uri": f"/app/v1/ratings/{rid}.json",
                   "feedback_hash": h}
            self.db.x("INSERT INTO ratings(rating_id,asset_id,device,stars,v_from,v_to,created_at,body) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, asset_id, device, stars, rng[0], rng[1], self.clock(), json.dumps(out)))
            return out
