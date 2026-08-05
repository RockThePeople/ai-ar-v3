"""in-place 청크 교체의 **재료**를 잠근다 (W21 · D70).

씬 조작 자체는 Unity 가 아니면 못 잰다 — `tools/unity_inplace_check.sh` 가 정본
게이트다 (D65: 엔진 밖 모형은 엔진 안 결과의 증거가 아니다). 여기서 잠그는 것은
엔진 없이도 참이어야 하는 것들이다:

    · 패치가 **changed / added / removed 셋으로** 적혀 있는가 (D72)
    · recolor 가 청크 집합을 안 바꾸는가 (added = removed = 0)
    · C# 의 D9 역변환이 **정본 `frames.VOXEL_TO_GLB` 와 같은가**
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import numpy as np
import pytest

from server.pipeline.frames import GLB_TO_VOXEL, VOXEL_TO_GLB

REPO = pathlib.Path(__file__).resolve().parents[2]
APPLIER = REPO / "unity/Runtime/ChunkSceneApplier.cs"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """실제 `.cbin` 을 만든다. 3090 의 W20 수치(89청크·변경 24)와 대조하려는 것이다."""
    out = tmp_path_factory.mktemp("inplace")
    r = subprocess.run([sys.executable, str(REPO / "tools/build_moto_patch.py"), str(out)],
                       capture_output=True, text=True, cwd=str(REPO), timeout=1800)
    assert r.returncode == 0, r.stderr[-3000:]
    return out


def test_patch_matches_3090_numbers(built):
    """★ 89청크 · 변경 24 — 3090 이 실제 파이프라인에서 낸 수치와 같다."""
    patch = json.loads((built / "patch.json").read_text())
    assert patch["n_chunks_total"] == 89
    assert len(patch["changed"]) == 24
    assert len(list((built / "parent").glob("*.cbin"))) == 89
    assert len(list((built / "patch").glob("*.cbin"))) == 24

    ref = json.loads((REPO / "handoff/w20-recolor/result.json").read_text())
    assert patch["n_chunks_total"] == ref["n_chunks_total"]
    assert len(patch["changed"]) == ref["n_chunks_changed"]


def test_patch_is_written_as_three_sets_not_two(built):
    """🔴 D72 — changed/added/removed 를 **셋으로** 적는다. 쌍으로 적으면
    removed 가 changed 에 섞여 들어가고, 그때 GameObject 는 **파괴된다.**"""
    patch = json.loads((built / "patch.json").read_text())
    for key in ("changed", "added", "removed"):
        assert key in patch, f"{key} 가 없다 — 셋이 아니라 쌍으로 적혔다"
    # recolor 는 청크 집합을 바꾸지 않는다. 아니면 경로가 깨진 것이다.
    assert patch["added"] == [] and patch["removed"] == []


def test_saving_is_reference_only(built):
    """절감률은 **참고값**이다 (D70). 문턱으로 쓰지 않는다."""
    patch = json.loads((built / "patch.json").read_text())
    assert "saving_reference_only" in patch
    assert 0.0 < patch["saving_reference_only"] < 1.0
    # 이름에 목적이 박혀 있어야 한다 — 이름이 없으면 다음 세션이 문턱으로 쓴다.
    assert not any(k in patch for k in ("saving", "transfer_saving"))


def test_removed_fixture_exists_for_the_dangerous_path(built):
    """removed 는 GameObject 를 파괴한다 — 그 경로를 시험할 자료가 있어야 한다 (§3-E)."""
    p = json.loads((built / "patch-removed.json").read_text())
    assert len(p["removed"]) == 1
    victim = p["removed"][0]
    assert victim not in p["changed"], "파괴할 청크를 동시에 changed 로 보내면 안 된다"
    assert (built / "parent" / f"{victim}.cbin").exists()


# ══════════════ D9 — C# 역변환이 정본과 같은가
def test_csharp_voxel_to_unity_matches_the_canonical_transform():
    """🔴 매직넘버가 두 언어에 흩어져 있다. **드리프트는 테스트가 막는다.**

    정본은 `frames.GLB_TO_VOXEL` (voxel = (x, −z, y)) 이고, C# 은 그 역을 쓴다.
    소스에서 식을 읽어 정본과 **수치로** 대조한다 — 주석 대조가 아니다.
    """
    src = APPLIER.read_text()
    m = re.search(r"VoxelToUnity\(Vector3 v\)\s*=>\s*new Vector3\(([^)]+)\)", src)
    assert m, "VoxelToUnity 를 못 찾았다"
    expr = [t.strip() for t in m.group(1).split(",")]

    def apply(v):
        env = {"v.x": v[0], "v.y": v[1], "v.z": v[2]}
        out = []
        for e in expr:
            neg = e.startswith("-")
            out.append((-1 if neg else 1) * env[e.lstrip("-")])
        return np.array(out, dtype=np.float64)

    rng = np.random.default_rng(0)
    for v in rng.integers(-9, 10, size=(20, 3)).astype(np.float64):
        assert np.allclose(apply(v), VOXEL_TO_GLB.apply(v.reshape(1, 3))[0]), v
        # 왕복이 항등이어야 한다 — 한쪽만 맞으면 조용히 뒤집힌 채로 돈다.
        assert np.allclose(GLB_TO_VOXEL.apply(apply(v).reshape(1, 3))[0], v), v


def test_csharp_uses_entity_id_not_instance_id():
    """Unity 6.5 는 `GetInstanceID()` 를 폐기했다. int 로 좁히지도 않는다 —
    좁히면 미래에 서로 다른 오브젝트가 같은 값으로 보이고, 그때 '유지됐다' 가
    조용히 거짓이 된다."""
    src = APPLIER.read_text()
    # 주석은 뺀다 — 폐기 사실을 **설명하는** 주석까지 걸리면 검사가 자기를 속인다.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    assert "GetEntityId()" in code
    assert "GetInstanceID()" not in code
    assert "public EntityId EntityId;" in code


def test_applier_does_not_reimplement_chunkbin():
    """디코딩·ApplyTo 는 계약 코드가 한다. 재구현 금지."""
    src = APPLIER.read_text()
    assert "ChunkBin.Decode(" in src and "ChunkBin.ApplyTo(" in src
    for banned in ("ReadHeader", "BitConverter.ToUInt32(blob", "CBN1"):
        assert banned not in src, f"ChunkBin 을 재구현했다: {banned}"


def test_removed_path_deletes_from_the_dictionary():
    """§3-E — 파괴 직후 사전에서 지우는 줄이 실제로 있는가.

    없으면 다음 패치가 파괴된 MeshFilter 에 ApplyTo 를 걸고 **예외가 안 난다.**
    """
    src = APPLIER.read_text()
    body = src[src.index("if (removed != null)"):src.index("// ── ② changed")]
    assert "_nodes.Remove(key)" in body, "파괴만 하고 사전에서 안 지운다 (§3-E)"
    assert "DestroyNode(node)" in body
