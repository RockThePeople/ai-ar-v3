"""S20e: 사용자 라쏘 볼륨 다듬기(잘라내기·합치기, 상수 = 물체 크기 비례) + 1.1.1 요청 분기."""

from __future__ import annotations

import numpy as np

from server.app import automask as am
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture

PASS = {"element": True, "position": True, "mask_fit": True, "shape_3d": True}


def test_size_consts():
    for L, T, M in ((30, 3, 4), (60, 6, 8), (120, 12, 16)):
        c = am.size_consts([[0, 0, 0], [L - 1, 0, 0]])
        assert (c["L"], c["T"], c["M"]) == (L, T, M)
    assert am.size_consts([[0, 0, 0], [9, 0, 0]])["T"] == 3            # 하한
    assert am.size_consts([[0, 0, 0], [59, 0, 0]], depth_ratio=0.2)["T"] == 12
    assert am.size_consts([[0, 0, 0], [59, 0, 0]], fixed=(6, 8)) == {"L": 60, "T": 6, "M": 8, "fixed": True}


def _plates():
    """앞판(y 40–41, 카메라 쪽) + 뒤판(y 20–21), x 24–39 · z 20–39. 카메라 = az000(+y 에서 −y 를 봄)."""
    front = [[x, y, z] for x in range(24, 40) for y in (40, 41) for z in range(20, 40)]
    back = [[x, y, z] for x in range(24, 40) for y in (20, 21) for z in range(20, 40)]
    return front, back


def _cam0(occ):
    """고도 0° 정면 카메라(+y 에서 −y 를 봄) — 뒤판이 앞판에 완전히 가린다."""
    c = am.centers(occ)
    mid = (c.min(0) + c.max(0)) / 2
    return {"frame": "normalized_voxel", "position": [float(mid[0]), float(mid[1] + 1.6), float(mid[2])],
            "forward": [0.0, -1.0, 0.0], "up": [0.0, 0.0, 1.0], "fov_y_deg": 40.0, "aspect": 1.0}


def _box(cells, cam, pad=0):
    x, y, _ = am._grid_xy(am.centers(cells), cam)
    return [int(x.min()) - pad, int(y.min()) - pad, int(x.max()) + 1 + pad, int(y.max()) + 1 + pad]


def test_lasso_cells_through_volume():
    front, back = _plates()
    occ = front + back
    cam = am.view_cameras(occ)["az000"]
    U = am.lasso_cells(cam, [[0, 0], [1000, 0], [1000, 1000], [0, 1000]])
    su = {tuple(c) for c in U}
    assert {tuple(c) for c in occ} <= su and len(U) > len(occ)       # 관통: 앞·뒤 점유 + 빈 셀


def test_depth_cut_removes_back_plate():
    front, back = _plates()
    occ = front + back
    cam = _cam0(occ)
    U = front + back
    consts = am.size_consts(occ)                                      # L = 20(y 20–41 … 최장 변) → T = 3
    plan = {"op": "add", "keep_box": [0, 0, 1000, 1000], "old_object_box": None, "new_space_box": None}
    r = am.refine_volume(U, occ, cam, plan, consts)
    s = {tuple(c) for c in r["cells"]}
    assert s == {tuple(c) for c in front} and r["cut_depth"] == len(back)


