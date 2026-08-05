#!/usr/bin/env python3
"""라쏘 케이스를 Unity 배치 검사용 평문으로 내보낸다 (W18).

Unity 런타임에는 `System.Text.Json` 이 없다. JSON 파서를 Editor 쪽에 새로 짜면
그 파서가 또 하나의 미검증 코드가 되므로, **입력 형식을 단순하게** 만든다.

    python3 tools/lasso_case_export.py <probe_input.json> <golden.json> <out.case>
"""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> None:
    inp = json.loads(pathlib.Path(sys.argv[1]).read_text())
    golden = json.loads(pathlib.Path(sys.argv[2]).read_text())
    out = pathlib.Path(sys.argv[3])

    cam = inp["camera"]
    lines = [f"NAME {out.stem}"]
    lines.append("CAM " + " ".join(
        f"{v:.6f}" for v in (*cam["position"], *cam["target"], *cam["up"],
                             cam["fov_deg"], cam["width"], cam["height"])))

    # 🔴 D60 — 최종 산출물만이 아니라 **단계별 계수 전부**를 기대값으로 싣는다.
    for key in ("n_cells", "mask_fingerprint", "grid_source", "projected",
                "behind_camera", "in_polygon", "after_solidify", "solidify_added",
                "intersect_removed", "dominant_axis"):
        lines.append(f"EXPECT {key} {golden[key]}")

    lines.append("POLY")
    lines += [f"{x:.6f} {y:.6f}" for x, y in inp["polygon"]]
    lines.append("COORDS")
    lines += [f"{x} {y} {z}" for x, y, z in inp["slat_coords"]]

    out.write_text("\n".join(lines) + "\n")
    print(f"{out} · 폴리곤 {len(inp['polygon'])} · 복셀 {len(inp['slat_coords'])} "
          f"· 기대 셀 {golden['n_cells']}")


if __name__ == "__main__":
    main()
