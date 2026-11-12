"""S11: 기본은 실물형, 양식은 사용자가 말할 때만."""

from __future__ import annotations

from server import t2i
from server.app.llm_adapter import (ClaudeAdapter, FakeAdapter, LLMInvalid, _style_guard, banned_words,
                                    DETAIL_STYLE_SYSTEM, strip_banned, style_note)
from server.tests.test_app_server import A, edit, env, job, make_asset  # noqa: F401 — fixture

REAL = "{subject}, three-quarter front view, centered, full object visible, pure white background, no shadow"


def test_banned_words_detect_and_allow_from_source():
    assert banned_words("A standing baseball player figure, white uniform") == ["figure"]
    assert banned_words("a Plastic-Toy fire truck, cartoonish wheels") == ["Plastic-Toy", "cartoonish"]
    assert banned_words("a low poly chair, toys", "low-poly chair") == ["toys"]     # 원문에 있으면 허용
    assert banned_words("a toy fire truck", "장난감 같은 소방차") == []              # 한국어 원문 허용
    assert banned_words("a real full-size fire truck, red paint") == []
    assert banned_words("a figured maple top") == []                                # 단어 경계


def test_strip_banned_cleans_commas():
    assert strip_banned("A standing baseball player figure, white uniform", ["figure"]) == \
        "A standing baseball player, white uniform"
    assert strip_banned("a miniature red toy truck, wooden", ["miniature", "toy"]) == "a red truck, wooden"
    assert ", ," not in strip_banned("a chair, toy, wooden legs", ["toy"])


def test_check_detail_style():
    ok = {"subject_detailed": "A red fire truck, a ladder, chrome bumper, red paint", "style": None, "style_words": None}
    assert ClaudeAdapter.check_detail_style(ok, "a red fire truck") is None
    st = {**ok, "style": "toy-like", "style_words": "toy like"}
    assert ClaudeAdapter.check_detail_style(st, "toy like fire truck") is None
    assert "원문" in ClaudeAdapter.check_detail_style(st, "a red fire truck")        # 원문에 없는 양식 = 지어냄
    assert ClaudeAdapter.check_detail_style({**ok, "style": "cartoon"}, "x")          # 짝이 안 맞음
    assert ClaudeAdapter.check_detail_style({**ok, "style": "cartoon style with white background",
                                             "style_words": "x"}, "x")
    assert ClaudeAdapter.check_detail_style({**st, "style_words": "만화풍", "style": "cartoon"}, "만화풍 야구 선수") is None


def test_system_prompt_is_realistic():
    low = DETAIL_STYLE_SYSTEM.lower()
    assert "game asset." not in low and "mailbox" in low and "real-world proportions" in low
    assert "jeep" not in low and "fire truck" not in low and "chair" not in low and "baseball" not in low
    assert "default realistic" in style_note(None) and "toy-like" in style_note("toy-like")


def _out(subj, style=None, words=None):
    return {"subject_detailed": subj, "style": style, "style_words": words}


def test_guard_retry_fixes():
    calls = []
    r = _style_guard(_out("A baseball player figure, white uniform"), "a standing baseball player",
                     lambda f: calls.append(f) or _out("A baseball player, white uniform"))
    assert calls == [["figure"]] and r["subject_detailed"] == "A baseball player, white uniform"
    assert r["check"] == {"banned_found": ["figure"], "retried": True, "removed": []} and r["style"] is None


def test_guard_retry_still_bad_then_strip():
    r = _style_guard(_out("A baseball player figure, white uniform"), "a standing baseball player",
                     lambda f: _out("A baseball player figurine, white uniform"))
    assert r["subject_detailed"] == "A baseball player, white uniform"
    assert r["check"]["removed"] == ["figurine"] and r["check"]["retried"]


def test_guard_retry_error_strips_original():
    def boom(f):
        raise LLMInvalid("x")
    r = _style_guard(_out("A toy fire truck, red"), "a red fire truck", boom)
    assert r["subject_detailed"] == "A fire truck, red" and r["check"]["removed"] == ["toy"] and r["check"]["retry_error"]