def test_side_cut_and_empty_rule():
    front, _ = _plates()
    occ = front
    cam = am.view_cameras(occ)["az000"]
    hand = [c for c in front if c[0] >= 32]                           # keep_box = 앞판 한쪽 절반
    empty = [[x, 50, z] for x in range(24, 40) for z in range(20, 40)]   # 판 앞 빈 셀
    U = front + empty
    consts = am.size_consts(occ)
    kb = _box(hand, cam)
    plan = {"op": "add", "keep_box": kb, "old_object_box": None, "new_space_box": None}
    r = am.refine_volume(U, occ, cam, plan, consts)
    s = {tuple(c) for c in r["cells"]}
    occ_kept = s & {tuple(c) for c in front}
    assert {tuple(c) for c in hand} <= occ_kept and r["cut_side"] > 0
    x, y, _ = am._grid_xy(am.centers([list(c) for c in occ_kept]), cam)
    assert x.min() >= kb[0] - am.SIDE_PAD - 1                         # 상자(+3%) 밖 점유 셀은 빠짐
    emp_kept = s - {tuple(c) for c in front}
    ex, _, _ = am._grid_xy(am.centers([list(c) for c in emp_kept]), cam)
    assert len(emp_kept) and ex.min() >= kb[0] - am.SIDE_PAD - 1     # 빈 셀도 keep ∪ new 안만
    r2 = am.refine_volume(U, occ, cam, dict(plan, op="recolor"), consts)
    assert not ({tuple(c) for c in r2["cells"]} & {tuple(c) for c in empty}) and r2["cut_empty"] == len(empty)


def test_merge_attaches_connected_old_object_only():
    body = [[x, y, z] for x in range(24, 40) for y in range(28, 36) for z in range(10, 50)]
    rod = [[x, 31, 30] for x in range(40, 56)]                        # 몸에 붙은 막대(원래 물건), +x 쪽
    far = [[60, 31, 12]]                                              # 막대와 안 이어진 점
    occ = body + rod + far
    cam = _cam0(occ)
    U = [c for c in rod if c[0] < 46]                                 # 라쏘는 막대 앞부분만
    consts = am.size_consts(occ, fixed=(6, 16))                         # 막대 전체가 라쏘 상자 + M 안에 들게
    ob = _box(rod, cam, 10)
    plan = {"op": "replace", "keep_box": _box(U, cam, 5), "old_object_box": ob, "new_space_box": None}
    r = am.refine_volume(U, occ, cam, plan, consts)
    s = {tuple(c) for c in r["cells"]}
    assert {tuple(c) for c in rod} <= s and r["merged"] >= len(rod) - len(U)   # 막대 전체(+ 상자 5% 안 맞닿은 몸통)
    assert (60, 31, 12) not in s                                      # 이어지지 않은 셀은 안 붙음
    assert not (s & {tuple(c) for c in body if c[0] < 36})           # 상자 밖 몸통은 안 붙음
    # 깊이 제한: 막대 뒤(카메라에서 먼 쪽)로 이어진 기둥은 d0 + 2T 밖이면 안 붙는다
    tail = [[50, y, 30] for y in range(10, 31)]
    occ2 = body + rod + tail
    r2 = am.refine_volume(U, occ2, cam, plan, am.size_consts(occ2, fixed=(6, 16)))
    s2 = {tuple(c) for c in r2["cells"]}
    T = 6
    assert (50, 31 - 2 * T - 4, 30) not in s2 and (50, 30, 30) in s2
    # 합치기 상자 제한: 라쏘 경계 상자 + M 밖은 안 붙는다
    long_rod = [[x, 31, 30] for x in range(40, 64)]
    occ3 = body + long_rod
    c3 = am.size_consts(occ3, fixed=(6, 4))
    r3 = am.refine_volume(U, occ3, cam, dict(plan, old_object_box=_box(long_rod, cam, 10)), c3)
    xs = [c[0] for c in r3["cells"]]
    assert max(xs) == max(c[0] for c in U) + 4


def test_fallback_when_empty_and_cap():
    front, _ = _plates()
    occ = front
    cam = am.view_cameras(occ)["az000"]
    plan = {"op": "add", "keep_box": [0, 0, 5, 5], "old_object_box": None, "new_space_box": None}
    r = am.refine_volume(front, occ, cam, plan, am.size_consts(occ))
    assert r["fallback"] and len(r["cells"]) == len(front)
    big = [[x, y, z] for x in range(64) for y in range(64) for z in range(20)]   # 81,920 셀
    r2 = am.refine_volume(big, big, cam, {"op": "add", "keep_box": [0, 0, 1000, 1000], "old_object_box": None,
                                          "new_space_box": [0, 0, 1000, 1000]}, {"L": 64, "T": 100, "M": 8})
    assert r2["n_final"] <= am.MAX_CELLS


