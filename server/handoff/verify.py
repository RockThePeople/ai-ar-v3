"""받는 쪽(A5000)이 돌리는 검사. **이걸 통과해야 인계 완료다** (D27④).

    python verify.py

두 겹으로 본다:
  ① sha256      — 보낸 것과 받은 것이 같은가
  ② 필수 API    — 약속한 심볼이 실제로 있는가

②가 W11 을 잡는 검사다. 그때 **sha256 은 일치했는데 API 는 없었다** —
①만 돌렸으면 통과했을 것이다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def _load(path: Path):
    """파일 경로로 모듈을 연다.

    ⚠️ `exec_module` **전에** `sys.modules` 에 등록해야 한다. `@dataclass` 가
       `sys.modules[cls.__module__]` 를 들여다보기 때문이다 — 등록하지 않으면
       `AttributeError: 'NoneType' object has no attribute '__dict__'` 로 죽고,
       그러면 "인계본에 API 가 없다" 로 **오판**한다. 실제로 그렇게 오판했다.
    """
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} 를 모듈로 열 수 없다")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def main(root: Path) -> int:
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = []

    for name, meta in manifest["files"].items():
        p = root / name
        if not p.is_file():
            bad.append(f"① {name}: 없다")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != meta["sha256"]:
            bad.append(f"① {name}: sha256 {got[:12]}… ≠ {meta['sha256'][:12]}…")

    for name, required in manifest["required_api"].items():
        p = root / f"{name}.py"
        if not p.is_file():
            bad.append(f"② {name}.py: 없다")
            continue
        try:
            mod = _load(p)
        except Exception as e:                                  # pragma: no cover
            bad.append(f"② {name}: import 실패 — {e}")
            continue
        names = set(dir(mod))
        for attr in list(names):
            member = getattr(mod, attr, None)
            if isinstance(member, type):
                names.update(dir(member))
                names.update(getattr(member, "__annotations__", {}).keys())
        missing = [s for s in required if s not in names]
        if missing:
            bad.append(f"② {name}: 필수 API 부재 {missing}")

    print(f"인계 검사 · git {manifest.get('git_commit', '?')[:12]}")
    if bad:
        print("🔴 실패:")
        for b in bad:
            print(f"   {b}")
        print("⇒ 정본을 다시 보내라. 받는 쪽이 이 검사를 통과했다고 보고해야 인계 완료다 (D27④).")
        return 1
    print("✅ ① sha256 일치 · ② 필수 API 전부 있음")
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main(Path(__file__).resolve().parent))
