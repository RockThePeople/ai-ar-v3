"""S14: 마커별 그대로 모드(raw_prompt) + 샘플 사본 등록."""

from __future__ import annotations

from server.tests.test_app_server import A, CAM, CELLS, env, make_asset, to_2d  # noqa: F401 — fixture


def _raw_edit(env, aid, text="a tall blue pointed wizard hat with yellow stars"):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": 1, "instruction": text, "camera": CAM, "volume_cells": CELLS})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return r.json()["job_id"]


def _llm_spy(env):
    calls = []
    for name in ("refine", "classify", "detail_style", "detail_prompt"):
        orig = getattr(env.eng.llm, name)
        setattr(env.eng.llm, name, lambda *a, _n=name, _o=orig, **k: (calls.append(_n), _o(*a, **k))[1])
    return calls


def test_raw_prompt_no_llm_and_verbatim(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("booth-07", True)
    calls = _llm_spy(env)
    jid = _raw_edit(env, aid)
    j = env.eng.get(jid)
    a = j["attempts"][0]
    assert calls == [] and j["raw_prompt"] is True
    assert a["prompt"] == "a tall blue pointed wizard hat with yellow stars" and a["inpaint_src"] == "raw_prompt"
    assert ("inpaint_args", "a tall blue pointed wizard hat with yellow stars", 0) in env.be.calls
    # 거절 → 이유 원문이 새 문장, seed +1, 분류 LLM 없음
    r = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "a taller hat with more stars"}, headers=A).json()
    assert r["state"] == "inpainting" and r["review"]["category"] == "prompt"
    env.eng.drain()
    assert ("inpaint_args", "a taller hat with more stars", 1) in env.be.calls and calls == []
    # 커밋: 장면 문장 = 이전 + '; ' + 지시문
    scene0 = env.eng.db.asset(aid)["scene_prompt"]
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    assert env.eng.db.asset(aid)["scene_prompt"] == f"{scene0}; a tall blue pointed wizard hat with yellow stars"
    assert calls == []


def test_raw_prompt_only_on_flagged_marker(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("other-qr", True)
    jid = _raw_edit(env, aid, "change the man into Ironman")
    assert not env.eng.get(jid).get("raw_prompt")
    assert env.eng.get(jid)["attempts"][0]["inpaint_src"] != "raw_prompt"


def test_raw_reject_limit(env):
    aid = make_asset(env)
    env.eng.db.set_qr_raw_prompt("booth-07", True)
    jid = _raw_edit(env, aid)
    for k in range(2):
        env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": f"try {k}"}, headers=A)
        env.eng.drain()
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "again"}, headers=A).json()
    assert j["state"] == "failed" and j["error_code"] == "RETRY_LIMIT"


def test_copy_sample_tool(tmp_path, monkeypatch):
    from server.app.store import ChunkStore, Db
    from server.tools import papermode
    data = tmp_path / "d"
    db, st = Db(data / "app.sqlite3"), ChunkStore(data)
    st.put_version("sample-x", 1, {"8_8_8": b"A", "8_8_9": b"B"})
    st.put_slat_coords("sample-x", 1, [[1, 2, 3]])
    db.x("INSERT INTO assets(asset_id,qr_id,creator,scene_prompt,version,is_sample) VALUES('sample-x',NULL,NULL,'s',1,1)")
    monkeypatch.setenv("APP_DATA_DIR", str(data))
    assert papermode.main(["copy-sample", "sample-x", "1", "st-09", "0xdev", "--scene", "a hero", "--asset-id", "a-test0001"]) == 0
    db2, st2 = Db(data / "app.sqlite3"), ChunkStore(data)
    a = db2.asset("a-test0001")
    assert a["qr_id"] == "st-09" and a["creator"] == "0xdev" and a["version"] == 1 and a["scene_prompt"] == "a hero"
    assert set(st2.blobs("a-test0001", 1)) == {"8_8_8", "8_8_9"} and st2.slat_coords("a-test0001", 1) == [[1, 2, 3]]
    assert papermode.main(["flag", "st-09", "on"]) == 0 and db2.qr_raw_prompt("st-09")
    import pytest
    with pytest.raises(SystemExit):
        papermode.main(["copy-sample", "sample-x", "1", "st-09", "0xdev", "--scene", "x"])   # 마커가 비어 있지 않다


