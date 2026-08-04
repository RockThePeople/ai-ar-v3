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
            "base·donor 가 <b>각자 독립적으로</b> NORMALIZED 격자를 꽉 채우게 정규화된다. "
            "그래서 기증자는 크롭 없이는 머리 마스크에 절대 안 들어간다 — "
            "스케일은 계약이 금지한다(6-이웃 유지율 s=2.0 → 0%). "
            "지금 화면의 기증자는 호박의 <b>위쪽 30%</b> 다.</div>"
        )
    return (
        '<div class="warn"><b>⚠️ 지금 보이는 것은 합성 픽스처다. 실자산이 아니다.</b><br>'
        f"{source_label}<br>"
        "화면의 base 는 <b>구 2개</b>(눈사람이 아니다), donor 는 <b>육면체</b>"
        "(호박이 아니다)다. 실자산은 <a href=\"/real\">/real</a> 에 있다.</div>"
    )


def render_html(run: dict, *, source_label: str, gate_rows, kind: str = "synthetic") -> str:
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
<tr><td>마스크 셀 / halo 팽창 후</td><td class="n">{m.n_cells} / {m.n_dilated}</td></tr>
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
