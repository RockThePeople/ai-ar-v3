"""S20 에이전트 편집(app-api 1.1.0 10절): 역투영, 제어 루프(가짜 오케스트레이터), 계약 모양."""

from __future__ import annotations

import pytest

from server.app import automask as am
from server.app.agent import best_attempt, cause_of
from server.tests.test_app_server import A, B, CAM, CELLS, env, make_asset  # noqa: F401 — fixture

PASS = {"element": True, "position": True, "mask_fit": True, "shape_3d": True}


def _f(**kw):
    return dict(PASS, **kw)


# ══════════════════════════════════════════════════════════ 역투영
def _body():
    """정육면체(몸통) + +x 쪽 막대(팔)."""
    cube = [[i, j, k] for i in range(24, 40) for j in range(24, 40) for k in range(16, 40)]
    rod = [[i, 31, 30] for i in range(40, 52)]
    return cube + rod


def test_projection_center_and_sides():
    occ = _body()
    cams = am.view_cameras(occ)
    c = am.centers(occ)
    mid = (c.min(0) + c.max(0)) / 2
    for v in am.VIEW_AZ:
        px, py, z = am.project(mid[None], cams[v])
        assert abs(px[0] - 512) < 1 and abs(py[0] - 512) < 1 and z[0] > 0       # forward → 경계 상자 중심
    # 막대(+x)는 az000(+y 쪽에서 봄)에서 이미지 왼쪽, az180 에서 오른쪽, az090(+x 쪽)에서 가운데 앞
    tip = am.centers([[51, 31, 30]])
    assert am.project(tip, cams["az000"])[0][0] < 512 < am.project(tip, cams["az180"])[0][0]
    assert abs(am.project(tip, cams["az090"])[0][0] - 512) < 60


def _box_of(cells, cam, pad=0):
    px, py, _ = am.project(am.centers(cells), cam)
    s = 1000 / am.RES
    return [int(px.min() * s) - pad, int(py.min() * s) - pad, int(px.max() * s) + 1 + pad, int(py.max() * s) + 1 + pad]


def test_backproject_two_views_selects_rod_region():
    occ = _body()
    cams = am.view_cameras(occ)
    rod = [[i, 31, 30] for i in range(44, 52)]
    boxes = {"az000": _box_of(rod, cams["az000"], 60), "az270": _box_of(rod, cams["az270"], 60),
             "az090": None, "az180": None}
    add = am.backproject([{"name": "arm", "boxes": boxes}], cams, occ, "add")
    rep = am.backproject([{"name": "arm", "boxes": boxes}], cams, occ, "replace")
    sa, sr = {tuple(c) for c in add["cells"]}, {tuple(c) for c in rep["cells"]}
    assert {tuple(c) for c in rod} <= sr <= sa                     # 막대 셀은 둘 다, replace ⊂ add
    assert len(sa) > len(sr)                                       # add 는 빈 공간 포함
    near = am.dilate(occ, 2)
    assert all(near[c] for c in sr)                                # replace = 점유 2셀 팽창과 겹치는 셀만
    assert not any(near[c] for c in sa - sr)
    assert max(c[0] for c in sa) < 56 and min(c[0] for c in sa) >= 40   # 몸통 쪽으로 번지지 않음


def test_backproject_too_large_shrinks_then_fails():
    occ = _body()
    cams = am.view_cameras(occ)
    full = {"az000": [0, 0, 1000, 1000], "az090": [0, 0, 1000, 1000], "az180": None, "az270": None}
    r = am.backproject([{"name": "all", "boxes": full}], cams, occ, "add")
    assert r["n"] <= am.MAX_CELLS and r["shrink"] >= 1 and not r["too_large"]
    big = [[i, j, k] for i in range(64) for j in range(64) for k in range(64) if (i + j + k) % 2 == 0]
    r2 = am.backproject([{"name": "all", "boxes": full}], am.view_cameras([[0, 0, 0], [63, 63, 63]]), big, "add")
    assert r2["too_large"] is True and r2["cells"] == []


def test_cause_rules_and_best_attempt():
    assert cause_of(PASS) is None
    assert cause_of(_f(position=False, element=False)) == "mask"
    assert cause_of(_f(mask_fit=False)) == "mask"
    assert cause_of(_f(element=False, shape_3d=False)) == "prompt"
    assert cause_of(_f(shape_3d=False)) == "seed"
    t = [{"attempt": 1, "checks": _f(position=False)}, {"attempt": 2, "checks": _f(element=False)},
         {"attempt": 3, "checks": _f(position=False, element=False)}]
    assert best_attempt(t)["attempt"] == 2                         # 같은 3개 합격 → 나중
    t.append({"attempt": 4, "checks": _f(shape_3d=False)})
    assert best_attempt(t)["attempt"] == 4


