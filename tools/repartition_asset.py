#!/usr/bin/env python3
"""실물 `.cbin` 을 **현재 계약 격자로 재분할**한다 (D75: 8³ → 4³).

    python3 tools/repartition_asset.py <mesh.npz> <out_dir> [--slat coords.npy]

입력은 `tools/extract_v3_mesh.py` 가 낸 `.npz` 다 (정점·면·색). 구 계약 `.cbin` 을
여기서 직접 읽지 않는다 — v4 디코더가 v3 바이트를 거부하는 것이 옳고, 그 경계를
프로세스로 가른다.

🔴 왜 필요한가 — Unity 화면이 큐브인 진짜 원인
────────────────────────────────────────────────────────────────────────
리포의 moto-b 는 `occupancy_to_mesh`(복셀당 큐브 + FACE_INSET)로 만든 **합성본**이다.
형식과 부기는 실물이지만 기하가 큐브라 화면이 큐브로 보인다. A5000 TRELLIS 디코더가
낸 **실물 메시**는 3090 에만 있고 리포에 없었다.

이 도구는 그 실물 `.cbin` 을 읽어 **정점·면·색을 그대로** 새 격자로 다시 나눈다.
기하를 새로 만들지 않는다 — 자르는 자리만 바뀐다.

⚠️ 옛 `.cbin` 과 새 `.cbin` 의 청크 키는 **같아 보여도 다른 자리**다 (D75).
   그래서 매니페스트에 `CONTRACT_CONSTANTS` 를 그대로 실어 받는 쪽이
   `assert_contract_compatible` 로 즉시 거부할 수 있게 한다.

⚠️ `voxel_cells` 로는 **slat 좌표**를 넘긴다 (D28 정본). 표면 복셀화를 넘기면
   청크별 `voxel_count` 가 정본과 어긋나는데 예외는 안 난다.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contract" / "python"))

from deltacontract import CONTRACT_CONSTANTS, CONTRACT_VERSION, encode  # noqa: E402
from deltacontract.partition import partition_mesh  # noqa: E402

def repartition(src: pathlib.Path, out: pathlib.Path,
                slat: pathlib.Path | None = None, asset_id: str = "") -> dict:
    z = np.load(src)
    verts, faces = z["vertices"], z["faces"]
    colors = z["colors"] if "colors" in z.files else None
    cells = np.load(slat).astype(np.int64) if slat and slat.is_file() else None

    meshes = partition_mesh(verts, faces, colors_rgba8=colors, voxel_cells=cells)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.cbin"):
        f.unlink()

    entries, total = [], 0
    for key in sorted(meshes):
        blob = encode(meshes[key])
        (out / f"{key}.cbin").write_bytes(blob)
        entries.append({
            "chunk_id": key,
            "hash": hashlib.sha256(blob).hexdigest(),
            "byte_length": len(blob),
            "vertex_count": int(len(meshes[key].positions)),
            "voxel_count": int(getattr(meshes[key], "voxel_count", 0) or 0),
        })
        total += len(blob)

    return {
        # 🔴 저장소가 자산을 찾는 **유일한 키**다. 없으면 라우트가 자산을 못 연다.
        "asset_id": asset_id,
        "chunk_count": len(entries),
        "cbin_bytes_total": total,
        "vertex_count_total": int(len(verts)),
        "index_count_total": int(faces.size),
        "voxel_count_total": int(len(cells)) if cells is not None else None,
        "has_color": colors is not None,
        # 🔴 받는 쪽이 즉시 거부할 수 있게 계약 상수를 **그대로** 싣는다.
        "contract": dict(CONTRACT_CONSTANTS),
        "contract_version": CONTRACT_VERSION,
        "chunks": entries,
    }


def main() -> int:
    src = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2])
    slat = None
    if "--slat" in sys.argv:
        slat = pathlib.Path(sys.argv[sys.argv.index("--slat") + 1])

    aid = sys.argv[sys.argv.index("--asset-id")+1] if "--asset-id" in sys.argv else ""
    man = repartition(src, out, slat, aid)
    (out / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    # 앱은 디렉터리를 훑을 수 없다 (Android StreamingAssets 는 APK 안이다).
    (out / "chunks.txt").write_text(
        "\n".join(e["chunk_id"] for e in man["chunks"]) + "\n", encoding="utf-8")

    print(f"{src.stem}: 청크 {man['chunk_count']} · {man['cbin_bytes_total']:,} B "
          f"· 정점 {man['vertex_count_total']:,} · 색 {'있음' if man['has_color'] else '없음'}"
          f" · slat {man['voxel_count_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
