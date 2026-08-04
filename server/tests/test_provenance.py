"""인계본 버전 검사 (`server/provenance.py`) — D27-b 제안.

W11 에서 A5000 이 받은 `slatmask` 인계본에 **D28-a 구조 강제가 없었다.**
`slatmask.py` 는 리포에 존재하지도 않는다 — 인계본이 정본에서 파생됐다는 근거가
처음부터 없었다. sha256 대조(D27②)는 "보낸 것과 받은 것이 같은가" 를 볼 뿐
"보낸 것이 정본인가" 는 보지 않는다. 그 틈이 이번 실패다.
"""

from __future__ import annotations

import types

import pytest

from server import provenance
from server.provenance import (
    HandoffMismatch,
    assert_required_api,
    check_required_api,
    file_digest,
    repo_manifest,
    verify_handoff,
)


# ══════════════════ 1. ★★ 검사 ② — 이번 실패를 정확히 잡는가
def test_missing_d28a_api_is_caught():
    """★★ **W11 재현.** `grid_source` 가 없는 인계본이 걸리는가.

    A5000 이 받은 판본에는 `grid_source=` · `require_slat_grid()` ·
    `is_x_symmetric()` 이 셋 다 없었다. 실효 방어는 런타임 2건뿐이었다.
    """
    old = types.SimpleNamespace(build_slat_mask=lambda *a, **k: None)

    report = check_required_api("slatmask", obj=old)
    assert not report.ok
    assert set(report.missing) == {
        "grid_source", "require_slat_grid", "is_x_symmetric"
    }
    # 왜 필요한지가 결과에 실려 있다 — 다시 조사하지 않아도 되게.
    assert "D28-a" in report.describe()
    assert "D35-a" in report.describe()

    with pytest.raises(HandoffMismatch, match="D28-a"):
        assert_required_api("slatmask", obj=old)


def test_a_compliant_handoff_passes():
    """거부가 과잉이면 정상 인계본까지 막는다."""
    class _Mask:
        grid_source: str = "slat_coords"

        def require_slat_grid(self):
            ...

    good = types.SimpleNamespace(MaskResult=_Mask, is_x_symmetric=lambda c: True)
    assert check_required_api("slatmask", obj=good).ok
    assert_required_api("slatmask", obj=good)


def test_class_fields_count_as_present():
    """`grid_source` 는 모듈 속성이 아니라 **dataclass 필드**다.

    최상위 이름만 보면 정상 모듈을 오탐한다.
    """
    from server.pipeline import mask as mask_mod

    assert "grid_source" in mask_mod.MaskResult.__annotations__
    assert check_required_api("server.pipeline.mask", obj=mask_mod).ok


# ══════════════════ 2. 리포 정본 자신이 약속을 지키는가
@pytest.mark.parametrize(
    "module", ["server.pipeline.frames", "server.pipeline.mask", "server.metrics"]
)
def test_repo_modules_satisfy_their_own_required_api(module):
    """★ 정본이 자기 약속을 어기면 인계본을 탓할 수 없다."""
    assert_required_api(module)


def test_unlisted_module_is_refused():
    """검사 목록 없는 모듈을 통과시키면 이 검사가 아무것도 막지 못한다."""
    with pytest.raises(HandoffMismatch, match="목록"):
        check_required_api("some.unlisted.module", obj=types.SimpleNamespace())


# ══════════════════ 3. 검사 ① — 바이트 동일성
def test_manifest_and_verify_roundtrip(tmp_path):
    import shutil
    from pathlib import Path

    rel = "server/pipeline/frames.py"
    manifest = repo_manifest([rel])
    assert len(manifest[rel]["sha256"]) == 64

    root = Path(__file__).resolve().parents[2]
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True)
    shutil.copy(root / rel, dest)
    assert verify_handoff(manifest, tmp_path) == []

    dest.write_text(dest.read_text() + "\n# drift\n")
    bad = verify_handoff(manifest, tmp_path)
    assert len(bad) == 1 and "sha256" in bad[0]


def test_missing_file_is_reported_not_ignored(tmp_path):
    manifest = repo_manifest(["server/metrics.py"])
    bad = verify_handoff(manifest, tmp_path)
    assert bad and "없다" in bad[0]


def test_manifest_refuses_a_path_absent_from_the_repo():
    """정본에 없는 파일은 매니페스트에 못 넣는다 — 그게 W11 의 상황이었다."""
    with pytest.raises(HandoffMismatch, match="정본에 없는"):
        repo_manifest(["server/does_not_exist.py"])


def test_slatmask_is_now_in_the_repo_and_satisfies_its_api():
    """★★ **W12 에서 상황이 바뀌었다.** `slatmask.py` 가 리포에 편입됐다.

    W11 까지는 리포 밖이라 "어느 버전이 A5000 에 갔는가" 를 확인할 근거가 없었다.
    이제 정본이 있고, 그 정본이 `REQUIRED_API` 를 실제로 만족하는지 여기서 본다 —
    A5000 이 받았던 판본은 `require_slat_grid`·`is_x_symmetric`·`grid_source` 가
    셋 다 없었다.
    """
    from pathlib import Path

    from server import slatmask

    root = Path(__file__).resolve().parents[2]
    assert (root / "server" / "slatmask.py").is_file()
    assert repo_manifest(["server/slatmask.py"])          # 이제 매니페스트에 들어간다

    report = check_required_api("slatmask", obj=slatmask)
    assert report.ok, report.describe()
