"""앱 서버 저장 — 판본 청크(`AssetStore` 확장) + SQLite 하나 + 파일(미리보기·평가·staging).

    <APP_DATA_DIR>/app.sqlite3                 QR·자산·작업·평가
    <APP_DATA_DIR>/store/<slot>/v<n>/*.cbin    커밋 판본(바뀐 청크만, 나머지 승계) + removed.json + slat_coords.json
    <APP_DATA_DIR>/staging/<asset>/<job>/      3D 확인 전 청크 + entries.json
    <APP_DATA_DIR>/jobs/<job>/<attempt>/       미리보기 render·edit·mask.png
    <APP_DATA_DIR>/ratings/<rating_id>.json    평가 피드백 파일
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from deltacontract import CONTRACT_CONSTANTS, chunk_uri  # type: ignore[import-not-found]
from deltacontract.schemas import ChunkManifest, ContractInfo  # type: ignore[import-not-found]

from ..assetstore import AssetStore, AssetNotFound, VersionNotFound

__all__ = ["ChunkStore", "Db", "AssetNotFound", "VersionNotFound", "staging_uri"]


def staging_uri(asset_id: str, job_id: str, key: str) -> str:
    return f"/v2/assets/{asset_id}/staging/{job_id}/chunks/{key}.cbin"


class ChunkStore(AssetStore):
    """`AssetStore` 에 **청크 제거**·자산 삭제·staging·manifest 캐시를 더한다.

    읽기 루트는 앱 저장소 하나다(리포의 시험 자산을 앱에 내보이지 않는다).
    """

    def __init__(self, data_dir: Path) -> None:
        super().__init__(root=Path(data_dir) / "store")
        self.read_roots = [self.write_root]
        self.staging_root = Path(data_dir) / "staging"
        self._man_cache: Dict[tuple, ChunkManifest] = {}

    # ── 제거 반영 ──────────────────────────────────────────────────
    def _removed(self, asset_id: str, version: int) -> set:
        p = self._vdir(asset_id, version) / "removed.json"
        return set(json.loads(p.read_text())) if p.is_file() else set()

    def blobs(self, asset_id: str, version: int) -> Dict[str, bytes]:
        self._vdir(asset_id, version)
        out: Dict[str, bytes] = {}
        for v in sorted(v for v in self.versions(asset_id) if v <= version):
            for k in self._removed(asset_id, v):
                out.pop(k, None)
            for p in sorted(self._vdir(asset_id, v).glob("*.cbin")):
                out[p.stem] = p.read_bytes()
        return out

    def chunk(self, asset_id: str, key: str, version: int) -> bytes:
        """`{key}.v{m}.cbin` — m 은 그 청크가 마지막으로 바뀐 판본이다(ChunkEntry.uri)."""
        p = self._vdir(asset_id, version) / f"{key}.cbin"
        if not p.is_file():
            raise VersionNotFound(f"{asset_id} v{version} 에 청크 {key} 가 없다")
        return p.read_bytes()

    def manifest(self, asset_id: str, version: Optional[int] = None) -> ChunkManifest:
        v = self.latest(asset_id) if version is None else int(version)
        ck = (asset_id, v)
        if ck in self._man_cache:
            return self._man_cache[ck]
        self._vdir(asset_id, v)
        last: Dict[str, int] = {}          # 키 → 마지막으로 바뀐 판본
        blobs: Dict[str, bytes] = {}
        for n in sorted(x for x in self.versions(asset_id) if x <= v):
            for k in self._removed(asset_id, n):
                last.pop(k, None)
                blobs.pop(k, None)
            for p in sorted(self._vdir(asset_id, n).glob("*.cbin")):
                last[p.stem], blobs[p.stem] = n, p.read_bytes()
        if not blobs:
            raise VersionNotFound(f"{asset_id} v{v} 이 비어 있다")
        chunks = {}
        for k in sorted(blobs):
            e = self._entry(asset_id, k, blobs[k], last[k])
            chunks[k] = e
        m = ChunkManifest(asset_id=asset_id, version=v, contract=ContractInfo(**CONTRACT_CONSTANTS), chunks=chunks)
        self._man_cache[ck] = m
        return m

    # ── 쓰기 ───────────────────────────────────────────────────────
    def put_version(self, asset_id: str, version: int, blobs: Dict[str, bytes],
                    removed: Iterable[str] = ()) -> Path:
        d = super().put_version(asset_id, version, blobs)
        rem = sorted(set(removed))
        if rem:
            (d / "removed.json").write_text(json.dumps(rem))
        self._man_cache = {k: m for k, m in self._man_cache.items() if k[0] != asset_id}
        return d

    def delete_asset(self, asset_id: str) -> None:
        with self._lock:
            d = self._slots().get(asset_id)
            if d is not None:
                shutil.rmtree(d, ignore_errors=True)
            self._extra.pop(asset_id, None)
            self._slots_cache = None
            self._man_cache = {k: m for k, m in self._man_cache.items() if k[0] != asset_id}
        shutil.rmtree(self.staging_root / self._slug(asset_id), ignore_errors=True)

    def copy_versions(self, src: str, dst: str, upto: int) -> None:
        """src 의 v1..upto 를 dst 로(가짜 모드 생성·샘플)."""
        for n in sorted(x for x in self.versions(src) if x <= upto):
            sd = self._vdir(src, n)
            blobs = {p.stem: p.read_bytes() for p in sd.glob("*.cbin")}
            d = self.put_version(dst, n, blobs, self._removed(src, n))
            if (sd / "slat_coords.json").is_file():
                c = json.loads((sd / "slat_coords.json").read_text())
                self.put_slat_coords(dst, n, c["coords"])
            del d

    # ── slat 좌표 ──────────────────────────────────────────────────
    def slat_coords(self, asset_id: str, version: int) -> Optional[list]:
        try:
            p = self._vdir(asset_id, version) / "slat_coords.json"
        except (AssetNotFound, VersionNotFound):
            return None
        return json.loads(p.read_text())["coords"] if p.is_file() else None

    def put_slat_coords(self, asset_id: str, version: int, coords: list) -> None:
        p = self._vdir(asset_id, version) / "slat_coords.json"
        p.write_text(json.dumps({"coords": coords}, separators=(",", ":")))

    # ── staging ────────────────────────────────────────────────────
    def staging_dir(self, asset_id: str, job_id: str) -> Path:
        return self.staging_root / self._slug(asset_id) / self._slug(job_id)

    def put_staging(self, asset_id: str, job_id: str, to_version: int, blobs: Dict[str, bytes]) -> List[dict]:
        d = self.staging_dir(asset_id, job_id)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        entries = []
        for k in sorted(blobs):
            (d / f"{k}.cbin").write_bytes(blobs[k])
            e = self._entry(asset_id, k, blobs[k], to_version).model_dump(mode="json")
            e["uri"] = staging_uri(asset_id, job_id, k)
            entries.append(e)
        (d / "entries.json").write_text(json.dumps(entries))
        return entries

    def staging_blobs(self, asset_id: str, job_id: str) -> Dict[str, bytes]:
        d = self.staging_dir(asset_id, job_id)
        return {p.stem: p.read_bytes() for p in d.glob("*.cbin")}

    def drop_staging(self, asset_id: str, job_id: str) -> None:
        shutil.rmtree(self.staging_dir(asset_id, job_id), ignore_errors=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY, qr_id TEXT UNIQUE, creator TEXT, scene_prompt TEXT,
  version INTEGER NOT NULL DEFAULT 0, is_sample INTEGER NOT NULL DEFAULT 0,
  sample_name TEXT, sample_note TEXT, created_at REAL, source TEXT);
CREATE TABLE IF NOT EXISTS commits (
  asset_id TEXT, version INTEGER, job_id TEXT, committed_at REAL, PRIMARY KEY (asset_id, version));
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, asset_id TEXT, kind TEXT, device TEXT, state TEXT,
  created_at REAL, updated_at REAL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_asset ON jobs(asset_id);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, job_id TEXT, kind TEXT, model TEXT, ok INTEGER, retried INTEGER,
  latency_s REAL, in_tokens INTEGER, out_tokens INTEGER, error TEXT, input TEXT, output TEXT, attempts TEXT);
CREATE INDEX IF NOT EXISTS llm_job ON llm_calls(job_id);
CREATE TABLE IF NOT EXISTS qr_flags (
  qr_id TEXT PRIMARY KEY, raw_prompt INTEGER NOT NULL DEFAULT 0, note TEXT, set_at REAL);
CREATE TABLE IF NOT EXISTS ratings (
  rating_id TEXT PRIMARY KEY, asset_id TEXT, device TEXT, stars INTEGER,
  v_from INTEGER, v_to INTEGER, created_at REAL, asset_deleted INTEGER NOT NULL DEFAULT 0, body TEXT);
"""


