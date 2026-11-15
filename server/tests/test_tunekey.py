"""S24b: 피드백 조정 문장을 물건 키로 저장·선택(키가 정확히 같은 줄 + general, 벡터 검색 없음)."""
import json

from server.app import llm_adapter as la
from server.app.ratings import Ratings
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture
from server.tests.test_chain import FakeChain, _edited_asset
from server.tests.test_gemini import PASS, FakeGemini, _lasso

SHIELD_RULE = "For a held shield, draw the hand gripping its inner side so fingers stay hidden behind it."


def _ratings(env):
    R = Ratings(env.eng, start_thread=False, client=FakeChain())
    env.eng.ratings = R
    return R


def test_old_global_lines_move_to_general(env):
    R = _ratings(env)
    R.set_kv("tuning_lines", {"masking": [], "refine": ["old global rule"]})
    env.eng.db.x("INSERT INTO agent_tuning(version,target,action,idx,before,after,rating_id,t) VALUES(1,'refine','add',0,'[]','[]','rt-x',0)")
    R2 = _ratings(env)                                                            # 기동 때 옮김
    assert R2.tuning_lines() == {"masking": {}, "refine": {"general": ["old global rule"]}}
    assert R2.kv("tuning_lines", None)["refine"] == {"general": ["old global rule"]}
    assert R2.tuning_rows()[0]["key"] == "general"
    assert env.eng.llm.notes["refine"] == {"general": ["old global rule"]}


def test_notes_for_exact_key_plus_general():
    class Ad:
        notes = {"refine": {"general": ["g1"], "shield": ["s1"], "sword": ["w1"]}}
    assert la.notes_for(Ad, "refine", ["Shield"]) == [("general", "g1"), ("shield", "s1")]
    assert la.notes_for(Ad, "refine", ["shields"]) == [("general", "g1")]          # 정확히 같은 키만(복수형 다름)
    assert la.notes_for(Ad, "refine", None) == [("general", "g1")]
    assert la.norm_key(" Round-Shield ") == "roundshield" and la.norm_key("") == "general"


def test_masking_notes_reach_mask_refine_prompt():
    """masking 대상 문장이 실제 mask_refine(agent_refine) 시스템 프롬프트에 붙는다(키 선택 포함)."""
    seen = {}

    class Stub(la.AgentMixin):
        notes = {"masking": {"general": ["G-RULE"], "shield": ["S-RULE"]}}
        effort, mask_effort = "low", "medium"

        def _call(self, kind, system, user, schema, check, job_id, log):
            seen.setdefault(kind, []).append(system)
            return {"found": True}
    st = Stub()
    st.agent_refine_plan(b"", b"", "왼손에 방패", "", None)
    st.agent_refine_plan(b"", b"", "왼손에 방패", "", None, note_keys=["shield"])
    a, b = seen["agent_refine"]
    assert "Notes from past user feedback:\n- G-RULE" in a and "S-RULE" not in a
    assert "- G-RULE" in b and "- S-RULE" in b
    assert st.last_notes["masking"]["lines"] == [("general", "G-RULE"), ("shield", "S-RULE")]


def _shield_review(env, R):
    aid = _edited_asset(env)
    fake = env.eng.agent_llm
    fake.tune_force, fake.tune_target, fake.tune_key, fake.tune_line = True, "refine", "shield", SHIELD_RULE
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers={"X-Device-Key": "rv"},
                 json={"stars": 4, "review": "방패 위쪽으로 손이 나와있는 모습이 아쉬움"}).json()
    rec = R.tune(o["rating_id"])
    assert rec["applied"] and rec["key"] == "shield"
    assert R.tuning_lines()["refine"] == {"shield": [SHIELD_RULE]}
    return aid


def test_shield_review_applies_to_shield_edit_not_sword(env):
    R = _ratings(env)
    _shield_review(env, R)
    R.set_kv("tuning_lines", dict(R.tuning_lines(), masking={"shield": ["MASK-SHIELD"]}))
    R._apply_notes()
    aid = make_asset(env, qr="tk-2")
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    fake = env.eng.agent_llm
    fake.judge_script = [PASS]
    fake.object_keys, fake.new_object = ["sword"], "a short sword"
    j1 = _lasso(env, aid, "오른손에 칼을 쥐여줘")
    t1 = env.eng.get(j1["job_id"])["tuning_applied"]
    assert t1["refine"]["keys"] == ["sword"] and t1["refine"]["lines"] == []           # 방패 줄은 칼 편집에 안 붙음
    assert t1["masking"]["rerun"] is False and t1["masking"]["lines"] == []
    fake.object_keys, fake.new_object = ["shield"], "a round shield"
    j2 = _lasso(env, aid, "왼손에 방패를 들게 해줘", base=env.eng.db.asset(aid)["version"])
    t2 = env.eng.get(j2["job_id"])["tuning_applied"]
    assert t2["refine"]["keys"] == ["shield"] and t2["refine"]["lines"] == [["shield", SHIELD_RULE]]   # 방패 편집에는 붙음
    assert t2["masking"]["rerun"] is True and t2["masking"]["lines"] == [["shield", "MASK-SHIELD"]]
    assert env.eng.get(j2["job_id"])["agent_plan"]["object_keys"] == ["shield"]


def test_admin_shows_key_column(env):
    R = _ratings(env)
    _shield_review(env, R)
    d = env.get("/admin/api").json()
    assert d["tuning"][0]["key"] == "shield" and d["tuning_lines"]["refine"] == {"shield": [SHIELD_RULE]}
    assert "<th>키</th>" in env.get("/admin").text
