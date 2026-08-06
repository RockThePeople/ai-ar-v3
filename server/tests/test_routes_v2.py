"""`/v2` 라우트 — **회귀를 막는다.**

지금까지 라우트 검증은 전부 살아 있는 서버에 curl 을 쏜 것이었다. 그건 그 순간의
사실이고 회귀를 못 막는다 — 다음 세션이 규약을 바꿔도 아무 증상이 없다.

여기서 잠그는 것:
  ① 계약 스키마가 요청을 거른다 — `grid_source` 생략은 **422** (D28-a)
  ② 판본이 없으면 **404**. 옛 판본으로 대신 답하지 않는다
  ③ 매니페스트가 `chunk_size`·`chunk_grid_res` 를 싣는다 (assert_contract_compatible 이 그걸 본다)
  ④ 없는 잡은 **404** — "실패" 가 아니라 "없다"
  ⑤ 청크 URI 는 계약 함수가 만든 모양 그대로 파싱된다

⚠️ 상류(A5000)·GPU·t2i 를 쓰지 않는다. 리포에 커밋된 실물 자산만 읽는다.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from deltacontract import CONTRACT_CONSTANTS  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
ASSET = "v3-moto-b"


@pytest.fixture(scope="module")
def client():
    from server.skeleton import app

    return TestClient(app)


@pytest.fixture(scope="module")
def mask_body():
    p = REPO / "handoff/lasso/moto-b.rear-wheel.mask.json"
    if not p.is_file():
        pytest.skip("실자산 마스크가 리포에 없다")
    return json.loads(p.read_text())["mask"]


def _has_assets() -> bool:
    return (REPO / "assets" / "moto-b" / "parent").is_dir()


needs_assets = pytest.mark.skipif(not _has_assets(), reason="리포에 실자산이 없다")


# ══════════════════════════════════════════ ③ 매니페스트가 계약을 싣는다
@needs_assets
def test_manifest_carries_chunk_size_and_grid(client):
    r = client.get(f"/v2/assets/{ASSET}/manifest.v1.json")
    assert r.status_code == 200, r.text
    c = r.json()["contract"]
    # 🔴 받는 쪽 `assert_contract_compatible` 이 **이 필드로** 거부한다.
    assert c["chunk_size"] == CONTRACT_CONSTANTS["chunk_size"]
    assert c["chunk_grid_res"] == CONTRACT_CONSTANTS["chunk_grid_res"]
    assert c["contract_version"] == CONTRACT_CONSTANTS["contract_version"]


@needs_assets
def test_unknown_asset_and_version_are_404(client):
    assert client.get("/v2/assets/v3-nope/manifest.v1.json").status_code == 404
    # 판본이 없으면 **옛것으로 대신 답하지 않는다** — 다른 물체가 된다.
    assert client.get(f"/v2/assets/{ASSET}/manifest.v99.json").status_code == 404


# ══════════════════════════════════════════ ⑤ 청크 전송
@needs_assets
def test_chunk_round_trips_and_matches_manifest(client):
    m = client.get(f"/v2/assets/{ASSET}/manifest.v1.json").json()
    key = sorted(m["chunks"])[0]
    entry = m["chunks"][key]
    r = client.get(entry["uri"])
    assert r.status_code == 200
    assert len(r.content) == entry["byte_length"]

    import hashlib

    assert hashlib.sha256(r.content).hexdigest() == entry["hash"]
    from server.contractguard import chunk_contract_version

    assert chunk_contract_version(r.content) == CONTRACT_CONSTANTS["contract_version"]


@needs_assets
def test_malformed_chunk_name_is_400_not_500(client):
    r = client.get(f"/v2/assets/{ASSET}/chunks/not-a-key.cbin")
    assert r.status_code == 400, r.text


@needs_assets
def test_missing_chunk_is_404(client):
    assert client.get(f"/v2/assets/{ASSET}/chunks/15_15_15.v1.cbin").status_code == 404


# ══════════════════════════════════════════ ① 스키마가 거른다 (D28-a)
@needs_assets
def test_edit_without_grid_source_is_422(client, mask_body):
    """🔴 서버가 `grid_source` 를 **채워 넣지 않는다.** 스키마가 거부해야 한다."""
    bad = {**mask_body}
    bad.pop("grid_source", None)
    r = client.post(f"/v2/assets/{ASSET}/edits", json={
        "session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 파랑으로",
        "mask": bad})
    assert r.status_code == 422, r.text


@needs_assets
def test_edit_with_surface_grid_source_is_422(client, mask_body):
    """진단용 격자(surface_voxelize)로 만든 마스크는 편집에 못 쓴다 (D28)."""
    r = client.post(f"/v2/assets/{ASSET}/edits", json={
        "session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 파랑으로",
        "mask": {**mask_body, "grid_source": "surface_voxelize"}})
    # 스키마가 통과시키면 `require_slat_grid()` 가 잡는다 — 둘 중 하나는 막아야 한다.
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        job = client.get(f"/v2/jobs/{r.json()['job_id']}").json()
        for _ in range(40):
            if job["state"] in ("succeeded", "failed"):
                break
            import time

            time.sleep(0.25)
            job = client.get(f"/v2/jobs/{r.json()['job_id']}").json()
        assert job["state"] == "failed", "진단용 격자가 편집을 통과했다"


@needs_assets
def test_edit_on_unknown_asset_is_404(client, mask_body):
    r = client.post("/v2/assets/v3-nope/edits", json={
        "session_id": "t", "base_version": 1, "raw_prompt": "파랑으로", "mask": mask_body})
    assert r.status_code == 404


@needs_assets
def test_edit_on_missing_base_version_is_409(client, mask_body):
    r = client.post(f"/v2/assets/{ASSET}/edits", json={
        "session_id": "t", "base_version": 99, "raw_prompt": "파랑으로", "mask": mask_body})
    assert r.status_code == 409, r.text


# ══════════════════════════════════════════ ④ 폴링
def test_unknown_job_is_404_not_failed(client):
    """잡은 인메모리다. 재시작 뒤 폴링은 **'없다'** 이지 '실패' 가 아니다."""
    r = client.get("/v2/jobs/j-does-not-exist")
    assert r.status_code == 404


def test_job_states_are_contract_literals():
    from deltacontract.schemas import JobStatus

    lit = JobStatus.model_fields["state"].annotation
    # ⚠️ 계약의 상태는 **넷**이다. W26 배선 문서에 다섯("cancelled" 포함)이라고
    #    적었는데 틀렸다 — 계약을 안 열고 기억으로 썼다. 여기서 계약을 정본으로 잠근다.
    assert set(getattr(lit, "__args__", ())) == {
        "queued", "running", "succeeded", "failed"}
