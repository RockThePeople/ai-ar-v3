"""앱 서버 v1 — 상태 전이·잠금·판본·권한·평가·삭제·오류 모양 (app-api 1.0.1). GPU·네트워크 없음."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from server.app.config import Settings
from server.app.main import create_app
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
    app = create_app(_settings(tmp_path), backend=be, clock=clock, start_thread=False)
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
    assert h["app_api"] == "1.0.1" and h["ok"] is True and h["contract_version"] == 4 and h["mock"] is False
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
    assert ("inpaint_args", j["refine"]["new_scene_prompt"], 0) in env.be.calls


def test_reject_quality_prompt_then_retry_limit(env):
    aid = make_asset(env)
    jid = to_2d(env, aid)
    n_render = sum(1 for c in env.be.calls if c == ("render",))
    j = env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": False, "reason": "the helmet looks blurry"}, headers=A).json()
    assert j["state"] == "inpainting" and j["review"]["category"] == "quality" and j["attempt"] == 2
    env.eng.drain()
    assert sum(1 for c in env.be.calls if c == ("render",)) == n_render          # 품질 재시도는 렌더 재사용
    assert ("inpaint_args", job(env, jid)["refine"]["new_scene_prompt"], 1) in env.be.calls   # seed = 0 + attempt − 1
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
    app = create_app(_settings(tmp_path), backend=FakeBackend(), clock=clock, start_thread=False)
    c = TestClient(app)
    r = c.post("/app/v1/qr/q9/asset", json={"prompt": "x"}, headers=A)      # 큐에만 있고 실행 전
    app2 = create_app(_settings(tmp_path), backend=FakeBackend(), clock=clock, start_thread=False)
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
    app = create_app(_settings(tmp_path, mock=True), start_thread=False)
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
