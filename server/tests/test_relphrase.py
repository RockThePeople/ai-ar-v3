"""S12: 보정 문장에서 바뀐 부위를 대상 바로 뒤 관계어(wearing·holding·with)로 잇는다. 명령문 금지."""

from __future__ import annotations

from server.app.backends import fake_refine
from server.app.llm_adapter import REFINE_SYSTEM, REFINE_SYSTEM_REL, imperative_words
from server.tests.test_app_server import A, edit, env, make_asset  # noqa: F401 — fixture


def test_imperative_words():
    assert imperative_words("Change the helmet into a red cowboy hat") == ["change"]
    assert imperative_words("A baseball player, replace the bat with a flag") == ["replace"]
    assert imperative_words("A baseball player wearing a red cowboy hat, grey pants, white cleats") == []
    assert imperative_words("A jeep, olive green paint, a make-up mirror") == []          # 쉼표 뒤 명사·복합어
    assert imperative_words("A van with a roof rack, add-on side steps") == []
    assert imperative_words("a chair, Make it taller") == ["make"]


def test_rel_template_rules_and_examples():
    t = REFINE_SYSTEM_REL
    for w in ('"wearing"', '"holding"', '"with"', "Never insert the changed element in the middle",
              "Delete the old description", "never a command", "rename the subject itself"):
        assert w in t, w
    assert "wearing a black cotton baseball cap" in t and "with an aluminum ladder rack" in t
    assert "an open empty right hand" in t                                               # 제거 = 관계어 없이 긍정 묘사
    low = t.lower()
    for bad in ("figure", "toy", "jeep", "baseball player", "spider-man", "ironman", "iron man", "cowboy"):
        assert bad not in low, bad                                                      # 확인 사례·금지어를 예시에 쓰지 않는다
    assert REFINE_SYSTEM != REFINE_SYSTEM_REL


def test_fake_refine_relphrase():
    o = fake_refine("the man riding a motorcycle", "change the man into Ironman", relphrase=True)
    assert o["new_scene_prompt"] == "Ironman riding a motorcycle" and not imperative_words(o["new_scene_prompt"])
    o = fake_refine("A man riding a motorcycle", "change the man into Ironman", relphrase=True)
    assert o["new_scene_prompt"] == "A man riding a motorcycle, with Ironman"
    o = fake_refine("A jeep, olive paint", "add a roof rack", relphrase=True)
    assert o["new_scene_prompt"] == "A jeep, olive paint, with add a roof rack"                # 가짜 모드: 쉼표 뒤 명령형 아님
    assert not imperative_words(o["new_scene_prompt"])


def _stub(env, outs):
    seen = []

    def refine(scene, instruction, hist, job_id="", **kw):
        seen.append(kw)
        return outs.pop(0)
    env.eng.llm.refine = refine
    return seen


R = {"target_element": "the man", "replacement": "Ironman", "op": "replace"}


def test_imperative_retry_fixes(env):
    aid = make_asset(env)
    seen = _stub(env, [dict(R, new_scene_prompt="Change the man into Ironman riding a motorcycle"),
                       dict(R, new_scene_prompt="An Ironman riding a motorcycle")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    a = env.eng.get(jid)["attempts"][0]
    assert seen[0].get("relphrase") is True and "imperative" not in seen[0]
    assert seen[1]["imperative"] == ["change"] and seen[1]["relphrase"] is True
    assert a["style"]["imperative_found"] == ["change"] and a["style"]["imperative_left"] == [] and a["style"]["retried"]
    assert a["prompt"] == "An Ironman riding a motorcycle"


def test_imperative_left_is_recorded_not_failed(env):
    aid = make_asset(env)
    _stub(env, [dict(R, new_scene_prompt="Make the man Ironman"), dict(R, new_scene_prompt="Make the man an Ironman")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    j = env.eng.get(jid)
    assert j["state"] == "awaiting_2d_review" and j["attempts"][0]["style"]["imperative_left"] == ["make"]


def test_banned_and_imperative_share_one_retry(env):
    aid = make_asset(env)
    seen = _stub(env, [dict(R, new_scene_prompt="Replace the man, an Ironman figure"),
                       dict(R, new_scene_prompt="An Ironman riding a motorcycle")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    st = env.eng.get(jid)["attempts"][0]["style"]
    assert len(seen) == 2 and seen[1]["avoid"] == ["figure"] and seen[1]["imperative"] == ["replace"]
    assert st["banned_found"] == ["figure"] and st["imperative_found"] == ["replace"]


def test_relphrase_off_uses_old_template(env):
    env.eng.s.relphrase = False
    aid = make_asset(env)
    seen = _stub(env, [dict(R, new_scene_prompt="Change the man into Ironman")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    assert "relphrase" not in seen[0] and len(seen) == 1
    assert "imperative_found" not in env.eng.get(jid)["attempts"][0]["style"]


def test_clients_shows_rel_and_template_title(env):
    aid = make_asset(env)
    page = env.get(f"/clients/{aid}").text
    assert "relView" in page and "보정 템플릿 — system 프롬프트 전문" in page
