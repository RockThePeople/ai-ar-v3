"""최근 산출물 목록 — **스캔해서 모은다. 손으로 등록하지 않는다.**

사용자 요청: DebugView 에서 최근 생성·수정 5건을 목록으로 보고 클릭하면 상세를 본다.

────────────────────────────────────────────────────────────────────────
왜 스캔인가
────────────────────────────────────────────────────────────────────────
산출물이 리포 밖 여러 곳에 흩어져 있다 — `ai-ar-v3-assets/` · `ai-ar-data/assets/` ·
`ai-ar-v3/w{N}/` · 그리고 A5000 쪽(3090 에 없다). 등록을 손으로 하게 만들면
등록을 빼먹은 산출물이 "없는 것" 이 되고, 그게 이 프로젝트가 여섯 번 물린 모양이다.

스캔 대상은 **환경변수 목록**으로 받는다 (`RUNS_SCAN_DIRS`, `:` 구분). import 로
묶지 않는다 — 산출물을 만드는 쪽이 무엇을 노출하든 이 모듈은 안 고친다.
`recolor` 자리·W9 자리와 같은 규칙이다.

────────────────────────────────────────────────────────────────────────
🔴 없는 값은 "미도착" 이다. 추정으로 채우지 않는다 (원칙 7)
────────────────────────────────────────────────────────────────────────
`manifest.json` / `judgment.json` / `metrics.json` 중 **있는 것으로** 항목을
구성한다. 없는 필드는 화면에 "미도착" 으로 나간다. 특히:

  · 게이트 판정은 **여기서 계산하지 않는다.** `gate_g2()` 가 정본이고 이 모듈은
    기록된 결과를 **읽기만** 한다. 판정 파일이 없으면 "미도착" 이다 —
    화면이 문턱을 다시 적는 순간 게이트와 화면이 갈라지고, 갈라진 줄 아무도 모른다.
  · A5000 산출물은 지금 3090 에 없다. 추정하지 않고 **"A5000 미인계"** 로 낸다.

────────────────────────────────────────────────────────────────────────
🔴 원시 개수를 비율보다 앞에 둔다 (D37)
────────────────────────────────────────────────────────────────────────
halo 계열은 표본이 45~48복셀이라 비율에 유효숫자가 없다. `metrics.py` 의
`HaloBandMeasurement.describe()` 가 "원시 개수를 **항상** 앞에 둔다" 로 잠가 뒀고,
이 화면도 같은 순서를 쓴다. 비율은 뒤에 "참고" 로 붙인다.

────────────────────────────────────────────────────────────────────────
보안 (§7)
────────────────────────────────────────────────────────────────────────
공개 URL 로 나가는 화면이다. **절대경로·호스트·키를 렌더하지 않는다.**
자산 id 와 스캔 루트 기준 상대경로까지만 낸다. `run_id` 는 경로의 해시라
경로를 되돌릴 수 없고, 라우트가 임의 경로를 열지 못한다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "MISSING",
    "PENDING_A5000",
    "Run",
    "detail_kind",
    "scan_dirs",
    "recent_runs",
]

#: 값이 없을 때 화면에 내는 것. 빈 문자열이나 0 으로 채우지 않는다.
MISSING = "미도착"
PENDING_A5000 = "A5000 미인계"

_RECORD_FILES = ("judgment.json", "metrics.json", "manifest.json")
_MAX_DEPTH = 3


def scan_dirs() -> List[Path]:
    """`RUNS_SCAN_DIRS`(`:` 구분) → 존재하는 디렉터리 목록."""
    raw = os.environ.get(
        "RUNS_SCAN_DIRS",
        ":".join(
            [
                str(Path.home() / "ai-ar-v3-assets"),
                str(Path.home() / "ai-ar-data" / "assets"),
                str(Path.home() / "ai-ar-v3-runs"),
            ]
        ),
    )
    dirs = [p for p in (Path(x).expanduser() for x in raw.split(":") if x.strip()) if p.is_dir()]
    a = a5000_dir()
    if a is not None and a not in dirs:
        dirs.append(a)      # A5000 인계분도 같은 목록에 든다
    return dirs


#: A5000 이 육안 인계로 밀어 준 산출물의 **파일명 → 라벨**. 화이트리스트다 —
#: 임의 파일명을 경로로 받지 않는다 (§7, 공개 URL).
DELIVERED = {
    "DELIVER_depth_w12_wide.png": ("편집 전 / 편집 후 (넓게)", True),
    "DELIVER_depth_w12.png": ("갈라짐 부위 확대", False),
    "depth_runC.png": ("편집 후 4방향", False),
    "depth_before.png": ("편집 전 4방향", False),
    "DELIVER_depth_w11_runA_FAILED.png": ("★ 대조 — W11 실패본 (목이 하나로 굵어지기만)", False),
}


def a5000_dir() -> Optional[Path]:
    """A5000 산출물 자리. 다음 웨이브에 `handoff/pack.py` 가 밀어주면 여기 붙는다.

    지금은 비어 있는 것이 정상이고, 그 사실을 **화면에 적는다**.
    """
    raw = os.environ.get("A5000_RUNS_DIR", str(Path.home() / "ai-ar-v3" / "w12-out")).strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Run:
    """산출물 한 건. **없는 값은 None 이고 화면이 '미도착' 으로 렌더한다.**"""

    run_id: str
    path: Path
    rel: str                      # 스캔 루트 기준 상대경로 (절대경로는 안 낸다)
    mtime: float
    kind: str                     # generate | edit | recolor | 미상
    asset_id: Optional[str]
    headline: str                 # 종류별 한눈 결과 (원시 개수 우선)
    gate: str                     # 통과 / 미결 / 실패 / 미도착 — **읽기만 한다**
    gate_detail: Dict[str, Any] = field(default_factory=dict)
    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pending_reason: Optional[str] = None

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.mtime, timezone.utc).astimezone().strftime(
            "%m-%d %H:%M"
        )

    @property
    def delivered(self) -> Dict[str, str]:
        """A5000 이 렌더해 보낸 그림. **다시 렌더하지 않는다** — 깊이 카메라가 그쪽 것이다."""
        return {n: lbl for n, (lbl, _) in DELIVERED.items() if (self.path / n).is_file()}

    @property
    def delivered_canonical(self) -> Optional[str]:
        for n, (_, canon) in DELIVERED.items():
            if canon and (self.path / n).is_file():
                return n
        return None

    @property
    def note_caution(self) -> Optional[str]:
        """NOTE 의 "주의" 절을 **그대로** 낸다. 요약하면 뜻이 바뀐다."""
        for f in sorted(self.path.glob("NOTE-*.md")):
            txt = f.read_text(encoding="utf-8", errors="replace")
            i = txt.find("## 주의")
            if i >= 0:
                return txt[i:].split("\n## ", 1)[0]
        return None

    @property
    def after_chunk_dir(self) -> Optional[Path]:
        """recolor 의 **편집 후** 청크. before 만 보여주면 색 변경이 안 보인다."""
        for name in ("chunks_level1", "chunks_after", "recolor"):
            d = self.path / name
            if d.is_dir() and any(d.glob("*.cbin")):
                return d
        return None

    @property
    def chunk_dir(self) -> Optional[Path]:
        d = self.path / "chunks"
        return d if d.is_dir() and any(d.glob("*.cbin")) else None


def _kind_of(rec: Dict[str, Dict[str, Any]], path: Path) -> str:
    """종류 추론. **모르면 '미상' 이다** — 그럴듯한 기본값을 고르지 않는다."""
    if any((path / n).is_file() for n in DELIVERED):
        return "edit"
    j = rec.get("judgment") or {}
    for key in ("kind", "op", "edit_kind"):
        v = j.get(key)
        if isinstance(v, str):
            if v in ("recolor",):
                return "recolor"
            if v in ("add", "remove", "replace_region", "edit"):
                return "edit"
            if v in ("generate", "create"):
                return "generate"
    if (path / "chunks_level1").is_dir() or (path / "recolor").is_dir():
        return "recolor"
    m = rec.get("manifest") or {}
    if m.get("a5000_job_id") and m.get("is_initial") is not False:
        return "generate"
    if m:
        return "generate"
    return "미상"


def _fmt_headline(kind: str, rec: Dict[str, Dict[str, Any]]) -> str:
    """한눈 결과. 🔴 **원시 개수가 비율보다 앞이다** (D37).

    비율만 크게 띄우면 halo 계열(표본 45~48복셀)에서 유효숫자 없는 숫자가
    권위를 갖는다. 개수를 먼저 쓰고 비율은 "참고" 로 붙인다.
    """
    met = rec.get("metrics") or {}
    man = rec.get("manifest") or {}
    j = rec.get("judgment") or {}
    src = {**met, **j}

    def g(*names):
        for n in names:
            if n in src and src[n] is not None:
                return src[n]
        return None

    parts: List[str] = []
    if kind == "edit":
        # A5000 judgment 형식: {"efficacy": {new, removed, ...}, "after_components": [...]}
        eff = (rec.get("judgment") or {}).get("efficacy") or {}
        comps = (rec.get("judgment") or {}).get("after_components") or []
        if eff or comps:
            if comps:
                parts.append(f"머리 {len(comps)}개")
            if eff:
                parts.append(f"신규 {eff.get('new', MISSING)} / 제거 {eff.get('removed', MISSING)}")
                if eff.get("largest_cc_frac") is not None:
                    parts.append(f"참고 최대성분 {eff['largest_cc_frac']:.3f}")
            return " · ".join(parts)
        new, rm = g("efficacy_new_voxels", "n_new"), g("efficacy_removed_voxels", "n_removed")
        heads = g("head_count", "n_heads")
        if heads is not None:
            parts.append(f"머리 {heads}개")
        if new is not None or rm is not None:
            parts.append(f"신규 {new if new is not None else MISSING}"
                         f" / 제거 {rm if rm is not None else MISSING}")
        save = g("transfer_saving")
        if save is not None:
            parts.append(f"참고 절감 {float(save) * 100:.1f}%")
    elif kind == "recolor":
        hue = g("hue_shift_deg", "hue_shift", "mean_hue_shift")
        chg = g("changed_chunks", "n_changed")
        if chg is not None:
            parts.append(f"변경 청크 {chg}")
        parts.append(f"hue 이동 {float(hue):.1f}°" if hue is not None else f"hue 이동 {MISSING}")
        save = g("transfer_saving")
        if save is not None:
            parts.append(f"참고 절감 {float(save) * 100:.1f}%")
    elif kind == "generate":
        cc = man.get("chunk_count")
        vc = man.get("voxel_count_total")
        parts.append(f"청크 {cc}" if cc is not None else f"청크 {MISSING}")
        parts.append(f"복셀 {vc:,}" if isinstance(vc, int) else f"복셀 {MISSING}")
    return " · ".join(parts) if parts else MISSING


def _gate_of(rec: Dict[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """게이트 요약. **여기서 판정하지 않는다** — 기록된 것을 읽기만 한다.

    `gate_g2()` 가 정본이다. 판정 기록이 없으면 `미도착` 이고, 화면이 문턱을
    다시 적어서 만들어 내지 않는다 (W5 에 정한 규약).
    """
    j = rec.get("judgment") or {}
    gate = j.get("gate_g2") or j.get("gate") or {}
    if not isinstance(gate, dict) or not gate:
        return MISSING, {}
    vals = [v for k, v in gate.items() if isinstance(v, bool) or v is None]
    if any(v is False for v in vals):
        return "실패", gate
    if any(v is None for v in vals):
        return "미결", gate
    if vals and all(v is True for v in vals):
        return "통과", gate
    return MISSING, gate


def _iter_candidates(root: Path):
    """레코드 파일을 가진 디렉터리를 얕게 훑는다."""
    if any((root / f).is_file() for f in _RECORD_FILES):
        yield root
    for depth in range(1, _MAX_DEPTH + 1):
        for p in root.glob("/".join(["*"] * depth)):
            if p.is_dir() and any((p / f).is_file() for f in _RECORD_FILES):
                yield p


def recent_runs(limit: int = 5) -> List[Run]:
    """최근 `limit` 건. 없으면 빈 목록이다 — 만들어 내지 않는다."""
    seen: Dict[Path, Run] = {}
    for root in scan_dirs():
        for d in _iter_candidates(root):
            if d in seen:
                continue
            rec: Dict[str, Dict[str, Any]] = {}
            newest = 0.0
            for fname in _RECORD_FILES:
                f = d / fname
                if f.is_file():
                    data = _load(f)
                    if data is not None:
                        rec[fname.removesuffix(".json")] = data
                        newest = max(newest, f.stat().st_mtime)
            if not rec:
                continue
            kind = _kind_of(rec, d)
            gate, gate_detail = _gate_of(rec)
            man = rec.get("manifest") or {}
            j = rec.get("judgment") or {}
            try:
                rel = str(d.relative_to(root))
            except ValueError:
                rel = d.name
            seen[d] = Run(
                run_id=hashlib.sha1(str(d).encode()).hexdigest()[:12],
                path=d,
                rel=f"{root.name}/{rel}" if rel != "." else root.name,
                mtime=newest or d.stat().st_mtime,
                kind=kind,
                asset_id=man.get("asset_id") or j.get("asset_id"),
                headline=_fmt_headline(kind, rec),
                gate=gate,
                gate_detail=gate_detail,
                records=rec,
            )
    runs = sorted(seen.values(), key=lambda r: r.mtime, reverse=True)[:limit]

    if a5000_dir() is None and not any(r.delivered for r in runs):
        runs.append(
            Run(
                run_id="a5000-pending",
                path=Path("."),
                rel=PENDING_A5000,
                mtime=0.0,
                kind="edit",
                asset_id=None,
                headline=MISSING,
                gate=MISSING,
                pending_reason=(
                    "A5000 편집 산출물은 아직 3090 에 없다. 다음 웨이브에 "
                    "handoff/pack.py 가 밀어주면 A5000_RUNS_DIR 만 그쪽으로 열면 된다"
                ),
            )
        )
    return runs


def detail_kind(kind: str) -> Dict[str, Any]:
    """🔴 **종류별 정본 그림이 다르다.** 틀리면 판정이 무의미해진다.

        형태 변경  실루엣 4분할 + **앞면 깊이맵**이 정본 (D19)
                   실루엣만으로는 파낸 구멍·오목 디테일이 원리적으로 안 보인다
        색 변경    **색 렌더가 정본** (D19-a)
                   레벨1 은 기하 불변이라 실루엣도 깊이맵도 before/after 가 똑같다
        생성       실루엣 3뷰 + 깊이맵
    """
    if kind == "recolor":
        return {
            "canonical": "color_after",
            "images": ["color", "color_after"],
            "why": (
                "레벨1 은 <b>기하가 불변</b>이라 실루엣도 깊이맵도 before/after 가 "
                "똑같이 나온다 — 둘 다 색 채널을 안 본다. <b>색 렌더가 정본</b>이다 (D19-a)."
            ),
        }
    if kind == "edit":
        return {
            "canonical": "depth",
            "images": ["front", "side", "top", "depth"],
            "why": (
                "실루엣은 <b>투영</b>이라 깊이를 뭉갠다 — 파낸 구멍·오목 디테일이 "
                "뒷면으로 메워져 <b>원리적으로 안 보인다</b>. <b>앞면 깊이맵이 정본</b>이다 (D19)."
            ),
        }
    return {
        "canonical": "depth",
        "images": ["front", "side", "top", "depth"],
        "why": "생성물은 실루엣 3뷰로 형태를, 깊이맵으로 오목 디테일을 본다 (D19).",
    }
