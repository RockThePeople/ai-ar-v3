"""S13: 라쏘 이미지로 편집 의도 파악 + 거절 처리 보강."""

from __future__ import annotations

import numpy as np

from server.app.cropinpaint import decode_png, encode_png
from server.app.llm_adapter import (ClaudeAdapter, LLMUnavailable, _scrub, placement_ok, strip_realworld,
                                    subject_head)
from server.app.regionimg import region_images
from server.tests.test_app_server import A, edit, env, make_asset, to_2d  # noqa: F401 — fixture


def _rgba_and_mask():
    r = np.zeros((200, 200, 4), np.uint8)
    r[40:160, 60:140] = (200, 200, 200, 255)                  # 물체 120×80 = 9,600
    m = np.zeros((200, 200, 1), np.uint8)
    m[40:80, 60:140] = 255                                    # 머리 40×80 = 3,200
    return encode_png(r), encode_png(m)


def test_region_images_ratio_and_sizes():
    rp, mp = _rgba_and_mask()
    o = region_images(rp, mp)
    assert o["mask_px"] == 3200 and o["object_px"] == 9600 and o["mask_ratio"] == round(3200 / 9600, 4)
    ov, z = decode_png(o["overlay_png"]), decode_png(o["zoom_png"])
    assert max(ov.shape[:2]) <= 768 and max(z.shape[:2]) <= 512
    assert (ov[..., 0].astype(int) - ov[..., 1].astype(int) > 60).any()   # 빨간 겹침이 있다


def test_text_helpers():
    assert subject_head("A big snowman, a carrot nose") == "big snowman"
    assert subject_head("A large orange Halloween pumpkin, three segments") == "large orange halloween"
    assert subject_head("Spider-Man wearing Nike shoes, red gloves") == "spider-man"
    assert placement_ok("A big snowman wearing a carved orange pumpkin as its head, a scarf",
                        "a carved orange jack-o-lantern pumpkin", "replace")
    assert not placement_ok("Spider-Man wearing Nike Air Jordan shoes, red gloves with white trim",
                            "red gloves with white trim", "add")
    assert placement_ok("anything", "x", "remove")
    assert strip_realworld("A firefighter standing upright at real-world human proportions wearing a hat, boots") == \
        "A firefighter standing upright wearing a hat, boots"
    assert strip_realworld("A chair, real-world proportions, oak") == "A chair, oak"


def test_scrub_replaces_image_data():
    import base64
    req = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(b"\x89PNGxx").decode()}},
        {"type": "text", "text": "hi"}]}]}
    out = _scrub(req)
    assert out["messages"][0]["content"][0]["source"]["data"].startswith("<png 6 bytes sha256=")
    assert req["messages"][0]["content"][0]["source"]["data"] != out["messages"][0]["content"][0]["source"]["data"]


def test_check_refine_region():
    ok = {"target_element": "the head", "replacement": "a pumpkin head", "op": "replace",
          "new_scene_prompt": "A snowman wearing a pumpkin head", "selected_part": "the snowman's head",
          "region_fit": "ok", "region_reason": "fits"}
    assert ClaudeAdapter.check_refine_region(ok) is None
    assert ClaudeAdapter.check_refine_region({**ok, "region_fit": "big"})
    assert ClaudeAdapter.check_refine_region({k: v for k, v in ok.items() if k != "selected_part"})


def test_image_failure_falls_back_to_text(monkeypatch):
    ad = ClaudeAdapter.__new__(ClaudeAdapter)
    calls = []

    def fake_call(kind, system, user, schema, check, job_id, log):
        calls.append((isinstance(user, list), "selected_part" in schema["properties"]))
        if isinstance(user, list):
            raise LLMUnavailable("API 400: image not supported")
        return {"target_element": "t", "replacement": "r", "op": "replace", "new_scene_prompt": "A x"}
    monkeypatch.setattr(ad, "_call", fake_call, raising=False)
    out = ad.refine("A x", "make it red", [], region={"images": [b"png"], "mask_ratio": 0.3})
    assert calls == [(True, True), (False, False)] and out["new_scene_prompt"] == "A x"


# ── 엔진 ───────────────────────────────────────────────────────────
R = {"target_element": "a big snowman", "replacement": "a carved orange jack-o-lantern pumpkin head", "op": "replace"}


