"""앱 서버 v1 — 상태 전이·잠금·판본·권한·평가·삭제·오류 모양 (app-api 1.0.1). GPU·네트워크 없음."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from server.app.config import Settings
from server.app.engine import INPAINT_SUFFIX, inpaint_prompt
from server.app.main import create_app
from server.app.llm_adapter import FakeAdapter
from server.tests.app_fakes import BASE_KEYS, FakeBackend

CAM = {"frame": "normalized_voxel", "position": [0.1, -1.3, 0.4], "forward": [0, 1, 0], "up": [0, 0, 1],
       "fov_y_deg": 60, "aspect": 0.46}
CELLS = [[31, 40, 22], [31, 40, 23], [31, 40, 22]]
A, B = {"X-Device-Key": "0x02aaaa"}, {"X-Device-Key": "0x03bbbb"}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _settings(tmp_path, mock=False):
    return Settings(data_dir=tmp_path, mock=mock, a5000_url="http://a5000.invalid", a5000_key="", inpaint_port=1,
                    shared_token="", bind_host="127.0.0.1", bind_port=0, render_mode="camera", mock_time_scale=0.0,
                    mock_source="mock-src")


@pytest.fixture
def env(tmp_path):
    clock, be = Clock(), FakeBackend()
    app = create_app(_settings(tmp_path), backend=be, clock=clock, start_thread=False, llm=FakeAdapter())
    c = TestClient(app)
    c.eng, c.be, c.clock, c.tmp = app.state.app.engine, be, clock, tmp_path
    return c


def err(r, status, code):
    assert r.status_code == status, (r.status_code, r.text)
    b = r.json()
    assert b["error_code"] == code and isinstance(b["message"], str), b
    return b


def make_asset(c, qr="booth-07", dev=A):
    r = c.post(f"/app/v1/qr/{qr}/asset", json={"prompt": "a man riding a motorcycle"}, headers=dev)
    assert r.status_code == 202, r.text
    c.eng.drain()
    return r.json()["asset_id"]


def edit(c, aid, v=1, dev=A, **kw):
    return c.post(f"/app/v1/assets/{aid}/edits", headers={**dev, **kw.pop("h", {})},
                  json={"base_version": v, "instruction": "change the man into Ironman", "camera": CAM,
                        "volume_cells": CELLS, **kw})


def to_2d(c, aid, v=1):
    r = edit(c, aid, v)
    assert r.status_code == 202, r.text
    c.eng.drain()
    return r.json()["job_id"]


def job(c, jid, dev=A):
    return c.get(f"/app/v1/jobs/{jid}", headers=dev).json()


ALWAYS = ("job_id", "kind", "state", "stage", "asset_id", "is_mine", "progress", "created_at", "updated_at", "elapsed_s")


# ── health · 헤더 ──────────────────────────────────────────────────
def test_health(env):
    h = env.get("/app/v1/health").json()
    assert h["app_api"] == "1.2.0" and h["features"] == ["agent_edit", "agent_lasso", "chain_rating"] and h["ok"] is True and h["contract_version"] == 4 and h["mock"] is False
    assert set(h["workers"]) == {"a5000", "inpaint", "llm"}


def test_device_key_required(env):
    err(env.get("/app/v1/qr/booth-07"), 400, "INVALID_REQUEST")
    err(env.get("/app/v1/qr/bad qr!", headers=A), 400, "INVALID_QR")


# ── 생성 ───────────────────────────────────────────────────────────
def test_generate_flow(env):
    err(env.get("/app/v1/qr/booth-07", headers=A), 404, "QR_EMPTY")
    r = env.post("/app/v1/qr/booth-07/asset", json={"prompt": "a man riding a motorcycle"}, headers=A)
    assert r.status_code == 202 and set(r.json()) == {"job_id", "asset_id"}
    q = env.get("/app/v1/qr/booth-07", headers=B).json()          # 생성 중
    assert q["version"] == 0 and q["manifest_uri"] is None and q["scene_prompt"] is None and q["can_rate"] is False
    assert q["active_job"]["state"] == "queued" and q["active_job"]["is_mine"] is False and q["is_creator"] is False
    err(env.post("/app/v1/qr/booth-07/asset", json={"prompt": "x"}, headers=B), 409, "QR_TAKEN")
    env.eng.drain()
    j = job(env, r.json()["job_id"])
    assert j["state"] == "committed" and j["result"] == {"to_version": 1, "manifest_uri": f"/v2/assets/{j['asset_id']}/manifest.v1.json"}
    assert all(j[k] is not None for k in ALWAYS) and j["attempt"] is None
    q = env.get("/app/v1/qr/booth-07", headers=A).json()
    assert q["version"] == 1 and q["is_creator"] and q["active_job"] is None and q["can_rate"] is False and q["rate_range"] is None
    m = env.get(q["manifest_uri"]).json()
    assert sorted(m["chunks"]) == BASE_KEYS
    e = m["chunks"]["8_8_8"]
    assert hashlib.sha256(env.get(e["uri"]).content).hexdigest() == e["hash"]
    s = env.get(q["slat_coords_uri"]).json()
    assert s["n_cells"] == 2 and s["version"] == 1


def test_generate_failure_frees_qr(env):
    env.be.fail["generate"] = "T2I_FAILED"
    r = env.post("/app/v1/qr/q1/asset", json={"prompt": "x"}, headers=A)
    env.eng.drain()
    j = job(env, r.json()["job_id"])
    assert j["state"] == "failed" and j["error_code"] == "T2I_FAILED"
    err(env.get("/app/v1/qr/q1", headers=A), 404, "QR_EMPTY")


# ── 편집: 2D 확인 ─────────────────────────────────────────────────
def test_edit_to_2d_review_and_preview(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    j = job(env, jid)
    assert j["state"] == "awaiting_2d_review" and j["attempt"] == 1 and j["max_attempts"] == 3
    assert j["refine"]["target_element"] == "the man" and j["refine"]["replacement"] == "Ironman"
    assert j["preview"]["edit_url"] == f"/app/v1/jobs/{jid}/preview/1/edit.png"
    for n in ("render", "edit", "mask"):
        r = env.get(j["preview"][f"{n}_url"])
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    rf0 = env.eng.get(jid)["refine"]                                                            # 앱 API 는 4필드만(모양 그대로)
    want = rf0.get("inpaint_prompt") or rf0["new_scene_prompt"]                                 # S13b: 인페인팅 전용 문장
    assert ("inpaint_args", inpaint_prompt(want, True, None), 0) in env.be.calls   # S11 실물형: 꼬리 없음


def test_reject_quality_prompt_then_retry_limit(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    n_render = sum(1 for c in env.be.calls if c == ("render",))
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "the helmet looks blurry"}, headers=A).json()
    assert j["state"] == "inpainting" and j["review"]["category"] == "quality" and j["attempt"] == 2
    env.eng.drain()
    assert sum(1 for c in env.be.calls if c == ("render",)) == n_render          # 품질 재시도는 렌더 재사용
    rf = env.eng.get(jid)["refine"]
    assert ("inpaint_args", inpaint_prompt(rf.get("inpaint_prompt") or rf["new_scene_prompt"], True, None), 1) in env.be.calls   # seed = 0 + attempt − 1
    assert job(env, jid)["preview"]["attempt"] == 2
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "프롬프트를 잘못 이해함"}, headers=A).json()
    assert j["state"] == "refining" and j["review"]["category"] == "prompt" and j["attempt"] == 3
    env.eng.drain()
    assert job(env, jid)["state"] == "awaiting_2d_review"
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "still bad"}, headers=A).json()
    assert j["state"] == "failed" and j["error_code"] == "RETRY_LIMIT"
    assert edit(env, aid).status_code == 202                                     # 잠금 풀림


def test_reject_region_needs_redraw(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "wrong area selected"}, headers=A).json()
    assert j["state"] == "needs_redraw" and j["review"]["category"] == "region"
    assert edit(env, aid).status_code == 202


def test_reject_needs_reason_and_wrong_state(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    err(env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False}, headers=A), 400, "INVALID_REQUEST")
    err(env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A), 409, "WRONG_STATE")
    err(env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=B), 403, "NOT_OWNER")
    err(env.get("/app/v1/jobs/j-nope", headers=A), 404, "JOB_NOT_FOUND")


def test_cancel(env):
    aid = make_asset(env)
    r = edit(env, aid)
    err(env.post(f"/app/v1/jobs/{r.json()['job_id']}/cancel", headers=A), 409, "WRONG_STATE")   # 대기 중
    env.eng.drain()
    j = env.post(f"/app/v1/jobs/{r.json()['job_id']}/cancel", headers=A).json()
    assert j["state"] == "cancelled"


# ── 편집: 3D ───────────────────────────────────────────────────────
def to_3d(env, aid, v=1):
    jid = to_2d(env, aid, v)
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A).json()
    assert j["state"] == "editing_3d" and j["queue_pos"] == 0
    env.eng.drain()
    return jid


def test_keep_commits_and_staging(env):
    aid = make_asset(env)
    jid = to_3d(env, aid)
    j = job(env, jid)
    assert j["state"] == "awaiting_3d_review" and j["expires_at"] == env.clock.t + 1800
    res = j["result"]
    assert res["base_version"] == 1 and res["to_version"] == 2 and res["removed_chunk_ids"] == ["9_8_8"]
    for e in res["chunks"]:
        assert e["uri"].startswith(f"/v2/assets/{aid}/staging/{jid}/chunks/")
        assert hashlib.sha256(env.get(e["uri"]).content).hexdigest() == e["hash"]
    j = env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A).json()
    assert j["state"] == "committed" and j["result"]["manifest_uri"] == f"/v2/assets/{aid}/manifest.v2.json"
    err(env.get(res["chunks"][0]["uri"]), 404, "JOB_NOT_FOUND")                   # 커밋 뒤 staging 은 판본 경로로
    m = env.get(j["result"]["manifest_uri"]).json()
    assert sorted(m["chunks"]) == ["10_8_8", "8_8_8", "8_8_9"]                   # 9_8_8 제거
    assert m["chunks"]["8_8_9"]["version"] == 1 and m["chunks"]["8_8_8"]["version"] == 2
    for e in m["chunks"].values():
        assert hashlib.sha256(env.get(e["uri"]).content).hexdigest() == e["hash"]
    assert m["chunks"]["8_8_8"]["hash"] == {e["uri"].rsplit("/", 1)[1][:-5]: e for e in res["chunks"]}["8_8_8"]["hash"]
    q = env.get("/app/v1/qr/booth-07", headers=A).json()
    assert q["version"] == 2 and "Ironman" in q["scene_prompt"]
    assert env.get(f"/v2/assets/{aid}/slat_coords.v2.json").json()["coords"] == [[1, 2, 3]]


def test_discard_and_auto_discard(env):
    aid = make_asset(env)
    jid = to_3d(env, aid)
    j = env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": False}, headers=A).json()
    assert j["state"] == "discarded" and ("discard",) in env.be.calls
    err(env.get(j["result"]["chunks"][0]["uri"]), 404, "JOB_NOT_FOUND")          # 되돌리면 staging 도 지운다
    jid = to_3d(env, aid, 1)
    env.clock.t += 1799
    env.eng.tick()
    assert job(env, jid)["state"] == "awaiting_3d_review"
    env.clock.t += 2
    env.eng.tick()
    assert job(env, jid)["state"] == "discarded"
    assert env.get("/app/v1/qr/booth-07", headers=A).json()["version"] == 1


def test_timeout(env):
    aid = make_asset(env)
    jid = edit(env, aid).json()["job_id"]
    j = env.eng.get(jid)
    env.eng._set(j, "refining", "refining")
    env.clock.t += 601
    env.eng.tick()
    assert job(env, jid)["error_code"] == "TIMEOUT"


# ── 잠금·판본·검사 순서 (1.0.1 3.3) ────────────────────────────────
def test_lock_version_order(env):
    aid = make_asset(env)
    jid = edit(env, aid).json()["job_id"]
    b = err(edit(env, aid), 409, "ASSET_LOCKED")
    assert b["detail"]["job_id"] == jid
    err(edit(env, aid, v=7), 409, "ASSET_LOCKED")                                 # 잠금 > 판본
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    err(edit(env, aid, dev=B), 409, "UNCOMMITTED_PENDING")
    err(edit(env, aid, v=7), 409, "UNCOMMITTED_PENDING")
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    b = err(edit(env, aid, v=1), 409, "VERSION_CONFLICT")
    assert b["detail"]["latest_version"] == 2
    err(edit(env, "a-missing"), 404, "ASSET_NOT_FOUND")


def test_input_errors_first(env):
    aid = make_asset(env)
    err(edit(env, "a-missing", volume_cells=[]), 400, "MASK_EMPTY")                # 400 > 404
    err(edit(env, aid, volume_cells=[[i % 64, (i // 64) % 64, i // 4096] for i in range(65537)]), 400, "MASK_TOO_LARGE")
    err(edit(env, aid, camera={**CAM, "position": [0, 0, 0.1]}), 400, "INVALID_CAMERA")
    err(edit(env, aid, camera={**CAM, "up": [0, 1, 0]}), 400, "INVALID_CAMERA")
    err(edit(env, aid, camera={**CAM, "frame": "unity"}), 400, "INVALID_CAMERA")
    err(env.post(f"/app/v1/assets/{aid}/edits", json={"base_version": 1}, headers=A), 400, "INVALID_REQUEST")


def test_read_only_sample(env):
    env.eng.db.x("INSERT INTO assets(asset_id,is_sample,version,sample_name,created_at) VALUES('sample-x',1,1,'x',0)")
    env.eng.store.put_version("sample-x", 1, {"8_8_8": b"\0"})
    err(edit(env, "sample-x"), 403, "READ_ONLY")
    s = env.get("/app/v1/samples").json()["samples"]
    assert s == [{"name": "x", "asset_id": "sample-x", "versions": [1], "note": ""}]


# ── 평가 ───────────────────────────────────────────────────────────
def test_ratings(env):
    aid = make_asset(env)
    err(env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 4}, headers=A), 409, "NOTHING_TO_RATE")
    jid = to_3d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    q = env.get("/app/v1/qr/booth-07", headers=B).json()
    assert q["can_rate"] is True and q["rate_range"] == [2, 2]
    r = env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 4}, headers=A)
    assert r.status_code == 201
    b = r.json()
    assert (b["value"], b["decimals"], b["tag1"], b["version_range"]) == (80, 0, "starred", [2, 2])
    f = env.get(b["feedback_uri"])
    assert hashlib.sha256(f.content).hexdigest() == b["feedback_hash"] and f.json()["value"] == 80
    err(env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 4}, headers=A), 409, "NOTHING_TO_RATE")
    assert env.get("/app/v1/qr/booth-07", headers=A).json()["can_rate"] is False
    jid = to_3d(env, aid, 2)
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    assert env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 1}, headers=A).json()["version_range"] == [3, 3]
    assert env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 5}, headers=B).json()["version_range"] == [2, 3]
    err(env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 6}, headers=A), 400, "INVALID_REQUEST")


# ── 삭제 ───────────────────────────────────────────────────────────
def test_delete_keeps_ratings(env):
    aid = make_asset(env)
    jid = to_3d(env, aid)
    err(env.delete("/app/v1/qr/booth-07", headers=A), 409, "ASSET_LOCKED")
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    rid = env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 3}, headers=B).json()["rating_id"]
    err(env.delete("/app/v1/qr/booth-07", headers=B), 403, "NOT_CREATOR")
    assert env.delete("/app/v1/qr/booth-07", headers=A).status_code == 204
    err(env.get("/app/v1/qr/booth-07", headers=A), 404, "QR_EMPTY")
    err(env.get(f"/app/v1/jobs/{jid}", headers=A), 404, "JOB_NOT_FOUND")
    err(env.get(f"/v2/assets/{aid}/manifest.v2.json"), 404, "ASSET_NOT_FOUND")
    row = env.eng.db.q("SELECT asset_id, asset_deleted FROM ratings WHERE rating_id=?", (rid,))[0]
    assert (row["asset_id"], row["asset_deleted"]) == (aid, 1)
    assert env.get(f"/app/v1/ratings/{rid}.json").status_code == 200
    err(env.delete("/app/v1/qr/booth-07", headers=A), 404, "QR_EMPTY")
    assert make_asset(env) != aid                                                    # QR 다시 비었다


# ── 재기동 ─────────────────────────────────────────────────────────
def test_restart_marks_running_failed(tmp_path):
    clock = Clock()
    app = create_app(_settings(tmp_path), backend=FakeBackend(), clock=clock, start_thread=False, llm=FakeAdapter())
    c = TestClient(app)
    r = c.post("/app/v1/qr/q9/asset", json={"prompt": "x"}, headers=A)      # 큐에만 있고 실행 전
    app2 = create_app(_settings(tmp_path), backend=FakeBackend(), clock=clock, start_thread=False, llm=FakeAdapter())
    c2 = TestClient(app2)
    j = c2.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()
    assert j["state"] == "failed" and j["error_code"] == "SERVER_RESTART"
    err(c2.get("/app/v1/qr/q9", headers=A), 404, "QR_EMPTY")


# ── 가짜 모드 ──────────────────────────────────────────────────────
def test_mock_mode(tmp_path):
    from server.app.store import ChunkStore
    from server.tests.app_fakes import cbin, png

    st = ChunkStore(tmp_path)
    st.put_version("mock-src", 1, {k: cbin(k) for k in BASE_KEYS})
    st.put_version("mock-src", 2, {"8_8_8": cbin("8_8_8", 1)}, removed=["9_8_8"])
    fx = tmp_path / "mock_fixtures"
    fx.mkdir()
    for n in ("render", "mask", "edit"):
        (fx / f"{n}.png").write_bytes(png(8, 8))
    app = create_app(_settings(tmp_path, mock=True), start_thread=False, llm=FakeAdapter())
    c = TestClient(app)
    eng = app.state.app.engine
    assert c.get("/app/v1/health").json()["mock"] is True
    aid = c.post("/app/v1/qr/m1/asset", json={"prompt": "jeep"}, headers=A).json()["asset_id"]
    eng.drain()
    assert sorted(c.get(f"/v2/assets/{aid}/manifest.v1.json").json()["chunks"]) == BASE_KEYS
    for k in range(1, 4):                                                          # 원천 끝을 넘으면 마지막 변경분 반복
        jid = c.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": k, "instruction": "x",
                     "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
        eng.drain()
        assert c.get(f"/app/v1/jobs/{jid}", headers=A).json()["preview"]["attempt"] == 1
        c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
        eng.drain()
        j = c.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A).json()
        assert j["state"] == "committed" and j["result"]["removed_chunk_ids"] == ["9_8_8"]
    jid = c.post(f"/app/v1/assets/{aid}/edits", headers={**A, "X-Mock-Fail": "INPAINT_FAILED"},
                 json={"base_version": 4, "instruction": "x", "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
    eng.drain()
    j = c.get(f"/app/v1/jobs/{jid}", headers=A).json()
    assert j["state"] == "failed" and j["error_code"] == "INPAINT_FAILED"


# ── S3: 보관(1.0.2 9절)·/clients ───────────────────────────────────
def test_soft_delete_archives_and_hides(env):
    aid = make_asset(env)
    jid = to_3d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": False}, headers=A)        # 되돌림 → archive
    jid2 = to_3d(env, aid, 1)
    env.post(f"/app/v1/jobs/{jid2}/review3d", json={"keep": True}, headers=A)
    rid = env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 5}, headers=A).json()["rating_id"]
    assert env.delete("/app/v1/qr/booth-07", headers=A).status_code == 204
    # 앱 API: 1.0.1 과 같은 404 응답
    for url, code in ((f"/app/v1/qr/booth-07", "QR_EMPTY"), (f"/app/v1/jobs/{jid2}", "JOB_NOT_FOUND"),
                      (f"/app/v1/jobs/{jid2}/preview/1/edit.png", "JOB_NOT_FOUND"),
                      (f"/v2/assets/{aid}/manifest.v2.json", "ASSET_NOT_FOUND"),
                      (f"/v2/assets/{aid}/slat_coords.v1.json", "ASSET_NOT_FOUND")):
        b = err(env.get(url, headers=A), 404, code)
        assert set(b) <= {"error_code", "message", "detail"}
    err(edit(env, aid, v=2), 404, "ASSET_NOT_FOUND")
    err(env.post(f"/app/v1/assets/{aid}/ratings", json={"stars": 1}, headers=A), 404, "ASSET_NOT_FOUND")
    # 서버 안 보관: 판본·작업·미리보기·archive·평가가 남는다
    assert sorted(env.eng.store.versions(aid)) == [1, 2]
    assert (env.tmp / "jobs" / jid2 / "1" / "edit.png").is_file()
    assert any((env.tmp / "archive" / jid).glob("*.cbin"))
    assert env.get(f"/app/v1/ratings/{rid}.json").status_code == 200
    d = env.get(f"/clients/api/assets/{aid}").json()
    assert d["status"] == "삭제됨" and d["versions"] == [1, 2] and {j["outcome"] for j in d["jobs"]} >= {"유지", "되돌리기"}
    g = env.get(f"/clients/glb/archive/{jid}.glb")                                 # 되돌린 결과 3D
    assert g.status_code == 200 and g.content[:4] == b"glTF"
    assert env.get(f"/clients/glb/{aid}/v2.glb").content[:4] == b"glTF"
    # 같은 QR 재생성 → 목록 2행
    env.clock.t += 10
    aid2 = make_asset(env)
    rows = [r for r in env.get("/clients/api/assets").json() if r["qr_id"] == "booth-07"]
    assert [r["asset_id"] for r in rows] == [aid2, aid] and [r["status"] for r in rows] == ["활성", "삭제됨"]


def test_clients_cidr(env, monkeypatch):
    monkeypatch.setenv("CLIENTS_ALLOW_CIDR", "10.0.0.0/8")
    err(env.get("/clients/api/assets"), 403, "NOT_OWNER")                           # TestClient 주소는 testclient
    monkeypatch.setenv("CLIENTS_ALLOW_CIDR", "")
    assert env.get("/clients/api/assets").status_code == 200
    assert env.get("/clients").status_code == 200


# ── S5b: 미리보기는 저장본 그대로(app-api 1.0.4, V1 은 앱이 (x, z, y) 로 처리) ──
def test_preview_bytes_equal_stored(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    for n in ("render", "edit", "mask"):
        p = env.tmp / "jobs" / jid / "1" / f"{n}.png"
        assert env.get(f"/app/v1/jobs/{jid}/preview/1/{n}.png").content == p.read_bytes()


# ── S7: 마스크 주변만 잘라 인페인팅(APP_INPAINT_CROP, 기본 0 — 사용자 결정) ─────
def _small_mask_render(env):
    import numpy as np
    from server.app.cropinpaint import encode_png
    m = np.zeros((1024, 1024), np.uint8)
    m[500:560, 400:460] = 255
    r = np.full((1024, 1024, 3), 90, np.uint8)
    seen = []
    orig = env.be.inpaint

    def inpaint(rp, mp, prompt, seed, info=None):
        seen.append(mp)
        return orig(rp, mp, prompt, seed, info=info)
    env.be.render = lambda *a: {"render_id": "r-s7", "render_png": encode_png(r), "mask_png": encode_png(m), "record": {}}
    env.be.inpaint = inpaint
    return r, m, seen


def test_crop_inpaint_off_by_default():
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("APP_INPAINT_CROP", None)
        assert Settings.from_env().inpaint_crop is False                         # 기본 0
        os.environ["APP_INPAINT_CROP"] = "1"
        assert Settings.from_env().inpaint_crop is True


def test_crop_inpaint_when_on(env):
    import numpy as np
    from server.app.cropinpaint import decode_png
    env.eng.s.inpaint_crop = True                                                # APP_INPAINT_CROP=1
    r, m, seen = _small_mask_render(env)
    aid = make_asset(env)
    jid = to_2d(env, aid)
    a = env.eng.get(jid)["attempts"][0]
    assert a["crop"]["box"] == [238, 338, 384] and a["inpaint_s"] >= 0
    assert decode_png(seen[0]).mean() > m.mean()                                 # 워커에는 키운 마스크
    out = decode_png(env.get(f"/app/v1/jobs/{jid}/preview/1/edit.png", headers=A).content)
    assert out.shape == (1024, 1024, 3) and (out[m == 0] == 90).all()            # 마스크 밖 원본 그대로
    assert (out[520:540, 420:440] == 200).all()                                  # 안쪽은 워커 결과(가짜 = 200)
    d = env.get(f"/clients/api/assets/{aid}").json()
    ca = [x for x in d["jobs"] if x["job_id"] == jid][0]["attempts"][0]
    assert ca["crop"]["box"] == [238, 338, 384]
    for n in ("crop_render", "crop_mask", "crop_edit"):
        assert env.get(ca["images"][n]).status_code == 200


def test_crop_inpaint_off_is_old_behavior(env):
    assert env.eng.s.inpaint_crop is False                                       # 기본 0 = 예전 동작
    r, m, seen = _small_mask_render(env)
    aid = make_asset(env)
    jid = to_2d(env, aid)
    a = env.eng.get(jid)["attempts"][0]
    assert a["crop"] == {"off": True}
    from server.tests.app_fakes import png
    assert seen[0] == (env.tmp / "jobs" / jid / "render" / "mask.png").read_bytes()   # 원래 마스크 그대로 보냄
    assert env.get(f"/app/v1/jobs/{jid}/preview/1/edit.png", headers=A).content == png(v=200)  # 워커 결과 그대로


# ── S9: /clients 실제 입력 전문 ─────────────────────────────────────
def test_clients_raw_fields(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    d = env.get(f"/clients/api/assets/{aid}").json()
    e = [x for x in d["jobs"] if x["job_id"] == jid][0]
    a = e["attempts"][0]["inpaint_raw"]
    assert not a["form"]["prompt"].endswith(INPAINT_SUFFIX) and a["settings"] and a["settings_source"]   # S11 실물형
    assert e["attempts"][0]["style"]["mode"] == "real"
    assert e["raw_edit3d"]["text"].startswith("텍스트 입력 없음") and e["raw_edit3d"]["attempt"] == 1
    assert all("attempts" in c for c in e["llm_calls"])
    full = env.get(e["job_json"]).json()
    assert full["job"]["job_id"] == jid and isinstance(full["llm_calls"], list)
    page = env.get(f"/clients/{aid}").text
    assert "llmRaw" in page and "e3Raw" in page and "inpRaw" in page


def test_clients_page_js_parses(env, tmp_path):
    """S9: 페이지 JS 문법 검사(Python 문자열 안 JS 의 \\n·\\s 이스케이프 실수 방지). node 가 없으면 건너뜀."""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    aid = make_asset(env)
    for url in ("/clients", f"/clients/{aid}"):
        js = "\n;\n".join(re.findall(r"<script>(.*?)</script>", env.get(url).text, re.S))
        f = tmp_path / "p.js"
        f.write_text(js)
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_clients_raw_links_crop_and_refine_retry(env):
    """S9 + S7: 잘라 보냈으면 전문에 자른 입력이 남는다. S9 + S8: 재요청 시도 표시와 llm 호출 기록이 함께 보인다."""
    import hashlib
    env.eng.s.inpaint_crop = True
    _small_mask_render(env)
    aid = make_asset(env)
    jid = to_2d(env, aid)
    d = env.get(f"/clients/api/assets/{aid}").json()
    a = [x for x in d["jobs"] if x["job_id"] == jid][0]["attempts"][0]
    sent = env.get(a["images"]["crop_render"]).content
    assert a["crop"]["box"] and a["inpaint_raw"]["form"]["seed"] == "0"
    assert ("crop_render" in a["images"]) and hashlib.sha256(sent).hexdigest()   # 자른 입력 이미지가 보인다
    page = env.get(f"/clients/{aid}").text
    assert "cropView(a)}${inpRaw(a,j)}" in page and "refine_retry" in page


# ── 사용자 결정(2026-11-12): 기기에서 삭제해도 A5000 워커 저장소까지 보관 ─────────────
def test_delete_keeps_worker_store_by_default(env):
    aid = make_asset(env)
    assert env.delete("/app/v1/qr/booth-07", headers=A).status_code == 204
    assert ("delete",) not in env.be.calls                                    # A5000 DELETE 를 부르지 않는다
    a = env.eng.db.asset(aid)
    assert a["deleted_at"] is not None and a["worker_kept"] == 1
    assert env.eng.store.blobs(aid, 1)                                        # 앱 서버 청크도 그대로
    row = [r for r in env.get("/clients/api/assets").json() if r["asset_id"] == aid][0]
    assert row["status"] == "삭제됨" and row["worker_kept"] is True


def test_delete_worker_store_when_enabled(env):
    env.eng.s.worker_delete = True
    aid = make_asset(env)
    assert env.delete("/app/v1/qr/booth-07", headers=A).status_code == 204
    assert ("delete",) in env.be.calls and env.eng.db.asset(aid)["worker_kept"] == 0
