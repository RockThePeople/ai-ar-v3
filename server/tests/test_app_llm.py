"""S2 — LLM 어댑터 연결: 실패 코드, 거절 이력, 검사 함수, health, 폴백. 네트워크 없음(가짜 어댑터)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app.llm_adapter import ClaudeAdapter, FakeAdapter, LLMInvalid, LLMUnavailable, make_adapter
from server.app.main import create_app
from server.tests.test_app_server import env  # noqa: F401  (S8 시험이 같은 fixture 사용)
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
    assert ("inpaint_args", "An Ironman riding" + INPAINT_SUFFIX, 1) in c.eng.be.calls   # 같은 프롬프트, seed + 1
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


# ── S4·S6: 생성 프롬프트 상세화(영어도 거침), 부정 표현 금지, 길이 상한 ──────────
from server.app.engine import INPAINT_SUFFIX  # noqa: E402
from server.app.llm_adapter import MAX_TEXT, is_english  # noqa: E402

PAPER = ("A compact off-road jeep, three-quarter front view, centered, full object visible, chunky all-terrain tires, "
         "a black front winch bumper, a side air-intake snorkel on the right front pillar, a spare tire mounted on the "
         "rear door, olive green paint, simple clean 3D game asset style, pure white background, no shadow")
DETAILED = ("A compact off-road jeep, chunky all-terrain tires, a black front winch bumper, a side air-intake snorkel on "
            "the right front pillar, a spare tire mounted on the rear door, olive green paint")


class DetailLLM(StubLLM):
    def __init__(self, result, refines=()):
        super().__init__(refines=refines)
        self.result = result

    def detail_prompt(self, prompt, job_id=""):
        self.calls.append(("detail", prompt))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _gen(c, prompt):
    r = c.post("/app/v1/qr/st-01/asset", json={"prompt": prompt}, headers=A)
    c.eng.drain()
    return job(c, r.json()["job_id"]), c.eng.get(r.json()["job_id"])


def test_detail_always_runs_and_sets_scene(tmp_path):
    llm = DetailLLM(DETAILED)
    c = _env(tmp_path, llm)
    j, raw = _gen(c, "A compact off-road jeep")                                   # 영어도 거친다
    assert j["state"] == "committed" and ("detail", "A compact off-road jeep") in llm.calls
    assert raw["prompt_en"] == DETAILED and raw["translate"] == {"ok": True, "mode": "detail"}
    assert c.get("/app/v1/qr/st-01", headers=A).json()["scene_prompt"] == DETAILED   # 꼬리 없이


def test_detail_failure_falls_back_to_raw(tmp_path):
    c = _env(tmp_path, DetailLLM(LLMUnavailable("down")))
    j, raw = _gen(c, "빨간 트럭")
    assert j["state"] == "committed" and raw["prompt_en"] == "빨간 트럭" and raw["translate"]["ok"] is False


def test_inpaint_prompt_gets_style_suffix(tmp_path):
    r = {"target_element": "the bumper", "replacement": "a chrome bull bar", "op": "replace",
         "new_scene_prompt": DETAILED.replace("a black front winch bumper", "a chrome steel bull bar")}
    llm = DetailLLM(DETAILED, refines=[r])
    c = _env(tmp_path, llm)
    c.eng.llm, keep = FakeAdapter(), llm
    aid = make_asset(c)
    c.eng.llm = keep
    jid = edit(c, aid).json()["job_id"]
    c.eng.drain()
    want = r["new_scene_prompt"] + INPAINT_SUFFIX
    assert ("inpaint_args", want, 0) in c.eng.be.calls
    a = c.eng.get(jid)["attempts"][0]
    assert a["prompt"] == want and a["new_scene_prompt"] == r["new_scene_prompt"]
    assert "view" not in INPAINT_SUFFIX and "background" not in INPAINT_SUFFIX


def test_negation_style_and_length_checks():
    ok = {"target_element": "the armrests", "replacement": "plain open seat sides", "op": "remove",
          "new_scene_prompt": "a wooden chair with plain open sides and a knob on the north side"}
    assert ClaudeAdapter.check_refine(ok) is None                                  # knob·north 는 통과
    for bad in ("a chair without armrests", "a backrest, not a solid panel", "no armrests", "it isn't red",
                "slats instead of a panel"):
        assert "부정" in ClaudeAdapter.check_refine({**ok, "new_scene_prompt": bad}), bad
    assert "부정" in ClaudeAdapter.check_refine({**ok, "replacement": "nothing"})
    long313 = PAPER.replace(", no shadow", "")                                    # 꼬리의 'no shadow' 는 서버 문구
    assert len(PAPER) >= 313 and ClaudeAdapter.check_refine({**ok, "new_scene_prompt": long313}) is None
    assert ClaudeAdapter.check_refine({**ok, "new_scene_prompt": "x " * 401})     # 800자 초과
    assert ClaudeAdapter.check_detail({"subject_detailed": DETAILED}) is None
    assert "스타일" in ClaudeAdapter.check_detail({"subject_detailed": DETAILED + ", pure white background"})
    assert "부정" in ClaudeAdapter.check_detail({"subject_detailed": "a teapot with no lid"})
    assert ClaudeAdapter.check_detail({"subject_detailed": "a " * 401})
    assert MAX_TEXT == 800 and is_english("a man’s red car — 2 doors") and not is_english("빨간 차")


def test_t2i_template_tail():
    from server import t2i
    eff = t2i.DEFAULT_TEMPLATE.replace("{subject}", DETAILED)
    parts = lambda x: sorted(t.strip() for t in x.split(","))                      # noqa: E731
    assert parts(eff) == parts(PAPER)                                              # 논문 X3 문장과 같은 구절(순서만 다름)
    assert eff.endswith("simple clean 3D game asset style, pure white background, no shadow")


def test_detail_example_is_not_the_jeep():
    """S6 추가: 예시가 지프면 'A compact off-road jeep' 입력에서 예시를 그대로 베낀다(Haiku 실측) → 다른 물체로."""
    from server.app.llm_adapter import DETAIL_SYSTEM
    assert "jeep" not in DETAIL_SYSTEM.lower() and "winch" not in DETAIL_SYSTEM.lower()


# ── S8: prompt 거절 뒤 같은 문장이면 1회 더 ──────────────────────────────
def _s8(c, outputs):
    """refine 출력을 차례로 돌려주는 가짜. 첫 시도용 1개 + 거절 뒤 출력들."""
    calls = []
    seq = list(outputs)

    def refine(scene, instruction, hist, job_id="", changed_from=None):
        calls.append(changed_from)
        nsp = seq.pop(0)
        return {"target_element": "the man", "replacement": "Ironman", "op": "replace", "new_scene_prompt": nsp}
    c.eng.llm.refine = refine
    return calls


def _reject_prompt(c, jid):
    from server.tests.test_app_server import A
    j = c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "프롬프트를 잘못 이해함"}, headers=A).json()
    assert j["review"]["category"] == "prompt"
    c.eng.drain()
    return c.eng.get(jid)


def test_s8_same_sentence_retried_and_changes(env):
    from server.tests.test_app_server import make_asset, to_2d
    calls = _s8(env, ["An Ironman riding.", "an ironman  riding", "A man in red Ironman armor riding"])
    jid = to_2d(env, make_asset(env))
    j = _reject_prompt(env, jid)
    assert calls[0] is None and calls[1] is None and calls[2]["prev"] == "An Ironman riding."
    assert calls[2]["reason"] == "프롬프트를 잘못 이해함" and calls[2]["guidance"]
    a = j["attempts"][-1]
    assert a["new_scene_prompt"] == "A man in red Ironman armor riding" and a["refine_retry"] and not a["refine_unchanged"]
    assert not j.get("refine_unchanged")


def test_s8_still_same_proceeds_and_marks(env):
    from server.tests.test_app_server import make_asset, to_2d
    _s8(env, ["An Ironman riding", "An Ironman riding.", "AN IRONMAN RIDING"])
    jid = to_2d(env, make_asset(env))
    j = _reject_prompt(env, jid)
    assert j["state"] == "awaiting_2d_review" and j["attempt"] == 2
    a = j["attempts"][-1]
    assert a["refine_unchanged"] is True and a["seed"] == 1 and j["refine_unchanged"] is True
    d = env.get(f"/clients/api/assets/{j['asset_id']}").json()
    assert [x for x in d["jobs"] if x["job_id"] == jid][0]["attempts"][-1]["refine_unchanged"] is True


def test_s8_changed_sentence_no_extra_call(env):
    from server.tests.test_app_server import make_asset, to_2d
    calls = _s8(env, ["An Ironman riding", "A golden Ironman riding"])
    jid = to_2d(env, make_asset(env))
    j = _reject_prompt(env, jid)
    assert len(calls) == 2 and "refine_retry" not in j["attempts"][-1]
