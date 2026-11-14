"""S20f: 인페인팅을 Gemini 이미지 편집(마스크 없음)으로 — 시점 고르기, 크기 맞추기, REST 요청, 흐름(토글)."""

from __future__ import annotations

import base64
import json

import httpx
import numpy as np
import pytest

from server.app import automask as am
from server.app.cropinpaint import decode_png, encode_png
from server.app.gemini import GeminiClient, GeminiError, fit_to, full_prompt, outside_diff
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture

PASS = {"element": True, "position": True, "mask_fit": True, "shape_3d": True}


def _cam0(occ, axis="y"):
    c = am.centers(occ)
    mid = (c.min(0) + c.max(0)) / 2
    return {"frame": "normalized_voxel", "position": [float(mid[0]), float(mid[1] + 1.6), float(mid[2])],
            "forward": [0.0, -1.0, 0.0], "up": [0.0, 0.0, 1.0], "fov_y_deg": 40.0, "aspect": 1.0}


def test_visible_count_hides_back_cells():
    front = [[x, 40, z] for x in range(24, 40) for z in range(20, 40)]
    back = [[x, 20, z] for x in range(24, 40) for z in range(20, 40)]
    occ = front + back
    cam = _cam0(occ)
    assert am.visible_count(back, occ, cam) == 0                       # 앞판 뒤에 숨은 셀은 안 센다
    assert am.visible_count(front, occ, cam) == len(front)
    empty = [[x, 50, 30] for x in range(24, 40)]
    assert am.visible_count(empty, occ, cam) == 0                       # 빈 셀은 안 센다(점유 셀만)


def test_pick_view_max_and_tie_order():
    body = [[x, y, z] for x in range(24, 40) for y in range(28, 36) for z in range(10, 50)]
    arm = [[x, 31, 30] for x in range(40, 50)]                         # +x 쪽 막대
    occ = body + arm
    cams = am.view_cameras(occ)
    side_hidden = [[24, y, 30] for y in range(28, 36)]                  # −x 면(az270 에서 보임)
    pk = am.pick_view(side_hidden, occ, cams, _cam0(occ))
    assert pk["view"] == "az270" and pk["counts"]["az270"] == max(pk["counts"].values())
    pk2 = am.pick_view(arm, occ, cams, cams["az000"])                  # 사용자 카메라 = 최다 시점과 같으면 user 먼저
    assert pk2["counts"]["user"] == pk2["counts"]["az000"] == max(pk2["counts"].values()) and pk2["view"] == "user"


def _png(h, w, v=(10, 200, 30)):
    return encode_png(np.full((h, w, 3), v, np.uint8))


def test_fit_to_and_outside_diff():
    out = decode_png(fit_to(_png(512, 768), 1024, 1024))
    assert out.shape[:2] == (1024, 1024)
    assert (out[0, 0] == 255).all() and (out[512, 512] == (10, 200, 30)).all()   # 가운데 맞춤, 위아래 흰색
    r = np.full((64, 64, 3), 100, np.uint8)
    e = r.copy()
    e[:32] = 140                                                        # 위 절반이 바뀜
    m = np.zeros((64, 64), np.uint8)
    m[:32] = 255                                                        # 바뀐 곳 = 마스크 안
    d = outside_diff(encode_png(r), encode_png(e), encode_png(m))
    assert d["mean_outside"] == 0.0
    m2 = np.zeros((64, 64), np.uint8)
    d2 = outside_diff(encode_png(r), encode_png(e), encode_png(m2))
    assert d2["mean_outside"] == 20.0 and d2["frac_outside_changed_gt20"] == 0.5


