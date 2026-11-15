"""S20g: 생성 직후 정면·뒷면 얼굴 판단(다시 생성), 방향 고르기 정면 기준, 옆·뒤 시점 꼬리 (APP_FRONT_FIX)."""
from server.app import automask as am
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture
from server.tests.test_gemini import PASS, FakeGemini, _lasso

FACE = {"is_character": True, "front_view": "az180", "faces_on_multiple_sides": True,
        "face_views": ["az000", "az180"], "note": "eyes on the back"}
CLEAN = dict(FACE, faces_on_multiple_sides=False, face_views=["az180"], note="plain back")


def _gen_with_png(env):
    """가짜 백엔드 생성 결과에 T2I 이미지(다시 생성 때 재사용)를 붙인다."""
    orig = env.be.generate

    def gen(*a, **k):
        out = orig(*a, **k)
        out["png"] = b"\x89PNG-fake-t2i"
        return out
    env.be.generate = gen


def _seeds(env):
    return [(c[1], c[2]) for c in env.be.calls if c[0] == "generate_seed"]


def _gen_job(env, aid):
    return next(j for j in env.eng.jobs.values() if j["asset_id"] == aid and j["kind"] == "generate")


def test_off_by_default_no_front_call(env):
    aid = make_asset(env)
    assert env.eng.s.front_fix is False and env.eng.asset_front(aid) is None
    assert "front_check" not in _gen_job(env, aid)


def test_first_clean_saves_front_no_regen(env):
    env.eng.s.front_fix = True
    _gen_with_png(env)
    env.eng.agent_llm.front_script = [CLEAN]
    aid = make_asset(env)
    j = _gen_job(env, aid)
    assert j["state"] == "committed" and len(_seeds(env)) == 1
    fc = j["front_check"]
    assert fc["chosen_seed"] == _seeds(env)[0][0] and fc["front_view"] == "az180" and fc["face_left"] is False
    assert env.eng.asset_front(aid) == "az180" and not j.get("gen_events")
    assert (env.eng._views_dir(aid, 1) / "views.json").is_file()      # 판단에 쓴 4장이 미리 렌더 캐시로 남는다


def test_face_then_clean_regenerates_with_same_image_seed_plus_one(env):
    env.eng.s.front_fix = True
    _gen_with_png(env)
    env.eng.agent_llm.front_script = [FACE, CLEAN]
    aid = make_asset(env)
    j = _gen_job(env, aid)
    s = _seeds(env)
    assert j["state"] == "committed" and len(s) == 2
    assert s[1] == (s[0][0] + 1, True)                                  # 같은 T2I 이미지, seed + 1
    assert ("delete",) in env.be.calls or any(c[0] == "delete" for c in env.be.calls)
    assert [e["text"] for e in j["gen_events"]] == ["뒷면에 얼굴이 생겨 다시 만듭니다(2/3)"]
    assert j["front_check"]["chosen"] == 1 and j["front_check"]["face_left"] is False


def test_three_faces_falls_back_to_first(env):
    env.eng.s.front_fix = True
    _gen_with_png(env)
    env.eng.agent_llm.front_script = [FACE]
    aid = make_asset(env)
    j = _gen_job(env, aid)
    s = _seeds(env)
    s0 = s[0][0]
    assert [x[0] for x in s] == [s0, s0 + 1, s0 + 2, s0]               # 셋 다 → 첫 seed 로 되돌림(4번째 생성)
    fc = j["front_check"]
    assert fc["chosen"] == 0 and fc["chosen_seed"] == s0 and fc["face_left"] is True
    texts = [e["text"] for e in j["gen_events"]]
    assert texts[:2] == ["뒷면에 얼굴이 생겨 다시 만듭니다(2/3)", "뒷면에 얼굴이 생겨 다시 만듭니다(3/3)"]
    assert texts[-1] == "뒷면 얼굴 남음" and j["state"] == "committed"


def test_not_character_stores_none(env):
    env.eng.s.front_fix = True
    env.eng.agent_llm.front_script = [{"is_character": False, "front_view": "none", "faces_on_multiple_sides": False,
                                       "face_views": [], "note": "a chair"}]
    aid = make_asset(env)
    assert env.eng.asset_front(aid) == "none"


def test_pick_view_front_order_and_back_weight():
    occ = [[x, y, z] for x in range(28, 36) for y in range(28, 36) for z in range(10, 50)]   # 정사각 몸통
    cams = am.view_cameras(occ)
    top = [[x, y, 49] for x in range(28, 36) for y in range(28, 36)]                    # 위 면: 네 방향 같은 수
    pk = am.pick_view(top, occ, cams, cams["az090"], front_view="az180", back_weight=0.3, front_order=True)
    c = pk["counts"]
    assert len({c[v] for v in am.VIEW_AZ} | {c["user"]}) == 1 and pk["weights"]["az000"] == 0.3
    assert pk["view"] == "az180" and pk["view_side"] == "front"                        # 동점 → 정면(az000 우선 없음)
    old = am.pick_view(top, occ, cams, cams["az090"], front_view="az180")
    assert old["view"] == "user"                                                      # 끔: 예전 순서(사용자 → az000)
    pk3 = am.pick_view(top, occ, cams, cams["az090"], front_view=None, back_weight=0.3, front_order=True)
    assert pk3["view"] == "user" and pk3["view_side"] is None                         # 정면 모름 → 사용자 먼저
    pk4 = am.pick_view(top, occ, cams, cams["az000"], front_view="az090", back_weight=0.3, front_order=True)
    assert pk4["view"] == "az090"                                                     # 정면이 사용자보다 먼저
    pk5 = am.pick_view(top, occ, cams, cams["az270"], front_view="az090", back_weight=0.3, front_order=True)
    assert pk5["weights"]["user"] == 0.3 and pk5["view"] == "az090"                    # 사용자가 뒤쪽이면 0.3


