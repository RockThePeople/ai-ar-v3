"""S2 — LLM 어댑터 연결: 실패 코드, 거절 이력, 검사 함수, health, 폴백. 네트워크 없음(가짜 어댑터)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app.llm_adapter import ClaudeAdapter, FakeAdapter, LLMInvalid, LLMUnavailable, make_adapter
from server.app.main import create_app
from server.tests.app_fakes import FakeBackend
from server.tests.test_app_server import A, Clock, _settings, edit, err, job, make_asset


class StubLLM:
    """refine·classify 결과를 차례로 내거나 예외를 던진다."""

    name = "claude"
    model = "stub"

    def __init__(self, refines=(), classes=()):
        self.refines, self.classes = list(refines), list(classes)
        self.calls = []
        self.last_ok = None

    def _next(self, q, rec):
        self.calls.append(rec)
        x = q.pop(0)
        if isinstance(x, Exception):
            self.last_ok = False
            raise x
        self.last_ok = True
        return x

    def refine(self, scene, instruction, history, job_id=""):
        return self._next(self.refines, ("refine", scene, instruction, list(history)))

    def classify(self, reason, refine, history, job_id=""):
        return self._next(self.classes, ("classify", reason, list(history)))


R1 = {"target_element": "the man", "replacement": "Ironman", "op": "replace", "new_scene_prompt": "An Ironman riding"}
R2 = {"target_element": "the man", "replacement": "a red Ironman", "op": "replace", "new_scene_prompt": "A red Ironman riding"}


def _env(tmp_path, llm):
    app = create_app(_settings(tmp_path), backend=FakeBackend(), clock=Clock(), start_thread=False, llm=llm)
    c = TestClient(app)
    c.eng = app.state.app.engine
    return c


def _asset(c):
    c.eng.llm, keep = FakeAdapter(), c.eng.llm      # 생성은 LLM 을 안 쓴다
    aid = make_asset(c)
    c.eng.llm = keep
    return aid


def test_prompt_reject_feeds_history_and_scene_updates(tmp_path):
    llm = StubLLM([R1, R2], [{"category": "prompt", "guidance": "make the armor red"}])
    c = _env(tmp_path, llm)
    aid = _asset(c)
    jid = edit(c, aid).json()["job_id"]
    c.eng.drain()
    assert job(c, jid)["refine"] == R1
    j = c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "갑옷 색이 틀렸어"}, headers=A).json()
    assert j["state"] == "refining" and j["review"] == {"reason": "갑옷 색이 틀렸어", "category": "prompt",
                                                         "guidance": "make the armor red"}
    c.eng.drain()
    assert llm.calls[-1] == ("refine", "a man riding a motorcycle", "change the man into Ironman", ["make the armor red"])
    assert job(c, jid)["refine"] == R2
    c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    c.eng.drain()
    c.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    assert c.get("/app/v1/qr/booth-07", headers=A).json()["scene_prompt"] == "A red Ironman riding"
    assert c.get("/app/v1/health").json()["workers"]["llm"] == "configured"


@pytest.mark.parametrize("exc,code", [(LLMInvalid("x"), "REFINE_INVALID"), (LLMUnavailable("x"), "LLM_UNAVAILABLE")])
def test_refine_failures(tmp_path, exc, code):
    c = _env(tmp_path, StubLLM([exc]))
    jid = edit(c, _asset(c)).json()["job_id"]
    c.eng.drain()
    j = job(c, jid)
    assert j["state"] == "failed" and j["error_code"] == code


@pytest.mark.parametrize("exc,code", [(LLMInvalid("x"), "CLASSIFY_FAILED"), (LLMUnavailable("x"), "LLM_UNAVAILABLE")])
def test_classify_failures(tmp_path, exc, code):
    c = _env(tmp_path, StubLLM([R1], [exc]))
    jid = edit(c, _asset(c)).json()["job_id"]
    c.eng.drain()
    r = c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "별로"}, headers=A)
    assert r.status_code == 200 and r.json()["state"] == "failed" and r.json()["error_code"] == code
    assert c.get("/app/v1/health").json()["workers"]["llm"] == "error"


def test_quality_and_region(tmp_path):
    c = _env(tmp_path, StubLLM([R1], [{"category": "quality", "guidance": "blurry"},
                                      {"category": "region", "guidance": "the left wheel"}]))
    jid = edit(c, _asset(c)).json()["job_id"]
    c.eng.drain()
    j = c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A).json()
    assert j["state"] == "inpainting" and j["attempt"] == 2
    c.eng.drain()
    assert ("inpaint_args", "An Ironman riding", 1) in c.eng.be.calls             # 같은 프롬프트, seed + 1
    j = c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "다른 바퀴"}, headers=A).json()
    assert j["state"] == "needs_redraw"


def test_check_functions():
    ok = dict(R1)
    assert ClaudeAdapter.check_refine(ok) is None
    assert "한글" in ClaudeAdapter.check_refine({**ok, "new_scene_prompt": "아이언맨"})
    assert "좌표" in ClaudeAdapter.check_refine({**ok, "replacement": "helmet at (31, 40, 22)"})
    assert "op" in ClaudeAdapter.check_refine({**ok, "op": "move"})
    assert "필드" in ClaudeAdapter.check_refine({**ok, "position": 1})
    assert ClaudeAdapter.check_classify({"category": "region", "guidance": "x"}) is None
    assert "category" in ClaudeAdapter.check_classify({"category": "other", "guidance": "x"})


def test_fallback_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_adapter(False), FakeAdapter)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(make_adapter(True), FakeAdapter)                           # 가짜 모드는 키가 있어도 규칙 기반
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(create_app(_settings(tmp_path), backend=FakeBackend(), clock=Clock(), start_thread=False))
    assert c.get("/app/v1/health").json()["workers"]["llm"] == "missing"


def test_llm_calls_logged(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _env(tmp_path, None)                                                    # 키 없음 → FakeAdapter + DB 기록
    jid = edit(c, _asset(c)).json()["job_id"]
    c.eng.drain()
    calls = c.get(f"/app/v1/debug/api/jobs/{jid}").json()["llm_calls"]
    assert [x["kind"] for x in calls] == ["refine"] and calls[0]["model"] == "rule-based"


# ── S4: 생성 프롬프트 영어 변환, 부정 표현 금지 ─────────────────────
from server.app.llm_adapter import is_english  # noqa: E402


class TransLLM(StubLLM):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def translate_prompt(self, prompt, job_id=""):
        self.calls.append(("translate", prompt))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _gen(c, prompt):
    r = c.post("/app/v1/qr/st-01/asset", json={"prompt": prompt}, headers=A)
    c.eng.drain()
    return job(c, r.json()["job_id"]), c.eng.get(r.json()["job_id"])


def test_translate_korean_prompt(tmp_path):
    llm = TransLLM("a blue ceramic teapot")
    c = _env(tmp_path, llm)
    j, raw = _gen(c, "파란 도자기 찻주전자")
    assert j["state"] == "committed" and ("translate", "파란 도자기 찻주전자") in llm.calls
    assert raw["prompt_en"] == "a blue ceramic teapot" and raw["translate"] == {"ok": True}
    assert c.get("/app/v1/qr/st-01", headers=A).json()["scene_prompt"] == "a blue ceramic teapot"


def test_translate_skips_english_and_survives_failure(tmp_path):
    llm = TransLLM(LLMUnavailable("down"))
    c = _env(tmp_path, llm)
    j, raw = _gen(c, "a red pickup truck")
    assert raw["translate"] == {"skipped": "english"} and not [x for x in llm.calls if x[0] == "translate"]
    c.delete("/app/v1/qr/st-01", headers=A)
    j, raw = _gen(c, "빨간 트럭")                                                 # LLM 실패 → 원문으로 생성 계속
    assert j["state"] == "committed" and raw["prompt_en"] == "빨간 트럭" and raw["translate"]["ok"] is False


def test_negation_rejected():
    ok = {"target_element": "the armrests", "replacement": "plain open seat sides", "op": "remove",
          "new_scene_prompt": "a wooden chair with plain open sides and a knob on the north side"}
    assert ClaudeAdapter.check_refine(ok) is None                                  # knob·north 는 통과
    for bad in ("a chair without armrests", "a backrest, not a solid panel", "no armrests", "it isn't red",
                "slats instead of a panel"):
        assert "부정" in ClaudeAdapter.check_refine({**ok, "new_scene_prompt": bad}), bad
    assert "부정" in ClaudeAdapter.check_refine({**ok, "replacement": "nothing"})
    assert ClaudeAdapter.check_translate({"prompt_en": "a teapot with no lid"}) and \
        ClaudeAdapter.check_translate({"prompt_en": "a blue teapot"}) is None
    assert is_english("a man’s red car — 2 doors") and not is_english("빨간 차")