def _transport(capture, status=200, image=True):
    def handler(req: httpx.Request):
        capture.append(req)
        if status != 200:
            return httpx.Response(status, text="quota")
        parts = [{"text": "done"}]
        if image:
            parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_png(1024, 1024)).decode()}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gemini_rest_request_shape_and_errors():
    cap = []
    g = GeminiClient(model="gemini-3-pro-image", key="k-test", http=_transport(cap))
    png, info = g.edit(_png(1024, 1024), "Edit only the hand.")
    req = cap[0]
    body = json.loads(req.content)
    parts = body["contents"][0]["parts"]
    assert "gemini-3-pro-image:generateContent" in str(req.url) and "key=" not in str(req.url)
    assert req.headers["x-goog-api-key"] == "k-test"
    assert [("text" in p, "inline_data" in p) for p in parts] == [(True, False), (False, True)]   # 이미지 1장, 마스크 없음
    assert decode_png(png).shape[:2] == (1024, 1024) and info["model"] == "gemini-3-pro-image"
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        GeminiClient(key="", http=_transport([])).edit(_png(8, 8), "x")
    with pytest.raises(GeminiError, match="429"):
        GeminiClient(key="k", http=_transport([], status=429)).edit(_png(8, 8), "x")
    with pytest.raises(GeminiError, match="이미지가 없다"):
        GeminiClient(key="k", http=_transport([], image=False)).edit(_png(8, 8), "x")
    assert full_prompt("A steel sword", "shield").endswith("plain white background. Output one image.")


class FakeGemini:
    def __init__(self):
        self.calls = []

    def edit(self, image_png, prompt, model=None):
        self.calls.append(prompt)
        return _png(768, 768, (200, 200, 200)), {"model": "fake-gemini", "latency_s": 0.0, "prompt": prompt,
                                                "text": "", "finish": "STOP", "usage": None}


def _lasso(env, aid, text="오른손에 칼을 들게 해줘"):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": 1, "instruction": text, "camera": CAM, "volume_cells": CELLS, "agent": True})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def test_gemini_flow(env):
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.gemini = FakeGemini()
    fake = env.eng.agent_llm
    fake.op = "replace"
    fake.judge_script = [dict(PASS, position=False), PASS]             # 위치 불합격이어도 마스크 없음 → 프롬프트 다시
    j = _lasso(env, aid, "방패를 칼로 바꿔줘")
    assert j["state"] == "committed" and j["agent"]["backtracks"] == 1
    texts = [e["text"] for e in j["events"]]
    assert any("물건이 가장 잘 보이는 방향" in t and "골랐습니다" in t for t in texts)
    assert any("프롬프트 보정부터 다시 합니다" in t for t in texts) and not any("마스크" in t and "다시" in t for t in texts)
    calls = [c[0] for c in env.be.calls]
    assert calls.count("inpaint") == 0 and len(env.eng.gemini.calls) == 2   # 로컬 인페인팅 안 부름
    assert all("Edit only the the old object." in p or "Edit only the" in p for p in env.eng.gemini.calls)
    jj = env.eng.get(j["job_id"])
    a = jj["attempts"][-1]
    assert a["inpaint_src"] == "gemini" and a["inpaint_raw"]["backend"] == "gemini"
    assert "mean_outside" in a["inpaint_raw"]["outside"] and a["inpaint_raw"]["raw_size"] == [768, 768]
    assert jj["agent_pick"]["view"] in ("user", "az000", "az090", "az180", "az270")
    assert jj["cells"] == [list(c) for c in sorted({tuple(c) for c in CELLS})] or len(jj["cells"]) == len(set(map(tuple, CELLS)))
    ed = decode_png((env.eng.s.data_dir / "jobs" / j["job_id"] / str(a["attempt"]) / "edit.png").read_bytes())
    assert ed.shape[:2] == (1024, 1024)                                  # 렌더 크기로 맞춤


def test_default_local_unchanged(env):
    aid = make_asset(env)
    assert env.eng.s.inpaint_backend == "local"
    env.eng.agent_llm.judge_script = [PASS]
    env.eng.gemini = FakeGemini()
    j = _lasso(env, aid)
    assert j["state"] == "committed" and env.eng.gemini.calls == []
    assert [c[0] for c in env.be.calls].count("inpaint") == 1
