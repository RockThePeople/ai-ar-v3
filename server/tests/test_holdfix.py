"""S24 손에 쥐여 주기: 손잡이 문장(Gemini), 그림 기준 좌우(고른 시점), 보정 모양 풀어 쓰기, 채점 손잡이 조건."""
from server.app import automask as am
from server.app.gemini import hold_clause, side_clause
from server.app.llm_adapter import AGENT_JUDGE_SYSTEM
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture
from server.tests.test_gemini import PASS, FakeGemini, _lasso


def test_hold_clause_kinds():
    assert "grips the sword by its handle" in hold_clause("a short sword", "오른손에 칼을 쥐여줘", "right hand")
    assert "the blade points away" in hold_clause("sword", "오른손에 칼", "right hand")
    assert "the head points away" in hold_clause("a wooden torch", "왼손에 횃불", "left hand")
    s = hold_clause("a round shield", "왼손에 방패를 들게 해줘", "left hand", "left")
    assert s == "The shield is held on the character's left forearm, its face pointing outward (away from the body). "
    assert "strap" not in s and "inner grip" not in s
    assert hold_clause("a red hat", "머리에 모자", "head") == ""                    # 손이 아니면 없음
    assert hold_clause("a cat", "오른손에 고양이", "right hand") == ""               # 도구·방패가 아니면 없음


def test_side_clause_both_frames():
    s = side_clause({"side": "right", "image_side": "left"})
    assert "ONLY in the hand on the LEFT side of the image" in s and "character's right hand" in s and "right side of the image stays empty" in s
    assert side_clause({"side": "right", "image_side": None}) == "" and side_clause(None) == ""


def _body():
    body = [[x, y, z] for x in range(24, 40) for y in range(28, 36) for z in range(10, 50)]
    hand_px = [[x, y, z] for x in range(40, 44) for y in range(28, 36) for z in range(28, 32)]   # +x 쪽 손
    return body + hand_px, hand_px


def test_hand_image_side_front_back_and_lasso():
    occ, hand = _body()
    cams = am.view_cameras(occ)
    # 정면 az000(+y 카메라): +x 는 그림 왼쪽 → 캐릭터 오른손(+x)이 그림 왼쪽
    r = am.hand_image_side("오른손에 칼을 쥐여줘", hand, occ, cams["az000"], 0, "az000")
    assert r["expected"] == "left" and r["image_side"] == "left" and r["source"] == "lasso" and r["agree"] is True
    # 뒤 az180 에서 보면 그대로 오른쪽
    r2 = am.hand_image_side("오른손에 칼을 쥐여줘", hand, occ, cams["az180"], 180, "az000")
    assert r2["expected"] == "right" and r2["image_side"] == "right"
    # 옆 시점은 계산하지 않고 라쏘만
    r3 = am.hand_image_side("오른손에 칼을 쥐여줘", hand, occ, cams["az090"], 90, "az000")
    assert r3["expected"] is None and r3["view_offset"] == 90 and r3["image_side"] is None   # 옆 시점: 좌우 안 냄
    # 지시와 라쏘가 다르면 라쏘(사용자가 그린 곳)를 따르고 agree False
    r4 = am.hand_image_side("왼손에 방패", hand, occ, cams["az000"], 0, "az000")
    assert r4["expected"] == "right" and r4["image_side"] == "left" and r4["agree"] is False
    assert am.hand_image_side("머리에 모자", hand, occ, cams["az000"], 0, "az000")["image_side"] is None


def test_judge_system_has_grip_rule():
    assert "gripped by its handle/hilt" in AGENT_JUDGE_SYSTEM and "blade held" in AGENT_JUDGE_SYSTEM
    assert "Do NOT judge straps, the inner grip" in AGENT_JUDGE_SYSTEM and "roughly outward" in AGENT_JUDGE_SYSTEM


def test_flow_prompt_has_hold_side_and_shape(env):
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    fake = env.eng.agent_llm
    fake.new_object = "a short sword"
    fake.judge_script = [PASS]
    env.eng.llm.name = "rule"                       # 가짜 보정의 '지시문 그대로' 분기를 피해 보정 입력을 본다
    seen = {}
    real_judge = fake.agent_judge

    def judge(*a, **k):
        seen["side_hint"] = k.get("side_hint")
        return real_judge(*a, **k)
    fake.agent_judge = judge
    j = _lasso(env, aid, "오른손에 칼을 쥐여줘")
    assert j["state"] == "committed"
    p = env.eng.gemini.calls[0]
    assert "grips the sword by its handle" in p
    jj = env.eng.get(j["job_id"])
    hs = jj["agent_hand_side"]
    assert hs["side"] == "right"
    if hs["image_side"]:
        assert f"hand on the {hs['image_side'].upper()} side of the image" in p and hs["image_side"].upper() in seen["side_hint"]
    assert "Describe the new object concretely by its shape" in jj["agent_refine_input"]