class Db:
    """SQLite 하나. 스레드마다 연결을 쓰지 않고 잠금 하나로 직렬화한다(요청 수가 작다)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.c.executescript(SCHEMA)
            for col in ("deleted_at REAL", "qr_origin TEXT",            # 1.0.2 보관(S3) — 옛 DB 에 열 추가
                        "style TEXT",                                   # S11 자산 양식(NULL = 기본 실물형, S11 이전 포함)
                        "worker_kept INTEGER"):                         # 삭제 때 A5000 저장소 보관 여부(NULL = 11-12 이전 삭제 = 지워짐)
                try:
                    self.c.execute(f"ALTER TABLE assets ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            self.c.execute("UPDATE assets SET qr_origin=qr_id WHERE qr_origin IS NULL AND qr_id IS NOT NULL")

    def q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.c.execute(sql, args))

    def x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.c.execute(sql, args)

    # 자산
    def asset(self, asset_id: str) -> Optional[sqlite3.Row]:
        r = self.q("SELECT * FROM assets WHERE asset_id=?", (asset_id,))
        return r[0] if r else None

    def live_asset(self, asset_id: str) -> Optional[sqlite3.Row]:
        """앱 API 에 보이는 자산(삭제되지 않음). 삭제된 자산은 앱 API 에서 404 다(1.0.2 9절)."""
        a = self.asset(asset_id)
        return a if a is not None and a["deleted_at"] is None else None

    # S14 마커별 플래그(관리 스크립트로 켜고 끔, 앱 API 변경 없음)
    def qr_raw_prompt(self, qr_id: Optional[str]) -> bool:
        if not qr_id:
            return False
        r = self.q("SELECT raw_prompt FROM qr_flags WHERE qr_id=?", (qr_id,))
        return bool(r and r[0]["raw_prompt"])

    def set_qr_raw_prompt(self, qr_id: str, on: bool, note: str = "") -> None:
        import time as _t
        self.x("INSERT INTO qr_flags(qr_id, raw_prompt, note, set_at) VALUES(?,?,?,?) "
               "ON CONFLICT(qr_id) DO UPDATE SET raw_prompt=excluded.raw_prompt, note=excluded.note, set_at=excluded.set_at",
               (qr_id, 1 if on else 0, note, _t.time()))

    def asset_by_qr(self, qr_id: str) -> Optional[sqlite3.Row]:
        r = self.q("SELECT * FROM assets WHERE qr_id=?", (qr_id,))
        return r[0] if r else None

    # 작업
    def save_job(self, j: dict) -> None:
        self.x("INSERT OR REPLACE INTO jobs(job_id,asset_id,kind,device,state,created_at,updated_at,body) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (j["job_id"], j["asset_id"], j["kind"], j["device"], j["state"], j["created_at"],
                j["updated_at"], json.dumps(j, ensure_ascii=False)))

    def log_llm(self, r: dict) -> None:
        self.x("INSERT INTO llm_calls(t,job_id,kind,model,ok,retried,latency_s,in_tokens,out_tokens,error,input,output,attempts) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (time.time(), r.get("job_id"), r["kind"], r["model"], int(bool(r["ok"])), int(bool(r["retried"])),
                r["latency_s"], r["in_tokens"], r["out_tokens"], r.get("error"),
                json.dumps(r.get("input"), ensure_ascii=False), json.dumps(r.get("output"), ensure_ascii=False),
                json.dumps(r.get("attempts") or [], ensure_ascii=False)))

    def llm_calls(self, job_id: str) -> List[dict]:
        out = []
        for row in self.q("SELECT * FROM llm_calls WHERE job_id=? ORDER BY id", (job_id,)):
            d = dict(row)
            for k in ("input", "output", "attempts"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    def jobs(self, where: str = "1", args: tuple = (), limit: int = 1000) -> List[dict]:
        return [json.loads(r["body"]) for r in
                self.q(f"SELECT body FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT {int(limit)}", args)]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def now() -> float:
    return time.time()
