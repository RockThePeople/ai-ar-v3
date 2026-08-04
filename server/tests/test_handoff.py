"""인계 꾸러미 (`server/handoff/`) — A5000 이 리포 없이 돌리는 정본.

W11 에서 `gate_g2` 와 `provenance.py` 가 A5000 에 없어 **동등 구현을 직접 만들었다.**
정본이 없으면 각자 만들고, 각자 만들면 갈라진다. 이 파일이 그 갈라짐을 막는다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from server import metrics
from server.handoff import gatecheck
from server.handoff.pack import PAYLOAD, REQUIRED_API, pack

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contract" / "python"


# ══════════════════ 1. ★ 단독 실행 — 리포를 import 하지 않는가
@pytest.mark.parametrize("rel", PAYLOAD)
def test_payload_does_not_import_the_repo(rel):
    """★★ A5000 은 리포 클론이 아니라 scp 로 받는다. `server.*` 를 import 하면 죽는다."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "server." not in stripped and not stripped.startswith("from ."), (
                f"{rel}: 리포 모듈을 import 한다 — 단독 실행이 깨진다\n  {stripped}"
            )


def test_slatmask_runs_standalone(tmp_path):
    """★ 실제로 리포 밖에서 돌려 본다 (deltacontract + numpy 만)."""
    import shutil

    shutil.copy(ROOT / "server" / "slatmask.py", tmp_path / "slatmask.py")
    script = tmp_path / "run.py"
    script.write_text(
        "import numpy as np, slatmask\n"
        "cells=[(x,30,z) for z in range(10,60) "
        "for x in range(28,36) if z<40 or abs(x-31)<2]\n"
        "a=np.array(sorted(set(cells)),dtype=np.int64)\n"
        "s=slatmask.build_head3_mask(a, source=slatmask.SLAT, symmetrize=True)\n"
        "s.require_slat_grid(); s.require_x_symmetric()\n"
        "print('OK', s.n_cells, s.grid_source, s.x_symmetric)\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True,
        env={"PYTHONPATH": str(CONTRACT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.startswith("OK ")
    assert "slat_coords True" in r.stdout


# ══════════════════ 2. ★★ 꾸러미 + 검사 (D27-b)
def test_pack_then_verify_passes(tmp_path):
    out = pack(tmp_path / "h")
    assert (out / "MANIFEST.json").is_file()
    r = subprocess.run(
        [sys.executable, "verify.py"], cwd=out, capture_output=True, text=True,
        env={"PYTHONPATH": str(CONTRACT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stdout + r.stderr[-800:]
    assert "sha256 일치" in r.stdout and "필수 API 전부 있음" in r.stdout


def test_verify_catches_byte_drift(tmp_path):
    """검사 ① — 한 글자만 달라도 잡는다."""
    out = pack(tmp_path / "h")
    (out / "slatmask.py").write_text(
        (out / "slatmask.py").read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8"
    )
    r = subprocess.run(
        [sys.executable, "verify.py"], cwd=out, capture_output=True, text=True,
        env={"PYTHONPATH": str(CONTRACT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 1
    assert "sha256" in r.stdout


def test_verify_catches_missing_api_even_when_sha_matches(tmp_path):
    """★★ **W11 재현.** sha256 은 맞는데 API 가 없는 경우를 잡는가.

    그때 실제로 sha256 은 일치했고 `require_slat_grid`·`is_x_symmetric`·
    `grid_source` 가 셋 다 없었다. 검사 ①만 돌렸으면 통과했을 것이다.
    """
    out = pack(tmp_path / "h")
    old = "def build_head3_mask(*a, **k):\n    return None\n"
    (out / "slatmask.py").write_text(old, encoding="utf-8")

    manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    import hashlib
    manifest["files"]["slatmask.py"]["sha256"] = hashlib.sha256(
        old.encode()).hexdigest()          # ① 는 통과하도록 맞춰 준다
    (out / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "verify.py"], cwd=out, capture_output=True, text=True,
        env={"PYTHONPATH": str(CONTRACT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 1
    assert "필수 API 부재" in r.stdout
    for sym in ("require_slat_grid", "is_x_symmetric", "grid_source"):
        assert sym in r.stdout, sym
    assert "sha256" not in r.stdout        # ①은 통과했다 — ②만 잡았다


def test_verify_reports_import_failure_as_failure_not_missing_api(tmp_path):
    """import 가 죽으면 "API 없음" 이 아니라 **import 실패**로 보고해야 한다.

    ⚠️ 실제로 오판했다: `@dataclass` 는 `exec_module` 전에 모듈이 `sys.modules` 에
       있어야 하는데 등록을 안 해서 전부 "import 실패" 로 죽었고, 그대로 두면
       "인계본에 API 가 없다" 로 잘못 읽힌다.
    """
    out = pack(tmp_path / "h")
    (out / "gatecheck.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    import hashlib
    m = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    m["files"]["gatecheck.py"]["sha256"] = hashlib.sha256(
        (out / "gatecheck.py").read_bytes()).hexdigest()
    (out / "MANIFEST.json").write_text(json.dumps(m), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "verify.py"], cwd=out, capture_output=True, text=True,
        env={"PYTHONPATH": str(CONTRACT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 1
    assert "import 실패" in r.stdout


# ══════════════════ 3. 드리프트 — 정본과 인계판이 갈라지지 않는가
def test_direction_rules_match_the_canonical_ones():
    """★ `gatecheck` 는 `metrics` 의 부분집합이다. 규칙이 갈라지면 여기서 걸린다."""
    assert set(gatecheck.DIRECTION_RULES) == set(metrics.DIRECTION_RULES)

    from server.metrics import VoxelDelta as CanonDelta

    cases = [(500, 100), (100, 500), (300, 300), (0, 0), (0, 500), (500, 0)]
    for op in gatecheck.DIRECTION_RULES:
        for new, removed in cases:
            canon = metrics.direction_holds(op, CanonDelta(new, removed))
            try:
                gatecheck.check_direction(op, gatecheck.VoxelDelta(new, removed))
                got = True
            except gatecheck.DirectionMismatch:
                got = False
            assert got == canon, f"{op} ({new},{removed}): 인계판 {got} ≠ 정본 {canon}"


def test_w10_fails_add_in_the_handoff_copy_too():
    """★ 인계판도 W10 을 잡는가 — 정본만 잡으면 A5000 에서는 통과한다."""
    with pytest.raises(gatecheck.DirectionMismatch):
        gatecheck.check_direction("add", gatecheck.VoxelDelta(new=304, removed=730))


def test_handoff_baseline_refuses_bare_asset_and_cross_region():
    """D33/D36 이 인계판에도 있는가."""
    with pytest.raises(gatecheck.BaselineMisapplied):
        gatecheck.NoiseFloor(0.1, "")
    nf = gatecheck.NoiseFloor(0.2222, "dragon-c", "halo_band_1", 226)
    with pytest.raises(gatecheck.BaselineMisapplied):
        nf.require("dragon-c", "neck")
    nf.require("dragon-c", "halo_band_1")


def test_handoff_halo_refuses_ratio_only():
    """D37 이 인계판에도 있는가 — 45복셀에서 비율 단독 판정은 거부된다."""
    nf = gatecheck.NoiseFloor(0.2222, "dragon-c", "halo_band_1", 226)
    with pytest.raises(gatecheck.RatioWithoutResolution):
        gatecheck.halo_verdict(n_new=1, n_removed=0, n_union=45, baseline=nf)
    assert gatecheck.halo_verdict(
        n_new=1, n_removed=0, n_union=45, baseline=nf, visual_confirmed=True
    ) is True


def test_slatmask_slat_constant_matches_frames():
    """`slatmask.SLAT` 는 `frames.VOXEL_GRID_SOURCE` 의 **복사본**이다.

    단독 실행 때문에 import 할 수 없어 값을 복사했다 — 갈라짐은 여기서 막는다.
    """
    from server import slatmask
    from server.pipeline import frames

    assert slatmask.SLAT == frames.VOXEL_GRID_SOURCE


def test_required_api_lists_agree():
    """`pack.REQUIRED_API` 와 `provenance.REQUIRED_API` 가 어긋나면 검사가 반쪽이 된다."""
    from server.provenance import REQUIRED_API as CANON

    assert set(REQUIRED_API["slatmask"]) == set(CANON["slatmask"])
