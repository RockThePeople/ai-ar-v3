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


def _lasso(env, aid, text="오른손에 칼을 들게 해줘", base=1):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": base, "instruction": text, "camera": CAM, "volume_cells": CELLS, "agent": True})
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


def test_to_png_converts_jpeg():
    import io
    import shutil
    from server.app.gemini import to_png
    png = _png(16, 16)
    assert to_png(png) == png
    if not shutil.which("/usr/bin/python3"):
        pytest.skip("시스템 파이썬 없음")
    import subprocess
    jpg = subprocess.run(["/usr/bin/python3", "-c", "import sys,io\nfrom PIL import Image\no=io.BytesIO();"
                          "Image.new('RGB',(16,16),(10,200,30)).save(o,'JPEG');sys.stdout.buffer.write(o.getvalue())"],
                         capture_output=True, check=True).stdout
    assert jpg[:2] == b"\xff\xd8"
    out = to_png(jpg)
    assert out[:8] == b"\x89PNG\r\n\x1a\n" and decode_png(out).shape[:2] == (16, 16)
    assert io is not None


# ══════════════════════════════════════════════════════════ 2.6 이미지 차이로 볼륨 넓히기
def _proj_mask(cells, cam, res=1024, r=6):
    m = np.zeros((res, res), bool)
    px, py, _ = am.project(am.centers(cells), cam)
    for x, y in zip(px.astype(int), py.astype(int)):
        m[max(0, y - r):y + r, max(0, x - r):x + r] = True
    return m


def test_grow_from_image_adds_only_empty_cells_touching_lasso():
    body = [[x, 40, z] for x in range(20, 44) for z in range(10, 54)]            # 앞판(몸)
    rod = [[x, 38, 30] for x in range(30, 34)]                                    # 판 앞 짧은 막대 = 원래 물건(라쏘)
    occ = body + rod
    cam = _cam0(occ)
    U = rod
    um = _proj_mask(U, cam)
    render = np.full((1024, 1024, 3), 255, np.uint8)
    edit = render.copy()
    ys, xs = np.nonzero(um)
    y0, y1, x0, x1 = ys.min() - 120, ys.max() + 120, xs.min() - 60, xs.max() + 60
    edit[y0:y1, x0:x1] = (90, 60, 30)                                             # 라쏘보다 큰 새 물건(방패)
    edit[20:40, 20:40] = (0, 0, 0)                                                # 떨어진 작은 조각(제거)
    edit[900:1000, 900:1000] = (0, 0, 0)                                          # 떨어진 큰 조각(라쏘와 안 닿음 → 제거)
    g = am.grow_from_image(render, edit, um, U, occ, cam, T=4)
    assert g["skip"] is None and g["n_grown"] > 0
    occset, uset = {tuple(c) for c in occ}, {tuple(c) for c in U}
    grown = {tuple(c) for c in g["grown"]}
    assert not (grown & occset) and not (grown & uset)                            # 빈 셀만, 새 셀만
    _, _, z = am._grid_xy(am.centers(g["grown"]), cam)
    zr = g["depth_range"]
    assert (z * 64 >= zr[0] - 1e-6).all() and (z * 64 <= zr[1] + 1e-6).all()
    gx, gy, _ = am._grid_xy(am.centers(g["grown"]), cam)
    assert gx.max() < 900 and gy.max() < 900                                      # 떨어진 큰 조각 쪽으로는 안 넓힘
    S = g["s_mask"]
    assert not S[:15, :15].any() and not S[230:, 230:].any()


def test_grow_skips_when_whole_image_changes():
    occ = [[x, 40, z] for x in range(20, 44) for z in range(10, 54)]
    cam = _cam0(occ)
    render = np.full((256, 256, 3), 255, np.uint8)
    edit = np.zeros((256, 256, 3), np.uint8)
    g = am.grow_from_image(render, edit, np.ones((256, 256), bool), occ[:10], occ, cam, T=4)
    assert g["skip"] == "too_large" and g["n_grown"] == 0


