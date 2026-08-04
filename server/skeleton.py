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

# 실자산 관통 파라미터. 합성 픽스처의 상수를 그대로 쓰면 **안 된다** — 실측으로 정했다:
# base·donor 가 각자 독립적으로 정규화돼 둘 다 격자를 꽉 채우므로(base span 49×50×64,
# donor 60×60×64), 기증자를 크롭하지 않으면 머리 마스크(span 49×50×23)에 절대 안 들어간다.
# 스케일은 계약이 금지한다(6-이웃 유지율 s=2.0 → 0%). 그래서 크롭으로만 맞춘다.
REAL_HEAD_FRACTION = float(os.environ.get("REAL_HEAD_FRACTION", "0.35"))
REAL_CROP_FRACTION = float(os.environ.get("REAL_CROP_FRACTION", "0.30"))


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
        ("efficacy_largest_component", f"{d['efficacy_largest_component']:.4f}",
         "≥ 0.80", mark(d["efficacy_largest_component"] >= 0.8)),
        ("churn_ratio (안/밖)", f"{d['churn_ratio']:.3f}", "≥ 3.0",
         mark(d["churn_ratio"] >= 3.0)),
        ("inherited_byte_identity", f"{d['inherited_byte_identity']:.4f}", "== 1.0",
         mark(d["inherited_byte_identity"] >= 1.0)),
        ("preservation_geometry_distance", f"{d['preservation_geometry_distance']:.6f}",
         ("≤ 잡음 바닥값 "
          + (f"{d['preservation_baseline']:.6f}" if d["preservation_baseline"] is not None
             else "(바닥값 없음 — D5-b 대기)")),
         mark(None if d["preservation_baseline"] is None
              else d["preservation_geometry_distance"] <= d["preservation_baseline"])),
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
        kind=kind,
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
