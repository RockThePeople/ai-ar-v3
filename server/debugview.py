"""DebugView — 관통 결과를 **눈으로** 확인하는 4분할.

`docs/PROGRESS.md` §5 S2-6 이 요구하는 것이다: 원본 / 결과 / 마스크 / 변경청크.

────────────────────────────────────────────────────────────────────────
왜 지금 합성 데이터로 만드나
────────────────────────────────────────────────────────────────────────
처음 만들 때 실자산 `.cbin` 이 아직 없었다(A5000 다운). 그걸 기다리면
"보이는가" 라는 질문이 자산 도착 시점까지 통째로 밀린다. best-practices §6 과
PROGRESS §2 D3-⑤ 가 같은 말을 한다 — **프로젝트가 실제로 죽는 지점은 핵심 로직이
아니라 포트·방화벽·렌더 경로다.** 그래서 맥북이 이미 통과시킨 합성 픽스처
(구 2개 + 육면체)를 그대로 화면에 띄운다.

실자산은 W3 에서 도착했고 `/real` 이 같은 렌더러로 그린다 — `build_scene()` 은
합성인지 실자산인지 **모른다.** 입력이 `_run_pipeline()` 이냐
`run_real_walkthrough()` 냐만 다르고, 둘의 반환 모양이 같기 때문이다.
D11(기증자 크기)이 닫히면 `realasset.py` 의 파라미터만 바뀌고 이 파일은 손댈 것이 없다.

⚠️ 그래서 이 화면에 지금 보이는 것은 **눈사람이 아니라 구 두 개**이고,
   호박이 아니라 **육면체**다. 화면에도 그렇게 적는다 — 합성 결과를 실자산처럼
   보이게 두는 것이 이 프로젝트가 여섯 번 물린 자리다(방법론 5조 1번).

────────────────────────────────────────────────────────────────────────
D9 — 좌표계를 여기서 변환하지 않는다
────────────────────────────────────────────────────────────────────────
GLB 는 Y-up, 복셀 격자는 Z-up 이다. 이 모듈은 **그 변환을 하지 않는다.**
받는 것은 이미 VOXEL 격자(Z-up)의 셀 배열이고, 하는 일은 그것을 축 하나로
투영해 2D 그림으로 만드는 것뿐이다. 투영과 화면 방향 맞춤(전치·상하반전)은
**표시용**이지 자산의 좌표계 변환이 아니다.

투영 자체도 새로 쓰지 않았다 — `server.metrics._silhouette` 을 그대로 부른다.
게이트가 실루엣을 재는 함수와 화면이 그리는 함수가 **같아야** 화면과 숫자가
어긋나지 않는다. (그 함수가 private 인 것은 맥북 담당 판단이라 그대로 두고
호출만 한다. 공개가 필요하면 맥북 세션에 올린다.)

────────────────────────────────────────────────────────────────────────
왜 뷰가 3개인가
────────────────────────────────────────────────────────────────────────
D5 가 비싸게 배운 것이다 — 시선 방향으로 튀어나온 변화는 정면에서 0.16%,
옆면에서 14.3% 였다. **한 뷰만 보여주는 화면은 편집을 숨긴다.** 그래서 각
분면마다 정면·옆면·위 세 방향을 같이 낸다.
"""

from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Optional, Tuple

import numpy as np

from deltacontract.coords import (  # type: ignore[import-not-found]
    CHUNK_SIZE,
    VOXEL_RES,
    chunk_key,
    voxel_to_chunk,
)

from . import metrics

__all__ = ["AXES", "PANES", "build_scene", "pane_png", "render_html"]

# ── 색 (다크) ─────────────────────────────────────────────────────────
BG = (0x10, 0x13, 0x1A)
GEOM = (0xD8, 0xDE, 0xE9)      # 기하 — 밝은 회색
GEOM_DIM = (0x3A, 0x42, 0x52)  # 배경으로 깔리는 기하
NEW = (0x22, 0xC5, 0x5E)       # 신규 복셀 — 초록
GONE = (0xEF, 0x44, 0x44)      # 제거된 자리 — 빨강
MASK = (0x38, 0xBD, 0xF8)      # 마스크 — 시안
HALO = (0x1E, 0x5F, 0x7A)      # halo 만 — 어두운 시안
BOOK = (0xF5, 0x9E, 0x0B)      # 변경청크 — 호박색

MISSING_TXT = "미도착"
NOT_APPLICABLE_TXT = "해당 없음"

SCALE = 8  # 64 → 512 픽셀 (최근접. 복셀 경계를 뭉개지 않는다)

# (axis, 라벨). metrics._silhouette 의 axis 규약을 그대로 쓴다.
AXES: Tuple[Tuple[int, str], ...] = (
    (1, "정면 · 가로 X · 세로 Z"),
    (0, "옆면 · 가로 Y · 세로 Z"),
    (2, "위에서 · 가로 X · 세로 Y"),
)

PANES: Tuple[Tuple[str, str], ...] = (
    ("original", "① 원본 (base)"),
    ("result", "② 결과 (splice)"),
    ("mask", "③ 마스크 + halo"),
    ("book", "④ 변경청크 하이라이트"),
)


# ══════════════════════════════════════════════════════════ PNG (표준 라이브러리만)
def _png(rgb: np.ndarray) -> bytes:
    """(H,W,3) uint8 → PNG 바이트.

    Pillow 를 쓰지 않는다. `server/requirements.txt` 에 의존성을 하나 더 넣으려면
    사용자 승인이 필요한데(CLAUDE.md), 화면 하나 그리자고 GPU 서버 두 대의 환경을
    건드릴 이유가 없다. zlib·struct 는 표준 라이브러리다.
    """
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# ══════════════════════════════════════════════════════════ 투영
def _project(cells: Optional[np.ndarray], axis: int) -> np.ndarray:
    """VOXEL 셀 → 화면 방향으로 맞춘 2D 불리언.

    ★ 좌표계 변환이 아니다. `metrics._silhouette` 이 낸 2D 배열을 화면에서
      **세로축이 위로 증가**하도록 전치·상하반전한 것뿐이다 (D9).
    """
    if cells is None or len(cells) == 0:
        return np.zeros((VOXEL_RES, VOXEL_RES), dtype=bool)
    sil = metrics._silhouette(metrics._codes(cells), axis)
    return np.flipud(sil.T)


