"""S20d: 자동 마스킹 좌우 규칙(코드 판정·뒤집기) + 마스크 다시 칠하기 별도 한도 + 가짜 모드 X-Mock-Agent."""

from __future__ import annotations

import pytest

from server.app import automask as am
from server.app.agent import AgentRun
from server.tests.test_app_server import A, env, make_asset  # noqa: F401 — fixture

PASS = {"element": True, "position": True, "mask_fit": True, "shape_3d": True}


@pytest.mark.parametrize("text, want", [
    ("오른손에 칼을 들게 해줘", ("right", "character")),
    ("왼손에 방패를 들게 해줘", ("left", "character")),
    ("왼쪽에 방패를 들게 해줘", ("left", "viewer")),
    ("오른쪽에 깃발을 꽂아줘", ("right", "viewer")),
    ("오른쪽 손에 칼", ("right", "character")),          # "쪽"이 있어도 몸 부위면 캐릭터 기준
    ("왼발에 장화", ("left", "character")),
    ("오른쪽 어깨에 앵무새", ("right", "character")),
    ("put a sword in his right hand", ("right", "character")),
    ("add a flag on the left", ("left", "viewer")),
    ("왼쪽 신발을 빨갛게", ("left", "viewer")),            # 신발은 몸 부위가 아니다 → 위치
    ("신발을 장화로 바꿔줘", None),
    ("칼을 들게 해줘", None),
    ("왼손에 칼, 오른손에 방패", None),                    # 서로 다름 → 애매(오케스트레이터 출력)
])
def test_parse_side(text, want):
    got = am.parse_side(text)
    assert (None if got is None else (got["side"], got["frame"])) == want


def test_expected_image_side():
    assert am.expected_image_side("right", "character") == "left"
    assert am.expected_image_side("left", "character") == "right"
    assert am.expected_image_side("left", "viewer") == "left"


def _body():
    return [[i, j, k] for i in range(24, 40) for j in range(26, 38) for k in range(10, 50)]


def test_image_side_and_mirror():
    occ = _body()
    cams = am.view_cameras(occ)
    plus_x = [[i, 31, 30] for i in range(42, 50)]            # 몸의 +x 쪽(az000 = +y 에서 보면 이미지 왼쪽)
    s = am.image_side_of(plus_x, occ, cams["az000"])
    assert s["side"] == "left" and s["dx"] < 0
    assert am.image_side_of(plus_x, occ, cams["az180"])["side"] == "right"
    m = am.mirror_cells(plus_x, occ, cams["az000"])
    cx2 = 24 + 39                                              # 몸 중심 x = (24 + 39) / 2 → 뒤집으면 x' = 63 − x
    assert sorted(map(tuple, m)) == sorted((cx2 - i, 31, 30) for i in range(42, 50))
    assert am.image_side_of(m, occ, cams["az000"])["side"] == "right"
    assert sorted(map(tuple, am.mirror_cells(m, occ, cams["az000"]))) == sorted(map(tuple, plus_x))   # 두 번 = 원래


class _Run(AgentRun):
    """side_rule 만 시험(엔진 없이)."""

    def __init__(self, instruction, occ):
        self.j = {"instruction": instruction}
        self.occ, self.cams = occ, am.view_cameras(occ)
        self.events = []

    def event(self, step, kind, text, images=None, detail=None):
        self.events.append({"step": step, "kind": kind, "text": text, "detail": detail})


def _plan(**kw):
    return dict({"front_view": "az000", "inpaint_view": "az000", "op": "add", "target_part_ko": "오른손",
                 "side": None, "side_frame": None}, **kw)


def test_side_rule_flips_wrong_side_and_hint():
    occ = _body()
    img_right = [[i, 31, 30] for i in range(16, 24)]          # −x 쪽(몸에 붙음) = az000 이미지 오른쪽
    img_left = [[i, 31, 30] for i in range(40, 48)]
    r = _Run("오른손에 칼을 들게 해줘", occ)                     # 캐릭터 오른손 = 이미지 왼쪽 기대
    out = r.side_rule(_plan(), img_right)
    assert sorted(map(tuple, out)) == sorted(map(tuple, am.mirror_cells(img_right, occ, r.cams["az000"])))
    rec = r.j["agent_side"][-1]
    assert rec["flipped"] and rec["expected"] == "left" and rec["got"]["side"] == "right" and rec["source"] == "code"
    assert r.events[-1]["kind"] == "check" and r.events[-1]["text"] == "오른쪽에 칠해져 오른손 쪽으로 옮겼습니다(좌우 규칙)"
    assert "IMAGE LEFT" in r.side_hint
    # 이미 맞는 쪽이면 그대로
    r2 = _Run("오른손에 칼을 들게 해줘", occ)
    assert r2.side_rule(_plan(), img_left) == img_left and not r2.j["agent_side"][-1]["flipped"] and r2.events == []
    # 보는 사람 기준 "왼쪽" = 이미지 왼쪽 기대 → 이미지 오른쪽에 칠했으면 뒤집는다
    r3 = _Run("왼쪽에 방패를 들게 해줘", occ)
    r3.side_rule(_plan(target_part_ko="왼쪽"), img_right)
    assert r3.j["agent_side"][-1]["flipped"] and r3.j["agent_side"][-1]["expected"] == "left"
    assert r3.events[-1]["text"] == "오른쪽에 칠해져 왼쪽 쪽으로 옮겼습니다(좌우 규칙)"
    # 인페인팅 시점이 앞의 반대(180°)면 기대 쪽도 반대, 옆(90°)이면 기대 문장 없음
    r4 = _Run("오른손에 칼", occ)
    r4.side_rule(_plan(inpaint_view="az180"), img_left)
    assert "IMAGE RIGHT" in r4.side_hint
    r5 = _Run("오른손에 칼", occ)
    r5.side_rule(_plan(inpaint_view="az090"), img_left)
    assert r5.side_hint is None


