"""S15: 작업 객체 prompts (app-api 1.0.5 4.4)."""

from __future__ import annotations

from server.tests.test_app_server import A, CAM, CELLS, edit, env, job, make_asset, to_2d  # noqa: F401 — fixture

KEYS = {"input", "detailed", "style", "inpaint", "mask_ratio", "region_fit", "refine_retried"}


def test_prompts_always_present_on_every_job_response(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    for resp in (env.get(f"/app/v1/jobs/{jid}", headers=A).json(),
                 env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A).json()):
        assert set(resp["prompts"]) == KEYS
    env.eng.drain()
    r = env.post(f"/app/v1/jobs/{jid}/cancel", headers=A).json()
    assert set(r["prompts"]) == KEYS


def test_generate_detailed_and_style_set_before_t2i(env):
    seen = {}
    orig = env.eng.be.generate

    def gen(*a, **k):
        j = [x for x in env.eng.jobs.values() if x["kind"] == "generate"][-1]
        seen["state"], seen["stage"] = j["state"], j["stage"]
        seen["p"] = env.get(f"/app/v1/jobs/{j['job_id']}", headers=A).json()["prompts"]
        return orig(*a, **k)
    env.eng.be.generate = gen
    r = env.post("/app/v1/qr/st-09/asset", json={"prompt": "a cartoon fox"}, headers=A).json()
    assert env.get(f"/app/v1/jobs/{r['job_id']}", headers=A).json()["prompts"]["input"] == "a cartoon fox"
    env.eng.drain()
    assert seen["state"] == "generating" and seen["stage"] == "t2i"
    p = seen["p"]
    assert p["input"] == "a cartoon fox" and p["style"] == "cartoon"
    assert p["detailed"].endswith("cartoon style, three-quarter front view, centered, full object visible, "
                                  "pure white background, no shadow")                      # T2I 문장 전체(꼬리 포함)
    assert p["inpaint"] is None and p["mask_ratio"] is None and p["region_fit"] is None and p["refine_retried"] is None


def test_generate_realistic_default(env):
    r = env.post("/app/v1/qr/st-08/asset", json={"prompt": "a red fire truck"}, headers=A).json()
    env.eng.drain()
    p = env.get(f"/app/v1/jobs/{r['job_id']}", headers=A).json()["prompts"]
    assert p["style"] == "realistic" and p["detailed"].startswith("a red fire truck, three-quarter front view")


def test_edit_prompts_at_2d_review_and_after_reject(env):
    aid = make_asset(env)
    jid = edit(env, aid).json()["job_id"]
    p0 = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]
    assert p0["input"] == "change the man into Ironman" and p0["style"] == "realistic" and p0["inpaint"] is None
    env.eng.drain()
    j = env.get(f"/app/v1/jobs/{jid}", headers=A).json()
    p = j["prompts"]
    assert j["state"] == "awaiting_2d_review"
    assert p["inpaint"] == env.eng.get(jid)["attempts"][-1]["prompt"]                      # 실제로 보낸 문장
    assert isinstance(p["mask_ratio"], float) and p["region_fit"] == "ok" and p["refine_retried"] is False
    assert p["detailed"] is None
    # 품질 거절 → 시도 2: 보정 없음 → refine_retried null, inpaint 는 시도 2 문장
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A)
    env.eng.drain()
    p2 = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]
    assert p2["refine_retried"] is None and p2["input"] == "change the man into Ironman"
    assert p2["inpaint"] == env.eng.get(jid)["attempts"][-1]["prompt"]


def test_edit_refine_retried_true(env):
    aid = make_asset(env)
    R = {"target_element": "the man", "replacement": "Ironman", "op": "replace"}
    outs = [dict(R, new_scene_prompt="Change the man into Ironman"), dict(R, new_scene_prompt="An Ironman riding a motorcycle")]
    env.eng.llm.refine = lambda *a, **k: outs.pop(0)
    jid = edit(env, aid).json()["job_id"]
    env.eng.drain()
    assert env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]["refine_retried"] is True


def test_raw_mode_prompts(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("booth-07", True)
    jid = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                   json={"base_version": 1, "instruction": "a blue hat", "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
    env.eng.drain()
    p = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]
    assert p == {"input": "a blue hat", "detailed": None, "style": None, "inpaint": "a blue hat",
                 "mask_ratio": None, "region_fit": None, "refine_retried": None}
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "a red hat"}, headers=A)
    env.eng.drain()
    p = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]
    assert p["inpaint"] == "a red hat" and p["input"] == "a blue hat"


def test_raw_mode_generate_prompts(env):
    env.eng.db.set_qr_raw_prompt("st-12", True)
    j = env.eng.create_asset("st-12", "0xdev", "a paper sentence, white background", 0)
    env.eng.drain()
    p = env.get(f"/app/v1/jobs/{j['job_id']}", headers=A).json()["prompts"]
    assert p["detailed"] == p["input"] == "a paper sentence, white background" and p["style"] is None


def test_old_job_without_prompts_gets_input_only(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.eng.get(jid).pop("prompts")
    p = env.get(f"/app/v1/jobs/{jid}", headers=A).json()["prompts"]
    assert set(p) == KEYS and p["input"] == "change the man into Ironman" and p["inpaint"] is None


def test_health_105(env):
    assert env.get("/app/v1/health").json()["app_api"] == "1.1.0"