def _stub(env, outs, ratio=0.41):
    seen = []

    def refine(scene, instruction, hist, job_id="", **kw):
        seen.append(kw)
        o = outs.pop(0)
        return {**o, "selected_part": "the snowman's head", "region_fit": "ok", "region_reason": "fits"}
    env.eng.llm.refine = refine
    env.eng._region = lambda j: {"images": [b"ov", b"zm"], "files": ["overlay", "zoom"], "mask_ratio": ratio}
    return seen


def _snow(env):
    aid = make_asset(env)
    env.eng.db.x("UPDATE assets SET scene_prompt=? WHERE asset_id=?", ("A big snowman, a carrot nose, a red scarf", aid))
    return aid


def test_subject_change_below_06_is_retried(env):
    aid = _snow(env)
    seen = _stub(env, [dict(R, new_scene_prompt="A large orange Halloween pumpkin, three stacked segments"),
                       dict(R, new_scene_prompt="A big snowman wearing a carved orange jack-o-lantern pumpkin head, a red scarf")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    a = env.eng.get(jid)["attempts"][0]
    assert seen[0]["region"]["mask_ratio"] == 0.41 and seen[0]["region"]["images"] == [b"ov", b"zm"]
    assert "renamed the whole subject" in seen[1]["fix"][0]
    st = a["style"]
    assert st["region_found"] == ["subject_changed"] and st["region_left"] == [] and st["selected_part"] == "the snowman's head"
    assert a["prompt"].startswith("A big snowman wearing a carved orange")


def test_subject_change_allowed_at_06(env):
    aid = _snow(env)
    seen = _stub(env, [dict(R, new_scene_prompt="A large carved orange jack-o-lantern pumpkin head, three segments")], ratio=0.7)
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    assert len(seen) == 1 and env.eng.get(jid)["attempts"][0]["style"]["region_found"] == []


def test_placement_retry(env):
    aid = _snow(env)
    r = {"target_element": "the hands", "replacement": "red gloves with white trim", "op": "add"}
    seen = _stub(env, [dict(r, new_scene_prompt="A big snowman wearing a top hat, a carrot nose, red gloves with white trim"),
                       dict(r, new_scene_prompt="A big snowman wearing red gloves with white trim, a top hat, a carrot nose")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    st = env.eng.get(jid)["attempts"][0]["style"]
    assert "first relation phrase" in seen[1]["fix"][0] and st["region_found"] == ["placement"] and st["region_left"] == []


def test_realworld_phrase_stripped(env):
    aid = _snow(env)
    _stub(env, [dict(R, new_scene_prompt="A big snowman at real-world proportions wearing a carved orange jack-o-lantern "
                                         "pumpkin head, a red scarf")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    assert "real-world" not in env.eng.get(jid)["attempts"][0]["prompt"]


def test_prompt_retry_reuses_render_and_passes_raw_reason(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    n = sum(1 for c in env.be.calls if c == ("render",))
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "프롬프트를 잘못 이해함"}, headers=A).json()
    assert j["review"]["category"] == "prompt"
    env.eng.drain()
    jj = env.eng.get(jid)
    assert sum(1 for c in env.be.calls if c == ("render",)) == n                 # 조건 렌더 재사용
    assert jj["attempts"][-1]["render_reused"] is True
    assert jj["reject_history"][-1].startswith('user said: "프롬프트를 잘못 이해함"')


def test_classify_gets_images(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    got = {}
    orig = env.eng.llm.classify

    def classify(reason, refine, history, job_id="", **kw):
        got.update(kw)
        return orig(reason, refine, history, job_id=job_id)
    env.eng.llm.classify = classify
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A)
    assert len(got.get("images") or []) == 2                                     # 겹친 렌더 + 2d_edit


def test_toggles_off(env):
    env.eng.s.regionrefine = False
    env.eng.s.rejectraw = False
    aid = make_asset(env)
    jid = to_2d(env, aid)
    a = env.eng.get(jid)["attempts"][0]
    assert not (a["style"] or {}).get("region")
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "프롬프트를 잘못 이해함"}, headers=A)
    env.eng.drain()
    assert not env.eng.get(jid)["reject_history"][-1].startswith("user said")


def test_clients_region_view(env):
    aid = make_asset(env)
    to_2d(env, aid)
    page = env.get(f"/clients/{aid}").text
    assert "regionView" in page and "/region/" in page


def test_no_placement_retry_when_subject_renamed_at_06(env):
    aid = _snow(env)
    seen = _stub(env, [dict(R, replacement="a red and gold Ironman suit",
                            new_scene_prompt="An Ironman superhero in a red and gold metallic armored suit, a helmet")], ratio=1.0)
    edit(env, aid)
    env.eng.drain()
    assert len(seen) == 1


# ── S13b: 인페인팅 전용 문장, 좌우 단어, 영역 분류 규칙 ─────────────────────────
def test_inpaint_prompt_used_for_inpaint_and_scene_kept(env):
    aid = _snow(env)
    ip = "A large carved orange jack-o'-lantern pumpkin head, sitting on a snowman of packed white snow"
    seen = _stub(env, [dict(R, new_scene_prompt="A big snowman wearing a carved orange jack-o-lantern pumpkin head, a red scarf",
                            inpaint_prompt=ip, selected_part="the left snowman head")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    j = env.eng.get(jid)
    a = j["attempts"][0]
    assert seen[0]["region"]["inpaint_prompt"] is True
    assert a["prompt"] == ip and a["inpaint_src"] == "inpaint_prompt"                 # 인페인팅 = inpaint_prompt
    assert a["new_scene_prompt"].startswith("A big snowman wearing")                  # 장면 문장은 그대로
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    assert env.eng.db.asset(aid)["scene_prompt"].startswith("A big snowman wearing")  # 자산 장면 = new_scene_prompt


def test_lateral_words_stripped_unless_instructed(env):
    from server.app.llm_adapter import strip_lateral
    assert strip_lateral("the right twig arm") == "the twig arm"
    assert strip_lateral("the jeep's lower-right side spare tire area") == "the jeep's side spare tire area"
    assert strip_lateral("the right arm", "change the right arm into a broom") == "the right arm"
    assert strip_lateral("a brightly lit window") == "a brightly lit window"
    aid = _snow(env)
    _stub(env, [dict(R, target_element="the right twig arm", new_scene_prompt="A big snowman wearing a carved orange "
                     "jack-o-lantern pumpkin head", inpaint_prompt="A pumpkin head on the right side, upper area")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    j = env.eng.get(jid)
    assert j["refine"]["target_element"] == "the twig arm" and "right" not in j["attempts"][0]["prompt"].lower()


def test_inpaintprompt_off_uses_scene(env):
    env.eng.s.inpaintprompt = False
    aid = _snow(env)
    seen = _stub(env, [dict(R, new_scene_prompt="A big snowman wearing a carved orange jack-o-lantern pumpkin head")])
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    a = env.eng.get(jid)["attempts"][0]
    assert seen[0]["region"]["inpaint_prompt"] is False and a["inpaint_src"] == "scene"


def test_classify_region_rule_flag(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    got = {}
    orig = env.eng.llm.classify

    def classify(reason, refine, history, job_id="", **kw):
        got.update(kw)
        return orig(reason, refine, history, job_id=job_id)
    env.eng.llm.classify = classify
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "i missed to select the color"}, headers=A)
    assert got.get("region_rule") is True


def test_check_refine_region_ip():
    from server.app.llm_adapter import ClaudeAdapter
    ok = {"target_element": "the head", "replacement": "a pumpkin head", "op": "replace",
          "new_scene_prompt": "A snowman wearing a pumpkin head", "selected_part": "the snowman's head",
          "region_fit": "ok", "region_reason": "fits", "inpaint_prompt": "A carved pumpkin head on a snowman"}
    assert ClaudeAdapter.check_refine_region_ip(ok) is None
    assert ClaudeAdapter.check_refine_region_ip({**ok, "inpaint_prompt": "a pumpkin without a stem"})
    assert ClaudeAdapter.check_refine_region_ip({**ok, "inpaint_prompt": "word " * 61})


def test_app_api_refine_shape_unchanged(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    r = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["refine"]
    assert set(r) == {"target_element", "replacement", "op", "new_scene_prompt"}


def test_classify_guidance_lateral_stripped(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.eng.llm.classify = lambda reason, refine, history, job_id="", **kw: {
        "category": "quality", "guidance": "the glove on the left hand looks like a white mesh"}
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "looks blurry"}, headers=A).json()
    assert "left" not in j["review"]["guidance"] and "glove on the hand" in j["review"]["guidance"]
    env.eng.drain()
    j = env.eng.get(jid)
    assert "left" not in j["attempts"][0]["guidance"]


def test_classify_guidance_lateral_kept_when_user_said(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.eng.llm.classify = lambda reason, refine, history, job_id="", **kw: {
        "category": "quality", "guidance": "the left glove is blurry"}
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "left glove blurry"}, headers=A).json()
    assert j["review"]["guidance"] == "the left glove is blurry"
