"""S25 계약 12절 판본 되돌리기 + 3.8 history to_version·below_bar(1.2.1 보충)."""
from server.app.store import ChunkStore
from server.tests.app_fakes import BASE_KEYS, cbin
from server.tests.test_app_server import A, env, err, make_asset, to_2d, to_3d  # noqa: F401 — fixture


def _keep(env, aid, v):
    jid = to_3d(env, aid, v)
    j = env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A).json()
    assert j["state"] == "committed"
    return j["result"]["to_version"]


def _on(env):
    env.eng.s.revert = True


def test_store_lineage_follows_revert(tmp_path):
    st = ChunkStore(tmp_path)
    A1, B1, A2, C4 = cbin("8_8_8"), cbin("8_8_9"), cbin("8_8_8", 1), cbin("9_8_8")
    st.put_version("x", 1, {"8_8_8": A1, "8_8_9": B1})
    st.put_version("x", 2, {"8_8_8": A2}, removed=["8_8_9"])
    st.put_revert("x", 3, 1)
    assert st.blobs("x", 3) == {"8_8_8": A1, "8_8_9": B1} and st.lineage("x", 3) == [1]
    m1, m3 = st.manifest("x", 1), st.manifest("x", 3)
    assert {k: (e.uri, e.hash) for k, e in m1.chunks.items()} == {k: (e.uri, e.hash) for k, e in m3.chunks.items()}
    st.put_version("x", 4, {"9_8_8": C4})                                    # 되돌린 뒤 편집 = v1 위에
    assert st.blobs("x", 4) == {"8_8_8": A1, "8_8_9": B1, "9_8_8": C4} and st.lineage("x", 4) == [1, 4]
    assert st.blobs("x", 2) == {"8_8_8": A2}                                  # 지난 판본 보존
    assert st.manifest("x", 4).chunks["8_8_8"].uri.endswith(".v1.cbin")      # m 의 청크를 그대로 가리킴


def test_revert_api_manifest_history_can_rate(env):
    _on(env)
    aid = make_asset(env)
    assert _keep(env, aid, 1) == 2
    h = env.get("/app/v1/health").json()
    assert "revert" in h["features"]
    r = env.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 2, "to_version": 1})
    assert r.status_code == 200, r.text
    o = r.json()
    assert o == {"asset_id": aid, "version": 3, "reverted_from": 1, "manifest_uri": f"/v2/assets/{aid}/manifest.v3.json",
                 "slat_coords_uri": f"/v2/assets/{aid}/slat_coords.v3.json"}
    m1 = env.get(f"/v2/assets/{aid}/manifest.v1.json").json()["chunks"]
    m3 = env.get(o["manifest_uri"]).json()["chunks"]
    assert {k: (e["uri"], e["hash"]) for k, e in m1.items()} == {k: (e["uri"], e["hash"]) for k, e in m3.items()}
    assert env.get(o["slat_coords_uri"]).status_code == 200
    q = env.get("/app/v1/qr/booth-07", headers=A).json()
    assert q["version"] == 3
    assert ("copy_version", aid, 1) in env.be.calls
    hist = env.get(f"/app/v1/assets/{aid}/history", headers=A).json()["jobs"]
    assert all("to_version" in x and "below_bar" in x for x in hist)
    g, e, rv = hist
    assert g["to_version"] == 1 and g["below_bar"] is None and e["to_version"] == 2 and e["below_bar"] is None
    assert rv == {"job_id": rv["job_id"], "kind": "revert", "created_at": rv["created_at"], "state": "committed",
                  "prompts": {"input": None, "detailed": None, "style": None, "mask_ratio": None, "region_fit": None},
                  "attempts": [], "outcome": "keep", "error_code": None, "to_version": 3, "reverted_from": 1,
                  "below_bar": None}
    # 다음 편집은 v3(= v1 내용) 위에서 v4
    assert _keep(env, aid, 3) == 4
    assert env.eng.store.lineage(aid, 4) == [1, 4]