# ══════════════════════════════════════════════════════════ 1.1.1 요청 분기·흐름
def _lasso(env, aid, text="오른손에 칼을 들게 해줘", agent=True, headers=None, base=1):
    body = {"base_version": base, "instruction": text, "camera": CAM, "volume_cells": CELLS}
    if agent is not None:
        body["agent"] = agent
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=dict(A, **(headers or {})), json=body)
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def test_health_and_branching(env):
    h = env.get("/app/v1/health").json()
    assert h["app_api"] == "1.2.0" and h["features"] == ["agent_edit", "agent_lasso", "chain_rating"]
    aid = make_asset(env)
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A, json={"base_version": 1, "instruction": "오른손에 칼"})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_REQUEST"   # 자동 마스킹은 토글 뒤
    o = _lasso(env, aid, agent=False)                                  # agent false = 1.0.6 수동 흐름
    assert o["state"] == "awaiting_2d_review" and "mode" not in o
    env.post(f"/app/v1/jobs/{o['job_id']}/cancel", headers=A)
    o2 = _lasso(env, aid, agent=None)
    assert o2["state"] == "awaiting_2d_review"
    env.post(f"/app/v1/jobs/{o2['job_id']}/cancel", headers=A)


def test_lasso_agent_flow_commits(env):
    aid = make_asset(env)
    env.eng.agent_llm.judge_script = [PASS]
    j = _lasso(env, aid)
    assert j["state"] == "committed" and j["mode"] == "agent" and j["agent"]["backtracks"] == 0
    texts = [e["text"] for e in j["events"]]
    assert texts[0].startswith("그린 영역을 봤습니다(라쏘 ") and any(t.startswith("마스크를 다듬었습니다(라쏘 ")
                                                                       or t.startswith("다듬지 못해") for t in texts)
    jj = env.eng.get(j["job_id"])
    assert jj["agent_source"] == "lasso" and jj["agent_consts"]["T"] >= 3 and "agent_mask" in jj
    assert any(s["stage"] == "mask_refine" for s in jj["stage_log"])
    assert not any("views" in (e.get("images") or {}) for e in j["events"])   # 시점 4장 렌더 없음
    assert jj["camera"]["position"] == CAM["position"]                  # 인페인팅은 사용자 카메라


def test_lasso_merge_event_and_retry(env):
    aid = make_asset(env)
    fake = env.eng.agent_llm
    fake.op = "replace"
    fake.mask_check_ok = [False, True]
    fake.judge_script = [dict(PASS, mask_fit=False), PASS]
    j = _lasso(env, aid, "방패를 칼로 바꿔줘")
    assert j["state"] == "committed"
    assert j["agent"]["mask_retries"] == 1 and j["agent"]["backtracks"] == 1
    texts = [e["text"] for e in j["events"]]
    assert any("마스크를 다시 다듬습니다(마스크 다시 다듬기 1/2)" in t for t in texts)
    assert any("마스크 다듬기부터 다시 합니다(되돌아가기 1/3)" in t for t in texts)


def test_lasso_no_target_and_mock_header(env):
    aid = make_asset(env)
    env.eng.agent_llm.mask_found = False
    j = _lasso(env, aid)
    assert j["state"] == "failed" and j["error_code"] == "NO_TARGET"
    env.eng.agent_llm.mask_found = True
    env.eng.s.mock = True
    try:
        j2 = _lasso(env, aid, headers={"X-Mock-Agent": "mask_retry"})
    finally:
        env.eng.s.mock = False
    assert j2["state"] == "committed" and j2["agent"]["mask_retries"] == 1
    assert np is not None
