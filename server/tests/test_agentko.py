"""S20b: 진행 보고 "그릴 내용"(한국어 요약 inpaint_ko) + events[].detail(인페인팅에 보낸 영어 문장)."""

from __future__ import annotations

import json

from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture


def _agent(env, aid, text="오른손에 칼을 들게 해줘", base=1):
    env.eng.s.automask = True                                 # S20e: 자동 마스킹 시험은 토글을 켠다
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": base, "instruction": text})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def _draw_events(j):
    return [e for e in j["events"] if e["step"] == "refining"]


def test_draw_event_text_ko_and_detail_is_sent_prompt(env):
    aid = make_asset(env)
    j = _agent(env, aid)
    assert j["state"] == "committed"
    ev = _draw_events(j)
    assert len(ev) == 2                                           # 기본 가짜 스크립트: 마스킹부터 다시 → 보정 2번
    for e in ev:
        assert e["text"] == "그릴 내용: 오른손에 칼을 들게 해줘"     # 가짜 inpaint_ko = 한글 지시문
    sent = [c[1] for c in env.be.calls if c[0] == "inpaint_args"]
    assert [e["detail"] for e in ev] == sent                      # detail = 실제로 보낸 문장 전체
    assert j["prompts"]["inpaint"] == sent[-1]
    # 원문이 필요 없는 이벤트에는 detail 키가 없다, 채점 불합격에는 고칠 방법(영어) 원문
    assert "detail" not in j["events"][0]
    judge = [e for e in j["events"] if e["kind"] == "judge"]
    assert judge[0]["detail"] == "use the other hand" and "detail" not in judge[-1]
    h = env.get(f"/app/v1/assets/{aid}/history").json()
    assert [x for x in h["jobs"] if x["kind"] == "edit"][0]["events"] == j["events"]
    row = [x for x in env.get(f"/clients/api/assets/{aid}").json()["jobs"] if x["kind"] == "edit"][0]
    assert _draw_events(row)[0]["detail"] == sent[0]
    assert "원문(detail)" in env.get(f"/clients/{aid}").text


def test_fallback_when_no_korean(env):
    aid = make_asset(env)
    env.eng.agent_llm.judge_script = [{"element": True, "position": True, "mask_fit": True, "shape_3d": True}]
    j = _agent(env, aid, "add a sword to the right hand")          # 가짜 inpaint_ko = "" → 대체 문장
    ev = _draw_events(j)
    assert ev[0]["text"] == "그릴 내용: 오른손 → the new object"
    assert ev[0]["detail"] and env.eng.get(j["job_id"])["agent_inpaint_ko_fallback"] is True


def test_raw_mode_agent_uses_instruction_verbatim(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("booth-07", True)
    env.eng.agent_llm.judge_script = [{"element": True, "position": True, "mask_fit": True, "shape_3d": True}]
    calls = []
    orig = env.eng.llm.refine
    env.eng.llm.refine = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    text = "a tall blue pointed wizard hat with yellow stars"
    j = _agent(env, aid, text)
    assert j["state"] == "committed" and calls == []              # 보정 LLM 없음
    ev = _draw_events(j)
    assert ev[0]["text"] == "그릴 내용: 지시문 그대로" and ev[0]["detail"] == text
    assert ("inpaint_args", text, 0) in env.be.calls
    assert j["prompts"]["style"] is None and j["prompts"]["inpaint"] == text


def test_manual_flow_refine_request_unchanged(env):
    """1.0.6 수동 흐름은 inpaint_ko 를 요청하지 않는다(요청 모양 그대로)."""
    aid = make_asset(env)
    seen = []
    orig = env.eng.llm.refine
    env.eng.llm.refine = lambda *a, **k: (seen.append(k.get("region")), orig(*a, **k))[1]
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": 1, "instruction": "빨간 모자", "camera": CAM, "volume_cells": CELLS})
    env.eng.drain()
    o = env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()
    assert o["state"] == "awaiting_2d_review" and "events" not in o
    assert seen and all("inpaint_ko" not in (rg or {}) for rg in seen)


def _claude(monkeypatch, replies):
    from server.app import llm_adapter as L

    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()

        def model_dump(self, **k):
            return {}
    sent = []
    it = iter(replies)

    class Client:
        class messages:
            @staticmethod
            def create(**req):
                sent.append(req)
                return Msg(json.dumps(next(it), ensure_ascii=False))
    ad = L.ClaudeAdapter.__new__(L.ClaudeAdapter)
    ad.anthropic = type("AN", (), {"AuthenticationError": RuntimeError, "RateLimitError": RuntimeError,
                                   "APIConnectionError": RuntimeError, "APIStatusError": RuntimeError,
                                   "__version__": "x"})
    ad.client, ad.model, ad.effort, ad.on_call, ad.last_ok = Client, "claude-haiku-5-5", "low", (lambda r: None), None
    monkeypatch.setattr(L, "_raw_response", lambda r: {}, raising=False)
    monkeypatch.setattr(L.ClaudeAdapter, "check_refine_region_ip", staticmethod(lambda o: None))
    base = {k: "x" for k in L.REFINE_REGION_IP_SCHEMA["properties"]}
    return ad, sent, base, L


def test_claude_inpaint_ko_retry_once_then_accept(monkeypatch):
    from server.app import llm_adapter as L
    base = {k: "x" for k in L.REFINE_REGION_IP_SCHEMA["properties"]}
    ad2, sent2, _, _ = _claude(monkeypatch, [dict(base, inpaint_ko="a short sword"), dict(base, inpaint_ko="sword")])
    out = ad2.refine("a man", "칼", [], job_id="j-1", region={"images": [], "mask_ratio": 0.1, "inpaint_prompt": True,
                                                              "inpaint_ko": True})
    assert len(sent2) == 2 and out["inpaint_ko"] == "sword"       # 1회 재요청, 그래도 없으면 받아 두고 대체는 호출한 쪽
    req = sent2[0]
    assert "inpaint_ko" in req["output_config"]["format"]["schema"]["properties"] and "inpaint_ko" in req["system"]
    ad3, sent3, _, _ = _claude(monkeypatch, [dict(base, inpaint_ko="오른손에 쥔 칼")])
    assert ad3.refine("a man", "칼", [], region={"images": [], "mask_ratio": 0.1, "inpaint_prompt": True,
                                                  "inpaint_ko": True})["inpaint_ko"] == "오른손에 쥔 칼"
    assert len(sent3) == 1
    ad4, sent4, _, _ = _claude(monkeypatch, [base])               # 끔(수동 흐름): 스키마·시스템에 없다
    ad4.refine("a man", "칼", [], region={"images": [], "mask_ratio": 0.1, "inpaint_prompt": True})
    assert "inpaint_ko" not in sent4[0]["output_config"]["format"]["schema"]["properties"]
    assert "inpaint_ko" not in sent4[0]["system"]
    assert L.inpaint_ko_problem("") and L.inpaint_ko_problem("sword") and L.inpaint_ko_problem("칼") is None
