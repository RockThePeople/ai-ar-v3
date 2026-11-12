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
