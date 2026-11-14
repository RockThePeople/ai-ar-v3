"""S22: 이름만으로 정해지는 대상(헬로키티 등)은 상세화 문장 대신 영어 이름(+ 한 얼굴 문구)으로 T2I."""

from __future__ import annotations

from server.tests.test_app_server import A, env  # noqa: F401 — fixture


def _gen_spy(env):
    seen = []
    orig = env.be.generate
    env.be.generate = lambda aid, prompt, seed, *a, **k: (seen.append((prompt, k.get("template"))),
                                                         orig(aid, prompt, seed, *a, **k))[1]
    return seen


def _gen(env, qr, prompt):
    r = env.post(f"/app/v1/qr/{qr}/asset", json={"prompt": prompt}, headers=A)
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.eng.get(r.json()["job_id"])


def test_named_subject_uses_name_and_method_suffix(env):
    from server.app.engine import NAMED_METHOD, NAMED_SUFFIX
    seen = _gen_spy(env)
    j = _gen(env, "st-11", "헬로키티")
    assert j["named"]["named_subject"] is True and j["named"]["subject_name"] == "Hello Kitty"
    assert j["named"]["method"] == NAMED_METHOD
    assert seen[-1][0] == "Hello Kitty" + NAMED_SUFFIX[NAMED_METHOD]
    assert j["prompt_en"] == seen[-1][0]                      # 첫 장면 문장도 같은 문장
    assert env.eng.db.asset(j["asset_id"])["scene_prompt"] == seen[-1][0]
    row = [x for x in env.get(f"/clients/api/assets/{j['asset_id']}").json()["jobs"] if x["kind"] == "generate"][0]
    assert row["named"]["method"] == NAMED_METHOD


def test_named_keeps_style_tail(env):
    seen = _gen_spy(env)
    j = _gen(env, "st-12", "toy like spongebob")
    assert j["named"]["subject_name"] == "SpongeBob SquarePants"
    assert "toy-like style" in seen[-1][1]                    # 양식 꼬리는 그대로


def test_generic_subject_unchanged(env):
    seen = _gen_spy(env)
    j = _gen(env, "st-13", "A Chibi Adventurer")
    assert j["named"] == {"named_subject": False, "subject_name": None, "method": None}
    assert seen[-1][0] == "A Chibi Adventurer"               # 가짜 상세화 = 원문 그대로(지금 흐름)


def test_toggle_off_no_named_field_and_old_call(env):
    env.eng.s.named_nodetail = False
    kws = []
    orig = env.eng.llm.detail_style
    env.eng.llm.detail_style = lambda p, job_id="", **kw: (kws.append(kw), orig(p, job_id=job_id, **kw))[1]
    seen = _gen_spy(env)
    j = _gen(env, "st-14", "헬로키티")
    assert "named" not in j and "named" not in kws[-1]
    assert seen[-1][0] == "헬로키티"                           # 가짜 상세화 그대로


def test_schema_and_check_named():
    from server.app.llm_adapter import DETAIL_STYLE_SCHEMA, DETAIL_STYLE_SCHEMA_NAMED, ClaudeAdapter, NAMED_RULE
    assert set(DETAIL_STYLE_SCHEMA["properties"]) == {"subject_detailed", "style", "style_words"}   # 끔 = 예전 그대로
    assert {"named_subject", "subject_name"} <= set(DETAIL_STYLE_SCHEMA_NAMED["required"])
    assert "not" not in NAMED_RULE.split() and "named_subject" in NAMED_RULE
    base = {"subject_detailed": "Hello Kitty, a white cat character", "style": None, "style_words": None}
    ok = dict(base, named_subject=True, subject_name="Hello Kitty")
    assert ClaudeAdapter.check_detail_style(ok, "헬로키티", True) is None
    assert ClaudeAdapter.check_detail_style(dict(base, named_subject=False, subject_name=None), "a cat", True) is None
    assert "subject_name" in ClaudeAdapter.check_detail_style(dict(ok, subject_name=None), "헬로키티", True)
    assert "subject_name" in ClaudeAdapter.check_detail_style(dict(ok, subject_name="헬로키티"), "헬로키티", True)
    assert "필드" in ClaudeAdapter.check_detail_style(base, "x", True)
    assert "필드" in ClaudeAdapter.check_detail_style(ok, "x", False)