def _canvas() -> np.ndarray:
    img = np.zeros((VOXEL_RES, VOXEL_RES, 3), dtype=np.uint8)
    img[:, :] = BG
    return img


def _paint(img: np.ndarray, layer: np.ndarray, color) -> None:
    """나중에 칠한 색이 이긴다. 레이어 순서가 곧 우선순위다."""
    img[layer] = color


def _upscale(img: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(img, SCALE, axis=0), SCALE, axis=1)


# ══════════════════════════════════════════════════════════ 장면
def _book_cells(result_cells: np.ndarray, book: List[str]) -> np.ndarray:
    """부기에 든 청크에 속하는 결과 복셀만 골라낸다.

    청크 좌표 계산을 손으로 하지 않는다 — `voxel_to_chunk` · `chunk_key` 가 계약의
    정본이다 (CLAUDE.md: 계약을 임포트해서 쓴다).
    """
    if len(result_cells) == 0 or not book:
        return np.zeros((0, 3), dtype=np.int64)
    wanted = set(book)
    cids = voxel_to_chunk(result_cells)
    keep = np.array(
        [chunk_key(c) in wanted for c in cids], dtype=bool
    )
    return np.asarray(result_cells)[keep]


def build_scene(run: dict) -> Dict[str, Dict[int, np.ndarray]]:
    """관통 결과 → {pane: {axis: RGB 이미지}}.

    `run` 은 `server.tests.test_pipeline._run_pipeline()` 의 반환 그대로다.
    이 모듈은 파이프라인을 **다시 구현하지 않는다** — 결과를 받아 그리기만 한다.
    """
    base = run["base"]
    res = run["splice"].cells
    mask = run["mask"]
    book = run["bk"].book

    base_codes = metrics._codes(base)
    res_codes = metrics._codes(res)
    new_codes = np.setdiff1d(res_codes, base_codes, assume_unique=True)
    gone_codes = np.setdiff1d(base_codes, res_codes, assume_unique=True)

    def cells_of(codes: np.ndarray) -> np.ndarray:
        if codes.size == 0:
            return np.zeros((0, 3), dtype=np.int64)
        return np.stack(
            [codes // (VOXEL_RES * VOXEL_RES),
             (codes // VOXEL_RES) % VOXEL_RES,
             codes % VOXEL_RES],
            axis=-1,
        )

    new_cells = cells_of(new_codes)
    gone_cells = cells_of(gone_codes)
    halo_only = _setdiff_cells(mask.dilated, mask.cells)
    book_cells = _book_cells(res, book)

    scene: Dict[str, Dict[int, np.ndarray]] = {k: {} for k, _ in PANES}
    for axis, _label in AXES:
        p_base = _project(base, axis)
        p_res = _project(res, axis)
        p_new = _project(new_cells, axis)
        p_gone = _project(gone_cells, axis)
        p_mask = _project(mask.cells, axis)
        p_halo = _project(halo_only, axis)
        p_book = _project(book_cells, axis)

        # ① 원본
        img = _canvas(); _paint(img, p_base, GEOM)
        scene["original"][axis] = _upscale(img)

        # ② 결과 — 신규는 초록, 사라진 자리는 빨강으로 같이 보여준다.
        #    "무엇이 바뀌었나" 를 화면에서 바로 읽으라고 넣은 것이지, 이 색이
        #    게이트 판정에 쓰이지는 않는다.
        img = _canvas()
        _paint(img, p_gone, GONE)
        _paint(img, p_res, GEOM)
        _paint(img, p_new, NEW)
        scene["result"][axis] = _upscale(img)

        # ③ 마스크 — halo 를 원본 마스크와 **다른 색**으로 낸다.
        #    효능은 mask.cells 에서, 보존은 mask.dilated 밖에서 잰다. 두 영역이
        #    다르다는 사실이 화면에서 보여야 한다 (metrics.py 모듈 docstring).
        img = _canvas()
        _paint(img, p_base, GEOM_DIM)
        _paint(img, p_halo, HALO)
        _paint(img, p_mask, MASK)
        scene["mask"][axis] = _upscale(img)

        # ④ 변경청크 — 부기에 든 청크에 속한 결과 복셀만 호박색
        img = _canvas()
        _paint(img, p_res, GEOM_DIM)
        _paint(img, p_book, BOOK)
        scene["book"][axis] = _upscale(img)

    return scene


def _setdiff_cells(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ca, cb = metrics._codes(a), metrics._codes(b)
    only = np.setdiff1d(ca, cb, assume_unique=True)
    if only.size == 0:
        return np.zeros((0, 3), dtype=np.int64)
    return np.stack(
        [only // (VOXEL_RES * VOXEL_RES),
         (only // VOXEL_RES) % VOXEL_RES,
         only % VOXEL_RES],
        axis=-1,
    )


def pane_png(scene, pane: str, axis: int) -> bytes:
    return _png(scene[pane][axis])


# ══════════════════════════════════════════════════════════ HTML
_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:20px; background:#0b0e14; color:#d8dee9;
       font:13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
h1 { font-size:17px; margin:0 0 4px; }
.sub { color:#8b96a8; margin:0 0 14px; }
.warn { border:1px solid #b45309; background:#2a1c07; color:#fbbf24;
        padding:10px 12px; border-radius:6px; margin:0 0 10px; }
.ok { border:1px solid #15803d; background:#07220f; color:#86efac;
      padding:10px 12px; border-radius:6px; margin:0 0 10px; }
a { color:#7dd3fc; }
.grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
@media (max-width:900px){ .grid{ grid-template-columns:1fr; } }
.pane { border:1px solid #232a36; border-radius:8px; background:#10131a; padding:10px; }
.pane h2 { font-size:13px; margin:0 0 8px; color:#e5e9f0; }
.views { display:flex; gap:8px; flex-wrap:wrap; }
.view { flex:1 1 0; min-width:0; }
.view img { width:100%; height:auto; display:block; image-rendering:pixelated;
            border:1px solid #232a36; border-radius:4px; background:#10131a; }
.view span { display:block; color:#6b7688; font-size:11px; margin-top:4px; }
.legend { margin-top:8px; color:#8b96a8; font-size:11px; }
.legend.warn { color:#e8b46a; }
.sw { display:inline-block; width:9px; height:9px; border-radius:2px;
      margin:0 4px 0 10px; vertical-align:middle; }
table { border-collapse:collapse; margin-top:18px; width:100%; }
th,td { border:1px solid #232a36; padding:5px 9px; text-align:left; }
th { background:#161b24; color:#8b96a8; font-weight:600; }
td.n { text-align:right; font-variant-numeric:tabular-nums; }
.pass { color:#22c55e; } .fail { color:#ef4444; }
.undecided { color:#fbbf24; } .ref { color:#6b7688; }
.foot { color:#6b7688; margin-top:16px; font-size:11px; }
"""

_LEGEND = {
    "original": [("기하", GEOM)],
    "result": [("기하", GEOM), ("신규 복셀", NEW), ("사라진 자리", GONE)],
    "mask": [("원본 마스크", MASK), ("halo 만", HALO), ("기하", GEOM_DIM)],
    "book": [("부기(변경) 청크", BOOK), ("나머지 기하", GEOM_DIM)],
}


def _rgb(c) -> str:
    return "#%02x%02x%02x" % c


def _banner(kind: str, source_label: str) -> str:
    """화면 맨 위 경고. **합성을 실자산처럼 보이게 두지 않는다.**

    이 프로젝트가 여섯 번 물린 자리가 "숫자는 통과했는데 그게 무엇을 잰 건지 몰랐다"
    이다(방법론 5조). 그래서 무엇을 보고 있는지를 화면이 먼저 말한다.
    """
    if kind == "real":
        return (
            '<div class="ok"><b>실자산이다.</b><br>'
            f"{source_label}<br>"
            "D11(rev7) — 기증자는 크롭이 아니라 <b>마스크 범위에 맞춘 재복셀화</b>로 "
            "들어간다(<code>fit_donor_to_mask</code>). 희소 좌표를 곱하는 것이 아니라 "
            "연속 메시를 새 cell_size 로 래스터화하므로 계약이 금지한 스케일이 아니다. "
            "W3 의 crop 0.30(호박 위쪽 뚜껑만)은 이제 쓰지 않는다 — 호박 <b>전체</b>가 들어간다."
            "</div>"
        )
    return (
        '<div class="warn"><b>⚠️ 지금 보이는 것은 합성 픽스처다. 실자산이 아니다.</b><br>'
        f"{source_label}<br>"
        "화면의 base 는 <b>구 2개</b>(눈사람이 아니다), donor 는 <b>육면체</b>"
        "(호박이 아니다)다. 실자산은 <a href=\"/real\">/real</a> 에 있다.</div>"
    )


def recolor_pane(ready: bool) -> str:
    """⑥ 레벨1(색만) 자리. 산출물이 없으면 **미도착이라고 적는다.**"""
    note = (
        "레벨1 은 <b>기하가 불변</b>이라 위 실루엣·깊이맵으로는 before/after 가 똑같이 나온다 "
        "— 두 그림 다 색 채널을 안 본다. 색 판정은 이 그림이 정본이다."
    )
    if not ready:
        return (
            '<div class="pane" style="margin-bottom:14px"><h2>⑥ 레벨1 색 편집 (recolor)</h2>'
            '<div class="legend"><b>산출물 미도착.</b> 맥북의 <code>recolor.py</code> 가 '
            '<code>.cbin</code> 세트를 내면 <code>RECOLOR_BEFORE_DIR</code> / '
            '<code>RECOLOR_AFTER_DIR</code> 를 그쪽으로 돌리면 된다 — 이 화면은 안 고친다.<br>'
            f'{note}</div></div>'
        )
    views = "".join(
        f'<div class="view"><img src="/debug/recolor/{side}.png" alt="recolor {side}">'
        f"<span>{label}</span></div>"
        for side, label in (("before", "편집 전"), ("after", "편집 후 (마스크 안만 색 변경)"))
    )
    return (
        '<div class="pane" style="margin-bottom:14px"><h2>⑥ 레벨1 색 편집 (recolor)</h2>'
        f'<div class="views">{views}</div>'
        f'<div class="legend">{note}</div></div>'
    )


def render_html(run: dict, *, source_label: str, gate_rows, kind: str = "synthetic",
                recolor_ready: bool = False) -> str:
    rep = run["report"]
    sp = run["splice"]
    bk = run["bk"]
    pkg = run["pkg"]
    m = run["mask"]

    panes = []
    for key, title in PANES:
        views = "".join(
            f'<div class="view"><img src="/debug/{key}.{axis}.png?kind={kind}" '
            f'alt="{title} {lab}"><span>{lab}</span></div>'
            for axis, lab in AXES
        )
        legend = "".join(
            f'<span class="sw" style="background:{_rgb(c)}"></span>{name}'
            for name, c in _LEGEND[key]
        )
        panes.append(
            f'<div class="pane"><h2>{title}</h2><div class="views">{views}</div>'
            f'<div class="legend">{legend}</div></div>'
        )

    # 판정 문자열은 `MetricReport.gate_g2()` 가 만든 것을 그대로 받는다.
    # 여기서 임계값을 다시 계산하지 않는다 — 화면이 게이트와 갈라지면 안 된다.
    _CLS = {"통과": "pass", "미달": "fail", "미결": "undecided", "참고": "ref"}
    rows = "".join(
        f'<tr><td>{name}</td><td class="n">{val}</td><td>{crit}</td>'
        f'<td class="{_CLS.get(verdict, "ref")}">{verdict}</td></tr>'
        for name, val, crit, verdict in gate_rows
    )

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-ar-v3 DebugView — 관통</title><style>{_CSS}</style></head><body>
<h1>ai-ar-v3 DebugView — S2 관통 4분할</h1>
<p class="sub">복셀 격자 {VOXEL_RES}³ · 청크 {CHUNK_SIZE}³ · 최근접 {SCALE}배 확대</p>

{_banner(kind, source_label)}
<p class="sub"><a href="/">합성 픽스처</a> · <a href="/real">실자산</a> ·
<a href="/debug/metrics.json?kind={kind}">metrics.json</a></p>
<div class="pane" style="margin-bottom:14px"><h2>⑤ 기증자 앞면 깊이맵 (진단 · 게이트 아님)</h2>
<div class="views"><div class="view" style="max-width:340px">
<img src="/debug/donor-depth.png?kind={kind}" alt="기증자 앞면 깊이맵">
<span>밝을수록 앞 · <b>어두운 자리가 파인 곳</b></span></div></div>
<div class="legend">위 4분할은 <b>실루엣 투영</b>이라 깊이를 뭉갠다 — 파낸 구멍(삼각눈·톱니입)이
뒷면으로 메워져 <b>원리적으로 안 보인다</b>. 그 질문에만 답하는 진단 그림이다.</div></div>

{recolor_pane(recolor_ready)}
<div class="grid">{"".join(panes)}</div>

<table>
<tr><th>D5-a/D5-b 게이트 지표 (rev6)</th><th>값</th><th>기준</th><th>판정</th></tr>
{rows}
</table>

<table>
<tr><th>관통 계측</th><th>값</th></tr>
<tr><td>base 점유 셀</td><td class="n">{sp.n_base}</td></tr>
<tr><td>donor 크롭 후 셀</td><td class="n">{sp.n_donor_cropped}</td></tr>
<tr><td>결과 점유 셀</td><td class="n">{sp.n_result}</td></tr>
<tr><td>마스크 셀 / halo 팽창 후</td><td class="n">{m.n_cells:,} / {m.n_dilated:,}</td></tr>
<tr><td>마스크가 격자에서 차지하는 비율</td>
    <td class="n">{100.0 * m.n_cells / (VOXEL_RES ** 3):.2f}%</td></tr>
<tr><td>🔴 비우기가 실제로 지운 점유 셀</td><td class="n">{sp.n_cleared_occupied}</td></tr>
<tr><td>부기 청크 (changed / removed)</td>
    <td class="n">{bk.n_book} ({len(bk.changed)} / {len(bk.removed)})</td></tr>
<tr><td>전체 재전송 바이트</td><td class="n">{pkg.full_bytes:,}</td></tr>
<tr><td>실제 전송 바이트</td><td class="n">{pkg.delta_bytes:,}</td></tr>
</table>

<p class="foot">
투영은 <code>server.metrics._silhouette</code> 을 그대로 부른다 — 게이트가 재는 함수와
화면이 그리는 함수가 같아야 숫자와 그림이 어긋나지 않는다.
좌표계 변환은 하지 않는다(D9): 입력은 이미 VOXEL 격자(Z-up)이고, 전치·상하반전은
화면 방향 맞춤이다. 뷰가 3개인 것은 D5 실측 때문이다 — 같은 편집이 정면 0.16%,
옆면 14.3% 였다. 한 뷰만 보여주는 화면은 편집을 숨긴다.
</p>
</body></html>"""


# ══════════════════════════════════════════════════════ 앞면 깊이맵 (진단용)
#
# 왜 4분할만으로는 부족한가: 분면 그림은 **실루엣 투영**이다. 깊이를 뭉개므로
# 파낸 구멍(호박의 삼각눈·톱니입)이 뒷면 표면으로 메워져 **원리적으로 안 보인다.**
# "호박이 얼굴을 갖고 있는가" 는 이 프로젝트의 육안 판정 핵심인데, 그 질문에
# 실루엣은 답할 수 없다. 그래서 진단용으로 앞면 깊이맵을 따로 낸다.
#
# 게이트 지표가 아니다. 판정은 여전히 `gate_g2()` 가 한다.
def depth_png(cells: np.ndarray, *, scale: int = 10, view_axis: int = 1) -> bytes:
    """깊이맵 PNG. 시선 축을 향해 가장 가까운 복셀의 깊이를 밝기로.

    밝을수록 앞, 어두울수록 뒤 — 즉 **어두운 자리가 파인 곳**이다.

    ⚠️ `view_axis` 를 두는 이유: 정면 하나로는 **자산에 따라 판정을 못 한다.**
       moto-b 가 그 자리였다 — 긴 축이 y 라, 정면(y 시선)에서는 오토바이를 앞에서
       본 그림이 나오고 바퀴가 전부 겹친다. 바퀴가 몸체와 갈리는지는 옆면(x 시선)
       에서만 보인다. 기본값은 종전 그대로 y 다.
    """
    c = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if view_axis != 1:
        # 보고 싶은 축을 깊이 자리(1)로 옮긴다. 좌표계 변환이 아니라 **표시용**이다 (D9).
        order = [a for a in (0, 1, 2) if a != view_axis]
        c = c[:, [order[0], view_axis, order[1]]]
    if c.size == 0:
        return _png(np.zeros((scale, scale, 3), dtype=np.uint8))
    c = c - c.min(axis=0)
    span = c.max(axis=0) + 1
    depth = np.full((span[0], span[2]), -1, dtype=np.int64)
    for x, y, z in c:
        if depth[x, z] < 0 or y < depth[x, z]:
            depth[x, z] = y

    valid = depth >= 0
    img = np.zeros((span[2], span[0], 3), dtype=np.uint8)
    if valid.any():
        d = depth.astype(float)
        lo, hi = d[valid].min(), d[valid].max()
        t = np.zeros_like(d)
        t[valid] = 1.0 - (d[valid] - lo) / max(hi - lo, 1.0)
        for x in range(span[0]):
            for z in range(span[2]):
                if valid[x, z]:
                    k = t[x, z]
                    img[span[2] - 1 - z, x] = (
                        int(40 + 215 * k), int(20 + 140 * k), int(10 + 30 * k)
                    )
    return _png(np.repeat(np.repeat(img, scale, axis=0), scale, axis=1))


# ══════════════════════════════════════════════════════ 색 렌더 (레벨1 / recolor)
#
# 왜 실루엣·깊이맵으로는 부족한가: 레벨1 은 **기하가 불변**이다. 형태가 안 바뀌므로
# 실루엣도 깊이맵도 before/after 가 똑같이 나온다 — 두 그림 다 색 채널을 안 본다.
# 색 편집을 눈으로 판정하려면 색을 그리는 그림이 따로 있어야 한다 (D19 와 같은 논리).
#
# 입력은 **`.cbin` 디렉터리**다. recolor 가 무엇을 노출하든 이 시스템의 산출물 모양은
# 청크 세트 하나뿐이므로(D8), 새 계약을 발명하지 않고 그것만 받는다.
def color_front_png(chunk_dir, *, scale: int = 8) -> bytes:
    """`.cbin` 디렉터리 → 정면 컬러 렌더 PNG.

    각 (x,z) 기둥에서 **가장 앞(min y)** 정점의 색을 쓴다. 색이 없는 청크
    (`FLAG_COLOR` 미설정)는 건너뛴다 — 무채색으로 채우면 "색이 안 바뀌었다" 와
    "색이 애초에 없다" 가 화면에서 같아 보인다.
    """
    from pathlib import Path

    from deltacontract.chunkbin import decode  # type: ignore[import-not-found]
    from deltacontract.coords import normalized_to_voxel  # type: ignore[import-not-found]

    best = {}
    for f in sorted(Path(chunk_dir).glob("*.cbin")):
        mesh = decode(f.read_bytes())
        if mesh.colors is None:
            continue
        cells = normalized_to_voxel(mesh.positions)
        for (x, y, z), c in zip(cells, mesh.colors):
            k = (int(x), int(z))
            if k not in best or y < best[k][0]:
                best[k] = (y, c[:3])

    img = _canvas()
    for (x, z), (_, c) in best.items():
        img[VOXEL_RES - 1 - z, x] = c
    return _png(np.repeat(np.repeat(img, scale, axis=0), scale, axis=1))


def placeholder_png(text_rows: int = 3, *, scale: int = 8) -> bytes:
    """산출물이 아직 없을 때의 자리표시. **빈 그림을 결과처럼 보이게 두지 않는다.**"""
    img = _canvas()
    for i in range(0, VOXEL_RES, 8):
        img[i : i + 1, :] = (0x3A, 0x42, 0x52)
    return _png(np.repeat(np.repeat(img, scale, axis=0), scale, axis=1))


def silhouette_png_arr(cells, axis: int):
    """단일 실루엣 RGB 배열. `/w9` 자리가 4분할 조립 없이 한 장만 필요할 때 쓴다."""
    img = _canvas()
    _paint(img, _project(cells, axis), GEOM)
    return _upscale(img)


# ══════════════════════════════════════════════ 최근 산출물 목록 · 상세
#
# 🔴 게이트 판정을 여기서 다시 계산하지 않는다. `gate_g2()` 가 정본이고 화면은
#    `runs.py` 가 **읽어 온 것**을 표시만 한다 (W5 규약). 문턱을 화면에 다시 적으면
#    게이트와 화면이 갈라지고, 갈라진 줄 아무도 모른다.
#
# 🔴 원시 개수가 비율보다 앞이다 (D37) — `runs._fmt_headline` 이 그 순서로 만든다.
_GATE_CLS = {"통과": "pass", "실패": "fail", "미결": "undecided",
             "해당 없음": "ref", "미도착": "ref"}
_KIND_KO = {"generate": "생성", "edit": "형태 변경", "recolor": "색 변경", "미상": "미상"}


#: 갈래별 표 머리말. Dragon 은 D56 으로 **종결**됐고 산출물만 남았다.
_TRACKS = (
    ("current", "현행 작업 (작업 1 / 2 / 3)",
     "여기 없는 작업의 자산은 <b>아직 안 만들었다</b>. 화면이 비어 있는 것이 "
     "그 사실이다 — 없는 것을 다른 갈래로 채우지 않는다."),
    ("dragon", "Dragon 갈래 — 참고 기록",
     "D56 으로 <b>종결된 갈래</b>다. 현행 작업의 직전 결과가 아니다 — "
     "대조군으로만 본다. 지우지 않는 이유는 지우면 대조군이 없어지기 때문이다."),
    ("legacy", "그 밖 — 이전 자산",
     "눈사람·호박 등 W2 때 확보한 것들이다. 현행 작업도 Dragon 갈래도 아니다."),
)


def render_runs_index(runs) -> str:
    """최근 산출물 목록. 없는 값은 '미도착' 으로 나간다 (원칙 7).

    🔴 갈래를 **갈라서** 낸다. 목록은 시간순이라, 섞어 놓으면 종결된 Dragon 런이
    현행 작업의 직전 결과처럼 읽힌다.
    """
    def _rows_for(runs):
        rows = []
        for r in runs:
            cls = _GATE_CLS.get(r.gate, "ref")
            if r.pending_reason:
                rows.append(
                    f'<tr class="pending"><td>—</td><td>{_KIND_KO.get(r.kind, r.kind)}</td>'
                    f'<td colspan="2">{r.rel} — {r.pending_reason}</td>'
                    f'<td class="ref">{r.gate}</td></tr>'
                )
                continue
            # 🔴 철회된 수치는 **철회됐다고 적고 낸다.** 지우면 "왜 안 보이지" 가 되고,
            #    그냥 두면 현행 수치와 구분이 안 된다. 둘 다 나쁘다.
            void = (f'<div class="void">철회 — {r.invalidated}</div>'
                    if r.invalidated else "")
            rows.append(
                f'<tr class="{"voided" if r.invalidated else ""}">'
                f'<td class="mono">{r.when}</td>'
                f"<td>{_KIND_KO.get(r.kind, r.kind)}</td>"
                f'<td class="mono">{r.asset_id or MISSING_TXT}</td>'
                # 취소선은 **수치 텍스트에만** 건다. 셀 전체에 걸면 철회 사유까지
                # 그어져서, 왜 철회됐는지를 읽지 말라는 화면이 된다.
                f'<td><span class="{"struck" if r.invalidated else ""}">{r.headline}</span>{void}</td>'
                # 사유를 같이 낸다 — "미도착" 두 건이 서로 다른 이유일 수 있다
                # (judgment.json 이 없다 vs gate_g2 블록이 없다). 라벨만으로는 못 가른다.
                f'<td class="{cls}">{"—" if r.gate == NOT_APPLICABLE_TXT else r.gate}'
                f'{f"<br><span class=\"why\">{r.gate_reason}</span>" if r.gate_reason else ""}</td>'
                f'<td><a href="/runs/{r.run_id}">상세 →</a></td></tr>'
            )
        return rows

    HEAD = ('<tr><th>시각</th><th>종류</th><th>자산 id</th><th>한눈 결과</th>'
            '<th>게이트</th><th></th></tr>')
    sections = ""
    for key, title, note in _TRACKS:
        rows = _rows_for([r for r in runs if r.track == key])
        if not rows:
            continue
        sections += (f"<h2>{title}</h2>"
                     + (f'<div class="legend">{note}</div>' if note else "")
                     + f"<table>{HEAD}{''.join(rows)}</table>")
    body = sections or '<table><tr><td>스캔된 산출물이 없다</td></tr></table>'
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-ar-v3 — 최근 산출물</title><style>{_CSS}
.mono {{ font-variant-numeric:tabular-nums; }}
.why {{ color:#6b7688; font-size:11px; }}
tr.pending td {{ color:#8b96a8; font-style:italic; }}
tr.voided td {{ color:#6b7688; }}
.struck {{ text-decoration:line-through; }}
.void {{ color:#e8b46a; font-size:11px; margin-top:4px; }}
</style></head><body>
<h1>최근 산출물 10건</h1>
<p class="sub"><a href="/">합성 픽스처</a> · <a href="/real">실자산</a> · <a href="/runs">목록</a></p>
{body}
<p class="foot">
게이트 열은 <code>gate_g2()</code> 가 기록한 것을 <b>읽기만</b> 한다 — 이 화면은 문턱을
다시 계산하지 않는다. 상태는 셋으로 갈린다:
<b>—(해당 없음)</b> G2 는 편집 게이트라 생성물에는 적용되지 않는다 ·
<b>미결</b> 판정에 필요한 값이 없다 ·
<b>미도착</b> judgment.json 이 없거나 <code>gate_g2</code> 블록이 없다.<br>
한눈 결과는 <b>원시 개수를 비율보다 앞에</b> 둔다 (D37) — halo 계열은 표본이 45~48복셀이라
비율에 유효숫자가 없다.
</p></body></html>"""


def render_run_detail(r, spec) -> str:
    """상세. **종류별 정본 그림이 다르다** — `runs.detail_kind()` 가 정한다."""
    labels = {"front": "정면 실루엣", "side": "옆면 실루엣", "top": "위 실루엣",
              "depth": "앞면 깊이맵", "depth_side": "옆면 깊이맵",
              "color": "색 렌더 (편집 전)",
              "color_after": "색 렌더 (편집 후)"}
    # A5000 이 렌더해 보낸 그림이 있으면 **그것이 정본**이다 — 깊이 카메라가 그쪽 것이고
    # 우리가 다시 렌더하면 다른 그림이 된다.
    delivered = r.delivered
    # ── 🔴 두 단계 개선을 **한 화면에서** 본다 (①보존 수정 → ②마스크 개선).
    #    따로 걸면 어느 단계가 무엇을 좋게 했는지 화면에서 안 갈린다. 둘 다 전폭이다 —
    #    판정 대상이 측면 머리의 주둥이·뿔 실루엣이라 줄이면 애초에 판정을 못 한다.
    stage = r.stage_pair
    stage_pane = ""
    if stage:
        stage_pane = (
            '<div class="pane"><h2>두 단계 — 위 ① / 아래 ②</h2>'
            + "".join(
                f'<div class="hero"><img src="/runs/{r.run_id}/img/{n}" alt="{lbl}">'
                f"<span>{lbl}</span></div>"
                for n, lbl in stage
            )
            + '<div class="legend">같은 자산·같은 절단면이다. ① 은 입력 바이트가 이전 웨이브와 '
            '동일하고 보존 수정만 다르다 — 그래서 둘의 차이는 <b>마스크</b> 차이다.</div></div>'
        )
    if delivered:
        skip = {n for n, _ in stage}
        canon_name = r.delivered_canonical
        if canon_name in skip:
            # 정본을 두 단계 창에서 이미 전폭으로 걸었다. 여기서 또 걸면 같은 그림이
            # 두 번 나오고, 두 번 나오면 어느 쪽이 정본인지가 흐려진다.
            canon_name = next((n for n in delivered if n not in skip), None)
        delivered = {k: v for k, v in delivered.items() if k not in skip}
    if delivered:
        # 정본은 **한 줄을 통째로** 쓴다. 나머지와 같은 크기로 늘어놓으면 정본이
        # 정본 구실을 못 한다 — 사용자가 보려는 그림이 그것이다.
        # 정본 표시는 **진짜 정본에만** 붙인다. 두 단계 창으로 옮겨 간 뒤 남은 그림에
        # 그대로 ★ 를 붙이면 화면이 정본을 두 개라고 말하게 된다.
        star = " ★ 정본" if canon_name == r.delivered_canonical else ""
        head = (
            f'<div class="hero"><img src="/runs/{r.run_id}/img/{canon_name}" '
            f'alt="{delivered[canon_name]}">'
            f'<span>{delivered[canon_name]}{star}</span></div>' if canon_name else ""
        )
        # 🔴 4방향 짝은 **정본과 같은 급**으로 전폭에 둔다. 옆·뒤 뷰가
        #    "정말 3D 로 바뀌었나" 에 답하는 그림이라 썸네일로 깔면 뜻이 없다.
        four = "_paired_4dir.png"
        if four in delivered:
            head += (
                f'<div class="hero"><img src="/runs/{r.run_id}/img/{four}" '
                f'alt="{delivered[four]}"><span>{delivered[four]}</span></div>'
            )
        tiles = [head] + [
            f'<div class="view"><img src="/runs/{r.run_id}/img/{n}" alt="{lbl}">'
            f"<span>{lbl}</span></div>"
            for n, lbl in delivered.items() if n not in (canon_name, four)
        ]
    else:
        tiles = []
        for name in spec["images"]:
            canon = " ★ 정본" if name == spec["canonical"] else ""
            tiles.append(
                f'<div class="view"><img src="/runs/{r.run_id}/img/{name}" alt="{labels[name]}">'
                f"<span>{labels[name]}{canon}</span></div>"
            )
    # ── 3D 뷰어 (model-viewer). 깊이맵은 **투영**이라 "3D 로 정말 바뀌었나" 에
    #    답하지 못한다 — 돌려 봐야 답이 나온다. CDN 스크립트 한 줄만 쓴다.
    models = r.models
    viewer = ""
    if models:
        cards = "".join(
            f'<div class="mv"><model-viewer src="/runs/{r.run_id}/img/{n}" '
            f'camera-controls touch-action="pan-y" auto-rotate rotation-per-second="20deg" '
            f'shadow-intensity="0.6" exposure="1.1" alt="{lbl}"></model-viewer>'
            f"<span>{lbl}</span></div>"
            for n, lbl in models.items()
        )
        only_after = "dragon-c_before.glb" not in models
        note_m = (
            '<div class="legend">⚠️ 편집 전 GLB 가 없어 <b>편집 후만</b> 걸었다.</div>'
            if only_after else
            '<div class="legend">드래그로 돌려 본다. 편집 전 GLB 는 3090 이 dragon-c 의 '
            '<code>.cbin</code> 청크를 이어 붙여 만든 것이다 — A5000 에 요청하지 않았고, '
            '<code>.cbin</code> 은 디코더가 낸 <b>실제 표면 메시</b>라 대용물이 아니다. '
            '좌표는 <code>frames.VOXEL_TO_GLB</code> 로 GLB 프레임에 맞췄다 — 안 걸면 '
            '편집 전만 90° 누워 보여 좌우 비교가 뜻을 잃는다 (D9).</div>'
            # 🔴 안 적으면 "색이 바뀌었다" 로 오독된다 — 이번 편집은 형태 편집이다.
            '<div class="legend warn">⚠️ 편집 전이 <b>흰색</b>인 것은 편집 결과가 아니다. '
            '<code>.cbin</code> 은 정점·면만 담고 <b>색 채널이 없다</b> — 여기서 색을 '
            '비교하지 마라. 이 판정에서 볼 것은 <b>형태</b>뿐이다.</div>'
        )
        viewer = (
            '<div class="pane"><h2>3D 뷰어 — 돌려서 확인</h2>'
            f'<div class="mvs">{cards}</div>{note_m}</div>'
        )

    # 🔴 철회된 수치는 화면 **맨 위에서** 철회라고 말한다. 표 밑에 각주로 달면
    #    숫자를 먼저 읽고 각주는 안 읽는다 — 그 순서가 이 프로젝트가 물린 자리다.
    void_banner = (
        f'<div class="warn"><b>⚠️ 이 산출물의 수치는 철회됐다.</b> {r.invalidated}.'
        ' 아래 숫자를 현행 수치로 인용하지 마라.</div>'
        if r.invalidated else ""
    )
    have = r.chunk_dir is not None or bool(r.delivered)
    note = "" if have else (
        '<div class="warn"><b>산출물 청크가 없다.</b> 자리표시를 그린다 — '
        "빈 그림을 결과처럼 보이게 두지 않는다.</div>"
    )

    def table(title, d):
        if not d:
            return f"<table><tr><th>{title}</th><td>{MISSING_TXT}</td></tr></table>"
        rows = "".join(
            f'<tr><td>{k}</td><td class="n">{v}</td></tr>'
            for k, v in d.items() if not isinstance(v, (dict, list))
        )
        return f"<table><tr><th>{title}</th><th>값</th></tr>{rows}</table>"

    # ── 성분 (원시 개수 우선 — D37). 런이 여럿이면 **각각** 낸다: 두 단계에서
    #    성분이 어떻게 커지고 벌어졌는지가 이번 인계의 요지다.
    comp_tbl = ""
    for run_name, comps in r.component_sets:
        names = {0: "중앙", 1: "우", 2: "좌"}
        ordered = sorted(comps, key=lambda c: -c.get("size", 0))
        rows_c = "".join(
            f'<tr><td>{names.get(i, str(i))}</td><td class="n">{c.get("size")}</td>'
            f'<td class="n">{c.get("xc")}</td>'
            f'<td class="n">z[{c.get("z", ["", ""])[0]},{c.get("z", ["", ""])[1]}]</td>'
            # 상승 = 절단면 **위로** 뻗은 칸수 (z_hi − z_lo). 칸 개수(+1)가 아니다 —
            # D29-a 가 "머리" 를 절단면 위로 뻗는 성분으로 정의했고, 그 기준이 상승폭이다.
            f'<td class="n">{c.get("z", [0, 0])[1] - c.get("z", [0, 0])[0]}칸</td></tr>'
            for i, c in enumerate(ordered)
        )
        title = f"성분 · {run_name}" if run_name else "성분 (절단면 위로 뻗은 것)"
        comp_tbl += (
            f"<table><tr><th>{title}</th><th>복셀</th><th>x중심</th>"
            f"<th>z 범위</th><th>상승</th></tr>{rows_c}</table>"
        )

    # ── NOTE 의 판정 절을 **그대로**. 요약하면 뜻이 바뀐다
    import html as _html
    import re as _re

    def _md(t: str) -> str:
        """**굵게** · `코드` 만 렌더한다. **문구는 한 글자도 안 바꾼다.**"""
        t = _html.escape(t)
        t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return _re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    caution = "".join(
        f'<div class="warn"><b>{_md(head)} (A5000 원문)</b><ul>'
        + "".join(f"<li>{_md(ln)}</li>" for ln in lines)
        + "</ul></div>"
        for head, lines in r.note_sections
    )
    # 게이트 옆에 A5000 이 적어 둔 단서. 게이트가 "실패" 로 찍히는데 이게 화면에
    # 없으면 화면이 사실의 절반만 말하게 된다. **판정은 안 바꾸고 단서만 같이 낸다.**
    if r.gate_notes:
        caution += (
            '<div class="warn"><b>게이트 단서 (judgment.json · A5000 원문)</b><ul>'
            + "".join(f"<li><b>{_md(k)}</b> — {_md(v)}</li>" for k, v in r.gate_notes.items())
            + "</ul></div>"
        )

    met = r.records.get("metrics") or {}
    man = r.records.get("manifest") or {}
    def _gate_row(key: str, v) -> str:
        st = "통과" if v is True else "실패" if v is False else "미결"
        return (f'<tr><td>{key}</td>'
                f'<td class="{_GATE_CLS.get(st, "ref")}">{st}</td></tr>')

    def _gate_rows(detail: dict) -> str:
        """평평한 블록도, **런별로 중첩된 블록도** 받는다.

        W15 인계본은 `judgment["runs"][런]["gate_g2"]` 로 두 런을 한 파일에 담는다.
        평평한 것만 읽던 판본은 여기서 아무 행도 못 만들고 "판정 기록이 없다" 를
        내는데, 그건 **기록이 있는데 없다고 말하는 것**이다.
        """
        rows = ""
        for k, v in (detail or {}).items():
            if isinstance(v, dict):
                rows += f'<tr><th colspan="2">{k}</th></tr>'
                # 불리언은 판정, 나머지(new/removed/…)는 근거 수치다. 둘 다 낸다 —
                # 원시 개수를 판정보다 앞에 두는 규칙(D37)이 여기서도 같다.
                for kk, vv in v.items():
                    rows += (_gate_row(kk, vv) if isinstance(vv, bool) or vv is None
                             else f'<tr><td>{kk}</td><td class="n">{vv}</td></tr>')
            elif isinstance(v, bool) or v is None:
                rows += _gate_row(k, v)
        return rows

    gate_rows = (_gate_rows(r.gate_detail)
                 or f'<tr><td colspan="2">{MISSING_TXT} — 판정 기록이 없다</td></tr>')

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-ar-v3 — {r.asset_id or r.rel}</title>
<script type="module" src="https://ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js"></script>
<style>{_CSS}
.mono {{ font-variant-numeric:tabular-nums; }}
.mvs {{ display:flex; gap:12px; flex-wrap:wrap; }}
.mv {{ flex:1 1 340px; min-width:0; }}
.mv model-viewer {{ width:100%; height:420px; background:#0b0e14;
                    border:1px solid #232a36; border-radius:6px; --poster-color:transparent; }}
.mv span {{ display:block; color:#8b96a8; font-size:12px; margin-top:6px; }}
.views .view {{ max-width:320px; }}
.hero {{ margin:0 0 14px; }}
.hero img {{ width:100%; height:auto; display:block; border:1px solid #232a36;
             border-radius:6px; image-rendering:auto; background:#fff; }}
.hero span {{ display:block; color:#e5e9f0; font-size:12px; margin-top:6px; }}
.warn ul {{ margin:8px 0 0 18px; }} .warn li {{ margin:4px 0; }}</style></head><body>
<h1>{_KIND_KO.get(r.kind, r.kind)} · {r.asset_id or r.rel}</h1>
<p class="sub">{r.when} · <code>{r.rel}</code> · <a href="/runs">← 목록</a></p>
{void_banner}
{note}
{viewer}
{stage_pane}
<div class="pane"><h2>{"그 밖의 인계 그림" if stage else "정본 그림"}</h2>{tiles[0] if delivered else ""}
<div class="views">{"".join(tiles[1:] if delivered else tiles)}</div>
<div class="legend">{spec["why"]}</div></div>
{caution}
{comp_tbl}
<table><tr><th>게이트 (gate_g2 기록)</th><th>판정</th></tr>{gate_rows}</table>
{table("지표 (원시 개수 우선)", met)}
{table("매니페스트", {k: v for k, v in man.items() if k != "chunks"})}
<p class="foot">
게이트는 <code>gate_g2()</code> 가 기록한 것을 그대로 표시한다 — 이 화면은 판정하지 않는다.<br>
실루엣은 진단용 표면 복셀화로 그린다 (D28: 마스크 좌표의 근거가 아니다).
</p></body></html>"""
