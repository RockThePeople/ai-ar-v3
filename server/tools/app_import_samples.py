"""샘플 자산(app-api 3.7·8절)과 가짜 모드 원천을 앱 저장소에 넣는다.

    python -m server.tools.app_import_samples                       # A5000 워커 HTTP 에서 샘플을 받는다
    python -m server.tools.app_import_samples --mock-edit <편집 결과 폴더>  # 가짜 모드 원천(A2 전 임시)
    python -m server.tools.app_import_samples --skip-samples --bind-qr <qr> --bind-asset <A5000 asset_id> --creator <기기 키>
                                                                    # A5000 자산(커밋 판본 전부)을 QR 에 묶는다(실제 모드 시험)

- 샘플: A5000 의 `vhl-ta-moto`·`vhl-ta-snowman`·`vhl-ta-chair` v1 → `sample-moto`·`sample-snowman`·`sample-chair`,
  `sample-jeep`·`sample-cottage`·`sample-adventurer` v1–v6(A2 가 A5000 에 넣으면). 없는 것은 건너뛴다. 읽기 전용.
- 가짜 모드 고정 PNG(`--mock-fixtures`, 계약 7절): A5000 `sample-jeep` v1 을 고정 카메라로 렌더한 조건 렌더·마스크
  (마스크 = v1→v2 에서 바뀐 청크 영역)와 같은 카메라의 v2 렌더(편집 결과 자리)를 `mock_fixtures/` 에 둔다.
  원천 자산은 `APP_MOCK_SOURCE`(기본 `sample-jeep`)이다.
- (옛, A2 전 임시) 가짜 모드 원천(`--mock-edit`): 편집 결과 폴더(종단 기록의 `<ID>/r<k>/`: `patch.json`, `store/<asset>/v<n>/*.cbin`,
  `2d_render.png`·`2d_mask.png`·`2d_edit.png`)를 받아 `mock-src` 자산(v1 = 그 편집의 부모 샘플, v2 = 변경분)과
  `mock_fixtures/` PNG 3장을 만든다. A2 뒤에는 `APP_MOCK_SOURCE=sample-jeep` 로 바꾸고 이 원천은 쓰지 않는다(계약 7절).
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from server.app.backends import RealBackend, StageError
from server.app.config import Settings, load_env_file
from server.app.store import ChunkStore, Db

TRACK_A = [("moto", "vhl-ta-moto"), ("snowman", "vhl-ta-snowman"), ("chair", "vhl-ta-chair")]
SEQ = ["jeep", "cottage", "adventurer"]


def import_one(be: RealBackend, st: ChunkStore, db: Db, name: str, src: str, dst: str, versions, note: str) -> list:
    """A5000 커밋 판본들을 앱 저장소로. **바뀐 청크는 이전 판본과 해시로 가른다.**

    A5000 manifest 는 판본마다 모든 청크를 그 판본 번호로 적는다(uri `.v{n}.cbin`). 그 번호로 가르면 매 판본이
    전체 교체가 되고, 앱 manifest 의 ChunkEntry.version(계약 3.5: 마지막으로 바뀐 판본 = 캐시 키)이 틀어진다.
    """
    st.delete_asset(dst)
    got, prev = [], {}
    for n in versions:
        try:
            man = be.fetch_committed(src, n)["chunks"]
        except StageError as e:
            print(f"  {src} v{n}: 없음 ({e})")
            break
        changed = sorted(k for k, e in man.items() if prev.get(k) != e["hash"])
        removed = sorted(set(prev) - set(man))
        blobs = {k: be.fetch_chunk(man[k]["uri"]) for k in changed}
        st.put_version(dst, n, blobs, removed)
        coords = be.slat_coords(src, n)
        if coords:
            st.put_slat_coords(dst, n, coords)
        prev = {k: e["hash"] for k, e in man.items()}
        got.append(n)
        print(f"  {src} v{n} → {dst}: 전체 {len(man)} · 바뀐 청크 {len(changed)} · 제거 {len(removed)} · "
              f"slat {len(coords) if coords else '없음'}")
    if got:
        db.x("INSERT OR REPLACE INTO assets(asset_id,qr_id,creator,scene_prompt,version,is_sample,sample_name,"
             "sample_note,created_at,source) VALUES(?,NULL,NULL,NULL,?,1,?,?,?,?)",
             (dst, max(got), name, note, time.time(), f"a5000:{src}"))
    return got


def mock_source(st: ChunkStore, data: Path, edit_dir: Path, parent: str) -> None:
    patch = json.loads((edit_dir / "patch.json").read_text())
    vdir = next((edit_dir / "store").glob(f"*/v{patch['to_version']}"))
    st.delete_asset("mock-src")
    st.copy_versions(parent, "mock-src", 1)
    st.put_version("mock-src", 2, {p.stem: p.read_bytes() for p in vdir.glob("*.cbin")}, patch["removed_chunk_ids"])
    fx = data / "mock_fixtures"
    fx.mkdir(parents=True, exist_ok=True)
    for a, b in (("2d_render", "render"), ("2d_mask", "mask"), ("2d_edit", "edit")):
        shutil.copyfile(edit_dir / f"{a}.png", fx / f"{b}.png")
    print(f"mock-src: v1 = {parent} v1, v2 = {patch['asset_id']} v{patch['from_version']}→v{patch['to_version']} "
          f"변경 {len(list(vdir.glob('*.cbin')))} · 제거 {len(patch['removed_chunk_ids'])}; 고정 PNG 3장 → {fx.name}/")


MOCK_CAMERA = {"frame": "normalized_voxel", "position": [1.1, -1.25, 0.75], "forward": [-0.6024, 0.6845, -0.4107],
               "up": [-0.2715, 0.3085, 0.9117], "fov_y_deg": 50.0, "aspect": 1.0}


def mock_fixtures(be: RealBackend, data: Path, src: str = "sample-jeep") -> None:
    """계약 7절 고정 PNG 3장을 sample-jeep 에서 만든다(렌더 v1·마스크, 같은 카메라의 v2 렌더)."""
    man1, man2 = be.fetch_committed(src, 1)["chunks"], be.fetch_committed(src, 2)["chunks"]
    changed = [k for k, e in man2.items() if (man1.get(k) or {}).get("hash") != e["hash"]]
    cells = sorted({(cx * 4 + i, cy * 4 + j, cz * 4 + k)
                    for key in changed for cx, cy, cz in [map(int, key.split("_"))]
                    for i in range(4) for j in range(4) for k in range(4)})
    cells = [list(c) for c in cells if all(0 <= x < 64 for x in c)]
    r1 = be.render(src, 1, MOCK_CAMERA, cells)
    r2 = be.render(src, 2, MOCK_CAMERA, cells)
    fx = data / "mock_fixtures"
    fx.mkdir(parents=True, exist_ok=True)
    (fx / "render.png").write_bytes(r1["render_png"])
    (fx / "mask.png").write_bytes(r1["mask_png"])
    (fx / "edit.png").write_bytes(r2["render_png"])
    (fx / "source.json").write_text(json.dumps({"asset": src, "render_v1": r1["render_id"], "render_v2": r2["render_id"],
                                                "changed_chunks_v2": len(changed), "mask_cells": len(cells),
                                                "camera": MOCK_CAMERA}, indent=1))
    print(f"mock_fixtures: {src} v1 렌더·마스크(바뀐 청크 {len(changed)}개 → {len(cells)}셀) + v2 렌더 → {fx.name}/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock-edit", help="가짜 모드 원천으로 쓸 편집 결과 폴더(A2 전 임시)")
    ap.add_argument("--mock-parent", default="sample-moto", help="그 편집의 부모 샘플 asset_id")
    ap.add_argument("--skip-samples", action="store_true")
    ap.add_argument("--only", help="샘플 이름 쉼표 목록(예: jeep)")
    ap.add_argument("--mock-fixtures", action="store_true", help="계약 7절 고정 PNG 3장을 sample-jeep 에서 만든다")
    ap.add_argument("--bind-qr")
    ap.add_argument("--bind-asset")
    ap.add_argument("--creator", default="")
    ap.add_argument("--scene", default=None, help="장면 문장")
    a = ap.parse_args()
    load_env_file()
    s = Settings.from_env()
    st, db = ChunkStore(s.data_dir), Db(s.data_dir / "app.sqlite3")
    be = RealBackend(s, st)
    if not a.skip_samples:
        only = set(a.only.split(",")) if a.only else None
        for name, src in TRACK_A:
            if only and name not in only:
                continue
            import_one(be, st, db, name, src, f"sample-{name}", [1], "트랙 A 생성 자산")
        for name in SEQ:
            if only and name not in only:
                continue
            import_one(be, st, db, name, f"sample-{name}", f"sample-{name}", range(1, 7),
                       "원본 + 연속 편집 5회(제안 경로)" if name == "jeep" else "원본 + 연속 편집 5회")
    if a.bind_qr:
        got = import_one(be, st, db, a.bind_asset, a.bind_asset, a.bind_asset, range(1, 100), "")
        db.x("UPDATE assets SET qr_id=?, creator=?, scene_prompt=?, is_sample=0, sample_name=NULL, sample_note=NULL "
             "WHERE asset_id=?", (a.bind_qr, a.creator, a.scene, a.bind_asset))
        for n in got:
            db.x("INSERT OR REPLACE INTO commits VALUES(?,?,?,?)", (a.bind_asset, n, "bind", time.time()))
        print(f"QR {a.bind_qr} → {a.bind_asset} v{max(got)}")
    if a.mock_fixtures:
        mock_fixtures(be, s.data_dir, s.mock_source)
    if a.mock_edit:
        mock_source(st, s.data_dir, Path(a.mock_edit), a.mock_parent)


if __name__ == "__main__":
    main()
