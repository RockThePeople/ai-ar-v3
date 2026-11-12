"""S10 — A5000 폴링 연결 오류 재시도와 접수·폴링 기록. 가짜 A5000 = httpx.MockTransport."""
from __future__ import annotations

import httpx
import pytest

from server.app.backends import RealBackend, StageError
from server.tests.test_app_server import A, _settings, env, make_asset, to_2d  # noqa: F401

RUNNING = {"job_id": "wj", "state": "running", "stage": "repaint", "progress": 0.4, "asset_id": "a1"}
DONE = {"asset_id": "a1", "to_version": 2, "chunks": [{"chunk_id": "8_8_8"}], "removed_chunk_ids": [], "stats": {"repaint_ms": 1}}
RENDER = {"render_id": "r1", "render_png": b"R", "mask_png": b"M"}


class FakeA5000:
    """폴링 응답을 차례로 낸다. 항목이 예외 클래스면 그 예외(RST 흉내), (status, body) 면 그 응답."""

    def __init__(self, polls, post=None):
        self.polls, self.post, self.calls = list(polls), post, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        if req.method == "POST":
            if isinstance(self.post, type) and issubclass(self.post, Exception):
                raise self.post("connect failed", request=req)
            return httpx.Response(202, json={"job_id": "wj"})
        if "/chunks/" in req.url.path:
            return httpx.Response(200, content=b"CBIN")
        item = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("[Errno 104] Connection reset by peer", request=req)
        status, body = item
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body or "")


@pytest.fixture
def be(tmp_path, monkeypatch):
    b = RealBackend(_settings(tmp_path), None)
    clock = {"t": 1000.0, "sleeps": []}

    def sleep(s):
        clock["sleeps"].append(s)
        clock["t"] += s
    b._now = lambda: clock["t"]
    b._sleep = sleep
    b.clock = clock
    b.fake = None
    monkeypatch.setattr(b, "_a", lambda timeout=120.0: httpx.Client(transport=httpx.MockTransport(b.fake),
                                                                     base_url="http://a5000"))
    return b


def _edit(b, trace):
    return b.edit3d("a1", 1, RENDER, b"E", [[1, 2, 3]], "idem-1", lambda *a: None, trace=trace)


def test_rst_once_then_recovers(be):
    be.fake = FakeA5000([(200, RUNNING), httpx.ReadError, (200, RUNNING), (200, DONE)])
    trace = {}
    out = _edit(be, trace)
    assert out["to_version"] == 2 and out["blobs"] == {"8_8_8": b"CBIN"}
    ps = trace["polls"]
    assert [p.get("status") for p in ps] == [200, None, 200, 200]
    assert "ConnectionReset" in ps[1]["error"] or "Connection reset" in ps[1]["error"]
    assert ps[1]["retry_wait_s"] == 1.0 and ps[2]["recovered_after_s"] == 1.0
    assert trace["submit"]["status"] == 202 and trace["submit"]["response"] == {"job_id": "wj"}
    assert trace["result"]["retries"] == 1


def test_disconnected_over_60s_fails(be):
    be.fake = FakeA5000([(200, RUNNING), httpx.ReadError])          # 이후 계속 RST
    trace = {}
    with pytest.raises(StageError) as e:
        _edit(be, trace)
    assert e.value.code == "WORKER_UNREACHABLE" and "연속 실패" in str(e.value)
    waits = [p["retry_wait_s"] for p in trace["polls"] if "retry_wait_s" in p]
    assert waits[:5] == [1.0, 2.0, 4.0, 8.0, 8.0] and sum(waits) >= 60
    assert trace["result"]["failed"] == "WORKER_UNREACHABLE" and trace["result"]["fail_window_s"] > 60


def test_one_ok_resets_the_window(be):
    seq = [(200, RUNNING)] + [httpx.ReadError] * 6 + [(200, RUNNING)] + [httpx.ReadError] * 6 + [(200, DONE)]
    be.fake = FakeA5000(seq)                                           # 끊김 6회(대기 합 31 s) · 정상 1회 · 다시 6회 → 창이 초기화돼 계속
    assert _edit(be, {})["to_version"] == 2


def test_submit_connect_error_fails_without_retry(be):
    be.fake = FakeA5000([(200, DONE)], post=httpx.ConnectError)
    with pytest.raises(StageError) as e:
        _edit(be, {})
    assert e.value.code == "WORKER_UNREACHABLE"
    assert [c for c in be.fake.calls if c[0] == "POST"] == [("POST", "/v2/trellis/edit")]   # 접수 1회만


def test_4xx_fails_immediately(be):
    be.fake = FakeA5000([(404, {"error_code": "NOT_FOUND", "message": "없는 job"})])
    with pytest.raises(StageError) as e:
        _edit(be, {})
    assert e.value.code == "EDIT_FAILED" and be.clock["sleeps"] == []


def test_5xx_with_a5000_error_is_job_failure(be):
    be.fake = FakeA5000([(200, RUNNING), (500, {"error_code": "INTERNAL", "message": "CUDA OOM"})])
    trace = {}
    with pytest.raises(StageError) as e:
        _edit(be, trace)
    assert e.value.code == "EDIT_FAILED" and "CUDA OOM" in str(e.value)
    assert not any("retry_wait_s" in p for p in trace["polls"])


def test_5xx_without_body_is_retried(be):
    be.fake = FakeA5000([(502, ""), (200, DONE)])
    trace = {}
    assert _edit(be, trace)["to_version"] == 2 and trace["polls"][0]["retry_wait_s"] == 1.0


def test_engine_keeps_trace_on_failure_and_clients_shows(env):
    def edit3d(*a, trace=None, **k):
        trace["submit"] = {"t": 1.0, "status": 202, "response": {"job_id": "wj"}}
        trace["polls"] = [{"n": 1, "t": 1.5, "status": 200, "state": "running"},
                          {"n": 2, "t": 2.0, "error": "ReadError: reset", "retry_wait_s": 1.0}]
        trace["result"] = {"failed": "WORKER_UNREACHABLE"}
        raise StageError("WORKER_UNREACHABLE", "잡 폴링: 연속 실패")
    aid = make_asset(env)
    jid = to_2d(env, aid)
    env.be.edit3d = edit3d
    env.post(f"/app/v1/jobs/{jid}/review2d", json={"accept": True}, headers=A)
    env.eng.drain()
    j = env.eng.get(jid)
    assert j["state"] == "failed" and j["a5000_trace"]["edit3d"]["polls"][1]["error"].startswith("ReadError")
    d = env.get(f"/clients/api/assets/{aid}").json()
    e = [x for x in d["jobs"] if x["job_id"] == jid][0]
    assert e["a5000_trace"]["edit3d"]["result"]["failed"] == "WORKER_UNREACHABLE"
    assert "trView" in env.get(f"/clients/{aid}").text
