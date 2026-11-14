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

from .backends import StageError
from .cropinpaint import crop_inpaint
from .llm_adapter import (LLMInvalid, LLMUnavailable, banned_words, imperative_words, make_adapter, placement_ok,
                          make_agent_adapter, FakeAgentAdapter,
                          strip_banned, strip_lateral, strip_realworld, style_from_request,
                          subject_head)
from .regionimg import region_images
from ..t2i import DEFAULT_TEMPLATE, app_template

#: S6: 인페인팅 문장 꼬리. 시점·배경 문구는 넣지 않는다(조건 렌더가 라쏘 시점이다)
INPAINT_SUFFIX = ", simple clean 3D game asset style"
#: S14 논문 X3 생성 설정의 TRELLIS seed(`x3/track_a/*/gen/gen.json`, 단계는 A5000 기본 25/25)
PAPER_TRELLIS_SEED = 42
#: S22 이름만으로 정해지는 대상의 T2I 문장 = 영어 이름 + 꼬리. B = 이름만, C = 이름 + 한 얼굴 문구(부정어 없음),
#: D = C + 전신 문구(C 는 헬로키티가 머리만 나옴 — 보고 9절)
NAMED_SUFFIX = {"B": "", "C": ", one face on the front, plain smooth back of the head",
                "D": ", full body, one face on the front, plain smooth back of the head"}
NAMED_METHOD = "D"   # 사용자 확정 2026-11-14(헬로키티 양면 위험은 받아들임)
#: S20 시점 이름(automask.VIEW_AZ 와 같은 순서)
VIEW_NAMES = ("az000", "az090", "az180", "az270")


def inpaint_prompt(new_scene_prompt: str, realstyle: bool, style: Optional[str]) -> str:
    """S11 켬: 장면 문장 + (양식이 있으면 ', {style} style'). 끔: S6 꼬리."""
    base = new_scene_prompt.rstrip(" .")
    if not realstyle:
        return base + INPAINT_SUFFIX
    return base + (f", {style} style" if style else "")
from .config import Settings
from .store import ChunkStore, Db

ACTIVE = {"queued", "generating", "refining", "inpainting", "awaiting_2d_review", "editing_3d", "awaiting_3d_review",
          "masking", "judging"}   # S20 에이전트 상태
RUNNING = {"queued", "generating", "refining", "inpainting", "editing_3d", "masking", "judging"}
TERMINAL = {"committed", "discarded", "cancelled", "failed", "needs_redraw"}