def test_flow_grow_too_large_forces_prompt_retry(env):
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.grow_from_image = True
    env.eng.s.agent_max_backtracks = 1                                            # 실측 설정과 같게
    env.eng.gemini = FakeGemini()                                                 # 회색 렌더 → 밝은 회색 = 화면 전체 차이
    env.eng.agent_llm.judge_script = [PASS]
    j = _lasso(env, aid)
    texts = [e["text"] for e in j["events"]]
    assert any("볼륨을 넓히지 않고 다시 그립니다" in t for t in texts)
    assert j["agent"]["backtracks"] == 1 and len(env.eng.gemini.calls) == 2
    assert env.eng.get(j["job_id"])["agent_grow"][0]["skip"] == "too_large"


def test_flow_grow_refreshes_judge_overlay(env, monkeypatch):
    """G4 실측: 넓힌 뒤에도 채점이 넓히기 전 마스크로 '영역 밖'이라 판정했다 → 넓힌 마스크 겹침을 채점에 넘긴다."""
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.grow_from_image = True
    env.eng.gemini = FakeGemini()
    fake = env.eng.agent_llm
    fake.judge_script = [PASS]
    grown = [[0, 0, 0], [1, 0, 0]]
    monkeypatch.setattr(am, "grow_from_image", lambda *a, **k: {
        "skip": None, "n_grown": len(grown), "grown": grown, "diff_frac": 0.05, "s_frac": 0.03,
        "s_mask": np.zeros((am.GROW_RES, am.GROW_RES), bool), "depth_range": [0, 1]})
    seen = []
    orig = fake.agent_judge
    fake.agent_judge = lambda render, overlay, edit, *a, **k: (seen.append(overlay), orig(render, overlay, edit, *a, **k))[1]
    j = _lasso(env, aid)
    jj = env.eng.get(j["job_id"])
    assert j["state"] == "committed" and jj["attempts"][-1]["grown"] == 2
    assert {tuple(c) for c in grown} <= {tuple(c) for c in jj["cells"]}               # 3D 볼륨 = U ∪ 넓힌 셀
    ov_now = (env.eng.s.data_dir / "jobs" / j["job_id"] / "region" / "overlay.png").read_bytes()
    assert seen and seen[-1] == ov_now                                             # 채점 = 넓힌 뒤 다시 만든 겹침
    assert any("볼륨을 넓혔습니다(+2셀)" in e["text"] for e in j["events"])


# ══════════════════════════════════════════════════════════ S20f-2
def test_full_prompt_replace_remove():
    p = full_prompt("A big round shield", "sword", "replace", "sword", "big round shield")
    assert "Completely remove the sword; no part of it may remain. Put the big round shield in its place." in p
    assert p.index("Completely remove") < p.index("Edit only the sword")
    assert "fill with what would be behind it" in full_prompt("x", "hat", "remove", "hat")
    assert "Completely remove" not in full_prompt("A sword", "right hand", "add")


def test_request_config_fallback_cache_and_log(tmp_path):
    cap = []

    def handler(req):
        cap.append(json.loads(req.content))
        if "imageConfig" in cap[-1]["generationConfig"]:
            return httpx.Response(400, text="imageConfig not supported")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "image/png", "data": base64.b64encode(_png(1024, 1024)).decode()}}]}}],
            "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 1290}})
    g = GeminiClient(model="gemini-3.1-flash-lite-image", key="k", http=httpx.Client(transport=httpx.MockTransport(handler)),
                     cache_dir=tmp_path / "gc")
    img = _png(1024, 1024)
    out, info = g.edit_cached(img, "P")
    c0 = cap[0]["generationConfig"]
    assert c0["responseModalities"] == ["IMAGE"] and c0["candidateCount"] == 1
    assert c0["imageConfig"] == {"imageSize": "1K", "aspectRatio": "1:1"}
    assert "imageConfig" not in cap[1]["generationConfig"] and len(cap) == 2          # 400 → 옵션 빼고 다시
    assert info["cached"] is False and info["calls_total"] == 1 and info["usage"]["candidatesTokenCount"] == 1290
    out2, info2 = g.edit_cached(img, "P")
    assert len(cap) == 2 and info2["cached"] is True and out2 == out and info2["calls_total"] == 1   # 캐시: 호출 없음
    g.edit_cached(img, "P2", loose="a|1|az000|칼을 방패로")
    g.edit_cached(_png(1024, 1024, (1, 2, 3)), "P3", loose="a|1|az000|칼을 방패로")      # 느슨한 키: 같은 이미지 재사용
    assert len(cap) == 4
    logs = (tmp_path / "gc" / "calls.jsonl").read_text().splitlines()
    assert sum('"cached": false' in ln for ln in logs) == 2 and sum('"cached": true' in ln for ln in logs) == 2
    big = GeminiClient(key="k", http=_transport(cap2 := []))
    big.edit(encode_png(np.full((2048, 1536, 3), 9, np.uint8)), "x")
    sent = decode_png(base64.b64decode(json.loads(cap2[0].content)["contents"][0]["parts"][1]["inline_data"]["data"]))
    assert max(sent.shape[:2]) == 1024                                               # 입력 1024² 이하로