def test_revert_alone_does_not_enable_rating(env):
    _on(env)
    aid = make_asset(env)
    _keep(env, aid, 1)
    assert env.get("/app/v1/qr/booth-07", headers=A).json()["can_rate"] is True
    assert env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4}).status_code == 201
    assert env.get("/app/v1/qr/booth-07", headers=A).json()["can_rate"] is False
    env.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 2, "to_version": 1})
    q = env.get("/app/v1/qr/booth-07", headers=A).json()
    assert q["version"] == 3 and q["can_rate"] is False                         # 되돌리기만으로 바뀌지 않음


def test_revert_errors_in_order(env):
    _on(env)
    err(env.post("/app/v1/assets/nope/revert", headers=A, json={"base_version": 2, "to_version": 2}), 400, "INVALID_REQUEST")
    err(env.post("/app/v1/assets/nope/revert", headers=A, json={"base_version": 2}), 400, "INVALID_REQUEST")
    err(env.post("/app/v1/assets/nope/revert", headers=A, json={"base_version": 2, "to_version": 1}), 404, "ASSET_NOT_FOUND")
    env.eng.db.x("INSERT INTO assets(asset_id,is_sample,version,sample_name,created_at) VALUES('sample-x',1,2,'x',0)")
    env.eng.store.put_version("sample-x", 1, {k: cbin(k) for k in BASE_KEYS})
    err(env.post("/app/v1/assets/sample-x/revert", headers=A, json={"base_version": 2, "to_version": 1}), 403, "READ_ONLY")
    aid = make_asset(env)
    _keep(env, aid, 1)
    to_2d(env, aid, 2)                                                           # 진행 중(2D 확인 대기)
    err(env.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 2, "to_version": 1}), 409, "ASSET_LOCKED")


def test_revert_version_conflict(env):
    _on(env)
    aid = make_asset(env)
    _keep(env, aid, 1)
    b = err(env.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 5, "to_version": 1}), 409,
            "VERSION_CONFLICT")
    assert b["detail"]["latest_version"] == 2


def test_revert_off_by_default(env):
    aid = make_asset(env)
    assert "revert" not in env.get("/app/v1/health").json()["features"]
    assert env.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 1, "to_version": 1}).status_code == 404


def _mock_app(tmp_path):
    from fastapi.testclient import TestClient
    from server.app.llm_adapter import FakeAdapter
    from server.app.main import create_app
    from server.tests.app_fakes import png
    from server.tests.test_app_server import _settings
    st = ChunkStore(tmp_path)
    st.put_version("mock-src", 1, {k: cbin(k) for k in BASE_KEYS})
    fx = tmp_path / "mock_fixtures"
    fx.mkdir()
    for n in ("render", "mask", "edit"):
        (fx / f"{n}.png").write_bytes(png(8, 8))
    app = create_app(_settings(tmp_path, mock=True), start_thread=False, llm=FakeAdapter())
    return TestClient(app), app.state.app.engine


def test_mock_mode_revert_same_shape(tmp_path):
    c, eng = _mock_app(tmp_path)
    assert "revert" in c.get("/app/v1/health").json()["features"]
    aid = c.post("/app/v1/qr/m1/asset", json={"prompt": "jeep"}, headers=A).json()["asset_id"]
    eng.drain()
    eng.store.put_version(aid, 2, {"8_8_8": cbin("8_8_8", 1)})                 # 가짜 편집 판본
    eng.db.x("UPDATE assets SET version=2 WHERE asset_id=?", (aid,))
    o = c.post(f"/app/v1/assets/{aid}/revert", headers=A, json={"base_version": 2, "to_version": 1}).json()
    assert o["version"] == 3 and o["reverted_from"] == 1
    assert c.get(f"/app/v1/assets/{aid}/history", headers=A).json()["jobs"][-1]["kind"] == "revert"


def test_agent_object_has_max_mask_retries(env):
    """계약 10.3(1.2.1): 작업 객체 agent.max_mask_retries 채움."""
    from server.tests.test_gemini import PASS, FakeGemini, _lasso
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    env.eng.agent_llm.judge_script = [PASS]
    j = _lasso(env, aid)
    assert j["agent"]["max_mask_retries"] == env.eng.s.agent_max_mask_retries == 2
