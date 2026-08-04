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
    for m in MIRRORED:
        cls = by_name.get(m)
        if cls is None:
            missing.append(f"{m} 이 schemas.py 에 없다 (MIRRORED 목록이 낡았다)")
            continue
        for f in _fields(cls):
            if f not in have:
                missing.append(f"{m}.{f}")
    return missing
