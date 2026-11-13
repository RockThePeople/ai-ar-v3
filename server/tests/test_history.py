"""S17: GET /app/v1/assets/{asset_id}/history (app-api 1.0.6 3.8)."""

from __future__ import annotations

from server.tests.test_app_server import A, CAM, CELLS, edit, env, make_asset, to_2d  # noqa: F401 — fixture

JOB_KEYS = {"job_id", "kind", "created_at", "state", "prompts", "attempts", "outcome", "error_code"}
P_KEYS = {"input", "detailed", "style", "mask_ratio", "region_fit"}
A_KEYS = {"attempt", "refine", "inpaint", "refine_retried", "review", "accepted"}


def _hist(env, aid):
    r = env.get(f"/app/v1/assets/{aid}/history", headers=A)
    assert r.status_code == 200, r.text
    return r.json()


def test_generate_only(env):
    aid = make_asset(env)
    h = _hist(env, aid)
    assert h["asset_id"] == aid and len(h["jobs"]) == 1
    g = h["jobs"][0]
    assert set(g) == JOB_KEYS and set(g["prompts"]) == P_KEYS
    assert g["kind"] == "generate" and g["attempts"] == [] and g["outcome"] == "keep" and g["error_code"] is None
    assert g["prompts"]["input"] == "a man riding a motorcycle" and g["prompts"]["detailed"] and g["prompts"]["style"] == "realistic"


def test_edit_reject_once_then_accept_keep(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A)   # quality → 인페인팅만
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    h = _hist(env, aid)
    assert [x["kind"] for x in h["jobs"]] == ["generate", "edit"]
    assert h["jobs"][0]["created_at"] <= h["jobs"][1]["created_at"]
    e = h["jobs"][1]
    assert e["outcome"] == "keep" and e["state"] == "committed" and len(e["attempts"]) == 2
    a1, a2 = e["attempts"]
    assert set(a1) == A_KEYS and a1["attempt"] == 1 and a2["attempt"] == 2
    assert a1["accepted"] is False and a1["review"]["category"] == "quality" and a1["review"]["reason"] == "흐릿함"
    assert a2["accepted"] is True and a2["review"] is None
    assert a1["refine_retried"] is False and a2["refine_retried"] is None       # 시도 2 는 보정 없음
    assert a2["refine"] == a1["refine"] and a1["refine"]["new_scene_prompt"]   # 다시 보정 안 함 → 앞 시도 값
    j = env.eng.get(jid)
    assert a1["inpaint"] == j["attempts"][0]["prompt"] and a2["inpaint"] == j["attempts"][1]["prompt"]
    assert e["prompts"]["input"] == "change the man into Ironman" and e["prompts"]["detailed"] is None


def test_edit_needs_redraw(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "영역이 틀렸다 area"}, headers=A)
    e = _hist(env, aid)["jobs"][1]
    assert e["state"] == "needs_redraw" and e["outcome"] == "needs_redraw"
    assert e["attempts"][-1]["accepted"] is False and e["attempts"][-1]["review"]["category"] == "region"


def test_in_progress_and_cancel(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    e = _hist(env, aid)["jobs"][1]
    assert e["outcome"] is None and e["attempts"][0]["accepted"] is None          # 확인 대기 중
    env.post(f"/app/v1/jobs/{jid}/cancel", headers=A)
    e = _hist(env, aid)["jobs"][1]
    assert e["outcome"] == "cancelled" and e["attempts"][0]["accepted"] is None


def test_deleted_and_missing_asset_404(env):
    aid = make_asset(env)
    assert env.delete("/app/v1/qr/booth-07", headers=A).status_code == 204
    r = env.get(f"/app/v1/assets/{aid}/history", headers=A)
    assert r.status_code == 404 and r.json()["error_code"] == "ASSET_NOT_FOUND"
    r = env.get("/app/v1/assets/a-nope0000/history", headers=A)
    assert r.status_code == 404 and r.json()["error_code"] == "ASSET_NOT_FOUND"


def test_raw_mode_nulls(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("booth-07", True)
    jid = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                   json={"base_version": 1, "instruction": "a blue hat", "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "a red hat"}, headers=A)
    env.eng.drain()
    e = _hist(env, aid)["jobs"][1]
    assert e["prompts"]["style"] is None and e["prompts"]["mask_ratio"] is None and e["prompts"]["region_fit"] is None
    assert [a["refine"] for a in e["attempts"]] == [None, None]
    assert [a["inpaint"] for a in e["attempts"]] == ["a blue hat", "a red hat"]
    assert [a["refine_retried"] for a in e["attempts"]] == [None, None] and e["attempts"][0]["accepted"] is False


def test_failed_error_code(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    for k in range(2):
        env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A)
        env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "흐릿함"}, headers=A)
    e = _hist(env, aid)["jobs"][1]
    assert e["outcome"] == "failed" and e["error_code"] == "RETRY_LIMIT" and e["attempts"][-1]["accepted"] is False


def test_pre_s17_attempt_fallback(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    j = env.eng.get(jid)
    for a in j["attempts"]:                          # S17 전처럼 시도별 refine·accepted·refine_retried 를 지운다
        for k in ("refine", "accepted", "refine_retried"):
            a.pop(k, None)
    a = _hist(env, aid)["jobs"][1]["attempts"][0]
    assert a["refine"]["new_scene_prompt"] == j["refine"]["new_scene_prompt"]   # 마지막 보정과 같은 문장 → 그 보정
    assert a["accepted"] is True                                               # 3D 단계로 감
    assert a["refine_retried"] is False                                        # style.retried 에서
