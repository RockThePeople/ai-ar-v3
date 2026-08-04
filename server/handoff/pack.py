"""인계 꾸러미를 만든다 (보내는 쪽 = 3090). `python -m server.handoff.pack --out DIR`."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

#: 인계에 들어가는 것. **리포 밖에서 단독으로 돌아야 하는 것만** 넣는다.
PAYLOAD = ["server/slatmask.py", "server/handoff/gatecheck.py", "server/provenance.py"]

#: 받는 쪽이 검사할 필수 API. `server/provenance.py` 의 REQUIRED_API 와 짝이다.
REQUIRED_API = {
    "slatmask": ["grid_source", "require_slat_grid", "is_x_symmetric"],
    "gatecheck": ["check_direction", "NoiseFloor", "halo_verdict"],
    "provenance": ["check_required_api", "verify_handoff"],
}


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:                       # pragma: no cover - git 없는 환경
        return "unknown"


def pack(out: Path, root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parents[2]
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for rel in PAYLOAD:
        src = base / rel
        dst = out / Path(rel).name
        shutil.copy(src, dst)
        files[Path(rel).name] = {"sha256": _sha256(src), "repo_path": rel}
    shutil.copy(Path(__file__).with_name("verify.py"), out / "verify.py")
    (out / "MANIFEST.json").write_text(
        json.dumps(
            {"git_commit": _git_commit(base), "files": files,
             "required_api": REQUIRED_API},
            indent=2, ensure_ascii=False,
        ) + "\n", encoding="utf-8",
    )
    return out


if __name__ == "__main__":                  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    print(f"인계 꾸러미: {pack(a.out)}")
