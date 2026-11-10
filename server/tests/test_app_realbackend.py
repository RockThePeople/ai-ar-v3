"""RealBackend ↔ 가짜 A5000·인페인팅 HTTP 워커. 호출 순서·형식을 잠근다(A1·A2 전 시험)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.app.config import Settings
from server.app.main import create_app
from server.tests.app_fakes import BASE_KEYS, FakeWorkers
from server.tests.test_app_server import A, CAM, CELLS


@pytest.fixture
def fw():
    w = FakeWorkers()
    yield w
    w.close()


def _app(tmp_path, fw, mode="camera"):
    s = Settings(data_dir=tmp_path, mock=False, a5000_url=f"http://127.0.0.1:{fw.port}", a5000_key="",
                 inpaint_port=fw.port, shared_token="", bind_host="127.0.0.1", bind_port=0, render_mode=mode,
                 mock_time_scale=0.0, mock_source="x")
    app = create_app(s, start_thread=False)
    c = TestClient(app)
    c.eng = app.state.app.engine
    return c


def test_generate_swap_order_and_edit(tmp_path, fw, monkeypatch):
    from server import t2i

    def fake_rgba(prompt, seed=42, keep_dir=None):
        assert fw.gpu_loaded is False, "T2I 는 인페인팅 워커를 내린 뒤에 돈다"
        fw.log.append(f"t2i {prompt} {seed}")
        return b"\x89PNG fake", {"t2i_s": 1.0}

    monkeypatch.setattr(t2i, "render_rgba", fake_rgba)
    c = _app(tmp_path, fw)
    assert c.get("/app/v1/health").json()["workers"] == {"a5000": "up", "inpaint": "up", "llm": "fake"}
    aid = c.post("/app/v1/qr/q1/asset", json={"prompt": "a jeep", "seed": 7}, headers=A).json()["asset_id"]
    c.eng.drain()
    seq = [x for x in fw.log if x.startswith(("POST /release", "t2i", "POST /reload", "POST /v2/trellis/generate"))]
    assert seq == ["POST /release", "t2i a jeep 7", "POST /reload", "POST /v2/trellis/generate"]
    m = c.get(f"/v2/assets/{aid}/manifest.v1.json").json()
    assert sorted(m["chunks"]) == BASE_KEYS
    assert c.get(f"/v2/assets/{aid}/slat_coords.v1.json").json()["coords"] == [[5, 5, 5], [6, 5, 5]]

    jid = c.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "make it red",
                 "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
    c.eng.drain()
    j = c.get(f"/app/v1/jobs/{jid}", headers=A).json()
    assert j["state"] == "awaiting_2d_review", j
    assert "render camera=True view=False" in fw.log
    assert "view" not in j and '"view"' not in json.dumps(j)                  # view=null 은 클라이언트에 안 나간다
    rec = c.eng.get(jid)["render"]["record"]
    assert rec["view"] is None and rec["camera_used"]["fov_deg"] == 49.1
    assert any(x.startswith("inpaint prompt=") and x.endswith("seed=0") for x in fw.log)
    c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    c.eng.drain()
    j = c.get(f"/app/v1/jobs/{jid}", headers=A).json()
    assert j["state"] == "awaiting_3d_review" and j["result"]["removed_chunk_ids"] == ["9_8_8"]
    c.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": True}, headers=A)
    assert any(x.endswith(f"/v2/trellis/assets/{aid}/commit") for x in fw.log)
    assert sorted(c.get(f"/v2/assets/{aid}/manifest.v2.json").json()["chunks"]) == ["8_8_8", "8_8_9"]
    assert c.delete("/app/v1/qr/q1", headers=A).status_code == 204
    assert f"DELETE /v2/trellis/assets/{aid}" in fw.log


def test_reload_even_when_t2i_fails(tmp_path, fw, monkeypatch):
    from server import t2i

    def boom(prompt, seed=42, keep_dir=None):
        raise t2i.T2IUnavailable("boom")

    monkeypatch.setattr(t2i, "render_rgba", boom)
    c = _app(tmp_path, fw)
    jid = c.post("/app/v1/qr/q2/asset", json={"prompt": "x"}, headers=A).json()["job_id"]
    c.eng.drain()
    j = c.get(f"/app/v1/jobs/{jid}", headers=A).json()
    assert j["error_code"] == "T2I_FAILED" and fw.gpu_loaded is True and "POST /reload" in fw.log


def test_view_mode_and_discard(tmp_path, fw, monkeypatch):
    from server import t2i
    monkeypatch.setattr(t2i, "render_rgba", lambda p, seed=42, keep_dir=None: (b"x", {}))
    c = _app(tmp_path, fw, mode="view")
    aid = c.post("/app/v1/qr/q3/asset", json={"prompt": "x"}, headers=A).json()["asset_id"]
    c.eng.drain()
    jid = c.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "x",
                 "camera": CAM, "volume_cells": CELLS}).json()["job_id"]
    c.eng.drain()
    assert "render camera=False view=True" in fw.log
    c.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    c.eng.drain()
    c.post(f"/app/v1/jobs/{jid}/review3d", json={"keep": False}, headers=A)
    assert any(x.endswith(f"/v2/trellis/assets/{aid}/discard") for x in fw.log)


def test_worker_unreachable(tmp_path):
    s = Settings(data_dir=tmp_path, mock=False, a5000_url="http://127.0.0.1:1", a5000_key="", inpaint_port=1,
                 shared_token="", bind_host="127.0.0.1", bind_port=0, render_mode="camera", mock_time_scale=0.0,
                 mock_source="x")
    app = create_app(s, start_thread=False)
    c = TestClient(app)
    assert c.get("/app/v1/health").json()["workers"]["a5000"] == "down"
    jid = c.post("/app/v1/qr/q4/asset", json={"prompt": "x"}, headers=A).json()["job_id"]
    app.state.app.engine.drain()
    assert c.get(f"/app/v1/jobs/{jid}", headers=A).json()["error_code"] == "WORKER_UNREACHABLE"