def test_edit_uses_stored_front_and_face_tail(env):
    env.eng.s.front_fix = True
    env.eng.agent_llm.front_script = [dict(CLEAN, front_view="az000")]
    aid = make_asset(env)
    assert env.eng.asset_front(aid) == "az000"
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    env.eng.agent_llm.judge_script = [PASS]
    j = _lasso(env, aid)
    jj = env.eng.get(j["job_id"])
    pk = jj["agent_pick"]
    assert pk["front_source"] == "asset" and pk["front_view"] == "az000" and pk["back_view"] == "az180"
    tail = "do not add a face or facial features on this side."
    p = env.eng.gemini.calls[0]
    if pk["view_side"] in ("side", "back"):
        assert f"This is a {pk['view_side']} view of the character; {tail}" in p
    else:
        assert tail not in p


def test_tail_only_on_side_or_back(env, monkeypatch):
    env.eng.s.front_fix = True
    env.eng.agent_llm.front_script = [dict(CLEAN, front_view="az000")]
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    env.eng.agent_llm.judge_script = [PASS]
    real = am.pick_view

    def forced(side):
        def f(*a, **k):
            out = real(*a, **k)
            out["view_side"] = side
            return out
        return f
    tail = "do not add a face or facial features on this side."
    for side, want in (("back", True), ("side", True), ("front", False)):
        monkeypatch.setattr(am, "pick_view", forced(side))
        env.eng.gemini.calls.clear()
        j = _lasso(env, aid, base=env.eng.db.asset(aid)["version"])
        assert j["state"] == "committed", j
        p = env.eng.gemini.calls[0]
        assert (f"This is a {side} view of the character; {tail}" in p) is want


# ── 2차: 계약 1.2.0 보충(S20g) stage front_check·regenerating, notice ──
def test_stages_and_notice_real_path(env):
    env.eng.s.front_fix = True
    _gen_with_png(env)
    env.eng.agent_llm.front_script = [FACE, CLEAN]
    seen = []
    real_set = env.eng._set

    def spy(j, *a, **k):
        real_set(j, *a, **k)
        if j["kind"] == "generate":
            seen.append((j["stage"], j.get("notice")))
    env.eng._set = spy
    aid = make_asset(env)
    j = _gen_job(env, aid)
    stages = [s for s, _ in seen]
    assert stages.index("front_check") < stages.index("regenerating") < len(stages) - 1 - stages[::-1].index("front_check")
    assert ("regenerating", "뒷면에 얼굴이 생겨 다시 만듭니다(2/3)") in seen
    o = env.get(f"/app/v1/jobs/{j['job_id']}", headers=A).json()
    assert o["state"] == "committed" and o["notice"] is None                     # 끝나면 null
    assert "front_check" in [x["stage"] for x in j["stage_log"]]


def test_notice_key_null_when_off(env):
    aid = make_asset(env)
    o = env.get(f"/app/v1/jobs/{_gen_job(env, aid)['job_id']}", headers=A).json()
    assert "notice" in o and o["notice"] is None


def _mock_app(tmp_path):
    from fastapi.testclient import TestClient
    from server.app.main import create_app
    from server.app.llm_adapter import FakeAdapter
    from server.app.store import ChunkStore
    from server.tests.app_fakes import BASE_KEYS, cbin, png
    from server.tests.test_app_server import _settings
    st = ChunkStore(tmp_path)
    st.put_version("mock-src", 1, {k: cbin(k) for k in BASE_KEYS})
    fx = tmp_path / "mock_fixtures"
    fx.mkdir()
    for n in ("render", "mask", "edit"):
        (fx / f"{n}.png").write_bytes(png(8, 8))
    s = _settings(tmp_path, mock=True)
    s.front_fix = True
    app = create_app(s, start_thread=False, llm=FakeAdapter())
    return TestClient(app), app.state.app.engine


def test_mock_front_check_and_regen_header(tmp_path):
    c, eng = _mock_app(tmp_path)
    r = c.post("/app/v1/qr/m1/asset", json={"prompt": "jeep"}, headers=A).json()
    eng.drain()
    j = eng.get(r["job_id"])
    st = [x["stage"] for x in j["stage_log"]]
    assert "front_check" in st and "regenerating" not in st and j["state"] == "committed"
    r2 = c.post("/app/v1/qr/m2/asset", json={"prompt": "jeep"}, headers={**A, "X-Mock-Regen": "1"}).json()
    eng.drain()
    j2 = eng.get(r2["job_id"])
    st2 = [x["stage"] for x in j2["stage_log"]]
    assert st2.count("regenerating") == 1 and st2.count("front_check") == 2
    assert c.get(f"/app/v1/jobs/{r2['job_id']}", headers=A).json()["notice"] is None
