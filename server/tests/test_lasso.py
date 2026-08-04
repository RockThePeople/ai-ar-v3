"""라쏘 → SLat 복셀 마스크 (W17 ① · D57 · D58).

🔴 **이 파일이 답하는 질문은 하나다: 라쏘가 slat 격자에서 도는가.**

작업 2 의 관문("바퀴 제외")이 정확히 마스킹 방법의 문제이고, z 밴드로는 **원리적으로**
안 갈린다. 그 "원리적으로" 를 말로 두지 않고 `test_no_z_band_can_separate_the_wheels`
가 **전수 탐색으로 증명**한다 — 규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다.

검증 구조가 두 겹인 이유:

    ① 항상 도는 검사 — 커밋된 골든 산출물의 성질 (dotnet 불필요)
    ② dotnet 이 있을 때 — `contract/unity/LassoVolume.cs` 를 **실제로 컴파일해 재실행**
       하고 골든과 대조 (C# 드리프트를 잡는다)

①만 있으면 C# 이 바뀌어도 모른다. ②만 있으면 dotnet 없는 곳에서 아무것도 안 지킨다.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import numpy as np
import pytest

from deltacontract import mask_fingerprint

from server.pipeline.mask import build_mask
from server.pipeline.frames import VOXEL_GRID_SOURCE

from . import lasso_fixtures as F

CASES = pathlib.Path(__file__).parent / "lasso_cases"
REPO = pathlib.Path(__file__).resolve().parents[2]


def golden(name: str) -> dict:
    return json.loads((CASES / f"{name}.golden.json").read_text())


@pytest.fixture(scope="module")
def car():
    c = F.build_car()
    return {
        "body": set(map(tuple, c["body"].tolist())),
        "wheels": set(map(tuple, c["wheels"].tolist())),
        "all": set(map(tuple, c["slat_coords"].tolist())),
    }


# ══════════════ ① 골든 산출물의 성질 — dotnet 없이도 돈다
def test_fingerprint_matches_the_contract_function():
    """★ C# 이 낸 지문이 `deltacontract.mask_fingerprint` 와 **같은 값**이어야 한다.

    양쪽이 각자 직렬화하면 어긋나도 예외가 안 나고 "지문 불일치" 로만 보인다 —
    그러면 마스크가 달랐던 건지 인코딩이 달랐던 건지 구분이 안 되고, 그 구분
    불가가 이 필드를 넣은 목적 자체를 없앤다 (계약 3.14.0).
    """
    for name in ("body", "wide"):
        g = golden(name)
        cells = np.array(g["cells"], dtype=np.int64)
        assert mask_fingerprint(cells) == g["mask_fingerprint"], name
        assert len(cells) == g["n_cells"]


def test_grid_source_is_slat_coords(car):
    """D28-a — 산출물에 격자 출처가 박혀 있어야 한다. 아니면 판정이 거부한다."""
    for name in ("body", "wide"):
        assert golden(name)["grid_source"] == VOXEL_GRID_SOURCE == "slat_coords"

    g = golden("body")
    m = build_mask(np.array(g["cells"], dtype=np.int64), halo=1,
                   grid_source=g["grid_source"])
    m.require_slat_grid()            # 서버 판정 경로가 이걸 통과해야 한다
    assert m.is_canonical_grid


def test_wheels_are_excluded_when_only_the_body_is_circled(car):
    """★★ **분리 테스트.** 몸체만 둘러 그렸을 때 바퀴 대역이 안 잡히는가."""
    g = golden("body")
    picked = set(map(tuple, g["cells"]))

    assert picked & car["wheels"] == set(), (
        f"바퀴가 {len(picked & car['wheels'])}셀 딸려 들어왔다"
    )
    covered = len(picked & car["body"]) / len(car["body"])
    assert covered > 0.99, f"몸체를 {covered:.1%} 밖에 못 잡았다"
    assert picked <= car["all"], "점유하지 않은 셀이 마스크에 들어갔다 (교집합 누락)"


def test_wide_lasso_catches_everything(car):
    """★★ **양성 대조.** 이걸 안 두면 '아무것도 안 잡는 구현' 이 위 검사를 통과한다.

    보존만 재면 아무것도 안 하는 구현이 전부 통과한다 — 이 리포가 여섯 번 물린 자리다.
    """
    picked = set(map(tuple, golden("wide")["cells"]))
    assert picked == car["all"]
    assert picked & car["wheels"] == car["wheels"], "전부 감쌌는데 바퀴가 빠졌다"


def test_no_z_band_can_separate_the_wheels(car):
    """★★ **z 밴드로는 원리적으로 안 갈린다** — 전수 탐색으로 증명한다 (D57).

    세션이 손으로 만든 z 밴드 마스크는 지금까지 **다섯 번 정정**됐다
    (D25-a/b/c/d · D50 · D53). 그 다섯 번이 실수가 아니라 **방법의 한계**였음을
    여기서 보인다: 바퀴와 몸체의 z 범위가 겹치면 어떤 [lo,hi] 도 둘을 못 가른다.
    """
    body_z = np.array(sorted({c[2] for c in car["body"]}))
    wheel_z = np.array(sorted({c[2] for c in car["wheels"]}))
    assert set(body_z) & set(wheel_z), "겹치지 않으면 이 픽스처는 논점을 못 만든다"

    best = 0.0
    for lo in range(64):
        for hi in range(lo, 64):
            band = {c for c in car["all"] if lo <= c[2] <= hi}
            if band & car["wheels"]:
                continue                       # 바퀴가 섞였다 — 관문 실패
            best = max(best, len(band & car["body"]) / len(car["body"]))
    assert best < 0.75, (
        f"z 밴드가 바퀴를 빼고 몸체를 {best:.1%} 나 잡았다 — 픽스처가 논점을 잃었다"
    )

    lasso = set(map(tuple, golden("body")["cells"]))
    assert len(lasso & car["body"]) / len(car["body"]) > best + 0.2, (
        "라쏘가 z 밴드보다 확실히 낫지 않다"
    )


def test_solidify_is_a_no_op_on_a_shell_and_says_so():
    """★ 껍질 표현에서는 압출이 넣은 셀을 교집합이 **도로 지운다.** 버그가 아니다.

    LassoVolume 머리말이 미리 적어 둔 실측 주의다. 계수를 산출물에 남기라고까지
    적혀 있고, 그래서 이 단계가 밥값을 하는지 스스로 증명한다 — 여기서는 **안 한다**
    는 것이 증명됐다. 오목한 껍질이 오면 두 수가 갈릴 것이다.
    """
    for name in ("body", "wide"):
        g = golden(name)
        assert g["solidify_added"] == g["intersect_removed"] > 0, name
        assert g["after_solidify"] - g["intersect_removed"] == g["n_cells"]


def test_camera_faces_the_dominant_axis():
    """측면(-Y) 카메라이므로 압출 축은 y(=1) 여야 한다."""
    assert golden("body")["dominant_axis"] == 1
    assert golden("body")["behind_camera"] == 0


# ══════════════ 구조적 강제 — 재구현 금지 · 상수 드리프트
def test_headless_harness_links_the_contract_file_instead_of_copying():
    """🔴 **LassoVolume 을 재구현하지 않는다**를 파일 구조로 강제한다.

    복사본을 두면 드리프트가 생기고, 그러면 "증명된 코드를 썼다" 는 말이 거짓이 된다.
    """
    csproj = (REPO / "unity/Headless/LassoProbe.csproj").read_text()
    assert "../../contract/unity/LassoVolume.cs" in csproj
    assert not (REPO / "unity/Headless/LassoVolume.cs").exists()
    assert not (REPO / "unity/Runtime/LassoVolume.cs").exists()

    picker = (REPO / "unity/Runtime/SlatLassoPicker.cs").read_text()
    for fn in ("LassoVolume.PointInPolygon", "LassoVolume.DominantAxis",
               "LassoVolume.SolidifyAlongAxis"):
        assert fn in picker, f"{fn} 을 직접 부르지 않는다 — 재구현했을 수 있다"
    for banned in ("static bool PointInPolygon", "static int DominantAxis",
                   "SolidifyAlongAxis(IEnumerable"):
        assert banned not in picker, f"판정을 재구현했다: {banned}"


def test_picker_constants_match_the_contract():
    """상수가 두 곳에 있다 — **드리프트는 테스트가 막는다** (handoff README 와 같은 처방)."""
    picker = (REPO / "unity/Runtime/SlatLassoPicker.cs").read_text()
    contracts = (REPO / "contract/unity/ChunkContracts.cs").read_text()

    from deltacontract.coords import NORMALIZED_MAX, NORMALIZED_MIN, VOXEL_RES

    for cs_name, py_value in (("VoxelRes", VOXEL_RES),
                              ("NormalizedMin", NORMALIZED_MIN),
                              ("NormalizedMax", NORMALIZED_MAX)):
        lit = f"{py_value}f" if isinstance(py_value, float) else str(py_value)
        assert f"const int {cs_name} = {lit}" in picker \
            or f"const float {cs_name} = {lit}" in picker, cs_name
        assert f"const int {cs_name} = {lit}" in contracts \
            or f"const float {cs_name} = {lit}" in contracts, cs_name


def test_picker_projects_voxels_not_vertices():
    """🔴 D58 — 정점을 투영하면 결과가 메시 정점 집합이라 slat 마스크가 아니다."""
    picker = (REPO / "unity/Runtime/SlatLassoPicker.cs").read_text()
    assert "slatCoords" in picker and "VoxelCenter" in picker
    assert "Mesh" not in picker and "vertices" not in picker


# ══════════════ ② dotnet 이 있으면 **실제로 컴파일해 재실행**한다
def _dotnet() -> str | None:
    if os.environ.get("DOTNET_ROOT"):
        cand = pathlib.Path(os.environ["DOTNET_ROOT"]) / "dotnet"
        if cand.exists():
            return str(cand)
    found = shutil.which("dotnet")
    if found:
        return found
    for p in sorted(pathlib.Path("/Applications/Unity/Hub/Editor").glob(
            "*/Unity.app/Contents/Resources/Scripting/DotNetSdk/dotnet"), reverse=True):
        return str(p)
    return None


@pytest.mark.skipif(_dotnet() is None, reason="C# 컴파일러가 없다 — 골든 검사만 돈다")
@pytest.mark.parametrize("name,polygon", [("body", F.BODY_POLYGON),
                                          ("wide", F.WIDE_POLYGON),
                                          ("onewheel", F.ONE_WHEEL_POLYGON)])
def test_probe_reproduces_the_golden(tmp_path, name, polygon):
    """★ 계약의 `LassoVolume.cs` 를 그대로 컴파일해 돌리고 골든과 대조한다."""
    dotnet = _dotnet()
    inp = tmp_path / "in.json"
    out = tmp_path / "out.json"
    inp.write_text(json.dumps(F.probe_input(polygon)))

    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    env.setdefault("DOTNET_ROOT", str(pathlib.Path(dotnet).parent))
    r = subprocess.run(
        [dotnet, "run", "--project", str(REPO / "unity/Headless"), "-c", "Release",
         "--", str(inp), str(out)],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=600,
    )
    assert r.returncode == 0, r.stderr[-2000:]

    got = json.loads(out.read_text())
    want = golden(name)
    assert got["mask_fingerprint"] == want["mask_fingerprint"], (
        "C# 산출물이 골든과 다르다 — 라쏘 경로가 드리프트했다"
    )
    # 🔴 **산출 셀만 대조하면 부족하다.** 실제로 훼손 시험에서 확인했다:
    #    `SolidifyAlongAxis` 의 채움 범위를 1 늘려도 껍질에서는 교집합이 도로
    #    지워서 셀 목록도 지문도 그대로였다. 압출이 바뀐 걸 못 잡는다.
    #    ⇒ **단계별 계수까지 전부 대조한다.** "예외가 안 났다" ≠ "안 바뀌었다".
    assert got == want, (
        "단계별 계수가 골든과 다르다 — 셀은 같아도 경로가 바뀌었다: "
        + ", ".join(f"{k}: {want.get(k)}→{got.get(k)}"
                    for k in want if k != "cells" and got.get(k) != want.get(k))
    )


# ══════════════ ② 라쏘 마스크 → HTTP 본문 (계약 3.26.0)
def test_slat_coords_payload_round_trips_through_the_schema(car):
    """서버가 보낼 본문이 계약 스키마를 통과하는가."""
    pydantic = pytest.importorskip("pydantic")  # noqa: F841
    from deltacontract.schemas import SlatCoordsResponse

    from server.editreq import build_slat_coords_payload

    coords = np.array(sorted(car["all"]), dtype=np.int64)
    payload = build_slat_coords_payload("dragon-c", 7, coords)
    r = SlatCoordsResponse(**{k: v for k, v in payload.items() if k != "uri"})

    assert r.n_cells == len(coords) == len(r.coords)
    assert r.fingerprint == mask_fingerprint(coords)
    assert payload["uri"] == "/v2/assets/dragon-c/slat_coords.v7.json"


def test_edit_request_carries_the_lasso_mask():
    """★ 라쏘 산출물 → EditRequest. 계약 스키마가 그대로 받아야 한다."""
    pytest.importorskip("pydantic")
    from deltacontract.schemas import EditRequest

    from server.editreq import build_edit_request

    req = build_edit_request(
        asset_id="dragon-c", session_id="s1", base_version=3,
        raw_prompt="몸체만 빨갛게", lasso_result=golden("body"),
    )
    parsed = EditRequest(**req)
    assert parsed.mask.mode == "voxels"
    assert parsed.mask.grid_source == "slat_coords"
    assert parsed.mask.is_canonical_grid
    assert len(parsed.mask.voxels) == golden("body")["n_cells"]
    assert mask_fingerprint(np.array(parsed.mask.voxels)) == golden("body")["mask_fingerprint"]


def test_edit_mask_refuses_a_mask_without_a_declared_grid():
    """🔴 D28-a — 격자 출처가 없으면 **채워 넣지 않고 거부한다.**

    채워 넣으면 "무엇으로 만든 마스크인지" 를 서버가 지어낸 것이 된다.
    """
    from server.editreq import GridSourceMissing, build_edit_mask

    g = dict(golden("body"))
    for bad in (None, "surface_voxelize", "lasso"):
        g["grid_source"] = bad
        with pytest.raises(GridSourceMissing, match="D28-a"):
            build_edit_mask(g)


def test_edit_mask_catches_a_truncated_cell_list():
    """잘린 목록은 형태가 멀쩡해서 예외를 안 낸다 — 지문이 잡는다."""
    from server.editreq import build_edit_mask

    g = dict(golden("body"))
    g["cells"] = g["cells"][:-1]           # 한 셀만 잘라낸다
    with pytest.raises(ValueError, match="지문"):
        build_edit_mask(g)


def test_idempotency_key_is_derived_from_content():
    """★ 3.15.5 — 고정 키면 다른 마스크를 보내도 서버가 **옛 연산을 재생**한다.

    Unity 하네스가 실제로 그 상태였고, 재실행할 때마다 재생 경로만 타서
    **정상 경로를 영영 안 밟았다.**
    """
    from server.editreq import derive_idempotency_key

    cells = golden("body")["cells"]
    base = derive_idempotency_key("dragon-c", 3, "빨갛게", cells)
    assert base == derive_idempotency_key("dragon-c", 3, "빨갛게", cells[::-1]), (
        "셀 순서가 키를 바꾼다 — 같은 마스크가 다른 편집으로 취급된다"
    )
    assert base != derive_idempotency_key("dragon-c", 3, "빨갛게", golden("wide")["cells"])
    assert base != derive_idempotency_key("dragon-c", 4, "빨갛게", cells)
    assert base != derive_idempotency_key("dragon-c", 3, "파랗게", cells)
    assert base != derive_idempotency_key("snowman", 3, "빨갛게", cells), (
        "자산이 달라도 같은 키다 — 한쪽 결과가 다른 쪽으로 재생된다"
    )
    assert base != derive_idempotency_key("dragon-c", 3, "빨갛게", cells, seed=43)


# ══════════════ ★★ W18 — 손잡이(handedness). Unity 가 정본이다
def test_one_wheel_lasso_locks_the_handedness(car):
    """★★ **좌우 뒤집힘은 개수로도 계수로도 안 잡힌다** — 소속으로 잡는다.

    W18 실측: Unity 실행 결과가 헤드리스 골든과 **단계별 계수는 전부 일치**하고
    (투영 3,884 · 폴리곤안 3,017 · 압출 +2,592 · 교집합제거 2,592 · 축 1)
    **지문만** 달랐다. 원인은 Unity 가 **왼손 좌표계**라 `transform.LookAt` 의
    기저가 `right = up × fwd` 인데 헤드리스가 오른손(`fwd × up`)을 썼던 것이다.
    Unity 결과가 골든의 **x 미러(63−x)** 와 바이트 단위로 일치해 확정했다.

    자산이 x 대칭이라 몸체 라쏘로는 이게 안 드러난다. 한쪽 바퀴만 잡으면
    뒤집히는 순간 **다른 바퀴**가 잡히므로 소속으로 드러난다.
    """
    g = golden("onewheel")
    picked = set(map(tuple, g["cells"]))
    left = {c for c in car["wheels"] if c[0] < 32}
    right = {c for c in car["wheels"] if c[0] > 32}

    assert picked == right, "🔴 좌우가 뒤집혔다 — 카메라 기저의 손잡이를 확인해라"
    assert picked & left == set()
    assert picked & car["body"] == set(), "몸체가 딸려 들어왔다"
    assert g["n_cells"] == 422


def test_headless_camera_basis_matches_unity():
    """헤드리스 카메라 기저가 Unity(왼손)와 같은 순서인가. 드리프트는 테스트가 막는다."""
    src = (REPO / "unity/Headless/Program.cs").read_text()
    assert "Cross(up, fwd)" in src, "오른손 기저로 되돌아갔다 — Unity 와 x 가 반대가 된다"
    assert "Norm(Cross(fwd, up))" not in src


def test_all_goldens_agree_with_the_contract_fingerprint():
    """세 케이스 전부 계약 함수와 지문이 일치해야 한다."""
    for name in ("body", "wide", "onewheel"):
        g = golden(name)
        assert mask_fingerprint(np.array(g["cells"], dtype=np.int64)) == g["mask_fingerprint"], name