PLAN = ("A chibi adventurer figurine standing, three-quarter front view, centered, full body visible, wearing a brown "
        "leather hat, holding a wooden sword in the right hand, a round wooden shield on the left arm, brown leather boots, "
        "green tunic, simple clean 3D game asset style, pure white background, no shadow")


def test_raw_generate_verbatim_and_paper_seeds(env):
    env.eng.db.set_qr_raw_prompt("st-02", True)
    calls = _llm_spy(env)
    assert len(PLAN) > 300                                                   # 앱 API 상한(300자)보다 길다 → 서버가 직접 연다
    j = env.eng.create_asset("st-02", "0xdevice", PLAN, 0)
    env.eng.drain()
    j = env.eng.get(j["job_id"])
    r = type("R", (), {"json": lambda self: {"asset_id": j["asset_id"]}})()
    assert j["state"] == "committed" and calls == []
    assert ("generate_args", PLAN, "{subject}") in env.be.calls            # 꼬리 없이 원문 그대로
    assert ("generate_seeds", 0, 42) in env.be.calls                       # T2I seed 0, TRELLIS seed 42
    assert j["translate"]["mode"] == "raw" and j["paper_gen"]["trellis_seed"] == 42
    assert env.eng.db.asset(r.json()["asset_id"])["scene_prompt"] == PLAN


def test_generate_other_marker_unchanged(env):
    env.eng.db.set_qr_raw_prompt("st-02", True)
    r = env.post("/app/v1/qr/st-03/asset", json={"prompt": "a red fire truck", "seed": 5}, headers=A)
    env.eng.drain()
    j = env.eng.get(r.json()["job_id"])
    assert not j.get("raw_prompt") and not any(c[0] == "generate_seeds" for c in env.be.calls)


# ── S14 덧붙임: T2I 원본 보관, 얼굴 규칙 ───────────────────────────────────
def test_facehint_passed_and_rule_text(env):
    from server.app.llm_adapter import FACE_HINT
    assert "clearly visible face" in FACE_HINT
    seen = []
    orig = env.eng.llm.detail_style
    env.eng.llm.detail_style = lambda p, job_id="", **kw: (seen.append(kw), orig(p, job_id=job_id))[1]
    env.post("/app/v1/qr/st-05/asset", json={"prompt": "a cartoon style adventurer"}, headers=A)
    env.eng.drain()
    assert seen == [{"facehint": True, "named": True}]      # S22 APP_NAMED_NODETAIL 기본 켬
    env.eng.s.facehint = False
    env.post("/app/v1/qr/st-06/asset", json={"prompt": "a dog"}, headers=A)
    env.eng.drain()
    assert seen[-1] == {"named": True}


def test_t2i_raw_image_saved_and_served(env):
    from server.tests.app_fakes import png
    orig = env.be.generate
    env.be.generate = lambda *a, **k: dict(orig(*a, **k), png=png(v=50), image_png=png(v=60))
    r = env.post("/app/v1/qr/st-07/asset", json={"prompt": "a cat"}, headers=A).json()
    env.eng.drain()
    j = env.eng.get(r["job_id"])
    assert j["t2i"]["raw_file"] == "image.png"
    assert env.get(f"/clients/img/{r['job_id']}/image.png").content == png(v=60)
    row = [x for x in env.get(f"/clients/api/assets/{r['asset_id']}").json()["jobs"] if x["kind"] == "generate"][0]
    assert row["t2i_raw"] is True
