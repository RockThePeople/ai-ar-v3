"""S20 에이전트 편집(app-api 1.1.0 10절) — 제어 루프와 진행 보고(events).

순서는 코드가 고정한다: 시점 렌더 → 볼륨 마스킹(오케스트레이터) → 마스크 확인(오케스트레이터) → 보정(기존 Haiku)
→ 인페인팅(로컬 FLUX) → 채점(오케스트레이터) → 합격이면 3D 편집 → 자동 반영.
채점 4항목에서 되돌아갈 곳은 코드가 정한다(지시서 0절): ②위치·③마스크 적합 실패 → 마스킹, ①요소 실패 → 보정,
④3D 적합만 실패 → seed 바꿔 인페인팅. 되돌아가기는 작업당 최대 AGENT_MAX_BACKTRACKS(마스크 확인 실패 포함).
다 쓰고도 불합격이면 합격 항목이 가장 많은 시도(같으면 나중)로 3D, agent.below_bar = true.
"""

from __future__ import annotations

import json
import shutil
import time
from typing import Optional

from . import automask as am
from .backends import StageError
from .cropinpaint import decode_png, encode_png
from .llm_adapter import LLMInvalid, LLMUnavailable

CHECKS = ("element", "position", "mask_fit", "shape_3d")
CHECK_KO = {"element": "요소", "position": "위치", "mask_fit": "마스크 적합", "shape_3d": "3D 적합"}
CAUSE_STEP = {"mask": "masking", "prompt": "refining", "seed": "inpainting"}
CAUSE_KO = {"mask": "볼륨 마스킹부터", "prompt": "프롬프트 보정부터", "seed": "인페인팅(다른 seed)부터"}
#: 가짜 모드 단계 시간(초, × APP_MOCK_TIME_SCALE)
MOCK_DUR = {"views": 2.0, "mask": 2.0, "check": 1.0, "judge": 1.0}


def cause_of(checks: dict) -> Optional[str]:
    """0절 규칙. 모두 합격이면 None."""
    if not checks.get("position") or not checks.get("mask_fit"):
        return "mask"
    if not checks.get("element"):
        return "prompt"
    if not checks.get("shape_3d"):
        return "seed"
    return None


def best_attempt(tries: list) -> Optional[dict]:
    """합격 항목이 가장 많은 시도, 같으면 나중 시도."""
    if not tries:
        return None
    return max(enumerate(tries), key=lambda it: (sum(bool(it[1]["checks"].get(k)) for k in CHECKS), it[0]))[1]


class AgentStop(Exception):
    """시간·호출 한도 → 가장 나은 시도로 3D."""


