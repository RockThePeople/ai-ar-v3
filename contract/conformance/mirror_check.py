"""C# 미러 대조 — **pydantic 없이** 돈다.

🔴 왜 이 파일이 따로 있나.

`test_contract.py::test_schema_field_names_match_csharp_mirror` 가 같은 일을 하지만
`@needs_schemas` 라서 **pydantic 이 없으면 skip 된다.** 그리고 계약 작성자 환경에는
pydantic 이 없다.

    pydantic 있음(세션)     50 passed / 1 failed      ← 미러 누락이 보인다
    pydantic 없음(작성자)   47 passed / 0 failed      ← **전부 초록으로 보인다**

3.11.2 에서 이미 한 번 이 형태로 사고가 났고, HANDOFF 에 "스키마를 바꿀 때 C# 미러를
안 고치면 pydantic 있는 환경에서만 깨진다 — 작성자 환경에서는 skip 되어 안 보인다" 로
적혀 있었는데, **3.21.1 에서 그대로 재현됐다**(PatchPackage 의 마스크 반향 2필드).

적어두는 것으로는 안 막힌다. 검사가 **작성자 환경에서 실행되어야** 막힌다.
그래서 AST 로 필드를 뽑아 pydantic 없이 같은 대조를 한다.

⚠️ 이 파일과 `test_contract.py` 쪽은 **중복이 아니라 이중화다.** 한쪽은 실제 모델을
보고(정확), 한쪽은 소스를 보고(어디서나 실행). 둘 다 남긴다.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# C# 미러가 존재해야 하는 모델. test_contract.py 의 목록과 같게 유지한다.
MIRRORED = [
    "ContractInfo", "ChunkEntry", "ChunkManifest", "PatchPackage", "SpatialContext",
    "GenerateRequest", "SlatCoordsResponse", "EditMask", "EditRequest", "AssembleRequest", "JobStatus", "ErrorBody", "ServerHealth",
]

_SKIP = {"model_config"}


def _fields(cls: ast.ClassDef) -> list:
    out = []
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            n = node.target.id
            if n not in _SKIP and not n.startswith("_"):
                out.append(n)
    return out


#: C# `DeltaConstants` / `ChunkBin` 의 상수 ↔ 파이썬 `CONTRACT_CONSTANTS` 키.
#
# 🔴 **이름만 대조하면 값이 갈려도 통과한다.** C# 쪽은 `ChunkGridRes = VoxelRes/ChunkSize`
#    처럼 **보이지만 실제로는 리터럴**이라, 한쪽만 고치면 어긋나고 **런타임에만** 드러난다.
#    D75(청크 8→4)가 그 구멍을 실제로 밟을 수 있는 첫 사례라 값 대조를 넣는다.
_CONST_MIRROR = {
    "VoxelRes": "voxel_res",
    "ChunkSize": "chunk_size",
    "ChunkGridRes": "chunk_grid_res",
    "PositionQuantBits": "position_quant_bits",
    "SlatChannels": "slat_channels",
    "MeshRes": "mesh_res",
}


def _check_constant_values(cs: str) -> list:
    """C# 리터럴을 텍스트로 읽어 파이썬 상수와 **값까지** 대조한다."""
    import sys

    sys.path.insert(0, str(ROOT / "python"))
    from deltacontract.coords import CONTRACT_CONSTANTS  # noqa: PLC0415

    bad = []
    for cs_name, py_key in _CONST_MIRROR.items():
        m = re.search(rf"const\s+int\s+{cs_name}\s*=\s*(-?\d+)\s*;", cs)
        if m is None:
            bad.append(f"C# 에 상수 {cs_name} 이 없다")
            continue
        got, want = int(m.group(1)), CONTRACT_CONSTANTS[py_key]
        if got != want:
            bad.append(f"상수 값 불일치 {cs_name}={got} vs {py_key}={want}")

    # `ChunkBin.ContractVersion` 은 다른 파일에 또 하드코딩돼 있다 — 셋째 자리다.
    cb = (ROOT / "unity" / "ChunkBin.cs").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"const\s+uint\s+ContractVersion\s*=\s*(\d+)\s*;", cb)
    if m is None:
        bad.append("ChunkBin.cs 에 ContractVersion 이 없다")
    elif int(m.group(1)) != CONTRACT_CONSTANTS["contract_version"]:
        bad.append(f"ChunkBin.ContractVersion={m.group(1)} vs "
                   f"contract_version={CONTRACT_CONSTANTS['contract_version']}")

    # 파생 관계도 본다 — C# 은 계산이 아니라 리터럴이라 스스로 안 지켜진다.
    if CONTRACT_CONSTANTS["voxel_res"] // CONTRACT_CONSTANTS["chunk_size"] \
            != CONTRACT_CONSTANTS["chunk_grid_res"]:
        bad.append("chunk_grid_res 가 voxel_res/chunk_size 와 다르다")
    return bad


def check() -> list:
    """미러에 없는 `모델.필드` 목록. 빈 리스트면 정상."""
    cs_path = ROOT / "unity" / "ChunkContracts.cs"
    if not cs_path.exists():
        return ["unity/ChunkContracts.cs 가 없다 — 대조 불가 (Unity 트리가 정본이다)"]
    cs = cs_path.read_text(encoding="utf-8", errors="replace")
    have = set(re.findall(r'JsonProperty\("([^"]+)"', cs))

    tree = ast.parse((ROOT / "python" / "deltacontract" / "schemas.py").read_text(encoding="utf-8"))
    by_name = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    missing = []
    missing += _check_constant_values(cs)
    for m in MIRRORED:
        cls = by_name.get(m)
        if cls is None:
            missing.append(f"{m} 이 schemas.py 에 없다 (MIRRORED 목록이 낡았다)")
            continue
        for f in _fields(cls):
            if f not in have:
                missing.append(f"{m}.{f}")
    return missing


# ⚠️ 이 블록이 **없었다** (W24 발견). `python3 mirror_check.py` 는 CLAUDE.md 가
#    개발 명령으로 안내하는 검사인데, 단독 실행하면 아무것도 출력하지 않고 rc=0 으로
#    끝났다 — `run_conformance.py` 가 import 해서 쓸 때만 결과가 보였다.
#    **자기 검사가 자기 환경에서 안 돌면 자기를 보호하지 않는다** (방법론 5조).
if __name__ == "__main__":
    import sys as _sys

    problems = check()
    if problems:
        print(f"❌ C# 미러 불일치 {len(problems)}건")
        for p in problems:
            print(f"   · {p}")
        _sys.exit(1)
    print(f"ok C# 미러 — 모델 {len(MIRRORED)}개 필드 + 상수 {len(_CONST_MIRROR)}개 값 일치")