def test_back_view_weight_and_front_from_user():
    body = [[x, y, z] for x in range(24, 40) for y in range(28, 36) for z in range(10, 50)]
    occ = body
    cams = am.view_cameras(occ)
    back_face = [[x, 28, z] for x in range(24, 40) for z in range(10, 50)]            # −y 면(az180 쪽)
    pk = am.pick_view(back_face, occ, cams, cams["az090"], front_view="az000")        # 정면 az000 → 뒤 = az180
    assert pk["back_view"] == "az180" and pk["weights"]["az180"] == 0.5
    assert pk["view"] == "az180"                                                      # 뒤가 2배 넘게 잘 보이면 고른다
    side_face = [[24, y, z] for y in range(28, 36) for z in range(10, 50)]           # −x 면(az270 쪽)
    n_side = am.visible_count(side_face, occ, cams["az270"])
    sel = side_face + [c for c in back_face if c[0] > 24][:int(1.5 * n_side)]        # 뒤가 옆의 약 1.5배(2배 안)
    pk2 = am.pick_view(sel, occ, cams, cams["az090"], front_view="az000")
    c = pk2["counts"]
    assert c["az180"] > c["az270"] and c["az180"] * 0.5 < c["az270"] and pk2["view"] == "az270"
    assert am.front_from_user(cams["az090"], occ, "front") == "az090"
    assert am.front_from_user(cams["az090"], occ, "back") == "az270"
    assert am.front_from_user(cams["az270"], occ, "left") == "az000"                  # 왼쪽 옆이 보이면 정면 = +90°
    assert am.front_from_user(cams["az090"], occ, "right") == "az000"
    assert am.front_from_user(cams["az090"], occ, "unknown") is None


def test_merge_residue_attaches_old_object_outside_lasso():
    body = [[x, 40, z] for x in range(20, 44) for z in range(10, 54)]
    sword = [[x, 38, 30] for x in range(28, 50)]                                      # 몸 앞 칼, +x 끝은 라쏘 밖
    occ = body + sword
    cam = _cam0(occ)
    U = [c for c in sword if c[0] < 36]                                               # 손잡이 쪽만 라쏘
    um = _proj_mask(U, cam)
    sm = _proj_mask(sword, cam)                                                       # 칼 전체 자리가 바뀜(사라짐 + 새 물건)
    render = np.full((1024, 1024, 3), 255, np.uint8)
    edit = render.copy()
    edit[sm] = (90, 60, 30)
    g = am.grow_from_image(render, edit, um, U, occ, cam, T=4, op="replace", M=16)
    merged = {tuple(c) for c in g["merged"]}
    assert {tuple(c) for c in sword if c[0] >= 36} <= merged                          # 라쏘 밖 칼 끝 합침
    assert not (merged & {tuple(c) for c in body})                                   # 깊이 제한: 뒤 몸판은 안 합침
    g2 = am.grow_from_image(render, edit, um, U, occ, cam, T=4, op="add", M=16)
    assert g2["n_merged"] == 0                                                         # add 는 합치지 않음
    huge = [[x, 38, z] for x in range(20, 44) for z in range(10, 54)]                  # 칼이 큰 판에 붙어 있으면 번짐
    occ3 = body + huge
    hm = _proj_mask(huge, cam)
    e3 = render.copy()
    e3[hm] = (90, 60, 30)
    U3 = [[30, 38, 30], [31, 38, 30]]
    g3 = am.grow_from_image(render, e3, _proj_mask(U3, cam), U3, occ3, cam, T=4, op="replace", M=16, max_frac=0.9)
    assert g3["n_merged"] == 0 and g3["merge"]["skipped"] == "too_large"