# ══════════════════════════════════════════════════════════ 제어 루프
def _agent(env, aid, text="오른손에 칼을 들게 해줘", base=1):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": base, "instruction": text})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def test_default_script_mask_backtrack_then_commit(env):
    aid = make_asset(env)
    j = _agent(env, aid)
    assert j["state"] == "committed" and j["mode"] == "agent" and j["result"]["to_version"] == 2
    assert j["result"]["manifest_uri"].endswith("manifest.v2.json")
    ag = j["agent"]
    assert ag["backtracks"] == 1 and ag["attempt"] == 2 and ag["below_bar"] is False
    assert ag["verdict"] == {"pass": True, "checks": PASS, "cause": None}
    ev = j["events"]
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    assert ev[0]["images"]["views"].endswith("/agent/1/views.png") and ev[-1]["kind"] == "done"
    assert any(e["kind"] == "judge" and "볼륨 마스킹부터" in e["text"] and "1/3" in e["text"] for e in ev)
    assert sum(1 for e in ev if "overlay" in (e.get("images") or {})) == 2      # 마스킹 2번
    assert env.get(ev[0]["images"]["views"]).status_code == 200
    assert j["prompts"]["inpaint"] and j["review"] is None and j["expires_at"] is None
    assert env.eng.db.asset(aid)["version"] == 2
    # 마스킹 2번 → 렌더(시점 4 + 인페인팅 시점 2), 인페인팅 2번(seed 0, 1), 3D 1번
    calls = [c[0] for c in env.be.calls]
    assert calls.count("render") == 6 and calls.count("inpaint") == 2 and calls.count("edit3d") == 1
    assert [c[2] for c in env.be.calls if c[0] == "inpaint_args"] == [0, 1]


def test_prompt_cause_refines_again_seed_cause_does_not(env):
    aid = make_asset(env)
    env.eng.agent_llm.judge_script = [_f(element=False), PASS]
    j = _agent(env, aid)
    assert j["state"] == "committed" and j["agent"]["backtracks"] == 1
    assert sum(1 for e in j["events"] if "overlay" in (e.get("images") or {})) == 1       # 마스킹 1번
    assert sum(1 for e in j["events"] if e["step"] == "refining") == 2                     # 보정 2번
    assert any("프롬프트 보정부터" in e["text"] for e in j["events"])
    env.eng.agent_llm.judge_script = [_f(shape_3d=False), PASS]
    j2 = _agent(env, aid, "칼을 횃불로 바꿔줘", base=2)
    assert j2["state"] == "committed" and j2["result"]["to_version"] == 3
    assert sum(1 for e in j2["events"] if e["step"] == "refining") == 1                    # 보정 1번
    assert any("인페인팅(다른 seed)부터" in e["text"] for e in j2["events"])
    assert [c[2] for c in env.be.calls if c[0] == "inpaint_args"][-2:] == [0, 1]


def test_mask_check_failure_counts_as_backtrack(env):
    aid = make_asset(env)
    env.eng.agent_llm.mask_check_ok = [False, True]
    env.eng.agent_llm.judge_script = [PASS]
    j = _agent(env, aid)
    assert j["state"] == "committed" and j["agent"]["backtracks"] == 1 and j["agent"]["attempt"] == 1
    assert any(e["kind"] == "branch" and "1/3" in e["text"] for e in j["events"])
    assert [c[0] for c in env.be.calls].count("inpaint") == 1


def test_three_backtracks_then_best_attempt(env):
    aid = make_asset(env)
    env.eng.agent_llm.judge_script = [_f(position=False), _f(shape_3d=False), _f(element=False),
                                      _f(position=False, element=False)]
    j = _agent(env, aid)
    assert j["state"] == "committed" and j["agent"]["backtracks"] == 3 and j["agent"]["below_bar"] is True
    jj = env.eng.get(j["job_id"])
    assert jj["agent_chosen"] == 3                                 # 2·3 이 3개 합격 → 나중(3)
    assert any("시도 3의 이미지로" in e["text"] for e in j["events"])
    assert "기준에 못 미친" in j["events"][-1]["text"] or any("기준에 못 미친" in e["text"] for e in j["events"])
    h = env.get(f"/app/v1/assets/{aid}/history").json()
    e = [x for x in h["jobs"] if x["kind"] == "edit"][0]
    assert e["mode"] == "agent" and e["events"] == j["events"]
    assert [a["accepted"] for a in e["attempts"]] == [False, False, True, False]


def test_no_target(env):
    aid = make_asset(env)
    env.eng.agent_llm.mask_found = False
    j = _agent(env, aid)
    assert j["state"] == "failed" and j["error_code"] == "NO_TARGET" and j["events"][-1]["kind"] == "error"
    assert j["agent"]["backtracks"] == 0


def test_time_limit_before_any_attempt_fails_after_attempt_uses_best(env):
    aid = make_asset(env)
    env.eng.s.agent_time_s = -1
    j = _agent(env, aid)
    assert j["state"] == "failed" and j["error_code"] == "TIMEOUT"
    env.eng.s.agent_time_s = 600
    fake = env.eng.agent_llm
    orig = fake.agent_judge

    def judge_then_late(*a, **k):
        out = orig(*a, **k)
        env.clock.t += 700                                         # 첫 채점 뒤 10분 넘김
        return out
    fake.agent_judge = judge_then_late
    fake.judge_script = [_f(shape_3d=False)]
    j2 = _agent(env, aid)
    assert j2["state"] == "committed" and j2["agent"]["below_bar"] is True
    assert any("시간 한도" in e["text"] for e in j2["events"])


