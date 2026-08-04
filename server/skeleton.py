"""W2/3090 — `:8083` 자리 점유용 **스켈레톤**. 로직은 한 줄도 없다.

────────────────────────────────────────────────────────────────────────
왜 빈 서버를 먼저 띄우는가
────────────────────────────────────────────────────────────────────────
`docs/PROGRESS.md` §2 D3-⑤ 가 근거다 — "프로젝트가 실제로 죽는 지점은 핵심 로직이
아니라 포트·방화벽·인증이다." 그래서 관통(S2)의 내용물을 만들기 **전에** 포트·터널
경로부터 뚫어 놓는다. 이 파일이 그 역할의 전부다.

`ai-ar-prototype` 오케스트레이터가 쓰던 `:8083` 을 W1 에서 내렸고, ngrok 터널은
설정상 계속 `:8083` 을 가리키고 있다. 그 자리에 아무도 없으면 공개 URL 은 502 다.
여기 이 서버가 bind 되는 순간 200 으로 바뀐다 — 그게 이 세션이 증명하려는 사실이다.

────────────────────────────────────────────────────────────────────────
여기 무엇을 넣지 않는가
────────────────────────────────────────────────────────────────────────
복셀화 · 마스크 · splice · 델타 · 메트릭은 **맥북 담당이고 다음 웨이브다.**
이 파일에 그것들을 미리 얹지 않는다. 스코프를 넓히려면 코드가 아니라
`docs/PROGRESS.md` 를 먼저 고친다 (§0 원칙 4).

────────────────────────────────────────────────────────────────────────
보안 (§7)
────────────────────────────────────────────────────────────────────────
호스트·포트·키를 이 파일에 박지 않는다. 포트는 `DEBUGVIEW_PORT` 환경변수로만
받고, `/healthz` 는 **아무 비밀도 반환하지 않는다** — 공개 ngrok URL 로 그대로
노출되는 엔드포인트이기 때문이다.

────────────────────────────────────────────────────────────────────────
기동
────────────────────────────────────────────────────────────────────────
반드시 `setsid --fork` 로 띄우고 `ps -o pid,ppid,cmd -p <PID>` 로 PPID=1 을 눈으로
확인한다. 과거에 세션 종료와 함께 자식 프로세스가 죽어 서비스가 내려간 적이 있다.
`server/run-8083.sh` 가 그 절차를 담고 있다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response

_REPO = Path(__file__).resolve().parent.parent
# 계약은 리포 안 별도 패키지다. sys.path 에 얹어야 `deltacontract` 를 임포트할 수 있다.
_CONTRACT = _REPO / "contract" / "python"
if str(_CONTRACT) not in sys.path:
    sys.path.insert(0, str(_CONTRACT))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# 프로세스 기동 시각. **모듈 임포트 시점에 한 번만** 잡는다 — 요청마다 새로 잡으면
# "언제 뜬 서버인가" 를 못 가린다. §0 원칙 7("그 코드가 떴는가"는 별개의 사실이다).
STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build() -> dict[str, object]:
    """떠 있는 것이 **어느 커밋인가**. 테스트 통과와 무관한, 독립된 사실이다.

    git 이 없거나 리포 밖에서 돌면 `unknown` 이다 — 거짓말로 채우지 않는다.
    """
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(_REPO), *args],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    sha = git("rev-parse", "--short", "HEAD")
    porcelain = git("status", "--porcelain")
    if sha is None:
        return {"build": "unknown", "build_dirty": None, "build_untracked": None}
    lines = [ln for ln in (porcelain or "").splitlines() if ln.strip()]
    return {
        "build": sha,
        "build_dirty": any(not ln.startswith("??") for ln in lines),
        "build_untracked": sum(1 for ln in lines if ln.startswith("??")),
    }


BUILD = _build()

app = FastAPI(title="ai-ar-v3 skeleton", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """이 스켈레톤이 반환하는 **유일한** 것.

    `build` 와 `started_at` 을 같이 준다. 앞으로 "그 코드가 실제로 떴는가" 를
    이 두 필드로만 판정한다 — 테스트 결과나 체크아웃 상태로 판정하지 않는다.
    """
    return {
        "ok": True,
        "service": "ai-ar-v3-skeleton",
        "started_at": STARTED_AT,
        "uptime_s": round(time.time() - _BOOT_MONO, 1),
        **BUILD,
    }


_BOOT_MONO = time.time()


# ══════════════════════════════════════════════════════════════ DebugView
#
# PROGRESS §5 S2-6. 지금은 **합성 픽스처**로 채운다 — 실자산을 기다리면 "보이는가"
# 라는 질문이 자산 도착까지 통째로 밀린다 (D3-⑤: 죽는 지점은 로직이 아니라 렌더 경로다).
#
# 관통은 맥북이 이미 통과시킨 `server.tests.test_pipeline._run_pipeline()` 을 **그대로
# 부른다.** 파이프라인을 여기서 다시 구현하지 않는다 (CLAUDE.md 경계 규칙).
_SCENE_CACHE: dict = {}

# 실자산 위치. 리포 밖이다 (§7 — 홈 경로를 코드에 박지 않는다).
ASSET_ROOT = Path(os.environ.get("ASSET_ROOT", str(Path.home() / "ai-ar-v3-assets")))

# 실자산 관통 파라미터. D11(rev7) 이후 크기는 `fit_donor_to_mask` 의 재복셀화가
# 맞추므로 크롭은 1.0 이다 — W3 의 0.30 은 호박 위쪽 뚜껑만 남겨 얼굴을 잘랐다.
REAL_HEAD_FRACTION = float(os.environ.get("REAL_HEAD_FRACTION", "0.35"))
REAL_CROP_FRACTION = float(os.environ.get("REAL_CROP_FRACTION", "1.0"))

# ★ 잡음 바닥값 — `/real`(눈사람+호박) 에는 **없다.** None 이다.
#
# 🔴 W5~W8 에 여기 0.0701 을 박아 뒀는데 두 번 틀렸다:
#    ① 그 값은 이제 dragon-c 기준 0.2229 로 정정됐다 (D36 — 3.2배 과대평가였다)
#    ② 애초에 **자산이 다르다.** `metrics.NOISE_FLOORS` 는 전부 dragon-c 스코프이고
#       `/real` 은 눈사람+호박을 돌린다. 남의 자산 바닥값을 쓰면 예외 없이 틀린 판정이 나온다
#       — D33 이 `NoiseFloor` 를 **타입으로** 강제하는 이유가 정확히 이것이다.
#
# 눈사람에 대한 왕복 대조군은 아직 없다. 그래서 None 을 넘기고 보존은 **미결**로
# 렌더된다 (D5-b). 추정값으로 통과시키지 않는다.
REAL_NOISE_FLOOR = None

# ── recolor(레벨1) 결과 자리 ────────────────────────────────────────────
#
# 맥북이 `recolor.py` 를 올리면 그 산출물을 여기에 꽂는다. **import 로 묶지 않는다** —
# 이 시스템의 산출물 모양은 `.cbin` 청크 세트 하나뿐이므로(D8) 디렉터리 경로만 받는다.
# 그래야 recolor 가 무엇을 노출하든 DebugView 를 다시 고칠 일이 없다.
#
# 기본값은 W6 진단이 실제로 만들어 둔 것을 가리킨다 (눈사람 머리만 주황).
# 없으면 자리표시를 그리고 화면에 "미도착" 이라고 적는다 — 빈 그림을 결과처럼
# 보이게 두지 않는다.
RECOLOR_BEFORE = Path(os.environ.get("RECOLOR_BEFORE_DIR", str(ASSET_ROOT / "base" / "chunks")))
RECOLOR_AFTER = Path(os.environ.get("RECOLOR_AFTER_DIR", str(ASSET_ROOT / "base" / "chunks_level1")))


def _recolor_ready() -> bool:
    return any(RECOLOR_BEFORE.glob("*.cbin")) and any(RECOLOR_AFTER.glob("*.cbin"))


def _scene(kind: str = "synthetic"):
    """관통 1회 + 렌더용 장면. 첫 요청에서 만들고 캐시한다.

    합성은 약 3초, 실자산은 약 25초 걸린다 (청크 475개 디코딩 + 표면 복셀화 2회).
    """
    if kind in _SCENE_CACHE:
        return _SCENE_CACHE[kind]

    from server import debugview

    if kind == "real":
        from server.realasset import run_real_walkthrough

        run = run_real_walkthrough(
            ASSET_ROOT / "base" / "chunks",
            ASSET_ROOT / "donor" / "chunks",
            head_fraction=REAL_HEAD_FRACTION,
            crop_fraction=REAL_CROP_FRACTION,
            noise_floor=REAL_NOISE_FLOOR,
        )
    else:
        from server.tests.test_pipeline import _run_pipeline

        run = _run_pipeline()

    entry = {"run": run, "scene": debugview.build_scene(run), "mod": debugview}
    _SCENE_CACHE[kind] = entry
    return entry


def _gate_rows(run):
    """D5-a/D5-b 게이트 지표 + 판정 (rev6).

    ★ 판정은 **내가 하지 않는다** — `MetricReport.gate_g2()` 가 정본이고,
      그 함수가 PROGRESS §5 S2 의 문구와 1:1 이다. 여기서 임계값을 다시 적으면
      화면과 게이트가 갈라지고, 갈라진 줄 아무도 모른다 (방법론 5조 4번).

    ⚠️ `preservation` 은 잡음 바닥값이 없으면 `NoiseFloorUnknown` 으로 **거부된다.**
       그때 화면은 '미결' 로 낸다 — 추정값으로 통과시키지 않는다 (D5-b).
    """
    from server.metrics import NoiseFloorUnknown

    r = run["report"]
    d = r.as_dict()
    try:
        g = r.gate_g2()          # visual_confirmed=None → 효능은 '미결' 이 될 수 있다
    except NoiseFloorUnknown:
        g = {"efficacy": None, "preservation": None, "saving": None,
             "efficacy_numeric": None}

    def mark(ok):
        return {True: "통과", False: "미달", None: "미결"}[ok]

    rows = [
        ("efficacy_new_voxels (마스크 안)", d["efficacy_new_voxels"], "> 0",
         mark(d["efficacy_new_voxels"] > 0)),
        ("efficacy_removed_voxels (마스크 안)", d["efficacy_removed_voxels"],
         "쌍으로 본다 (D5-a①)", "참고"),
        ("efficacy_net_voxels", d["efficacy_net_voxels"], "순증", "참고"),
        ("efficacy_change_component (신규∪제거, D12)", f"{d['efficacy_change_component']:.4f}",
         "≥ 0.80", mark(d["efficacy_change_component"] >= 0.8)),
        ("churn_ratio (안/밖)", f"{d['churn_ratio']:.3f}", "≥ 3.0",
         mark(d["churn_ratio"] >= 3.0)),
        ("inherited_byte_identity", f"{d['inherited_byte_identity']:.4f}", "== 1.0",
         mark(d["inherited_byte_identity"] >= 1.0)),
        ("preservation_excess_ratio (D16)",
         ("—" if d.get("preservation_excess_ratio") is None
          else f"{d['preservation_excess_ratio']:.4f}×"),
         "바닥값 대비 초과배수 — 절대값 문턱이 아니다", mark(g.get("preservation"))),
        # ⚠️ `preservation_baseline` 은 **문자열**이다 (D36-a 이후). 맥북의
        #    `describe()` 가 영역·표본까지 담아 낸 것이라 그대로 낸다 —
        #    숫자로 포맷하려 들면 깨지고, 여기서 다시 판정하면 게이트와 갈라진다.
        ("preservation_geometry_distance", f"{d['preservation_geometry_distance']:.6f}",
         ("≤ 잡음 바닥값 · " + str(d["preservation_baseline"]))
         if d["preservation_baseline"] is not None else "(바닥값 없음 — D5-b 대기)",
         mark(g.get("preservation"))),
        ("transfer_saving", f"{d['transfer_saving'] * 100:.2f}%", "> 40%",
         mark(d["transfer_saving"] > 0.40)),
        ("outside_new / outside_removed",
         f"{d['outside_new_voxels']} / {d['outside_removed_voxels']}",
         "편집 영역 밖 — 0 이어야 정상", "참고"),
    ]
    # G2 3항목 종합. 육안 확인은 코드가 만들 수 없는 사실이라 항상 '미결' 로 시작한다.
    rows.append(("── G2 효능", mark(g["efficacy"]),
                 "숫자 AND 육안(사용자 확인 필요)", mark(g["efficacy"])))
    rows.append(("── G2 보존", mark(g["preservation"]),
                 "승계 바이트 100% AND 기하거리 ≤ 바닥값", mark(g["preservation"])))
    rows.append(("── G2 절감", mark(g["saving"]), "> 40%", mark(g["saving"])))
    return rows


_SOURCE_LABEL = {
    "synthetic": (
        "출처: server/tests/test_pipeline._run_pipeline() — 맥북 담당분의 "
        "합성 픽스처(구 2개 + 육면체)를 그대로 통과시킨 결과."
    ),
    "real": (
        "출처: A5000 :8082 TRELLIS-image-large 가 만든 실자산 .cbin "
        "(base 181청크 · donor 294청크). Z-Image→BiRefNet RGBA 로 생성."
    ),
}


@app.get("/")
def debugview_page() -> Response:
    return _page("synthetic")


@app.get("/real")
def debugview_real() -> Response:
    return _page("real")


def _page(kind: str) -> Response:
    s = _scene(kind)
    html = s["mod"].render_html(
        s["run"], source_label=_SOURCE_LABEL[kind], gate_rows=_gate_rows(s["run"]),
        kind=kind, recolor_ready=_recolor_ready(),
    )
    return Response(html, media_type="text/html; charset=utf-8")


@app.get("/debug/{pane}.{axis}.png")
def debug_pane(pane: str, axis: int, kind: str = "synthetic") -> Response:
    s = _scene(kind)
    scene = s["scene"]
    if pane not in scene or axis not in scene[pane]:
        raise HTTPException(status_code=404, detail=f"모르는 분면/축: {pane}.{axis}")
    return Response(
        s["mod"].pane_png(scene, pane, axis),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/debug/donor-depth.png")
def debug_donor_depth(kind: str = "real") -> Response:
    """기증자 앞면 깊이맵 — **4분할이 원리적으로 답할 수 없는 질문**을 위한 진단.

    분면 그림은 실루엣 투영이라 깊이를 뭉갠다. 호박의 삼각눈·톱니입처럼 파낸
    구멍은 뒷면 표면으로 메워져 실루엣에 안 나온다. 어두운 자리가 파인 곳이다.
    게이트 지표가 아니다 — 판정은 gate_g2() 가 한다.
    """
    s = _scene(kind)
    return Response(
        s["mod"].depth_png(s["run"]["donor"]),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/debug/recolor/{side}.png")
def debug_recolor(side: str) -> Response:
    """레벨1(색만) 편집의 before/after 컬러 렌더.

    실루엣·깊이맵은 색 채널을 안 본다 — 레벨1 은 기하가 불변이라 그 둘로는
    before/after 가 똑같이 나온다. 색 판정에는 이 그림이 정본이다.
    """
    from server import debugview

    if side not in ("before", "after"):
        raise HTTPException(status_code=404, detail=f"side 는 before/after: {side!r}")
    d = RECOLOR_BEFORE if side == "before" else RECOLOR_AFTER
    png = (
        debugview.color_front_png(d)
        if any(d.glob("*.cbin"))
        else debugview.placeholder_png()
    )
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


# ── W9 편집 결과 자리 (A5000 이 낸다) ────────────────────────────────────
#
# recolor 자리와 같은 규칙이다: **import 로 묶지 않는다.** 산출물 모양은 `.cbin`
# 청크 세트 하나뿐이므로(D8) 디렉터리 경로만 받는다. A5000 이 무엇을 노출하든
# 이 화면은 다시 안 고친다. 없으면 "미도착" 이라고 적는다.
#
# ⚠️ 여기서 하는 표면 복셀화는 **그림용이다** (D28: 진단 전용). 이 좌표로 마스크를
#    만들지 않는다 — 마스크는 A5000 이 slat 격자에서 만든다 (D34).
W9_BEFORE = Path(os.environ.get("W9_BEFORE_DIR", str(ASSET_ROOT / "dragon-c" / "chunks")))
W9_AFTER = Path(os.environ.get("W9_AFTER_DIR", str(ASSET_ROOT / "dragon-c" / "chunks_w9")))


def _w9_ready() -> bool:
    return any(W9_BEFORE.glob("*.cbin")) and any(W9_AFTER.glob("*.cbin"))


@app.get("/debug/w9/{side}.{kind}.png")
def debug_w9(side: str, kind: str) -> Response:
    """W9 편집 결과 before/after. `kind` ∈ front · depth.

    실루엣은 형태 변화(머리 3개)를, 깊이맵은 오목 디테일을 본다 — D19 가
    육안 게이트의 정본을 깊이맵으로 정한 것과 같은 이유로 둘 다 낸다.
    """
    from server import debugview
    from server.realasset import cbin_dir_to_occupancy

    if side not in ("before", "after") or kind not in ("front", "depth"):
        raise HTTPException(status_code=404, detail=f"side/kind 가 틀렸다: {side}.{kind}")
    d = W9_BEFORE if side == "before" else W9_AFTER
    if not any(d.glob("*.cbin")):
        return Response(debugview.placeholder_png(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})
    cells = cbin_dir_to_occupancy(d)          # 그림용 (D28 진단 전용)
    png = (debugview.depth_png(cells) if kind == "depth"
           else debugview.pane_png({"x": {1: debugview.silhouette_png_arr(cells, 1)}}, "x", 1))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


# ── 최근 산출물 목록 · 상세 (사용자 요청) ─────────────────────────────────
#
# ⚠️ 라우트를 `/runs/...` 아래에 둔다. `/debug/*.png` 아래에 점 구분 경로를 만들면
#    기존 `/debug/{pane}.{axis}.png`(axis:int) 와 충돌해 422 가 난다 — W7 에서 겪었다.
#    FastAPI 는 먼저 등록된 라우트가 이긴다.
#
# ⚠️ `run_id` 는 경로의 해시다. 라우트가 임의 경로를 열지 못하고, 화면에 절대경로가
#    안 나간다 (§7 — 공개 URL 이다).
_RUN_IMG_CACHE: dict = {}


def _find_run(run_id: str):
    from server import runs as runs_mod

    for r in runs_mod.recent_runs(limit=32):
        if r.run_id == run_id:
            return r
    raise HTTPException(status_code=404, detail=f"모르는 run: {run_id}")


@app.get("/runs")
def runs_index() -> Response:
    from server import debugview
    from server import runs as runs_mod

    return Response(
        debugview.render_runs_index(runs_mod.recent_runs(limit=5)),
        media_type="text/html; charset=utf-8",
    )


@app.get("/runs/{run_id}")
def runs_detail(run_id: str) -> Response:
    from server import debugview
    from server import runs as runs_mod

    r = _find_run(run_id)
    return Response(
        debugview.render_run_detail(r, runs_mod.detail_kind(r.kind)),
        media_type="text/html; charset=utf-8",
    )


@app.get("/runs/{run_id}/img/{name}")
def runs_image(run_id: str, name: str) -> Response:
    """상세 화면의 그림. 종류별 정본은 `runs.detail_kind()` 가 정한다."""
    from server import debugview

    if name not in ("front", "side", "top", "depth", "color", "color_after"):
        raise HTTPException(status_code=404, detail=f"모르는 그림: {name}")
    r = _find_run(run_id)
    key = (run_id, name)
    if key in _RUN_IMG_CACHE:
        return Response(_RUN_IMG_CACHE[key], media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    d = r.after_chunk_dir if name == "color_after" else r.chunk_dir
    if d is None:
        png = debugview.placeholder_png()
    elif name.startswith("color"):
        png = debugview.color_front_png(d)
    else:
        from server.realasset import cbin_dir_to_occupancy

        cells = cbin_dir_to_occupancy(d)   # 그림용 (D28: 진단 전용)
        if name == "depth":
            png = debugview.depth_png(cells)
        else:
            axis = {"front": 1, "side": 0, "top": 2}[name]
            png = debugview._png(debugview.silhouette_png_arr(cells, axis))
    _RUN_IMG_CACHE[key] = png
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


# ── W8 인계 (D27) ──────────────────────────────────────────────────────
#
# A5000 에 **밀어 넣을 수 없다**: tcp/22(ssh)·873(rsync) 이 닫혀 있고, 열려 있는
# 8082 는 이번 웨이브 금지다(A5000 이 GPU 작업 중). 그래서 **당겨 가게** 낸다 —
# 이쪽은 공개 URL 이 이미 서 있으므로 이게 실제로 존재하는 유일한 채널이다.
#
# 파일 하나만 고정 경로로 낸다. 경로를 파라미터로 받지 않는다 — 공개 URL 에서
# 임의 경로를 열어 주면 그건 다른 종류의 사고다.
HANDOFF_DIR = Path(os.environ.get("HANDOFF_DIR", str(ASSET_ROOT / "_handoff-w8")))
_HANDOFF_FILES = {
    "w8-dragon-c.tar.gz": "application/gzip",
    "w8-dragon-c.tar.gz.sha256": "text/plain; charset=utf-8",
    "RECEIPT.json": "application/json; charset=utf-8",
}


@app.get("/handoff/{name}")
def handoff(name: str) -> Response:
    """인계 번들. 수령 확인은 **sha256 대조**다 — 파일 존재 확인이 아니다 (D27②)."""
    media = _HANDOFF_FILES.get(name)
    if media is None:
        raise HTTPException(
            status_code=404,
            detail=f"모르는 인계 파일: {name!r}. 아는 것: {sorted(_HANDOFF_FILES)}",
        )
    path = HANDOFF_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"아직 준비되지 않았다: {name}")
    return Response(
        path.read_bytes(), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/debug/metrics.json")
def debug_metrics(kind: str = "synthetic") -> dict:
    """화면에 뜬 것과 **같은** 수치. 그림과 숫자가 다른 소스에서 나오면 안 된다.

    지표 본문은 `MetricReport.as_dict()` 를 그대로 낸다 — 여기서 다시 조립하면
    rev6 처럼 지표가 개정될 때마다 두 곳이 어긋난다.
    """
    from server.metrics import NoiseFloorUnknown

    s = _scene(kind)
    run = s["run"]
    r, sp, bk, pkg, m = run["report"], run["splice"], run["bk"], run["pkg"], run["mask"]
    try:
        gate = r.gate_g2()
    except NoiseFloorUnknown as exc:
        gate = {"error": "NoiseFloorUnknown", "detail": str(exc)}
    return {
        "source": kind,
        "gate_g2": gate,
        "metrics": r.as_dict(),
        "walkthrough": {
            "n_base": sp.n_base,
            "n_donor_cropped": sp.n_donor_cropped,
            "n_result": sp.n_result,
            "n_cleared_occupied": sp.n_cleared_occupied,
            "n_donor_outside_mask": sp.n_donor_outside_mask,
            "mask_cells": m.n_cells,
            "mask_dilated": m.n_dilated,
            "book": bk.n_book,
            "changed": len(bk.changed),
            "removed": len(bk.removed),
            "full_bytes": pkg.full_bytes,
            "delta_bytes": pkg.delta_bytes,
        },
        **BUILD,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("DEBUGVIEW_HOST", "0.0.0.0"),
        port=int(os.environ.get("DEBUGVIEW_PORT", "8083")),
        log_level="info",
    )