def test_side_rule_uses_orchestrator_when_ambiguous_and_skips_without_side():
    occ = _body()
    img_right = [[i, 31, 30] for i in range(16, 24)]
    r = _Run("칼을 들게 해줘", occ)                           # 코드 판정 없음 → 오케스트레이터 기본(오른손, 캐릭터)
    r.side_rule(_plan(side="right", side_frame="character"), img_right)
    rec = r.j["agent_side"][-1]
    assert rec["source"] == "orchestrator" and rec["flipped"]
    r2 = _Run("신발을 장화로 바꿔줘", occ)
    assert r2.side_rule(_plan(), img_right) == img_right and r2.j["agent_side"][-1]["parsed"] is None


def test_side_rule_replace_keeps_only_near_body():
    occ = _body()
    far = [[i, 31, 30] for i in range(2, 10)]                 # 몸에서 먼 셀(이미지 오른쪽) → 뒤집어도 몸 근처가 아님
    r = _Run("오른손의 칼을 횃불로 바꿔줘", occ)
    out = r.side_rule(_plan(op="replace"), far)
    assert out == far and "flip_failed" in r.j["agent_side"][-1]


# ══════════════════════════════════════════════════════════ 별도 한도·가짜 시나리오
def _agent(env, aid, text="오른손에 칼을 들게 해줘", base=1, headers=None):
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=dict(A, **(headers or {})),
                 json={"base_version": base, "instruction": text})
    assert r.status_code == 202, r.text
    env.eng.drain()
    return env.get(f"/app/v1/jobs/{r.json()['job_id']}", headers=A).json()


def test_mask_retry_limit_separate_from_backtracks(env):
    aid = make_asset(env)
    fake = env.eng.agent_llm
    fake.mask_check_ok = [False, False, False, True]
    fake.judge_script = [dict(PASS, element=False), dict(PASS, element=False), dict(PASS, element=False), PASS]
    j = _agent(env, aid)
    ag = j["agent"]
    assert j["state"] == "committed" and ag["mask_retries"] == 2 and ag["max_mask_retries"] == 2
    assert ag["backtracks"] == 3 and ag["below_bar"] is False      # 채점 뒤 되돌아가기 3회는 그대로 쓸 수 있다
    texts = [e["text"] for e in j["events"]]
    assert any("마스크 다시 칠하기 1/2" in t for t in texts) and any("마스크 다시 칠하기 2/2" in t for t in texts)
    assert any("마스크 다시 칠하기 한도라 이 마스크로 진행" in t for t in texts)
    assert any("되돌아가기 3/3" in t for t in texts)


@pytest.mark.parametrize("scenario, mr, bt, below", [
    ("pass", 0, 0, False), ("backtrack", 0, 1, False), ("below_bar", 0, 3, True), ("mask_retry", 1, 0, False)])
def test_mock_agent_header(env, scenario, mr, bt, below):
    aid = make_asset(env)
    env.eng.s.mock = True                                     # 헤더는 가짜 모드에서만 읽는다
    try:
        j = _agent(env, aid, headers={"X-Mock-Agent": scenario})
    finally:
        env.eng.s.mock = False
    assert j["state"] == "committed"
    assert (j["agent"]["mask_retries"], j["agent"]["backtracks"], j["agent"]["below_bar"]) == (mr, bt, below)


def test_mock_header_ignored_when_not_mock(env):
    aid = make_asset(env)
    env.eng.agent_llm.judge_script = [PASS]
    j = _agent(env, aid, headers={"X-Mock-Agent": "below_bar"})
    assert j["agent"]["below_bar"] is False and env.eng.get(j["job_id"])["mock_agent"] is None


def test_judge_gets_same_side_hint(env):
    """S20d: 채점도 마스크 확인과 같은 좌우 규칙·기대 위치 문장을 받는다(R2 실측: 채점만 옛 규칙이라 3회 불합격)."""
    from server.app.llm_adapter import AGENT_JUDGE_SYSTEM
    assert "viewer's side" in AGENT_JUDGE_SYSTEM and "expected position" in AGENT_JUDGE_SYSTEM
    aid = make_asset(env)
    fake = env.eng.agent_llm
    fake.judge_script = [PASS]
    seen = []
    orig = AgentRun.side_rule

    def spy(self, plan, cells):
        out = orig(self, plan, cells)
        self.side_hint = "TEST HINT"                              # 가짜 몸이 작아 실제 문장 대신 표시로 전달 경로만 확인
        seen.append(1)
        return out
    AgentRun.side_rule = spy
    try:
        _agent(env, aid)
    finally:
        AgentRun.side_rule = orig
    assert seen and fake.last_judge_hint == "TEST HINT"