def test_orchestrator_json_error_retry_then_agent_failed(env):
    aid = make_asset(env)
    env.eng.agent_llm.fail = {"agent_judge": 1}                    # 가짜 = 어댑터 안 재요청까지 실패한 경우
    j = _agent(env, aid)
    assert j["state"] == "failed" and j["error_code"] == "AGENT_FAILED"


def test_claude_agent_adapter_retries_once_then_invalid(monkeypatch):
    """ClaudeAdapter._call: 스키마 위반 1회 재요청, 그래도 틀리면 LLMInvalid(→ AGENT_FAILED)."""
    from server.app import llm_adapter as L

    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()

        def model_dump(self, **k):
            return {}
    sent = []

    class Client:
        class messages:
            @staticmethod
            def create(**req):
                sent.append(req)
                return Msg('{"found": true, "regions": []}')
    ad = L.ClaudeAgentAdapter.__new__(L.ClaudeAgentAdapter)
    ad.anthropic = type("AN", (), {"AuthenticationError": RuntimeError, "RateLimitError": RuntimeError,
                                   "APIConnectionError": RuntimeError, "APIStatusError": RuntimeError,
                                   "__version__": "x"})
    ad.client, ad.model, ad.effort, ad.on_call, ad.last_ok = Client, "claude-opus-5-5", "low", (lambda r: None), None
    monkeypatch.setattr(L, "_raw_response", lambda r: {}, raising=False)
    with pytest.raises(L.LLMInvalid):
        ad.agent_mask({v: b"\x89PNG" for v in L.AGENT_VIEWS}, "칼을 들게", "a man", None, job_id="j-x")
    assert len(sent) == 2


# ══════════════════════════════════════════════════════════ 계약 모양
def test_contract_shapes(env):
    aid = make_asset(env)
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "x", "camera": CAM})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_REQUEST"
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "x", "volume_cells": CELLS})
    assert r.status_code == 400
    # 1.0.6 흐름은 그대로(mode·agent·events 없음)
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": 1, "instruction": "빨간 모자", "camera": CAM, "volume_cells": CELLS})
    env.eng.drain()
    o = env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()
    assert o["state"] == "awaiting_2d_review" and "mode" not in o and "events" not in o
    env.post(f"/app/v1/jobs/{o['job_id']}/cancel", headers=A)
    # 에이전트 작업: review2d·review3d·cancel = 409 WRONG_STATE
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "오른손에 칼"})
    jid = r.json()["job_id"]
    o = env.get(f"/app/v1/jobs/{jid}", headers=A).json()
    assert o["state"] == "queued" and o["mode"] == "agent" and o["events"] == [] and o["agent"]["max_backtracks"] == 3
    for path, body in (("review2d", {"accept": True}), ("review3d", {"keep": True}), ("cancel", None)):
        rr = env.post(f"/app/v1/jobs/{jid}/{path}", headers=A, json=body)
        assert rr.status_code == 409 and rr.json()["error_code"] == "WRONG_STATE", path
    # 잠금·판본 검사는 3.3 과 같다
    rr = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "또"})
    assert rr.status_code == 409 and rr.json()["error_code"] == "ASSET_LOCKED"
    env.eng.drain()
    rr = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "또"})
    assert rr.status_code == 409 and rr.json()["error_code"] == "VERSION_CONFLICT"
    assert env.get(f"/app/v1/jobs/{jid}/agent/1/nope.png").status_code == 404


def test_agent_toggle_off(env):
    aid = make_asset(env)
    env.eng.s.agent = False
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "오른손에 칼"})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_REQUEST"
    assert env.get("/app/v1/health").json()["features"] == []


def test_mask_plan_check():
    from server.app.llm_adapter import _check_mask_plan
    ok = {"found": True, "regions": [{"name": "a", "boxes": {"az000": [1, 2, 30, 40], "az090": [5, 5, 50, 50],
                                                             "az180": None, "az270": None}}]}
    assert _check_mask_plan(ok) is None
    bad = {"found": True, "regions": [{"name": "a", "boxes": {"az000": [1, 2, 30, 40], "az090": None,
                                                              "az180": [1, 2, 30, 40], "az270": None}}]}
    assert "90°" in _check_mask_plan(bad)
    assert "x0<x1" in _check_mask_plan({"found": True, "regions": [{"name": "a", "boxes": {
        "az000": [30, 2, 1, 40], "az090": [5, 5, 50, 50], "az180": None, "az270": None}}]})
    assert _check_mask_plan({"found": False, "regions": []}) is None


def test_clients_shows_agent_events(env):
    aid = make_asset(env)
    j = _agent(env, aid)
    row = [x for x in env.get(f"/clients/api/assets/{aid}").json()["jobs"] if x["kind"] == "edit"][0]
    assert row["mode"] == "agent" and row["events"] == j["events"] and len(row["agent_tries"]) == 2
    assert row["agent_chosen"] == 2 and any(c["kind"] == "agent_judge" for c in row["llm_calls"])
    assert "agentView" in env.get(f"/clients/{aid}").text
