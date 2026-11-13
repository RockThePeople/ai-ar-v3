"""S14 관리 스크립트 — 마커별 그대로 모드(raw_prompt)와 샘플 사본 등록. 앱 API 는 바꾸지 않는다.

  python -m server.tools.papermode flag <qr_id> on|off [--note …]
  python -m server.tools.papermode show
  python -m server.tools.papermode copy-sample <sample_id> <version> <qr_id> <creator> --scene "<장면 문장>" [--asset-id a-…]

APP_DATA_DIR 로 데이터 폴더를 고른다(기본 ~/vhl-app-data). 서비스가 떠 있어도 SQLite 쓰기는 안전하지만,
새 자산 폴더는 서비스 재기동 뒤 저장소 캐시에 잡히므로 copy-sample 뒤에는 재기동한다.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from server.app.store import ChunkStore, Db


def _paths():
    data = Path(os.environ.get("APP_DATA_DIR") or "~/vhl-app-data").expanduser()
    return data, Db(data / "app.sqlite3"), ChunkStore(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("flag"); f.add_argument("qr"); f.add_argument("state", choices=["on", "off"]); f.add_argument("--note", default="")
    sub.add_parser("show")
    c = sub.add_parser("copy-sample")
    c.add_argument("sample"); c.add_argument("version", type=int); c.add_argument("qr"); c.add_argument("creator")
    c.add_argument("--scene", required=True); c.add_argument("--asset-id")
    g = sub.add_parser("generate", help="그 마커에서 생성 작업을 연다(앱 생성과 같은 엔진 경로, 서비스는 멈춘 상태에서)")
    g.add_argument("qr"); g.add_argument("creator"); g.add_argument("--prompt", required=True); g.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        return _generate(a)
    data, db, store = _paths()
    if a.cmd == "flag":
        db.set_qr_raw_prompt(a.qr, a.state == "on", a.note)
        print(json.dumps({"qr_id": a.qr, "raw_prompt": db.qr_raw_prompt(a.qr)}))
    elif a.cmd == "show":
        print(json.dumps([dict(r) for r in db.q("SELECT * FROM qr_flags")], ensure_ascii=False))
    else:
        s = db.asset(a.sample)
        if s is None or not s["is_sample"]:
            raise SystemExit(f"샘플이 아니다: {a.sample}")
        if db.asset_by_qr(a.qr) is not None:
            raise SystemExit(f"마커가 비어 있지 않다: {a.qr}")
        aid = a.asset_id or f"a-{uuid.uuid4().hex[:8]}"
        if db.asset(aid) is not None:
            raise SystemExit(f"asset_id 가 이미 있다: {aid}")
        # 판본 1 하나만(원본 v{version} 을 사본의 v1 로). copy_versions 는 1..upto 를 그대로 옮기므로 v1 만 쓴다
        if a.version != 1:
            raise SystemExit("지금은 v1 만 지원한다")
        store.copy_versions(a.sample, aid, upto=1)
        db.x("INSERT INTO assets(asset_id,qr_id,qr_origin,creator,scene_prompt,version,created_at,source) VALUES(?,?,?,?,?,1,?,?)",
             (aid, a.qr, a.qr, a.creator, a.scene, time.time(), f"sample_copy:{a.sample}@v{a.version}"))
        db.x("INSERT OR REPLACE INTO commits VALUES(?,?,?,?)", (aid, 1, f"sample_copy:{a.sample}@v{a.version}", time.time()))
        print(json.dumps({"asset_id": aid, "qr_id": a.qr, "version": 1, "n_chunks": len(store.blobs(aid, 1)),
                          "slat_coords": bool(store.slat_coords(aid, 1))}))
    return 0


def _generate(a) -> int:
    """앱 서버와 같은 Engine·실제 백엔드로 생성 1건을 이 프로세스에서 돌린다. 8083 은 멈춘 뒤 실행한다(GPU 큐 하나)."""
    from server.app.backends import RealBackend
    from server.app.config import Settings
    from server.app.engine import Engine
    s = Settings.from_env()
    if s.mock:
        raise SystemExit("APP_MOCK=1 에서는 쓰지 않는다")
    db, store = Db(s.data_dir / "app.sqlite3"), ChunkStore(s.data_dir)
    eng = Engine(s, db, store, RealBackend(s, store), start_thread=False)
    if not db.qr_raw_prompt(a.qr):
        raise SystemExit(f"마커 {a.qr} 에 raw_prompt 가 켜져 있지 않다(먼저 flag … on)")
    j = eng.create_asset(a.qr, a.creator, a.prompt, a.seed)
    eng.drain()
    j = eng.get(j["job_id"])
    print(json.dumps({"job_id": j["job_id"], "asset_id": j["asset_id"], "state": j["state"], "error": j.get("error_code"),
                      "paper_gen": j.get("paper_gen"), "t2i_effective": ((j.get("t2i") or {}).get("timing") or {}).get("prompt_effective")},
                     ensure_ascii=False))
    return 0 if j["state"] == "committed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
