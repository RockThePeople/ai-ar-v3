"""S20 에이전트 편집(app-api 1.1.0 10절) — 제어 루프와 진행 보고(events).

순서는 코드가 고정한다: 시점 렌더 → 볼륨 마스킹(오케스트레이터) → 마스크 확인(오케스트레이터) → 보정(기존 Haiku)
→ 인페인팅(로컬 FLUX) → 채점(오케스트레이터) → 합격이면 3D 편집 → 자동 반영.
채점 4항목에서 되돌아갈 곳은 코드가 정한다(지시서 0절): ②위치·③마스크 적합 실패 → 마스킹, ①요소 실패 → 보정,
④3D 적합만 실패 → seed 바꿔 인페인팅. 되돌아가기는 작업당 최대 AGENT_MAX_BACKTRACKS(마스크 확인 실패 포함).
다 쓰고도 불합격이면 합격 항목이 가장 많은 시도(같으면 나중)로 3D, agent.below_bar = true.
"""

from __future__ import annotations

import json

import numpy as np
import shutil
import time
from typing import Optional

from . import automask as am
from .backends import StageError
from .cropinpaint import decode_png, encode_png, mask_bool
from .llm_adapter import MOCK_AGENT, LLMInvalid, LLMUnavailable, inpaint_ko_problem

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
        self.side_hint: Optional[str] = None
        sc = MOCK_AGENT.get(j.get("mock_agent") or "")           # 계약 7절 가짜 모드 X-Mock-Agent(규칙 기반 어댑터만)
        if sc is not None and hasattr(self.llm, "per_job"):
            self.llm.per_job[j["job_id"]] = sc

    # ── 기록 ─────────────────────────────────────────────────────
    def event(self, step: str, kind: str, text: str, images: Optional[dict] = None,
              detail: Optional[str] = None) -> dict:
        j = self.j
        seq = len(j["events"]) + 1
        ev = {"seq": seq, "t": round(self.eng.clock(), 3), "step": step, "kind": kind, "text": text}
        if detail:                                          # S20b 10.3: 접어서 보일 원문
            ev["detail"] = detail
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
        if cells:
            cells = self.side_rule(plan, cells)
        if cells:                                          # 인페인팅 시점으로 조건 렌더(셀 = 결과)
            self._state("masking", "masking", 0.25)
            self.new_render(plan["inpaint_view"], cells)
        return {"plan": plan, "cells": cells, "n": bp["n"]}

    def side_rule(self, plan: dict, cells: list) -> list:
        """S20d 좌우 규칙(LLM 호출 없음). 지시문 낱말을 코드로 판정(애매하면 오케스트레이터 side·side_frame), 앞 시점에서
        칠한 셀 중심이 기대한 쪽의 반대면 몸 중심을 지나는 좌우 대칭면으로 뒤집는다."""
        j = self.j
        side, src = am.parse_side(j["instruction"]), "code"
        if side is None and plan.get("side") in ("left", "right") and plan.get("side_frame") in ("viewer", "character"):
            side, src = {"side": plan["side"], "frame": plan["side_frame"], "word": None}, "orchestrator"
        fv = plan.get("front_view") if plan.get("front_view") in self.cams else "az000"
        rec: dict = {"parsed": side, "source": src if side else None, "front_view": fv, "flipped": False}
        self.side_hint = None
        if side:
            exp = am.expected_image_side(side["side"], side["frame"])
            got = am.image_side_of(cells, self.occ, self.cams[fv])
            rec.update(expected=exp, got=got)
            if got["side"] not in (exp, "center"):
                flipped = am.mirror_cells(cells, self.occ, self.cams[fv])
                near = am.dilate(self.occ, 2)
                if plan.get("op") in ("replace", "remove", "recolor"):
                    flipped = [c for c in flipped if near[c[0], c[1], c[2]]]
                if flipped and len(flipped) <= am.MAX_CELLS and any(near[c[0], c[1], c[2]] for c in flipped):
                    cells = flipped
                    rec.update(flipped=True, n_after=len(cells),
                               got_after=am.image_side_of(cells, self.occ, self.cams[fv]))
                    ko = {"left": "왼쪽", "right": "오른쪽"}
                    target = side["word"] or (plan.get("target_part_ko") if side["frame"] == "character"
                                              else ko[side["side"]])
                    self.event("masking", "check", f"{ko[got['side']]}에 칠해져 {target} 쪽으로 옮겼습니다(좌우 규칙)",
                               detail=f"{src}: side={side['side']} frame={side['frame']} word={side['word']!r}; "
                                      f"{fv} image x body {got['body_x']} mask {got['mask_x']} → mirrored, {len(cells)} cells")
                else:
                    rec["flip_failed"] = {"n": len(flipped), "touches_body": bool(flipped)}
            # 마스크 확인에 줄 기대 위치(인페인팅 시점이 앞 시점이거나 그 반대일 때만 좌우가 뜻이 있다)
            iv = plan.get("inpaint_view")
            off = (am.VIEW_AZ.get(iv, 0) - am.VIEW_AZ[fv]) % 360
            img_side = exp if off == 0 else ({"left": "right", "right": "left"}[exp] if off == 180 else None)
            if img_side:
                frame_en = "the character's own side" if side["frame"] == "character" else "the viewer's side"
                self.side_hint = (f"the instruction's {side['side']} ({side['word'] or side['side']}) is {frame_en}; "
                                  f"in this image the edit area should be on the IMAGE {img_side.upper()} of the character")
        j.setdefault("agent_side", []).append(rec)
        return cells

    # ── S20e 사용자 라쏘 볼륨 다듬기 ─────────────────────────────
    def lasso_start(self) -> None:
        """사용자 카메라로 라쏘 볼륨 U 를 렌더하고, 판단에 줄 격자 이미지 2장(렌더, 라쏘 겹침)을 만든다."""
        j, eng = self.j, self.eng
        self._state("masking", "mask_refine", 0.05)
        self.cam, self.U = j["user_camera"], [list(c) for c in j["user_cells"]]
        self.occ = eng.occupied(j["asset_id"], j["base_version"])
        self.consts = am.size_consts(self.occ, self.s.agent_depth_ratio, self.s.agent_merge_ratio)
        j["agent_consts"] = self.consts
        self.render_with(self.cam, self.U, "user")
        rgb, m = self._render_arrays()
        self.user_rgb = rgb
        self.grid_render = encode_png(am.grid_full(rgb))
        over = am.tint_mask(rgb, m)
        self.grid_lasso = encode_png(am.grid_full(over))
        self.event("masking", "tool", f"그린 영역을 봤습니다(라쏘 {len(self.U):,}셀)", {"overlay": encode_png(am.half(over))})

    def _render_arrays(self):
        rd = self.s.data_dir / "jobs" / self.j["job_id"] / "render"
        r = decode_png((rd / "render.png").read_bytes())
        return am.to_rgb(r), mask_bool(decode_png((rd / "mask.png").read_bytes()))

    def render_with(self, cam: dict, cells: list, label: str) -> None:
        j, eng = self.j, self.eng
        j["camera"], j["cells"] = cam, cells
        j.pop("render", None)
        shutil.rmtree(self.s.data_dir / "jobs" / j["job_id"] / "region", ignore_errors=True)
        eng._ensure_render(j)
        j["render_view"] = label

    def refine_mask(self, feedback: Optional[str]) -> dict:
        """S20e 2.1 판단(오케스트레이터 1회, 이미지 2장) → 2.2 잘라내기·2.3 합치기(코드) → 다듬은 셀로 다시 렌더."""
        j = self.j
        self._state("masking", "mask_refine", 0.15)
        self._mock("mask")
        plan = self._orch(self.llm.agent_refine_plan, self.grid_render, self.grid_lasso, j["instruction"],
                          self.eng._scene(j["asset_id"]), feedback)
        j["agent_plan"] = plan
        if not plan.get("found"):
            self.event("masking", "error", f"지시한 부위를 찾지 못했습니다 — {plan.get('note_ko') or ''}".strip(" —"))
            raise StageError("NO_TARGET", plan.get("note_ko") or "지시한 부위를 찾지 못했다")
        r = am.refine_volume(self.U, self.occ, self.cam, plan, self.consts)
        j["agent_mask"] = {k: v for k, v in r.items() if k != "cells"} | {"consts": self.consts}
        cells = r["cells"]
        c = self.consts
        tm = f"L {c['L']} · T {c['T']} · M {c['M']}"
        if r["merged"]:
            self.event("masking", "tool", f"{plan.get('old_object') or '원래 물건'}의 라쏘 밖 부분 {r['merged']:,}셀을 합쳤습니다",
                       detail=f"26-neighbour merge inside old_object_box +5%, depth d0+2T, lasso bbox +M ({tm})")
        self.render_with(self.cam, cells, "user")
        rgb, m = self._render_arrays()
        after = encode_png(am.half(am.tint_mask(rgb, m)))
        if r["fallback"]:
            text = f"다듬지 못해 그린 영역 그대로 씁니다(라쏘 {r['n_user']:,}셀)"
        else:
            cut = r["cut_depth"] + r["cut_side"] + r["cut_empty"]
            text = (f"마스크를 다듬었습니다(라쏘 {r['n_user']:,}셀 → {r['n_final']:,}셀, 잘라냄 {cut:,}"
                    + (f"·합침 {r['merged']:,}" if r["merged"] else "") + ")")
        self.event("masking", "tool", text, {"overlay": after},
                   detail=f"{tm}; cut depth {r['cut_depth']} side {r['cut_side']} empty {r['cut_empty']}; "
                          f"op {plan['op']}; keep {plan.get('keep_box')} old {plan.get('old_object_box')} new {plan.get('new_space_box')}")
        return {"plan": plan, "cells": cells, "n": len(cells)}

    # ── S20f Gemini: 물건이 가장 잘 보이는 시점 고르기(볼륨은 라쏘 U 그대로) ──
    def gemini_prepare(self, feedback: Optional[str]) -> dict:
        j, eng = self.j, self.eng
        if not getattr(self, "g_plan", None):
            self._state("masking", "mask_refine", 0.15)
            plan = self._orch(self.llm.agent_refine_plan, self.grid_render, self.grid_lasso, j["instruction"],
                              eng._scene(j["asset_id"]), feedback)
            j["agent_plan"] = plan
            if not plan.get("found"):
                self.event("masking", "error", f"지시한 부위를 찾지 못했습니다 — {plan.get('note_ko') or ''}".strip(" —"))
                raise StageError("NO_TARGET", plan.get("note_ko") or "지시한 부위를 찾지 못했다")
            self.g_plan = plan
        plan = self.g_plan
        cams = am.view_cameras(self.occ)
        pk = am.pick_view(self.U, self.occ, cams, self.cam)
        j["agent_pick"] = {"view": pk["view"], "counts": pk["counts"]}
        names = ["user"] + list(am.VIEW_AZ)
        thumbs = self._pick_thumbs(cams, pk["view"])
        label = "사용자 카메라" if pk["view"] == "user" else f"방위 {am.VIEW_KO[pk['view']]}"
        self.event("masking", "tool", f"물건이 가장 잘 보이는 방향({label})을 골랐습니다", {"views": thumbs},
                   detail="visible lasso cells per view: " + ", ".join(f"{n} {pk['counts'][n]}" for n in names))
        self.render_with(pk["camera"], self.U, pk["view"])
        return {"plan": plan, "cells": self.U, "n": len(self.U)}

    def _pick_thumbs(self, cams: dict, chosen: str) -> Optional[bytes]:
        """5후보 축소 이미지(3×2, 고른 칸 빨간 테두리). 시점 렌더는 캐시(없으면 렌더)."""
        import numpy as np
        j, eng = self.j, self.eng
        try:
            v = eng.views_cached(j["asset_id"], j["base_version"]) or eng.render_views(j["asset_id"], j["base_version"])
            imgs = {n: am.half(am.half(am.to_rgb(decode_png(b)))) for n, b in v["png"].items()}
            rgb, _ = self._render_arrays_user()
            imgs["user"] = am.half(am.half(rgb))
        except Exception as e:                                 # noqa: BLE001 — 축소 이미지는 없어도 진행
            j["agent_pick_thumb_error"] = f"{type(e).__name__}: {e}"
            return None
        tiles = []
        for n in ["user"] + list(am.VIEW_AZ):
            t = imgs[n].copy()
            am.draw_text(t, n, 4, 4, scale=2)
            if n == chosen:
                t[:4], t[-4:], t[:, :4], t[:, -4:] = (255, 0, 0), (255, 0, 0), (255, 0, 0), (255, 0, 0)
            tiles.append(t)
        blank = np.full_like(tiles[0], 255)
        tiles.append(blank)
        return encode_png(np.concatenate([np.concatenate(tiles[:3], 1), np.concatenate(tiles[3:], 1)], 0))

    def _render_arrays_user(self):
        return self.user_rgb, None

    def check_lasso(self, m: dict, region: Optional[dict]) -> dict:
        j = self.j
        ov = (region or {}).get("images", [None])[0]
        self._state("masking", "mask_check", 0.3)
        self._mock("check")
        c = self._orch(self.llm.agent_mask_check, ov or b"", j["instruction"], m["plan"])
        self.event("mask_check", "check", ("마스크 확인: 맞습니다" if c["ok"] else f"마스크 확인: {c['reason_ko']}"),
                   detail=None if c["ok"] else (c.get("fix") or None))
        return c

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
        c = self._orch(self.llm.agent_mask_check, ov or b"", j["instruction"], plan, side_hint=self.side_hint)
        self.event("mask_check", "check", ("마스크 확인: 맞습니다" if c["ok"] else f"마스크 확인: {c['reason_ko']}"),
                   detail=None if c["ok"] else (c.get("fix") or None))
        return c

    def refine(self, plan: dict, feedback: Optional[str], region: Optional[dict]) -> None:
        j, eng = self.j, self.eng
        self._state("refining", "refining", 0.4)
        if eng.be.name == "mock":
            time.sleep(eng.be.DUR["refine"] * self.s.mock_time_scale)
        verb = {"add": "Add", "replace": "Replace with", "remove": "Remove", "recolor": "Recolor to"}[plan["op"]]
        ins = (f"Selected part: {plan['target_part']}. {verb}: {plan['new_object']}."
               + (f" Previous attempt problem: {feedback}." if feedback else "") + f" User instruction: {j['instruction']}")
        if j.get("raw_prompt"):                             # S20b: 그대로 모드 마커 — 보정 없이 지시문 그대로(S14 _run_raw 와 같다)
            raw = j["instruction"].strip()
            scene = eng._scene(j["asset_id"])
            j["refine"] = {"target_element": None, "replacement": raw, "op": "replace",
                           "new_scene_prompt": f"{scene}; {raw}" if scene else raw, "inpaint_prompt": raw}
            j["refine_meta"], j["style_meta"] = {"raw_prompt": True}, None
            self.event("refining", "tool", "그릴 내용: 지시문 그대로", detail=eng._inpaint_text(j)[1])
            return
        if getattr(eng.llm, "name", "") == "fake":         # 가짜 보정은 지시문을 그대로 옮기므로 사용자 원문을(앱 화면용)
            ins = j["instruction"]
        try:
            j["refine"], smeta = eng._refine_styled(j, eng._scene(j["asset_id"]), [], region=region, instruction=ins,
                                                    ko=True)
        except LLMInvalid as e:
            raise StageError("REFINE_INVALID", str(e))
        except LLMUnavailable as e:
            raise StageError("LLM_UNAVAILABLE", str(e))
        j["refine_meta"], j["style_meta"], j["agent_refine_input"] = {}, smeta, ins
        j["prompts"] = dict(j["prompts"], region_fit=(smeta or {}).get("region_fit"),
                            refine_retried=bool((smeta or {}).get("retried")))
        ko = str(j["refine"].get("inpaint_ko") or "").strip()
        if inpaint_ko_problem(ko):                          # S20b: 재요청 뒤에도 없으면 대체 문장
            ko = f"{plan.get('target_part_ko') or plan.get('target_part')} → {plan.get('new_object')}"
            j["agent_inpaint_ko_fallback"] = True
        j["agent_inpaint_ko"] = ko
        self.event("refining", "tool", f"그릴 내용: {ko}", detail=eng._inpaint_text(j)[1])

    def inpaint(self) -> bytes:
        j, eng = self.j, self.eng
        self._state("inpainting", "inpainting", 0.5)
        eng._run_render_inpaint(j, new_render=False, agent=True)
        d = eng._preview_dir(j, j["attempt"])
        edit = (d / "edit.png").read_bytes()
        self.event("inpainting", "tool", f"편집 이미지를 그렸습니다(시도 {j['attempt']})", {"edit": edit})
        if j.get("agent_source") == "lasso" and self.s.inpaint_backend == "gemini" and self.s.grow_from_image:
            self.grow(d, edit)
        return edit

    def grow(self, d, edit: bytes) -> None:
        """S20f 2.6: 새 물건이 라쏘보다 크면 빈 셀로 볼륨을 넓히고, 같은 카메라로 다시 렌더해 이번 시도 렌더·마스크를 바꾼다
        (A5000 은 편집 셀이 렌더 때 셀과 같아야 한다)."""
        j, eng = self.j, self.eng
        rgb = am.to_rgb(decode_png((d / "render.png").read_bytes()))
        um = mask_bool(decode_png((d / "mask.png").read_bytes()))
        er = am.to_rgb(decode_png(edit))
        T = (getattr(self, "consts", None) or am.size_consts(self.occ))["T"]
        g = am.grow_from_image(rgb, er, um, self.U, self.occ, j["camera"], T)
        rec = {k: v for k, v in g.items() if k not in ("grown", "s_mask")} | {"attempt": j["attempt"], "T": T}
        j.setdefault("agent_grow", []).append(rec)
        if g["skip"] == "too_large":
            self.force_prompt = True
            self.event("inpainting", "check", f"그림 전체가 많이 바뀌어(화면 {g['diff_frac']:.0%}) 볼륨을 넓히지 않고 다시 그립니다",
                       detail=f"diff fraction {g['diff_frac']} > {am.GROW_MAX_FRAC}")
            return
        if not g["n_grown"]:
            return
        final = [list(c) for c in {tuple(c) for c in self.U} | {tuple(c) for c in g["grown"]}]
        self.render_with(j["camera"], final, j.get("render_view") or "picked")
        rd = self.s.data_dir / "jobs" / j["job_id"] / "render"
        for n in ("render.png", "mask.png"):
            (d / n).write_bytes((rd / n).read_bytes())
        if j.get("attempts"):
            j["attempts"][-1].update(render_id=j["render"]["render_id"], grown=g["n_grown"])
        S = g["s_mask"]
        big = np.repeat(np.repeat(S, er.shape[0] // S.shape[0], 0), er.shape[1] // S.shape[1], 1)
        over = am.tint_mask(er, big[:er.shape[0], :er.shape[1]], color=(40, 120, 255))
        self.event("inpainting", "tool", f"새 물건이 그린 영역보다 커서 볼륨을 넓혔습니다(+{g['n_grown']:,}셀)",
                   {"overlay": encode_png(am.half(over))},
                   detail=f"diff {g['diff_frac']} silhouette {g['s_frac']} depth {g.get('depth_range')} T {T}; "
                          f"cells {len(self.U)} -> {len(final)}")

    def judge(self, edit: bytes, region: Optional[dict]) -> dict:
        j, eng = self.j, self.eng
        self._state("judging", "judging", 0.6)
        self._mock("judge")
        d = eng._preview_dir(j, j["attempt"])
        ov = (region or {}).get("images", [None])[0] or (d / "mask.png").read_bytes()
        return self._orch(self.llm.agent_judge, (d / "render.png").read_bytes(), ov, edit, j["instruction"],
                          j["prompts"].get("inpaint") or "", side_hint=self.side_hint)   # S20d: 확인과 같은 기대 위치

    # ── 루프 ─────────────────────────────────────────────────────
    def run(self) -> None:
        j, eng, s = self.j, self.eng, self.s
        ag = j["agent"]
        tries: list = []
        feedback: Optional[str] = None
        step = "mask"
        plan, region, m = None, None, None
        stop = None
        lasso = j.get("agent_source") == "lasso"          # S20e: 사용자 라쏘 볼륨 다듬기(자동 마스킹 아님)
        gem = lasso and s.inpaint_backend == "gemini"     # S20f: 라쏘 U 그대로 + 잘 보이는 시점 + Gemini
        try:
            if lasso:
                self.lasso_start()
            else:
                self.views()
            while True:
                if step == "mask":
                    m = self.gemini_prepare(feedback) if gem else (self.refine_mask(feedback) if lasso else self.mask(feedback))
                    plan = m["plan"]
                    region = self.region() if m["cells"] else None
                    c = {"ok": True} if gem else (self.check_lasso(m, region) if lasso else self.check(m, region))
                    if not c["ok"] or not m["cells"]:
                        # S20d: 마스크 다시 칠하기는 되돌아가기(채점 뒤)와 따로 센다
                        mr, mmax = ag.get("mask_retries", 0), ag.get("max_mask_retries", s.agent_max_mask_retries)
                        if mr < mmax:
                            ag["mask_retries"] = mr + 1
                            feedback = f"{c.get('reason_ko')} / {c.get('fix')}".strip(" /")
                            self.event("mask_check", "branch",
                                       f"{c.get('reason_ko')} → " + (f"마스크를 다시 다듬습니다(마스크 다시 다듬기 {mr + 1}/{mmax})"
                                       if lasso else f"볼륨 마스킹을 다시 합니다(마스크 다시 칠하기 {mr + 1}/{mmax})"))
                            continue
                        if not m["cells"]:
                            raise StageError("MASK_EMPTY", "자동 마스킹 셀이 없다(마스크 다시 칠하기 한도)")
                        self.event("mask_check", "branch", "마스크 다시 칠하기 한도라 이 마스크로 진행합니다")
                if step in ("mask", "prompt"):
                    self.refine(plan, feedback, region)
                edit = self.inpaint()
                v = self.judge(edit, region)
                checks = {k: bool(v["checks"].get(k)) for k in CHECKS}
                cause = cause_of(checks)
                if gem and cause:                             # S20f: 마스크 없음 → 원인이 무엇이든 프롬프트 다시 쓰기
                    cause = "prompt"
                if gem and getattr(self, "force_prompt", False):   # 2.6: 그림 전체가 바뀜 → 넓히지 않고 다시 그리기
                    cause, self.force_prompt = cause or "prompt", False
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
                self.event("judging", "judge", detail=v.get("fix") or None,
                           text=f"{v.get('reason_ko')}(불합격: {bad}) → {('마스크 다듬기부터' if lasso and cause == 'mask' else CAUSE_KO[cause])} 다시 합니다"
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
