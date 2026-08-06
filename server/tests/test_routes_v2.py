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


@pytest.fixture(scope="module", autouse=True)
def _no_execution():
    """라우트 접수만 잰다 — 잡 실행은 GPU·LLM 을 태운다."""
    import os

    old = os.environ.get("JOBS_EXECUTE")
    os.environ["JOBS_EXECUTE"] = "0"
    yield
    if old is None:
        os.environ.pop("JOBS_EXECUTE", None)
    else:
        os.environ["JOBS_EXECUTE"] = old


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
def test_surface_grid_source_is_refused_before_any_edit_runs(client, mask_body):
    """진단용 격자(surface_voxelize)로 만든 마스크는 편집에 못 쓴다 (D28).

    ⚠️ 잡을 돌려서 확인하지 않는다 — 그러면 GPU·LLM 을 태우고, 비동기라 타이밍에
       기대게 된다. 방어가 **동기 지점**에 있는지를 직접 잰다: 스키마가 막거나,
       `build_mask(...).require_slat_grid()` 가 막거나 둘 중 하나여야 한다.
    """
    import numpy as np

    from server.pipeline.frames import assert_slat_grid
    from server.pipeline.mask import build_mask

    r = client.post(f"/v2/assets/{ASSET}/edits", json={
        "session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 파랑으로",
        "mask": {**mask_body, "grid_source": "surface_voxelize"}})
    if r.status_code == 422:
        return                                    # 스키마가 막았다

    # 스키마가 통과시켰다면 편집 경로의 방어가 반드시 막아야 한다.
    m = build_mask(np.asarray(mask_body["voxels"], dtype=np.int64),
                   halo=1, grid_source="surface_voxelize")
    with pytest.raises(Exception) as e:
        m.require_slat_grid("테스트")
    assert "slat" in str(e.value).lower()


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