class AgentRun:
    def __init__(self, eng, j: dict) -> None:
        self.eng, self.j, self.s = eng, j, eng.s
        self.llm = eng.agent_llm
        self.t0 = eng.clock()

    # ── 기록 ─────────────────────────────────────────────────────
    def event(self, step: str, kind: str, text: str, images: Optional[dict] = None) -> dict:
        j = self.j
        seq = len(j["events"]) + 1
        ev = {"seq": seq, "t": round(self.eng.clock(), 3), "step": step, "kind": kind, "text": text}
        if images:
            d = self.s.data_dir / "jobs" / j["job_id"] / "agent" / str(seq)
            d.mkdir(parents=True, exist_ok=True)
            urls = {}
            for name, b in images.items():
                if b:
                    (d / f"{name}.png").write_bytes(b)
                    urls[name] = f"/app/v1/jobs/{j['job_id']}/agent/{seq}/{name}.png"
            if urls:
                ev["images"] = urls
        j["events"].append(ev)
        self.eng._save(j)
        return ev

    def _mock(self, k: str) -> None:
        if self.eng.be.name == "mock":
            time.sleep(MOCK_DUR[k] * self.s.mock_time_scale)

    def _guard(self) -> None:
        if self.eng.clock() - self.t0 > self.s.agent_time_s:
            raise AgentStop("time")
        if self.j["agent_calls"] >= self.s.agent_max_calls:
            raise AgentStop("calls")

    def _orch(self, fn, *a, **k):
        """오케스트레이터 호출 1회(한도 확인 → 호출 → 횟수). JSON 오류 1회 재요청은 어댑터 안에서, 그래도 틀리면 AGENT_FAILED."""
        self._guard()
        self.j["agent_calls"] += 1
        try:
            return fn(*a, job_id=self.j["job_id"], **k)
        except LLMInvalid as e:
            raise StageError("AGENT_FAILED", str(e))
        except LLMUnavailable as e:
            raise StageError("LLM_UNAVAILABLE", str(e))

    def _state(self, state: str, stage: Optional[str] = None, progress: Optional[float] = None) -> None:
        self.eng._set(self.j, state, stage or state, progress)

    # ── 단계 ─────────────────────────────────────────────────────
    def views(self) -> dict:
        j, eng = self.j, self.eng
        cached = eng.views_cached(j["asset_id"], j["base_version"])
        if cached is None:
            self._state("masking", "rendering_views", 0.05)
            self._mock("views")
            cached = eng.render_views(j["asset_id"], j["base_version"])
        j["agent_views"] = {"cached": cached["cached"], "render_s": cached.get("render_s")}
        imgs = {v: am.to_rgb(decode_png(b)) for v, b in cached["png"].items()}
        self.event("masking", "tool", "자산을 네 방향에서 봤습니다", {"views": encode_png(am.mosaic(imgs))})
        self.cams = cached["cameras"]
        self.occ = cached["occupied"]
        self.grid = {v: encode_png(am.grid_image(im, v)) for v, im in imgs.items()}
        return cached

    def mask(self, feedback: Optional[str]) -> dict:
        j = self.j
        self._state("masking", "masking", 0.15)
        self._mock("mask")
        plan = self._orch(self.llm.agent_mask, self.grid, j["instruction"], self.eng._scene(j["asset_id"]), feedback)
        j["agent_plan"] = plan
        if not plan.get("found"):
            self.event("masking", "error", f"지시한 부위를 찾지 못했습니다 — {plan.get('note_ko') or ''}".strip(" —"))
            raise StageError("NO_TARGET", plan.get("note_ko") or "지시한 부위를 찾지 못했다")
        bp = am.backproject(plan["regions"], self.cams, self.occ, plan["op"])
        j["agent_mask"] = {k: bp[k] for k in ("n", "shrink", "scale", "too_large", "per_region")}
        if bp["too_large"]:
            raise StageError("MASK_TOO_LARGE", f"자동 마스킹 셀 {bp['n']} > {am.MAX_CELLS}(0.9배 축소 3번 뒤)")
        cells = bp["cells"]
        if cells:                                          # 인페인팅 시점으로 조건 렌더(셀 = 결과)
            self._state("masking", "masking", 0.25)
            self.new_render(plan["inpaint_view"], cells)
        return {"plan": plan, "cells": cells, "n": bp["n"]}

    def new_render(self, view: str, cells: list) -> None:
        j, eng = self.j, self.eng
        j["camera"], j["cells"] = self.cams[view], cells
        j.pop("render", None)
        shutil.rmtree(self.s.data_dir / "jobs" / j["job_id"] / "region", ignore_errors=True)
        eng._ensure_render(j)
        j["render_view"] = view

    def region(self) -> Optional[dict]:
        r = self.eng._region(self.j)
        self.j["prompts"] = dict(self.j["prompts"], mask_ratio=(r or {}).get("mask_ratio"))
        return r

    def check(self, m: dict, region: Optional[dict]) -> dict:
        j = self.j
        plan = m["plan"]
        ov = (region or {}).get("images", [None])[0]
        side = ", ".join(am.VIEW_KO[v] for v in am.VIEW_AZ
                         if any((rg["boxes"] or {}).get(v) for rg in plan["regions"]))
        self.event("masking", "tool", f"{plan.get('note_ko') or plan['target_part_ko'] + ' 자리를 칠했습니다'}"
                   f"({side} 기준, {m['n']:,}셀)", {"overlay": ov})
        if not m["cells"]:
            return {"ok": False, "reason_ko": "칠한 셀이 없습니다", "fix": "make the boxes overlap the same 3D place"}
        self._state("masking", "mask_check", 0.3)
        self._mock("check")
        c = self._orch(self.llm.agent_mask_check, ov or b"", j["instruction"], plan)
        self.event("mask_check", "check", ("마스크 확인: 맞습니다" if c["ok"] else f"마스크 확인: {c['reason_ko']}"))
        return c

    def refine(self, plan: dict, feedback: Optional[str], region: Optional[dict]) -> None:
        j, eng = self.j, self.eng
        self._state("refining", "refining", 0.4)
        if eng.be.name == "mock":
            time.sleep(eng.be.DUR["refine"] * self.s.mock_time_scale)
        verb = {"add": "Add", "replace": "Replace with", "remove": "Remove", "recolor": "Recolor to"}[plan["op"]]
        ins = (f"Selected part: {plan['target_part']}. {verb}: {plan['new_object']}."
               + (f" Previous attempt problem: {feedback}." if feedback else "") + f" User instruction: {j['instruction']}")
        if getattr(eng.llm, "name", "") == "fake":         # 가짜 보정은 지시문을 그대로 옮기므로 사용자 원문을(앱 화면용)
            ins = j["instruction"]
        try:
            j["refine"], smeta = eng._refine_styled(j, eng._scene(j["asset_id"]), [], region=region, instruction=ins)
        except LLMInvalid as e:
            raise StageError("REFINE_INVALID", str(e))
        except LLMUnavailable as e:
            raise StageError("LLM_UNAVAILABLE", str(e))
        j["refine_meta"], j["style_meta"], j["agent_refine_input"] = {}, smeta, ins
        j["prompts"] = dict(j["prompts"], region_fit=(smeta or {}).get("region_fit"),
                            refine_retried=bool((smeta or {}).get("retried")))
        self.event("refining", "tool", f"그릴 문장을 정했습니다: {j['refine'].get('inpaint_prompt') or j['refine']['new_scene_prompt']}")

    def inpaint(self) -> bytes:
        j, eng = self.j, self.eng
        self._state("inpainting", "inpainting", 0.5)
        eng._run_render_inpaint(j, new_render=False, agent=True)
        d = eng._preview_dir(j, j["attempt"])
        edit = (d / "edit.png").read_bytes()
        self.event("inpainting", "tool", f"편집 이미지를 그렸습니다(시도 {j['attempt']})", {"edit": edit})
        return edit

    def judge(self, edit: bytes, region: Optional[dict]) -> dict:
        j, eng = self.j, self.eng
        self._state("judging", "judging", 0.6)
        self._mock("judge")
        d = eng._preview_dir(j, j["attempt"])
        ov = (region or {}).get("images", [None])[0] or (d / "mask.png").read_bytes()
        return self._orch(self.llm.agent_judge, (d / "render.png").read_bytes(), ov, edit, j["instruction"],
                          j["prompts"].get("inpaint") or "")

    # ── 루프 ─────────────────────────────────────────────────────
    def run(self) -> None:
        j, eng, s = self.j, self.eng, self.s
        ag = j["agent"]
        tries: list = []
        feedback: Optional[str] = None
        step = "mask"
        plan, region, m = None, None, None
        stop = None
        try:
            self.views()
            while True:
                if step == "mask":
                    m = self.mask(feedback)
                    plan = m["plan"]
                    region = self.region() if m["cells"] else None
                    c = self.check(m, region)
                    if not c["ok"] or not m["cells"]:
                        if ag["backtracks"] < ag["max_backtracks"]:
                            ag["backtracks"] += 1
                            feedback = f"{c.get('reason_ko')} / {c.get('fix')}".strip(" /")
                            self.event("mask_check", "branch",
                                       f"{c.get('reason_ko')} → 볼륨 마스킹을 다시 합니다(되돌아가기 {ag['backtracks']}/{ag['max_backtracks']})")
                            continue
                        if not m["cells"]:
                            raise StageError("MASK_EMPTY", "자동 마스킹 셀이 없다(되돌아가기 한도)")
                        self.event("mask_check", "branch", "되돌아가기 한도라 이 마스크로 진행합니다")
                if step in ("mask", "prompt"):
                    self.refine(plan, feedback, region)
                edit = self.inpaint()
                v = self.judge(edit, region)
                checks = {k: bool(v["checks"].get(k)) for k in CHECKS}
                cause = cause_of(checks)
                ag["verdict"] = {"pass": cause is None, "checks": checks, "cause": cause}
                tries.append({"attempt": j["attempt"], "checks": checks, "cause": cause, "render": dict(j["render"]),
                              "camera": j["camera"], "cells": j["cells"], "refine": j["refine"],
                              "style_meta": j.get("style_meta"), "inpaint": j["prompts"].get("inpaint"),
                              "reason_ko": v.get("reason_ko"), "fix": v.get("fix"), "plan": plan,
                              "render_view": j.get("render_view")})
                j["agent_tries"] = [{k: t[k] for k in ("attempt", "checks", "cause", "reason_ko", "fix", "inpaint",
                                                       "render_view")} | {"n_cells": len(t["cells"])} for t in tries]
                if j.get("attempts"):
                    j["attempts"][-1].update(agent_checks=checks, agent_cause=cause, agent_reason=v.get("reason_ko"))
                if cause is None:
                    self.event("judging", "judge", f"합격({v.get('reason_ko') or '4항목 모두 통과'}) → 3D 편집")
                    break
                bad = ", ".join(CHECK_KO[k] for k in CHECKS if not checks[k])
                if ag["backtracks"] >= ag["max_backtracks"]:
                    self.event("judging", "judge", f"{v.get('reason_ko')}(불합격: {bad}) → 되돌아가기 한도라 가장 나은 시도로 3D 편집")
                    ag["below_bar"] = True
                    break
                ag["backtracks"] += 1
                feedback = f"{v.get('reason_ko')} / {v.get('fix')}".strip(" /")
                self.event("judging", "judge", f"{v.get('reason_ko')}(불합격: {bad}) → {CAUSE_KO[cause]} 다시 합니다"
                                               f"(되돌아가기 {ag['backtracks']}/{ag['max_backtracks']})")
                step = cause
                j["attempt"] += 1
                ag["attempt"] = j["attempt"]
                eng._save(j)
        except AgentStop as e:
            stop = str(e)
            if not tries:
                raise StageError("TIMEOUT" if stop == "time" else "AGENT_FAILED",
                                 "실행 10분 초과" if stop == "time" else "오케스트레이터 호출 한도")
            ag["below_bar"] = not any(t["cause"] is None for t in tries)
            self.event(j["stage"], "branch", ("시간 한도" if stop == "time" else "호출 한도") + "라 가장 나은 시도로 3D 편집")
        chosen = tries[-1] if tries[-1]["cause"] is None else best_attempt(tries)
        self.edit3d(chosen)

    def edit3d(self, t: dict) -> None:
        j, eng = self.j, self.eng
        if t["attempt"] != j["attempt"]:
            self.event("editing_3d", "branch", f"시도 {t['attempt']}의 이미지로 3D 편집합니다")
        j["attempt"], j["render"], j["camera"], j["cells"] = t["attempt"], t["render"], t["camera"], t["cells"]
        j["refine"], j["style_meta"] = t["refine"], t["style_meta"]
        j["prompts"] = dict(j["prompts"], inpaint=t["inpaint"])
        j["agent"]["attempt"] = t["attempt"]
        j["agent_chosen"] = t["attempt"]
        eng._run_edit3d(j, agent=True)
        if j["state"] != "editing_3d":
            return
        self.event("editing_3d", "tool", f"3D 편집을 했습니다({len(t['cells']):,}셀)")
        try:
            eng._commit(j)
        except Exception as e:                              # noqa: BLE001 — AppError(반영 실패)
            raise StageError(getattr(e, "code", "COMMIT_FAILED"), str(e))
        to = j["result"]["to_version"]
        tail = " — 기준에 못 미친 시도로 진행했습니다" if j["agent"].get("below_bar") else ""
        self.event("done", "done", f"새 판본 v{to}을 반영했습니다{tail}")   # 미리 렌더는 _commit 이 대기열에
