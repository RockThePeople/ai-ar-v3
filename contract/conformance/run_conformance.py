"""
pytest 없이도 적합성 테스트를 돌리기 위한 폴백 러너.

3090 에는 pytest 가 있지만 A5000 의 `trellis2` env 에는 있는지 확인되지 않았고,
계약 검증은 **환경 설치를 기다리지 않고 즉시** 돌 수 있어야 한다.

    python conformance/run_conformance.py

pytest 가 있으면 그냥 `python -m pytest conformance/` 를 쓰면 된다 (동일한 테스트).
"""

from __future__ import annotations

import os
import inspect
import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
sys.path.insert(0, str(HERE))


def _install_pytest_stub() -> None:
    """pytest 가 없을 때 테스트 파일이 쓰는 최소 API 만 흉내낸다."""
    import types

    stub = types.ModuleType("pytest")

    class _Raises:
        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"{self.exc.__name__} 이 발생하지 않았다")
            return issubclass(et, self.exc)

    class _Skip(Exception):
        def __init__(self, reason=""):
            self.reason = reason

    def fixture(*a, **kw):
        def deco(fn):
            fn.__is_fixture__ = True
            return fn

        if a and callable(a[0]):
            return deco(a[0])
        return deco

    class _Mark:
        @staticmethod
        def skipif(condition, reason=""):
            def deco(fn):
                fn.__skipif__ = (bool(condition), reason)
                return fn

            return deco

    stub.raises = _Raises
    stub.fixture = fixture
    stub.mark = _Mark
    stub.skip = lambda reason="": (_ for _ in ()).throw(_Skip(reason))
    stub.Skipped = _Skip
    sys.modules["pytest"] = stub


# 🔴 **stub 은 pytest 유무와 무관하게 항상 설치한다** (3.14.0 수정)
#
# 이전 판은 pytest 가 있으면 진짜 pytest 를 썼다. 그러면 `@pytest.fixture` 가
# 진짜 픽스처 객체를 만드는데, 이 러너는 픽스처를 **평범한 함수로 직접 호출**한다.
# pytest 8+ 가 그걸 금지해서 러너가 27/48 에서 크래시했다.
#
#   → 3090·A5000 **양쪽**이 같은 증상을 보고했다 (2026-07-31).
#
# 증상의 방향이 고약하다: pytest 가 **있는** 환경에서만 폴백 러너가 깨진다.
# 계약 작성자 환경(pytest 없음)에서는 멀쩡히 돌아 45 passed 가 나왔다.
# 3.11.2 가 "검사가 필요한 환경에서 안 도는 것은 검사가 없는 것과 같다"로 고친
# 문제가, 이번엔 **그 고침을 담고 있는 러너 자신**에게 일어났다.
#
# 이 러너의 존재 이유는 "어떤 환경에서든 똑같이 돈다"이다. 환경에 따라 다른
# 경로를 타면 그 이유가 사라진다. pytest 를 쓰고 싶으면 pytest 를 직접 돌려라.
try:
    import pytest as _pt  # noqa: F401

    HAVE_PYTEST = True
except ImportError:
    HAVE_PYTEST = False

_install_pytest_stub()  # ← 조건 없이. sys.modules 를 덮어 테스트 모듈이 stub 을 본다

import pytest  # noqa: E402  (위에서 심은 stub 이다)
import test_contract as mod  # noqa: E402

assert getattr(pytest, "__name__", "") == "pytest" and not hasattr(pytest, "main"), (
    "진짜 pytest 가 잡혔다 — stub 설치가 test_contract 임포트보다 늦었다"
)


def main() -> int:
    if HAVE_PYTEST:
        print("[i] pytest 가 설치돼 있지만 폴백 러너는 항상 stub 으로 돈다.")
        print("[i] 전체 검증(스키마 테스트 포함)은 `python -m pytest conformance/` 로.\n")
    else:
        print("[i] pytest 없음 — 내장 폴백 러너로 실행한다.\n")

    # module-scope fixture 를 한 번만 만들어 재사용
    cache: dict[str, object] = {}

    def resolve(name: str):
        if name not in cache:
            fn = getattr(mod, name)
            cache[name] = fn()
        return cache[name]

    names = [n for n in dir(mod) if n.startswith("test_")]
    names.sort(key=lambda n: inspect.getsourcelines(getattr(mod, n))[1])

    passed = failed = skipped = 0
    failures = []
    for name in names:
        fn = getattr(mod, name)
        cond, reason = getattr(fn, "__skipif__", (False, ""))
        if cond:
            print(f"  SKIP  {name}  ({reason})")
            skipped += 1
            continue
        kwargs = {p: resolve(p) for p in inspect.signature(fn).parameters}
        try:
            fn(**kwargs)
        except getattr(pytest, "Skipped", ()) as e:  # type: ignore[misc]
            print(f"  SKIP  {name}  ({e})")
            skipped += 1
        except Exception:
            print(f"  FAIL  {name}")
            failures.append((name, traceback.format_exc()))
            failed += 1
        else:
            print(f"  ok    {name}")
            passed += 1

    # ── pydantic 없이도 반드시 도는 검사 ───────────────────────────────
    #
    # 3.11.1 에서 ChunkHashMismatch 를 errors.py 에만 넣고 ErrorBody 의 Literal 을
    # 안 고쳤다. 그걸 잡으라고 만든 test_error_classes_map_1to1_to_wire_codes 가
    # pydantic 의존이라 계약 작성자 환경에서 **항상 skip** 됐고, 두 서버 세션이
    # 각자 발견해서 보고했다.
    #
    # 검사가 필요한 환경에서 안 도는 것은 검사가 없는 것과 같다.
    # 그래서 소스를 텍스트로 읽어 같은 것을 확인한다 — 의존성이 없다.
    import ast as _ast

    _here = os.path.dirname(os.path.abspath(__file__))
    _pkg = os.path.join(_here, "..", "python", "deltacontract")

    # ── C# 미러 대조 (3.21.2) — pydantic 없이 돈다. 왜인지는 mirror_check.py 서두 ──
    import mirror_check as _mc
    _mm = _mc.check()
    if _mm:
        for _m in _mm:
            print("FAIL C# 미러 누락:", _m)
            failed += 1
    else:
        print(f"ok C# 미러 {len(_mc.MIRRORED)}개 모델 필드 일치 (no pydantic)")

    _codes_err = set(re.findall(r'^\s*error_code = "([A-Z_]+)"',
                                open(os.path.join(_pkg, "errors.py")).read(), re.M))
    _sch = open(os.path.join(_pkg, "schemas.py")).read()
    _lit = re.search(r"error_code: Literal\[(.*?)\]", _sch, re.S).group(1)
    _codes_sch = set(re.findall(r'"([A-Z_]+)"', _lit))

    if _codes_err != _codes_sch:
        only_e = sorted(_codes_err - _codes_sch)
        only_s = sorted(_codes_sch - _codes_err)
        print(f"  FAIL  errors.py <-> ErrorBody Literal 1:1")
        if only_e:
            print(f"          errors.py 에만: {only_e}")
        if only_s:
            print(f"          Literal 에만:   {only_s}")
        failed += 1
    else:
        print(f"  ok    errors.py <-> ErrorBody Literal 1:1  ({len(_codes_err)} codes, no pydantic)")
        passed += 1

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    for name, tb in failures:
        print(f"\n{'─' * 70}\n{name}\n{'─' * 70}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
