#!/usr/bin/env python3
"""라쏘 산출물 → **계약 3.26.0 EditMask** JSON (W18 ③).

3090 이 그대로 편집 요청에 실을 수 있는 형태로 낸다.

    python3 tools/lasso_export_mask.py <lasso_out.json> <mask.json> [--halo 1]

🔴 `grid_source` 를 **채워 넣지 않는다.** 산출물에 없으면 거부한다 (D28-a) —
   채워 넣으면 무엇으로 만든 마스크인지를 이 스크립트가 지어낸 것이 된다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contract" / "python"))

from deltacontract import mask_fingerprint  # noqa: E402

from server.editreq import build_edit_mask  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lasso_out")
    ap.add_argument("out")
    ap.add_argument("--halo", type=int, default=1)
    ap.add_argument("--asset-id", default="")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    raw = json.loads(pathlib.Path(args.lasso_out).read_text())
    mask = build_edit_mask(raw, halo_margin_voxels=args.halo)

    doc = {
        "mask": mask,
        # 편의 필드 — 계약 본문이 아니라 **인수인계용 메타**다.
        "meta": {
            "asset_id": args.asset_id,
            "n_cells": len(mask["voxels"]),
            "mask_fingerprint": mask_fingerprint(mask["voxels"]),
            "contract": "3.26.0",
            "note": args.note,
        },
    }
    pathlib.Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    m = doc["meta"]
    print(f"{args.out} · 셀 {m['n_cells']} · halo {args.halo} · "
          f"grid_source {mask['grid_source']} · 지문 {m['mask_fingerprint']}")


if __name__ == "__main__":
    main()