def test_guard_user_style_skips_check():
    r = _style_guard(_out("A fire truck, toy wheels", "toy-like", "toy like"), "toy like fire truck",
                     lambda f: (_ for _ in ()).throw(AssertionError("재요청하면 안 된다")))
    assert r["style"] == "toy-like" and r["check"]["banned_found"] == []


def test_t2i_templates():
    assert t2i.app_template(None) == REAL and "game asset" not in REAL
    assert t2i.app_template("toy-like") == ("{subject}, toy-like style, three-quarter front view, centered, "
                                            "full object visible, pure white background, no shadow")
    assert "game asset" in t2i.DEFAULT_TEMPLATE                                    # 다른 경로(S6)는 그대로


# ── 엔진: 생성에 양식 저장, 편집 문장, 끔 ────────────────────────────────
def _gen(env, prompt, qr="st-01"):
    r = env.post(f"/app/v1/qr/{qr}/asset", json={"prompt": prompt}, headers=A)
    env.eng.drain()
    return env.eng.get(r.json()["job_id"])


def test_generate_real_default(env):
    j = _gen(env, "a red fire truck")
    assert j["style"]["mode"] == "real" and j["t2i_template"] == REAL
    assert ("generate_args", "a red fire truck", REAL) in env.be.calls
    assert env.eng.db.asset(j["asset_id"])["style"] is None


def test_generate_user_style_and_edit_tail(env):
    j = _gen(env, "toy like fire truck")
    assert j["style"] == {"mode": "user", "style": "toy-like", "words": "toy like", "banned_found": [],
                          "retried": False, "removed": []}
    assert env.eng.db.asset(j["asset_id"])["style"] == "toy-like"
    assert ("generate_args", "fire truck", t2i.app_template("toy-like")) in env.be.calls
    jid = edit(env, j["asset_id"]).json()["job_id"]
    env.eng.drain()
    a = env.eng.get(jid)["attempts"][0]
    assert a["prompt"].endswith(", toy-like style") and a["style"]["mode"] == "user"
    page = env.get(f"/clients/{j['asset_id']}").text
    assert "styleView" in page


def test_edit_real_asset_banned_retry_then_strip(env):
    aid = make_asset(env)
    seq = [{"target_element": "the man", "replacement": "Ironman figure", "op": "replace",
            "new_scene_prompt": "An Ironman figure riding a motorcycle"},
           {"target_element": "the man", "replacement": "Ironman", "op": "replace",
            "new_scene_prompt": "An Ironman toy riding a motorcycle"}]
    seen = []

    def refine(scene, instruction, hist, job_id="", changed_from=None, style=None, avoid=None, **kw):
        seen.append((style, avoid))
        return seq.pop(0)
    env.eng.llm.refine = refine
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    j = env.eng.get(jid)
    a = j["attempts"][0]
    assert seen == [({"style": None}, None), ({"style": None}, ["figure"])]
    assert {k: a["style"][k] for k in ("mode", "style", "banned_found", "retried", "removed")} == \
        {"mode": "real", "style": None, "banned_found": ["figure"], "retried": True, "removed": ["toy"]}
    assert a["prompt"] == "An Ironman riding a motorcycle" and "toy" not in a["new_scene_prompt"]


def test_pre_s11_asset_is_real(env):
    aid = make_asset(env)
    env.eng.db.x("UPDATE assets SET style=NULL WHERE asset_id=?", (aid,))
    assert env.eng._asset_style(aid) is None


def test_realstyle_off_is_s6(env):
    env.eng.s.realstyle = False
    j = _gen(env, "toy like fire truck")
    assert "style" not in j and "t2i_template" not in j and j["translate"]["mode"] == "detail"
    assert ("generate_args", "toy like fire truck", None) in env.be.calls


def test_fake_adapter_detail_style():
    assert FakeAdapter().detail_style("장난감 같은 소방차")["style"] == "toy-like"
    assert FakeAdapter().detail_style("빨간 소방차")["style"] is None