def test_no_commit_preview_only_keeps_app_version(env):
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.agent_no_commit = True
    env.eng.gemini = FakeGemini()
    fake = env.eng.agent_llm
    fake.op = "replace"
    fake.judge_script = [PASS]
    j = _lasso(env, aid, "칼을 큰 방패로 바꿔줘")
    assert j["state"] == "committed" and j["result"]["preview_only"] is True and j["result"]["manifest_uri"] is None
    assert env.eng.db.asset(aid)["version"] == 1                                      # 앱 판본 그대로
    assert ("commit",) in env.be.calls                                                # A5000 에는 반영(미리보기 렌더용)
    assert any("미리보기만 남겼습니다" in e["text"] and "result" in (e.get("images") or {}) for e in j["events"])
    j2 = _lasso(env, aid, "칼을 큰 방패로 바꿔줘")                                      # 같은 판본(v1) 위 두 번째 편집 가능
    assert j2["state"] == "committed" and env.eng.db.asset(aid)["version"] == 1
    assert fake.last_judge_old == "the old object"                                    # 채점에 원래 물건(남으면 ① 불합격)


# ══════════════════════════════════════════════════════════ S20f-3 · 계약 10.2 1.1.1 보충(S20f)
def test_gemini_max_backtracks_one_and_stages(env, monkeypatch):
    aid = make_asset(env)
    j0 = _lasso(env, aid)                                                          # 로컬(기본): 설정값 그대로
    assert j0["agent"]["max_backtracks"] == env.eng.s.agent_max_backtracks == 3
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.grow_from_image = True
    env.eng.gemini = FakeGemini()
    env.eng.agent_llm.judge_script = [PASS]
    monkeypatch.setattr(am, "grow_from_image", lambda *a, **k: {
        "skip": None, "n_grown": 3, "grown": [[0, 0, 0], [1, 0, 0], [2, 0, 0]], "diff_frac": 0.05, "s_frac": 0.03,
        "s_mask": np.zeros((am.GROW_RES, am.GROW_RES), bool), "depth_range": [0, 1], "merged": [], "n_merged": 0,
        "merge": None})
    j = _lasso(env, aid, base=j0["result"]["to_version"])
    assert j["state"] == "committed" and j["agent"]["max_backtracks"] == 1          # Gemini 일 때 1
    stages = [s["stage"] for s in env.eng.get(j["job_id"])["stage_log"]]
    assert "view_select" in stages and "volume_grow" in stages
    imgs = [k for e in j["events"] for k in (e.get("images") or {})]
    assert "views" in imgs and "grow" in imgs and "overlay" in imgs                # 넓힌 윤곽 = grow
    ev = next(e for e in j["events"] if "grow" in (e.get("images") or {}))
    assert env.get(ev["images"]["grow"]).status_code == 200


def test_mock_mode_fixed_texts_no_gemini(env):
    aid = make_asset(env)
    env.eng.s.inpaint_backend = "gemini"
    env.eng.s.grow_from_image = True
    env.be.name = "mock"                                                           # 8084 가짜 모드 경로
    env.be.DUR = {"generate": 0, "refine": 0, "inpaint": 0, "edit3d": 0}               # MockBackend 단계 시간표 흉내
    env.eng.s.mock_time_scale = 0
    g = FakeGemini()
    env.eng.gemini = g
    env.eng.agent_llm.judge_script = [PASS]
    try:
        j = _lasso(env, aid)
    finally:
        env.be.name = "fake"
    assert j["state"] == "committed" and j["agent"]["max_backtracks"] == 1 and g.calls == []   # 실제 Gemini 안 부름
    texts = [e["text"] for e in j["events"]]
    assert "물건이 가장 잘 보이는 방향(방위 90°)을 골랐습니다(가짜)" in texts
    assert "새 물건이 그린 영역보다 커서 볼륨을 넓혔습니다(+2,140셀)(가짜)" in texts
    stages = [s["stage"] for s in env.eng.get(j["job_id"])["stage_log"]]
    assert "view_select" in stages and "volume_grow" in stages
    keys = [k for e in j["events"] for k in (e.get("images") or {})]
    assert "views" in keys and "grow" in keys
