"""조건 이미지 경로 (계약 3.28.0 · `server/condrender.py`) — 모의 A5000·모의 인페인팅 워커로 시험한다.

🔴 이 테스트가 증명하는 것은 **3090 쪽이 규약대로 요청한다**는 것이다. A5000 R8a 워커와
   실제로 맞물리는지는 종단 시험(두 장비)에서 따로 잰다. 모의 서버가 규약을 잘못 흉내 내면
   여기는 초록이어도 종단에서 깨진다 — 그래서 모의 서버의 거절 사유도 따로 잠근다(맨 아래).

기본값(VHL_R9 미설정)에서 옛 경로가 그대로인지도 잰다 — 토글이 꺼져 있는데 새 경로를 타면
결과가 바뀌는 항목이 기본값으로 켜진 셈이다.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from deltacontract.schemas import EditMask, EditRequest  # type: ignore[import-not-found]

from server import voxhammer
from server.assetstore import AssetStore
from server.editreq import derive_idempotency_key
from server.tests.mock_condition_upstream import (
    KEY,
    MockA5000,
    MockInpaint,
    moto_b_chunks,
    png,
    png_size,
)
from server.upstreamerr import UpstreamError

ASSET = "v3-moto-b"
PROMPT = "a red and gold Iron Man helmet as the rider's head"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def chunks():
    return moto_b_chunks(3)


@pytest.fixture
def env(monkeypatch, tmp_path, chunks):
    """모의 서버 둘 + 임시 쓰기 루트. 실제 자산 저장소에 쓰지 않는다."""
    with MockA5000(chunks, latest_version=1) as a, MockInpaint() as w:
        monkeypatch.setenv("VHL_R9", "1")
        monkeypatch.setenv("BLOCKEDIT_B_URL", a.url)
        monkeypatch.setenv("BLOCKEDIT_B_KEY", KEY)
        monkeypatch.setenv("INPAINT_PORT", str(w.port))
        monkeypatch.setenv("R9_TIMING_LOG", str(tmp_path / "timing.jsonl"))
        monkeypatch.setattr(voxhammer, "STORE", AssetStore(root=tmp_path / "store"))
        yield SimpleNamespace(a=a, w=w, tmp=tmp_path)


def _req(view=3, seed=42, key="unity-key-1"):
    mask = EditMask(mode="voxels", voxels=[(30, 30, 40), (30, 31, 40)], grid_source="slat_coords")
    return EditRequest(session_id="s", base_version=1, raw_prompt=PROMPT, mask=mask,
                       seed=seed, view=view, idempotency_key=key)


SPEC = SimpleNamespace(op="replace_region", target_prompt=PROMPT)


def _run(req=None):
    stages = []
    out = voxhammer.run_voxhammer_edit(ASSET, req or _req(), SPEC,
                                       lambda p, s="", d="": stages.append(s))
    return out, stages


def _posts(a, path):
    return [c for c in a.calls if c == ("POST", path)]


# ══════════════════════════════════════════════════════════ 정상 경로
def test_render_inpaint_edit_in_order_with_three_condition_images(env, chunks):
    out, stages = _run()
    a, w = env.a, env.w

    # ① → ② → ③ 순서, 각 1회
    assert len(_posts(a, "/v2/trellis/render")) == 1 and len(w.requests) == 1 and len(a.edits) == 1
    first = [s for s in stages if s in ("render", "inpaint", "submit")]
    assert first[:3] == ["render", "inpaint", "submit"]

    rid, rec = next(iter(a.renders.items()))
    edit = a.edits[0]
    # ③ 의 이미지 셋: 렌더·마스크는 ① 의 파일 그대로, 2d_edit 는 ② 의 결과 그대로
    assert _sha(edit["parts"]["2d_render"]) == rec["render_sha256"]
    assert _sha(edit["parts"]["2d_mask"]) == rec["mask_sha256"]
    assert _sha(edit["parts"]["2d_edit"]) == _sha(next(iter(w.out.values())))
    assert "image" not in edit["parts"], "옛 t2i 조건 파트를 같이 실으면 안 된다 (§4 결정 2)"
    meta = edit["meta"]
    assert (meta["render_id"], meta["render_sha256"], meta["mask_sha256"]) == (
        rid, rec["render_sha256"], rec["mask_sha256"])
    # 렌더 요청에 view 와 base_version 이 그대로 간다
    assert rec["view"] == 3 and rec["base_version"] == 1
    # ② 는 편집 프롬프트·시드 0(정오표 E5)·①의 렌더·마스크로 부른다
    r = w.requests[0]
    assert (r["prompt"], r["seed"], r["wait"]) == (PROMPT, 0, True)
    assert (r["image_sha256"], r["mask_sha256"]) == (rec["render_sha256"], rec["mask_sha256"])

    # 결과는 기존 경로와 같은 모양: 새 판본, 청크 전부, 구간 시간이 stage_detail 에
    assert out["patch"].to_version == 2 and set(out["patch"].changed_chunks) == set(chunks)
    assert "①렌더" in out["stage_detail"] and "②인페인팅" in out["stage_detail"]


def test_upstream_idempotency_key_includes_condition_hash(env):
    req = _req()
    _run(req)
    meta = env.a.edits[0]["meta"]
    edit_sha = _sha(env.a.edits[0]["parts"]["2d_edit"])
    want = derive_idempotency_key(ASSET, 1, PROMPT, req.mask.voxels, req.seed, condition_sha256=edit_sha)
    assert meta["idempotency_key"] == want
    # 조건 해시가 없으면 3.15.5 키 — 다른 값이어야 한다(조건이 다르면 다른 편집)
    assert want != derive_idempotency_key(ASSET, 1, PROMPT, req.mask.voxels, req.seed)
    assert meta["idempotency_key"] != req.idempotency_key


def test_condition_hash_is_optional_and_keeps_3_15_5_key():
    """조건 해시를 안 주면 키가 3.15.5 식 그대로다 — Unity·라우트 키가 바뀌지 않는다."""
    from deltacontract import mask_fingerprint  # type: ignore[import-not-found]
    import numpy as np

    cells = [(1, 2, 3), (4, 5, 6)]
    fp = mask_fingerprint(np.unique(np.asarray(cells, dtype=np.int64), axis=0))
    old = hashlib.sha256("|".join([ASSET, "1", PROMPT, fp, "42"]).encode()).hexdigest()[:16]
    assert derive_idempotency_key(ASSET, 1, PROMPT, cells, 42) == old
    k1 = derive_idempotency_key(ASSET, 1, PROMPT, cells, 42, condition_sha256="a" * 64)
    k2 = derive_idempotency_key(ASSET, 1, PROMPT, cells, 42, condition_sha256="b" * 64)
    assert len({old, k1, k2}) == 3


def test_timing_log_records_each_stage(env):
    _run()
    rec = json.loads((env.tmp / "timing.jsonl").read_text().splitlines()[-1])
    for k in ("render_s", "render_fetch_s", "inpaint_s", "submit_s", "condition_upload_bytes"):
        assert k in rec["timings"], k
    assert rec["render_attempts"] == 1 and rec["inpaint_reused"] is False
    assert (rec["view"], rec["view_requested"]) == (3, 3)


# ══════════════════════════════════════════════════════════ view 선택 항목 (§4-4 수정본)
def test_view_omitted_is_sent_as_null_and_server_choice_is_recorded(env):
    """Unity 가 view 를 안 보내면(None) 3090 은 null 로 넘기고, A5000 이 고른 시점을 기록한다."""
    out, _ = _run(_req(view=None))
    assert "view" in env.a.render_requests[0] and env.a.render_requests[0]["view"] is None
    rec = json.loads((env.tmp / "timing.jsonl").read_text().splitlines()[-1])
    assert (rec["view"], rec["view_requested"]) == (env.a.auto_view, None)
    assert f"view {env.a.auto_view} 자동" in out["stage_detail"]
    # 서버가 고른 시점의 렌더가 ②③ 에 그대로 쓰였다
    rid = env.a.edits[0]["meta"]["render_id"]
    assert env.a.renders[rid]["view"] == env.a.auto_view


def test_render_of_another_view_than_requested_is_refused(env):
    env.a.view_offset = 1
    from server.condrender import ConditionFailed

    with pytest.raises(ConditionFailed):
        _run(_req(view=3))
    assert env.w.requests == [] and env.a.edits == []


# ══════════════════════════════════════════════════════════ RENDER_UNKNOWN — ①부터 1회
def test_render_unknown_retries_from_render_once_and_reuses_identical_condition(env):
    env.a.forget_renders = 1
    out, _ = _run()
    assert len(_posts(env.a, "/v2/trellis/render")) == 2 and len(env.a.edits) == 2
    # 다시 렌더한 바이트가 같으면 ②를 다시 돌리지 않는다(INT4 는 같은 시드에서도 달라진다)
    assert len(env.w.requests) == 1
    assert env.a.edits[0]["parts"]["2d_edit"] == env.a.edits[1]["parts"]["2d_edit"]
    assert env.a.edits[1]["meta"]["render_id"] != env.a.edits[0]["meta"]["render_id"]
    assert out["patch"].to_version == 2 and "렌더 2회" in out["stage_detail"]


def test_render_unknown_with_changed_render_inpaints_again(env):
    env.a.forget_renders = 1
    env.a.change_render_on_retry = True
    _run()
    assert len(env.w.requests) == 2
    assert env.w.requests[0]["image_sha256"] != env.w.requests[1]["image_sha256"]


def test_render_unknown_twice_is_reported_not_looped(env):
    env.a.forget_renders = 5
    with pytest.raises(UpstreamError) as ei:
        _run()
    assert ei.value.error_code == "UPSTREAM_RENDER_UNKNOWN"
    assert len(_posts(env.a, "/v2/trellis/render")) == 2, "①부터 1회만 다시 한다 (§4 결정 1)"


# ══════════════════════════════════════════════════════════ 거절·실패는 그대로 보고
def test_render_mismatch_is_reported_without_retry(env):
    env.a.mismatch = True
    with pytest.raises(UpstreamError) as ei:
        _run()
    assert ei.value.error_code == "UPSTREAM_RENDER_MISMATCH"
    assert len(_posts(env.a, "/v2/trellis/render")) == 1 and len(env.a.edits) == 1


def test_render_of_another_version_is_refused_before_inpainting(env):
    env.a.render_base_version_offset = 1
    from server.condrender import ConditionFailed

    with pytest.raises(ConditionFailed):
        _run()
    assert env.w.requests == [] and env.a.edits == []


def test_inpaint_worker_down_fails_without_submitting(env, monkeypatch):
    """🔴 조건을 못 만들면 조건 없이 진행하지 않는다 — t2i 로 몰래 떨어지지도 않는다."""
    import server.t2i as t2i

    monkeypatch.setattr(t2i, "render_rgba", lambda *a, **k: pytest.fail("t2i 로 떨어졌다"))
    monkeypatch.setenv("INPAINT_PORT", "1")          # 아무도 안 듣는 포트
    with pytest.raises(UpstreamError) as ei:
        _run()
    assert ei.value.error_code == "UPSTREAM_UNREACHABLE" and "인페인팅" in str(ei.value)
    assert env.a.edits == []


def test_inpaint_failure_reason_is_propagated(env):
    env.w.fail = True
    with pytest.raises(UpstreamError) as ei:
        _run()
    assert ei.value.error_code == "UPSTREAM_INPAINT_FAILED"
    assert env.a.edits == []


# ══════════════════════════════════════════════════════════ 토글 기본값 꺼짐
def test_default_off_keeps_the_t2i_path(env, monkeypatch):
    monkeypatch.delenv("VHL_R9")
    import server.t2i as t2i

    cond = png((1, 2, 3))
    monkeypatch.setattr(t2i, "render_rgba", lambda *a, **k: (cond, {}))
    with pytest.raises(UpstreamError) as ei:
        _run()
    # 옛 경로는 multipart `meta`+`image` 를 보낸다. 새 A5000(모의)은 조건 3장이 없으니 거절한다.
    assert ei.value.error_code == "UPSTREAM_CONDITION_IMAGES_REQUIRED"
    assert _posts(env.a, "/v2/trellis/render") == [] and env.w.requests == []
    assert env.a.edits[0]["parts"]["image"] == cond


# ══════════════════════════════════════════════════════════ 모의 서버가 §3.3 을 지키는가
@pytest.fixture
def mock_only(chunks):
    with MockA5000(chunks, latest_version=1) as a:
        yield a, httpx.Client(base_url=a.url, headers={"X-Blockedit-Key": KEY})


def _render_once(c):
    mask = {"mode": "voxels", "voxels": [[1, 2, 3]], "grid_source": "slat_coords", "halo_margin_voxels": 2}
    r = c.post("/v2/trellis/render", json={"asset_id": ASSET, "base_version": 1, "mask": mask, "view": 2})
    rec = r.json()
    files = {n: c.get(f"/v2/trellis/renders/{rec['render_id']}/{n}.png").content for n in ("2d_render", "2d_mask")}
    meta = {"asset_id": ASSET, "base_version": 1, "prompt": "p", "mask": mask, "seed": 42,
            "render_id": rec["render_id"], "render_sha256": rec["render_sha256"], "mask_sha256": rec["mask_sha256"]}
    return rec, files, meta


def _edit(c, meta, parts):
    return c.post("/v2/trellis/edit", data={"meta": json.dumps(meta)},
                  files={k: (f"{k}.png", v, "image/png") for k, v in parts.items()})


def test_mock_enforces_rejection_reasons(mock_only):
    a, c = mock_only
    rec, files, meta = _render_once(c)
    edit = png((9, 9, 9))
    assert png_size(files["2d_render"]) == (1024, 1024)

    r = _edit(c, meta, {**files})
    assert (r.status_code, r.json()["error_code"], r.json()["detail"]["missing"]) == (
        422, "CONDITION_IMAGES_REQUIRED", "2d_edit")
    r = _edit(c, meta, {**files, "2d_edit": png((9, 9, 9), size=512)})
    assert (r.status_code, r.json()["error_code"]) == (400, "BAD_CONDITION_IMAGE")
    r = _edit(c, {**meta, "render_id": "nope"}, {**files, "2d_edit": edit})
    assert (r.status_code, r.json()["error_code"]) == (404, "RENDER_UNKNOWN")
    r = _edit(c, meta, {**files, "2d_render": png((1, 1, 1)), "2d_edit": edit})
    assert (r.status_code, r.json()["error_code"]) == (409, "RENDER_MISMATCH")
    r = c.post("/v2/trellis/edit", json=meta)          # 옛 JSON 경로
    assert (r.status_code, r.json()["error_code"]) == (422, "CONDITION_IMAGES_REQUIRED")
    r = _edit(c, meta, {**files, "2d_edit": edit})
    assert r.status_code == 202 and r.json()["job_id"]
