"""앱 C13: 8084 가짜 모드 — X-Mock-Agent: mask_retry 가 Gemini 라쏘 경로에서도 나오고, X-Mock-Fail: NO_TARGET 은 마스크 다듬기 단계에서."""
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture
from server.tests.test_gemini import PASS, FakeGemini


def _mock_mode(env):
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.grow_from_image = True
    env.eng.s.mock = True                                                         # 라우트가 X-Mock-* 헤더를 받는다
    env.be.name = "mock"
    env.be.DUR = {"generate": 0, "refine": 0, "inpaint": 0, "edit3d": 0}
    env.eng.s.mock_time_scale = 0
    env.eng.gemini = FakeGemini()
    env.eng.agent_llm.judge_script = [PASS]


def _lasso(env, aid, headers):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers={**A, **headers},
                 json={"base_version": 1, "instruction": "오른손에 칼을 들게 해줘", "camera": CAM, "volume_cells": CELLS,
                       "agent": True})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def test_mock_mask_retry_in_gemini_lasso_flow(env):
    aid = make_asset(env)
    _mock_mode(env)
    try:
        j = _lasso(env, aid, {"X-Mock-Agent": "mask_retry"})
    finally:
        env.be.name = "fake"
    assert j["state"] == "committed" and j["agent"]["mask_retries"] == 1
    texts = [e["text"] for e in j["events"]]
    checks = [t for t in texts if t.startswith("마스크 확인")]
    assert checks == ["마스크 확인: 칠한 자리가 너무 좁습니다", "마스크 확인: 맞습니다"]
    assert any("마스크를 다시 다듬습니다(마스크 다시 다듬기 1/" in t for t in texts)
    stages = [s["stage"] for s in env.eng.get(j["job_id"])["stage_log"]]
    assert stages.count("mask_check") >= 1


def test_mock_default_has_no_mask_check(env):
    aid = make_asset(env)
    _mock_mode(env)
    try:
        j = _lasso(env, aid, {"X-Mock-Agent": "pass"})
    finally:
        env.be.name = "fake"
    assert j["state"] == "committed" and not any(e["text"].startswith("마스크 확인") for e in j["events"])


def test_mock_no_target_fails_in_mask_refine(env):
    aid = make_asset(env)
    _mock_mode(env)
    try:
        j = _lasso(env, aid, {"X-Mock-Fail": "NO_TARGET"})
    finally:
        env.be.name = "fake"
    assert j["state"] == "failed" and j["error_code"] == "NO_TARGET"
    full = env.eng.get(j["job_id"])
    hist = [h.get("state") for h in full["history"]]
    assert "masking" in hist                                                      # queued 에서 바로가 아니라
    assert full["stage"] == "mask_refine"
    assert j["events"][-1]["kind"] == "error" and "지시한 부위를 찾지 못했습니다" in j["events"][-1]["text"]


def test_other_mock_fail_still_at_start(env):
    aid = make_asset(env)
    _mock_mode(env)
    try:
        j = _lasso(env, aid, {"X-Mock-Fail": "INPAINT_FAILED"})
    finally:
        env.be.name = "fake"
    assert j["state"] == "failed" and j["error_code"] == "INPAINT_FAILED"