class AppError(Exception):
    """HTTP 오류 응답(계약 1절 모양)."""

    def __init__(self, status: int, code: str, message: str, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


PROMPT_KEYS = ("input", "detailed", "style", "inpaint", "mask_ratio", "region_fit", "refine_retried")


def prompts_of(j: dict) -> dict:
    """S15 app-api 1.0.5 4.4: 작업 객체의 prompts(항상 7키). S15 이전 작업은 input 만 채운다."""
    p = {k: None for k in PROMPT_KEYS}
    p["input"] = j.get("instruction") if j.get("kind") == "edit" else j.get("prompt")
    p.update({k: v for k, v in (j.get("prompts") or {}).items() if k in PROMPT_KEYS})
    return p


def _norm(t: Optional[str]) -> str:
    """S8 비교용: 소문자·공백 하나·끝 마침표 제거."""
    return " ".join((t or "").lower().split()).rstrip(" .")


class Engine:
    def __init__(self, s: Settings, db: Db, store: ChunkStore, backend, clock: Callable[[], float] = time.time,
                 start_thread: bool = True, llm=None, agent_llm=None) -> None:
        self.s, self.db, self.store, self.be, self.clock = s, db, store, backend, clock
        self.llm = llm or make_adapter(s.mock, on_call=db.log_llm)
        # S20 오케스트레이터(AGENT_MODEL). 시험이 llm 만 넘기면 규칙 기반 가짜
        self.agent_llm = agent_llm or (FakeAgentAdapter(db.log_llm) if llm is not None else
                                       make_agent_adapter(s.mock, on_call=db.log_llm))
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
            if stage and stage != j["stage"]:
                j.setdefault("stage_log", []).append({"t": self.clock(), "stage": stage})
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
             "attempts": [], "mock_fail": None, "finished_at": None, "reject_history": [], **kw}
        j["prompts"] = {k: None for k in PROMPT_KEYS}           # S15 4.4: input 은 작업을 만들 때
        j["prompts"]["input"] = j.get("instruction") if kind == "edit" else j.get("prompt")
        with self.lock:
            self.jobs[j["job_id"]] = j
        self._save(j)
        return j

    def _enqueue(self, j: dict, task: str) -> None:
        with self.lock:
            j["enqueued_at"] = self.clock()
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
            self.db.x("INSERT INTO assets(asset_id,qr_id,qr_origin,creator,scene_prompt,version,created_at,source) "
                      "VALUES(?,?,?,?,?,0,?,?)", (asset_id, qr_id, qr_id, device, None, self.clock(), "generate"))
            raw = self.db.qr_raw_prompt(qr_id)            # S14 그대로 모드: 생성도 입력 문장 그대로
            j = self._new_job("generate", asset_id, device, qr_id, prompt=prompt, seed=seed, mock_fail=mock_fail,
                              raw_prompt=raw)
        self._enqueue(j, "generate")
        return j

    def _drop_unborn(self, j: dict) -> None:
        """생성 실패 → QR 을 다시 비운다. 기록은 남긴다(보관, `source=generate_failed`)."""
        a = self.db.asset(j["asset_id"])
        if a is not None and a["version"] == 0:
            # 사용자 삭제가 아니므로 deleted_at 을 두지 않는다 — 실패한 생성 작업은 앱에서 계속 조회된다
            self.db.x("UPDATE assets SET qr_id=NULL, source='generate_failed' WHERE asset_id=?", (j["asset_id"],))

    def _run_generate(self, j: dict) -> None:
        # S15: 상세화를 먼저 하고, detailed·style 을 넣은 뒤에 generating/t2i 로 바꾼다(앱이 t2i 단계에서 본다)
        # S6: 거친 입력을 상세 물체 묘사(subject_detailed)로 바꾼다. 영어 입력도 항상 거친다(계약 3.2 영어 변환 포함).
        # T2I 는 여기에 서버 고정 꼬리를 붙인다(server/t2i.py DEFAULT_TEMPLATE). LLM 실패면 원문으로 진행(생성은 막지 않는다)
        raw = j["prompt"].strip()
        style, template = None, None
        styled = self.s.realstyle and hasattr(self.llm, "detail_style")
        gen_kw: dict = {}
        if j.get("raw_prompt"):
            # S14 그대로 모드: LLM 없이 입력 문장 그대로 T2I(꼬리 없음). 논문 설정: T2I seed = 요청 seed, TRELLIS seed 42·25/25
            prompt_en, template = raw, "{subject}"
            j["translate"] = {"ok": True, "mode": "raw"}
            j["paper_gen"] = {"t2i_seed": int(j["seed"]), "trellis_seed": PAPER_TRELLIS_SEED, "template": template}
            gen_kw = {"t2i_seed": int(j["seed"])}
            j["prompts"] = dict(prompts_of(j), detailed=raw, style=None)    # 그대로 모드: detailed = input
            self._set(j, "generating", "t2i", 0.0)
            return self._generate_with(j, prompt_en, template, None, PAPER_TRELLIS_SEED, gen_kw)
        try:
            if styled:                                   # S11: {subject_detailed, style} + 금지어 검사
                fkw = {"facehint": True} if self.s.facehint else {}
                if self.s.named_nodetail:
                    fkw["named"] = True                  # S22: named_subject·subject_name 도 받는다
                d = self.llm.detail_style(raw, job_id=j["job_id"], **fkw)
                prompt_en, style = strip_realworld(d["subject_detailed"]), d.get("style")   # S13 7
                if self.s.named_nodetail:
                    prompt_en = self._named(j, d, prompt_en)
                j["style"] = {"mode": "user" if style else "real", "style": style, "words": d.get("style_words"),
                              **(d.get("check") or {})}
                j["translate"] = {"ok": True, "mode": "detail_style"}
            else:
                prompt_en = self.llm.detail_prompt(raw, job_id=j["job_id"])
                j["translate"] = {"ok": True, "mode": "detail"}
        except (LLMInvalid, LLMUnavailable) as e:
            prompt_en, j["translate"] = raw, {"ok": False, "mode": "detail_style" if styled else "detail",
                                              "error": f"{type(e).__name__}: {e}"}
        if self.s.realstyle:
            template = app_template(style)
            j.setdefault("style", {"mode": "real", "style": None, "words": None, "fallback": True})
            j["t2i_template"] = template
        # S15: T2I 에 실제로 넣을 문장 전체(양식 꼬리 포함)
        j["prompts"] = dict(prompts_of(j), detailed=(template or DEFAULT_TEMPLATE).replace("{subject}", prompt_en),
                            style=style or "realistic")
        self._set(j, "generating", "t2i", 0.0)
        return self._generate_with(j, prompt_en, template, style, int(j["seed"]), gen_kw)

    def _named(self, j: dict, d: dict, detailed: str) -> str:
        """S22: 이름만으로 모양이 정해지는 대상이면 상세화 문장(지어낸 부품·얼굴 규칙) 대신 영어 이름 + 방식 꼬리.
        양식(style)은 그대로 둔다 — 템플릿이 양식 꼬리를 붙인다. 기록: j['named']."""
        name = d.get("subject_name") if d.get("named_subject") else None
        rec = {"named_subject": bool(name), "subject_name": name, "method": None}
        if name:
            rec.update(method=NAMED_METHOD, detailed_unused=detailed)
            detailed = name + NAMED_SUFFIX[NAMED_METHOD]
        j["named"] = rec
        return detailed

    def _generate_with(self, j: dict, prompt_en: str, template: Optional[str], style: Optional[str], seed: int,
                       gen_kw: dict) -> None:
        j["prompt_en"] = prompt_en                      # = subject_detailed(꼬리 없음), 첫 scene_prompt
        self._save(j)
        trace: dict = {}                                  # S10: A5000 접수·폴링 회차 기록(실패해도 남긴다)
        j.setdefault("a5000_trace", {})["generate"] = trace
        try:
            kw = dict({"template": template} if template else {}, **gen_kw)
            out = self.be.generate(j["asset_id"], prompt_en, seed, self._progress(j), trace=trace, **kw)
        finally:
            self._save(j)
        if out.get("png"):                               # T2I RGBA 보관(S3)
            d = self.s.data_dir / "jobs" / j["job_id"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "t2i.png").write_bytes(out["png"])
            j["t2i"] = {"file": "t2i.png", "timing": out.get("t2i_timing")}
            if out.get("image_png"):                     # S14: 배경 제거 전 원본도 보관
                (d / "image.png").write_bytes(out["image_png"])
                j["t2i"]["raw_file"] = "image.png"
        if out.get("raw"):
            j["raw_generate"] = out["raw"]               # S9: T2I 명령·설정, A5000 /generate 요청·결과 전문
        if j["state"] != "generating":
            return                                        # TIMEOUT 등으로 이미 끝났다
        self._set(j, stage="chunking", progress=0.95)
        self.store.put_version(j["asset_id"], 1, out["blobs"])
        if out.get("slat_coords"):
            self.store.put_slat_coords(j["asset_id"], 1, out["slat_coords"])
        self.db.x("UPDATE assets SET version=1, scene_prompt=?, style=? WHERE asset_id=?", (prompt_en, style, j["asset_id"]))
        self.db.x("INSERT OR REPLACE INTO commits VALUES(?,?,?,?)", (j["asset_id"], 1, j["job_id"], self.clock()))
        j["result"] = {"to_version": 1, "manifest_uri": f"/v2/assets/{j['asset_id']}/manifest.v1.json"}
        j["progress"] = 1.0
        self._finish(j, "committed")
        self.enqueue_views(j["asset_id"], 1)               # S20 2.1: 다음 편집이 기다리지 않게

    # ── 편집 ───────────────────────────────────────────────────────
    def create_edit(self, asset_id: str, device: str, base_version: int, instruction: str, camera: Optional[dict],
                    cells: Optional[List[list]], mock_fail: Optional[str] = None) -> dict:
        """검사 순서(1.0.1 3.3): 입력(라우트) → 404 → 403 → 잠금 → 판본.
        S20: camera·cells 가 둘 다 None 이면 에이전트 편집(1.1.0 10절, 라우트가 APP_AGENT·한쪽만 있음을 막는다)."""
        agent = camera is None and cells is None
        with self.lock:
            a = self.db.live_asset(asset_id)
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
            raw = self.db.qr_raw_prompt(a["qr_id"])        # S14 그대로 모드(작업 생성 때 고정)
            ed_style = None if raw else (self._asset_style_src(asset_id)["style"] or "realistic")   # S15 4.4 편집 style
            kw = {}
            if agent:                                      # S20b: 그대로 모드 마커는 보정 없이 지시문 그대로 인페인팅
                kw = {"mode": "agent", "events": [], "agent_calls": 0,
                      "agent": {"model": getattr(self.agent_llm, "model", None), "attempt": 1, "backtracks": 0,
                                "max_backtracks": self.s.agent_max_backtracks, "verdict": None, "below_bar": False}}
            j = self._new_job("edit", asset_id, device, a["qr_id"], base_version=int(base_version), raw_prompt=raw,
                              instruction=instruction, camera=camera, cells=cells, mock_fail=mock_fail,
                              idem=hashlib.sha256(f"{asset_id}|{base_version}|{uuid.uuid4().hex}".encode()).hexdigest()[:32],
                              **kw)
            if agent:
                j["max_attempts"] = None
        j["prompts"]["style"] = ed_style
        self._save(j)
        self._enqueue(j, "agent" if agent else "refine")
        return j

    def _scene(self, asset_id: str) -> str:
        a = self.db.asset(asset_id)
        return (a["scene_prompt"] if a else "") or ""

    def _asset_style(self, asset_id: str) -> Optional[str]:
        """S11: 자산 양식(None = 기본 실물형)."""
        return self._asset_style_src(asset_id)["style"]

    def _asset_style_src(self, asset_id: str) -> dict:
        """자산 양식과 출처. S11 생성 자산은 기록(style 열, NULL = 실물형). S11 이전 자산(생성 작업에 양식 판정이
        없음)은 생성 원문의 양식어로 정한다(사용자 결정 2026-11-12). 장면 문장은 보지 않는다. 생성 기록이 없으면 실물형."""
        a = self.db.asset(asset_id)
        if a is not None and "style" in a.keys() and a["style"]:
            return {"style": a["style"], "source": "asset"}
        gens = self.db.jobs("asset_id=? AND kind='generate'", (asset_id,), limit=1)
        g = gens[0] if gens else None
        if g is None or "style" in g:
            return {"style": None, "source": "asset" if g is not None else "default"}
        st, w = style_from_request(g.get("prompt") or "")
        return {"style": st, "source": "pre_s11_request", "words": w, "request": g.get("prompt")}

    def _refine_styled(self, j: dict, scene: str, hist: list, changed_from: Optional[dict] = None,
                       region: Optional[dict] = None, instruction: Optional[str] = None, ko: bool = False):
        """S11: 자산 양식을 보정에 알리고, 실물형인데 금지어가 있으면 1회 재요청, 그래도 있으면 지운다.
        S12: 관계어 보정 템플릿, 명령형 동사가 있으면 1회 재요청, 남으면 기록만.
        S13: 라쏘 이미지·mask_ratio(region). mask_ratio < 0.6 인데 주어가 바뀌면, 또는 바뀐 부위가 첫 구에 없으면
        같은 1회 재요청에 묶는다. 'real-world proportions' 문구는 지운다(토글 없음)."""
        rs, rp = self.s.realstyle, self.s.relphrase
        ins_text = instruction or j["instruction"]          # S20: 에이전트가 정한 부위·물건을 앞에 붙인 지시문
        kw: dict = {"changed_from": changed_from} if changed_from else {}
        src_info = self._asset_style_src(j["asset_id"]) if rs else {"style": None}
        st = src_info["style"]
        if rs:
            kw["style"] = {"style": st}
        if rp:
            kw["relphrase"] = True
        if region is not None:
            kw["region"] = {"images": region.get("images"), "mask_ratio": region.get("mask_ratio"),
                            "inpaint_prompt": self.s.inpaintprompt}
            if ko:                                        # S20b: 에이전트 진행 보고용 한국어 요약
                kw["region"]["inpaint_ko"] = True

        def clean(o: dict) -> dict:
            """문구·좌우 정리(S13 7, S13b 2). 인페인팅 문장에는 금지어 삭제도(실물형)."""
            o = dict(o, new_scene_prompt=strip_realworld(o["new_scene_prompt"]))
            ins = ins_text
            for k in ("selected_part", "target_element"):
                if o.get(k):
                    o[k] = strip_lateral(o[k], ins) or o[k]
            if o.get("inpaint_prompt"):
                t = strip_lateral(strip_realworld(o["inpaint_prompt"]), ins)
                if rs and not st:
                    left = banned_words(t, f"{scene} {ins}")
                    if left:
                        t = strip_banned(t, left)
                o["inpaint_prompt"] = t
            return o
        out = clean(self.llm.refine(scene, ins_text, hist, job_id=j["job_id"], **kw))
        if not (rs or rp or region is not None):
            return out, None
        meta: dict = {"retried": False}
        if rs:
            meta.update(mode="user" if st else "real", style=st, banned_found=[], removed=[],
                        style_source=src_info.get("source"))
            if src_info.get("source") == "pre_s11_request":
                meta["words"] = src_info.get("words")
        if rp:
            meta.update(relphrase=True, imperative_found=[], imperative_left=[])
        ratio = (region or {}).get("mask_ratio")

        def region_issues(o: dict) -> list:
            if region is None:
                return []
            iss = []
            if ratio is not None and ratio < 0.6 and subject_head(o["new_scene_prompt"]) != subject_head(scene):
                iss.append("subject_changed")
            renamed = subject_head(o["new_scene_prompt"]) != subject_head(scene)
            if not renamed and not placement_ok(o["new_scene_prompt"], o.get("replacement") or "", o.get("op") or ""):
                iss.append("placement")                       # 주어를 바꾼 문장(0.6 이상 허용 또는 위 재요청)은 자리 검사 제외
            return iss
        if region is not None:
            meta.update(region=True, mask_ratio=ratio, selected_part=out.get("selected_part"),
                        region_fit=out.get("region_fit"), region_reason=out.get("region_reason"),
                        images=region.get("files"), region_found=[], region_left=[])
        src = f"{scene} {ins_text}"
        check_banned = rs and not st
        banned = banned_words(out["new_scene_prompt"], src) if check_banned else []
        imp = imperative_words(out["new_scene_prompt"]) if rp else []
        reg = region_issues(out)
        if banned or imp or reg:
            meta["retried"] = True
            extra: dict = {}
            if banned:
                meta["banned_found"], extra["avoid"] = banned, banned
            if imp:
                meta["imperative_found"], extra["imperative"] = imp, imp
            if reg:
                meta["region_found"] = reg
                fix = []
                if "subject_changed" in reg:
                    fix.append(f"Your previous new_scene_prompt {json.dumps(out['new_scene_prompt'])} renamed the whole "
                               f"subject, but mask_ratio is {ratio} (below 0.6): only a part was selected. Keep the subject "
                               "and its other details, and describe the selected part as changed with a relation word.")
                if "placement" in reg:
                    fix.append("Put the element changed in this edit right after the subject as the first relation phrase "
                               "(before any relation phrase from earlier edits).")
                extra["fix"] = fix
            out = clean(self.llm.refine(scene, ins_text, hist, job_id=j["job_id"], **kw, **extra))
            left = banned_words(out["new_scene_prompt"], src) if check_banned else []
            if left:
                out = dict(out, new_scene_prompt=strip_banned(out["new_scene_prompt"], left))
                meta["removed"] = left
            if rp:
                meta["imperative_left"] = imperative_words(out["new_scene_prompt"])
            if region is not None:
                meta["region_left"] = region_issues(out)
                meta.update(selected_part=out.get("selected_part"), region_fit=out.get("region_fit"),
                            region_reason=out.get("region_reason"))
        return out, meta

    def _ensure_render(self, j: dict) -> bool:
        """조건 렌더. 이미 있으면 다시 하지 않는다(S13 5: prompt 재시도도 카메라·볼륨이 같다). 새로 했으면 True."""
        if j.get("render"):
            return False
        r = self.be.render(j["asset_id"], j["base_version"], j["camera"], j["cells"])
        rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "render.png").write_bytes(r["render_png"])
        (rd / "mask.png").write_bytes(r["mask_png"])
        j["render"] = {"render_id": r["render_id"], "record": r.get("record")}
        return True

    def _region(self, j: dict) -> Optional[dict]:
        """S13: 라쏘 겹친 이미지·확대·mask_ratio. 파일은 jobs/<id>/region/ 에(같은 렌더면 한 번만 만든다)."""
        rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
        gd = self.s.data_dir / "jobs" / j["job_id"] / "region"
        try:
            if not (gd / "info.json").is_file():
                o = region_images((rd / "render.png").read_bytes(), (rd / "mask.png").read_bytes())
                gd.mkdir(parents=True, exist_ok=True)
                (gd / "overlay.png").write_bytes(o["overlay_png"])
                if o["zoom_png"]:
                    (gd / "zoom.png").write_bytes(o["zoom_png"])
                (gd / "info.json").write_text(json.dumps({k: o[k] for k in ("mask_ratio", "mask_px", "object_px", "boxes")}))
            info = json.loads((gd / "info.json").read_text())
            files = [n for n in ("overlay", "zoom") if (gd / f"{n}.png").is_file()]
            return {"images": [(gd / f"{n}.png").read_bytes() for n in files], "files": files, **info}
        except Exception as e:                            # noqa: BLE001 — 이미지가 안 되면 글자만으로
            j["region_error"] = f"{type(e).__name__}: {e}"
            return None

    def _run_raw(self, j: dict) -> None:
        """S14 그대로 모드: LLM 없이 지시문 원문을 인페인팅 문장으로. 장면 문장 = 이전 + '; ' + 지시문(기록용)."""
        ins = j["instruction"].strip()
        scene = self._scene(j["asset_id"])
        j["refine"] = {"target_element": None, "replacement": ins, "op": "replace",
                       "new_scene_prompt": f"{scene}; {ins}" if scene else ins, "inpaint_prompt": ins}
        j["refine_meta"], j["style_meta"] = {"raw_prompt": True}, None
        self._save(j)
        self._run_render_inpaint(j, new_render=False)

    def _run_refine(self, j: dict) -> None:
        self._set(j, "refining", "refining", 0.1)
        self._mock_gate(j)
        if j.get("raw_prompt"):
            return self._run_raw(j)
        if self.be.name == "mock":
            time.sleep(self.be.DUR["refine"] * self.s.mock_time_scale)
        region = None
        if self.s.regionrefine:                          # S13: 보정 전에 조건 렌더(재시도는 재사용)
            self._set(j, stage="rendering")
            j["render_reused"] = not self._ensure_render(j)
            if j["state"] != "refining":
                return
            region = self._region(j)
            j["prompts"] = dict(prompts_of(j), mask_ratio=(region or {}).get("mask_ratio"))   # S15 4.4
            self._set(j, stage="refining")
        try:
            scene, hist = self._scene(j["asset_id"]), list(j.get("reject_history") or [])
            j["refine"], smeta = self._refine_styled(j, scene, hist, region=region)
            j["refine_meta"] = {}
            last = (j.get("attempts") or [None])[-1]
            if last and last.get("rejected") and last.get("category") == "prompt" and \
                    _norm(j["refine"]["new_scene_prompt"]) == _norm(last.get("new_scene_prompt")):
                # S8: prompt 거절인데 같은 문장 → 1회 더(그래도 같으면 진행하고 기록)
                again, smeta2 = self._refine_styled(j, scene, hist, changed_from={
                    "prev": last.get("new_scene_prompt"), "reason": last.get("reason"), "guidance": last.get("guidance")},
                    region=region)
                if smeta2 is not None:
                    smeta = smeta2
                same = _norm(again["new_scene_prompt"]) == _norm(last.get("new_scene_prompt"))
                j["refine"] = again
                j["refine_meta"] = {"refine_retry": True, "refine_unchanged": same}
            j["style_meta"] = smeta                        # S11: 이 시도의 양식 판정·금지어 처리(끔이면 None)
            j["prompts"] = dict(prompts_of(j), region_fit=(smeta or {}).get("region_fit"),
                                refine_retried=bool((smeta or {}).get("retried") or j["refine_meta"].get("refine_retry")))
        except LLMInvalid as e:
            raise StageError("REFINE_INVALID", str(e))
        except LLMUnavailable as e:
            raise StageError("LLM_UNAVAILABLE", str(e))
        self._save(j)
        self._run_render_inpaint(j, new_render=False)    # S13 5: 첫 시도 렌더를 재사용(카메라·볼륨 같음)

    def _run_render_inpaint(self, j: dict, new_render: bool, agent: bool = False) -> None:
        if j["state"] not in ("refining", "inpainting", "queued"):
            return
        self._mock_gate(j)
        d = self._preview_dir(j, j["attempt"])
        if new_render or not j.get("render"):
            self._set(j, "inpainting", "rendering", 0.1)
            if new_render:
                j.pop("render", None)
            self._ensure_render(j)
        self._set(j, "inpainting", "inpainting", 0.3)
        rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
        rpng, mpng = (rd / "render.png").read_bytes(), (rd / "mask.png").read_bytes()
        seed = 0 + j["attempt"] - 1                       # 내부 규약 5절: 기본 seed + attempt − 1
        ip_src, ip = self._inpaint_text(j)
        j["prompts"] = dict(prompts_of(j), inpaint=ip)    # S15 4.4: 시도마다 보낼 때 갱신
        crop, crop_files = None, None
        info: dict = {}                                   # S9: 워커에 실제로 보낸 것(자르면 자른 입력)·워커 응답
        t0 = time.perf_counter()
        if self.s.inpaint_crop:                           # S7: 마스크 주변만 잘라 1024² 로 키워 보내고 원래 자리에 붙인다
            edit, crop, crop_files = crop_inpaint(rpng, mpng,
                                                  lambda r, m: self.be.inpaint(r, m, ip, seed, info=info))
        else:
            edit = self.be.inpaint(rpng, mpng, ip, seed, info=info)
        inpaint_s = round(time.perf_counter() - t0, 3)
        if j["state"] != "inpainting":
            return
        d.mkdir(parents=True, exist_ok=True)
        for n, b in (crop_files or {}).items():
            (d / n).write_bytes(b)
        (d / "render.png").write_bytes(rpng)
        (d / "mask.png").write_bytes(mpng)
        (d / "edit.png").write_bytes(edit)
        base = f"/app/v1/jobs/{j['job_id']}/preview/{j['attempt']}"
        j["preview"] = {"attempt": j["attempt"], "render_url": f"{base}/render.png",
                        "edit_url": f"{base}/edit.png", "mask_url": f"{base}/mask.png"}
        j["attempts"].append({"attempt": j["attempt"], "seed": seed, "prompt": ip, "style": j.get("style_meta"),
                              "new_scene_prompt": j["refine"]["new_scene_prompt"], "instruction": j["instruction"],
                              "render_id": j["render"]["render_id"], "t": self.clock(), "inpaint_s": inpaint_s,
                              "crop": crop if self.s.inpaint_crop else {"off": True}, "inpaint_raw": info,
                              "render_reused": j.get("render_reused"), "inpaint_src": ip_src,
                              # S17 시도별 기록: 그 시도에 쓰인 보정(그대로 모드 null)·보정 재요청·2D 판정
                              "refine": None if j.get("raw_prompt") else
                              {k: (j["refine"] or {}).get(k) for k in ("target_element", "replacement", "op", "new_scene_prompt")},
                              "refine_retried": prompts_of(j).get("refine_retried"), "accepted": None,
                              **(j.get("refine_meta") or {})})
        if (j.get("refine_meta") or {}).get("refine_unchanged"):
            j["refine_unchanged"] = True
        if agent:                                         # S20: 사람 확인 없이 채점으로
            self._save(j)
            return
        self._set(j, "awaiting_2d_review", "inpainting", 1.0)

    def _inpaint_text(self, j: dict) -> tuple:
        """인페인팅에 보낼 문장 (출처, 문장). S20b: 에이전트 진행 보고 detail 도 이 값."""
        # S13b: 보정이 인페인팅 전용 문장을 주면 그것을(장면 문장은 자산 갱신용으로 그대로)
        ip_src = "inpaint_prompt" if (self.s.inpaintprompt and (j["refine"] or {}).get("inpaint_prompt")) else "scene"
        base_text = j["refine"]["inpaint_prompt"] if ip_src == "inpaint_prompt" else j["refine"]["new_scene_prompt"]
        ip = inpaint_prompt(base_text, self.s.realstyle, (j.get("style_meta") or {}).get("style"))
        if j.get("raw_prompt"):                           # S14: 원문 그대로(번역·상세화·양식 꼬리 없음)
            ip_src, ip = "raw_prompt", j["refine"]["inpaint_prompt"]
        return ip_src, ip

    def _run_edit3d(self, j: dict, agent: bool = False) -> None:
        self._set(j, "editing_3d", "editing_3d", 0.05)
        self._mock_gate(j)
        d = self._preview_dir(j, j["attempt"])
        render = {"render_id": j["render"]["render_id"], "render_png": (d / "render.png").read_bytes(),
                  "mask_png": (d / "mask.png").read_bytes()}
        trace: dict = {"attempt": j["attempt"]}           # S10: A5000 접수·폴링 회차 기록(실패해도 남긴다)
        j.setdefault("a5000_trace", {})["edit3d"] = trace
        try:
            out = self.be.edit3d(j["asset_id"], j["base_version"], render, (d / "edit.png").read_bytes(), j["cells"],
                                 j["idem"], self._progress(j), trace=trace)
        finally:
            self._save(j)
        if j["state"] != "editing_3d":
            return
        self._set(j, stage="chunking", progress=0.95)
        entries = self.store.put_staging(j["asset_id"], j["job_id"], out["to_version"], out["blobs"])
        j["worker_job_id"] = out["worker_job_id"]
        j["edit_stats"] = out.get("stats")
        j["raw_edit3d"] = dict(out.get("raw") or {}, attempt=j["attempt"])   # S9: A5000 /edit 요청·결과 전문
        j["result"] = {"base_version": j["base_version"], "to_version": out["to_version"], "chunks": entries,
                       "removed_chunk_ids": out["removed"]}
        if agent:                                         # S20: 3D 확인 없이 바로 반영(호출한 쪽이 _commit)
            self._save(j)
            return
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
                if j.get("attempts"):
                    j["attempts"][-1]["accepted"] = True     # S17
                self._set(j, "editing_3d", "editing_3d", 0.0)
                self._enqueue(j, "edit3d")
                return j
            if not (reason or "").strip():
                raise AppError(400, "INVALID_REQUEST", "거절에는 reason 이 필요하다")
            if j.get("classifying"):
                raise AppError(409, "WRONG_STATE", "거절 이유를 분류하는 중이다")
            if j.get("raw_prompt"):                      # S14: 분류 LLM 없음 — 거절 이유 원문이 새 인페인팅 문장, seed +1
                return self._reject_raw(j, reason.strip())
            j["classifying"] = True
            self._set(j, stage="classifying")
        try:                                             # LLM 호출은 잠금 밖에서
            ckw = {}
            if self.s.regionrefine:                      # S13 3: 라쏘 겹친 렌더 + 거절한 2d_edit, S13b 영역 규칙
                ckw["region_rule"] = True
                reg = self._region(j)
                ed = self._preview_dir(j, j["attempt"]) / "edit.png"
                imgs = ([reg["images"][0]] if reg and reg.get("images") else []) + ([ed.read_bytes()] if ed.is_file() else [])
                if imgs:
                    ckw["images"] = imgs
            out = self.llm.classify(reason, j.get("refine"), list(j.get("reject_history") or []), job_id=j["job_id"], **ckw)
            fail = None
        except LLMInvalid as e:
            out, fail = None, ("CLASSIFY_FAILED", str(e))
        except LLMUnavailable as e:
            out, fail = None, ("LLM_UNAVAILABLE", str(e))
        with self.lock:
            j["classifying"] = False
            if j["state"] != "awaiting_2d_review":       # 그 사이 취소 등
                return j
            if fail:
                self._finish(j, "failed", *fail)
                return j
            cat, guide = out["category"], out["guidance"]
            if self.s.regionrefine:                      # S13c: 안내문의 좌우·위치 단어도 지운다(이유 원문에 있으면 둔다)
                guide = strip_lateral(guide, reason) or guide
            j["review"] = {"reason": reason, "category": cat, "guidance": guide}
            if j.get("attempts"):                        # 시도별 거절 기록(S3)
                j["attempts"][-1].update(rejected=True, accepted=False, reason=reason, category=cat, guidance=guide)
            if j["attempt"] >= self.s.max_attempts:
                self._finish(j, "failed", "RETRY_LIMIT", "2D 거절 3회")
                return j
            if cat == "region":
                self._finish(j, "needs_redraw")
                return j
            j["attempt"] += 1
            if cat == "prompt":
                # S13 4: 사용자 원문도 함께(원문 우선). 끔 = 안내문만
                j.setdefault("reject_history", []).append(
                    f'user said: "{reason.strip()}" (this comes first); guidance: {guide}' if self.s.rejectraw else guide)
                self._set(j, "refining", "refining", 0.0)
                self._enqueue(j, "refine_again")
            else:
                j["refine_meta"] = {}                    # 품질 재시도는 보정을 다시 부르지 않는다
                j["prompts"] = dict(prompts_of(j), refine_retried=None)   # S15: 이번 시도는 보정 없음
                self._set(j, "inpainting", "inpainting", 0.0)
                self._enqueue(j, "inpaint_again")
            return j

    def _reject_raw(self, j: dict, reason: str) -> dict:
        guide = "그대로 모드: 거절 이유 원문을 새 인페인팅 문장으로 다시 그린다(seed +1)"
        j["review"] = {"reason": reason, "category": "prompt", "guidance": guide}
        if j.get("attempts"):
            j["attempts"][-1].update(rejected=True, accepted=False, reason=reason, category="prompt", guidance=guide)
        if j["attempt"] >= self.s.max_attempts:
            self._finish(j, "failed", "RETRY_LIMIT", "2D 거절 3회")
            return j
        j["attempt"] += 1
        j["refine"] = dict(j["refine"], inpaint_prompt=reason)
        j["refine_meta"] = {"raw_prompt": True}
        self._set(j, "inpainting", "inpainting", 0.0)
        self._enqueue(j, "inpaint_again")                # 조건 렌더 재사용, seed = attempt − 1
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
        self.enqueue_views(j["asset_id"], to, j["job_id"] if j.get("mode") == "agent" else None)   # S20 2.1

    def _discard(self, j: dict, why: str) -> None:
        note = self.be.discard(j["asset_id"], j.get("worker_job_id") or "")
        src = self.store.staging_dir(j["asset_id"], j["job_id"])     # 되돌린 결과도 3D 로 볼 수 있게(S3)
        if src.is_dir():
            import shutil
            dst = self.s.data_dir / "archive" / j["job_id"]
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            j["archive"] = f"archive/{j['job_id']}"
        self.store.drop_staging(j["asset_id"], j["job_id"])
        j["discard_note"] = f"{why}{' · ' + note if note else ''}"
        self._finish(j, "discarded")

    # ── S20 2.1 시점 렌더(캐시 data_dir/views/<asset>/<version>/) ─────────
    def _views_dir(self, asset_id: str, version: int) -> Path:
        return self.s.data_dir / "views" / asset_id / str(int(version))

    def occupied(self, asset_id: str, version: int) -> list:
        """점유 셀 = slat_coords(앱 저장소, 없으면 A5000 에서 받아 저장)."""
        c = self.store.slat_coords(asset_id, version)
        if not c:
            c = self.be.slat_coords(asset_id, version)
            if c:
                try:
                    self.store.put_slat_coords(asset_id, version, c)
                except Exception:                         # noqa: BLE001 — 저장 못 해도 이번에는 쓴다
                    pass
        if not c:
            raise StageError("RENDER_FAILED", f"점유 셀(slat_coords)이 없다: {asset_id} v{version}")
        return c

    def views_cached(self, asset_id: str, version: int) -> Optional[dict]:
        d = self._views_dir(asset_id, version)
        try:
            meta = json.loads((d / "views.json").read_text())
            png = {v: (d / f"{v}.png").read_bytes() for v in VIEW_NAMES}
        except (OSError, ValueError):
            return None
        return {"png": png, "cameras": meta["cameras"], "occupied": self.occupied(asset_id, version), "cached": True,
                "render_s": meta.get("render_s")}

    def render_views(self, asset_id: str, version: int) -> dict:
        """방위 0·90·180·270° 렌더 4장(cells 는 중심 셀 1개 — 렌더 이미지만 쓴다) → 캐시."""
        from .automask import view_cameras
        occ = self.occupied(asset_id, version)
        cams = view_cameras(occ)
        mid = sorted(occ)[len(occ) // 2]
        t0 = time.perf_counter()
        png = {v: self.be.render(asset_id, version, cams[v], [list(mid)])["render_png"] for v in VIEW_NAMES}
        rs = round(time.perf_counter() - t0, 3)
        d = self._views_dir(asset_id, version)
        d.mkdir(parents=True, exist_ok=True)
        for v, b in png.items():
            (d / f"{v}.png").write_bytes(b)
        (d / "views.json").write_text(json.dumps({"cameras": cams, "render_s": rs, "t": self.clock()}))
        return {"png": png, "cameras": cams, "occupied": occ, "cached": False, "render_s": rs}

    def enqueue_views(self, asset_id: str, version: int, job_id: Optional[str] = None) -> None:
        """반영 직후 같은 대기열 뒤에 미리 렌더. 시험용 가짜 백엔드(name 'fake')에서는 하지 않는다."""
        if self.be.name not in ("real", "mock") or not self.s.agent:
            return
        self.q.put(("views", json.dumps([asset_id, int(version), job_id])))

    def _run_views(self, key: str) -> None:
        asset_id, version, job_id = json.loads(key)
        try:
            v = self.views_cached(asset_id, version) or self.render_views(asset_id, version)
        except Exception as e:                            # noqa: BLE001 — 실패해도 무시(편집 때 다시)
            print(f"[views] {asset_id} v{version}: {type(e).__name__}: {e}", flush=True)
            return
        j = self.get(job_id) if job_id else None
        if j is not None and j.get("mode") == "agent":    # 10.3: 반영 뒤 result 이미지(새 판본 앞 시점 1장)
            from .agent import AgentRun
            front = ((j.get("agent_plan") or {}).get("front_view")) or "az000"
            with self.lock:
                AgentRun(self, j).event("done", "tool", "새 판본을 앞에서 본 모습입니다", {"result": v["png"][front]})

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
        if task == "views":                               # S20 2.1 미리 렌더(작업 아님, 실패는 무시)
            return self._run_views(job_id)
        with self.lock:
            if job_id in self.order:
                self.order.remove(job_id)
            j = self.jobs.get(job_id)
            self.running = job_id
            if j is not None and j.get("enqueued_at") is not None:   # GPU 대기 시간(S3)
                j.setdefault("queue_waits", []).append({"task": task, "wait_s": round(self.clock() - j["enqueued_at"], 3)})
                j["enqueued_at"] = None
        try:
            if j is None or j["state"] in TERMINAL:
                return
            if task == "generate":
                self._mock_gate(j)
                self._run_generate(j)
            elif task == "refine":
                self._run_refine(j)
            elif task == "refine_again":
                self._run_refine(j)
            elif task == "inpaint_again":
                self._run_render_inpaint(j, new_render=False)
            elif task == "edit3d":
                self._run_edit3d(j)
            elif task == "agent":
                from .agent import AgentRun
                self._mock_gate(j)
                AgentRun(self, j).run()
        except StageError as e:
            if j["state"] not in TERMINAL:
                self._finish(j, "failed", e.code, str(e))
                if j["kind"] == "generate":
                    self._drop_unborn(j)
        except Exception as e:                    # noqa: BLE001
            traceback.print_exc()
            if j and j["state"] not in TERMINAL:
                code = {"generate": "GENERATE_FAILED", "refine": "RENDER_FAILED", "refine_again": "RENDER_FAILED",
                        "inpaint_again": "INPAINT_FAILED", "edit3d": "EDIT_FAILED", "agent": "AGENT_FAILED"}[task]
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
            note = None
            if self.s.worker_delete:                     # 끔(기본) = A5000 워커 저장소도 보관(사용자 결정 2026-11-12)
                try:
                    note = self.be.delete(aid)           # 켬 = 1.0.2 9절 원래 동작(A5000 저장소 삭제)
                except StageError as e:
                    raise AppError(409, e.code, str(e))
            # 앱 서버 기록은 보관한다: QR 만 비우고 판본·청크·작업·미리보기·LLM 기록·평가는 그대로(1.0.2 9절)
            self.db.x("UPDATE assets SET qr_id=NULL, deleted_at=?, worker_kept=? WHERE asset_id=?",
                      (self.clock(), 0 if self.s.worker_delete else 1, aid))
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
            a = self.db.live_asset(asset_id)
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
