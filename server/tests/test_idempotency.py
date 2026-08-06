"""`idempotency_key` — **재시도가 같은 키를 내는가.**

난수를 쓰면 재시도가 매번 새 잡이 되고, GPU 를 쓰는 편집에서 그건 곧 중복 실행이다.
계약 3.15.5 의 `derive_idempotency_key` 는 **내용 파생**이라 같은 요청은 같은 키다.

⚠️ 계약이 이 필드를 곧 필수로 바꾼다. 그때도 라우트는 안 깨져야 한다 — 클라가
   보내면 그것을 쓰고, 없으면 유도할 뿐이다.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from server.editreq import derive_idempotency_key  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
ASSET = "v3-moto-b"


@pytest.fixture(scope="module", autouse=True)
def _no_execution():
    """🔴 라우트 접수만 잰다. 잡을 실제로 돌리면 LLM·t2i·상류 GPU 를 태운다."""
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
def mask():
    p = REPO / "handoff/lasso/moto-b.rider-head.mask.json"
    if not p.is_file():
        pytest.skip("실자산 마스크가 리포에 없다")
    return json.loads(p.read_text())["mask"]


needs_assets = pytest.mark.skipif(
    not (REPO / "assets" / "moto-b" / "parent").is_dir(), reason="리포에 실자산이 없다")


# ══════════════════════════════════════════ 파생식 자체
def test_same_content_same_key():
    a = derive_idempotency_key("v3-x", 1, "빨강으로", [[1, 2, 3]], 42)
    b = derive_idempotency_key("v3-x", 1, "빨강으로", [[1, 2, 3]], 42)
    assert a == b, "재시도가 다른 키를 내면 GPU 편집이 중복 실행된다"


@pytest.mark.parametrize("change", ["asset", "version", "prompt", "mask", "seed"])
def test_different_content_different_key(change):
    base = ("v3-x", 1, "빨강으로", [[1, 2, 3]], 42)
    args = list(base)
    args[{"asset": 0, "version": 1, "prompt": 2, "mask": 3, "seed": 4}[change]] = {
        "asset": "v3-y", "version": 2, "prompt": "파랑으로",
        "mask": [[9, 9, 9]], "seed": 7}[change]
    assert derive_idempotency_key(*args) != derive_idempotency_key(*base)


def test_mask_order_does_not_change_the_key():
    """마스크는 정렬 불필요·중복 허용이다 (A5000 EDIT-API-SPEC) — 키가 흔들리면 안 된다."""
    a = derive_idempotency_key("v3-x", 1, "p", [[1, 2, 3], [4, 5, 6]], 42)
    b = derive_idempotency_key("v3-x", 1, "p", [[4, 5, 6], [1, 2, 3], [1, 2, 3]], 42)
    assert a == b


# ══════════════════════════════════════════ 라우트가 실제로 채우는가
@needs_assets
def test_route_derives_key_when_client_omits_it(client, mask):
    body = {"session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 파랑으로",
            "mask": mask}
    r = client.post(f"/v2/assets/{ASSET}/edits", json=body)
    assert r.status_code == 200, r.text
    got = r.json()["idempotency_key"]
    assert got == derive_idempotency_key(
        ASSET, 1, "뒷바퀴를 파랑으로", mask["voxels"], 42), "유도식과 다르다"


@needs_assets
def test_retry_without_key_reuses_the_same_job(client, mask):
    """🔴 키 없이 두 번 보내도 **잡이 하나**여야 한다 — 그게 중복 실행 방지의 전부다."""
    body = {"session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 초록으로",
            "mask": mask}
    a = client.post(f"/v2/assets/{ASSET}/edits", json=body).json()
    b = client.post(f"/v2/assets/{ASSET}/edits", json=body).json()
    assert a["job_id"] == b["job_id"], "재시도가 새 잡을 만들었다 (GPU 중복 실행)"


@needs_assets
def test_client_supplied_key_wins(client, mask):
    """클라가 보낸 키는 **그대로 쓴다** — 서버가 덮으면 클라의 재시도 추적이 깨진다."""
    body = {"session_id": "t", "base_version": 1, "raw_prompt": "뒷바퀴를 노랑으로",
            "mask": mask, "idempotency_key": "client-chosen-key"}
    r = client.post(f"/v2/assets/{ASSET}/edits", json=body)
    assert r.json()["idempotency_key"] == "client-chosen-key"
