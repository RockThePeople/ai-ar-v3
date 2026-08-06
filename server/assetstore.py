"""버전이 붙은 `.cbin` 저장소. 라우트가 자산을 읽고 쓰는 **유일한 문**이다.

────────────────────────────────────────────────────────────────────────
왜 별도 모듈인가
────────────────────────────────────────────────────────────────────────
라우트가 파일시스템을 직접 만지면 두 가지가 새어 나간다: 홈 경로(§6)와 버전 규약.
버전은 `.cbin` 파일명이 아니라 **디렉터리**로 가른다 — `v1/` `v2/` … 그래야 한 판본을
통째로 승계하거나 버릴 수 있고, 파일명 파싱 실수로 옛 판본을 조용히 섞지 않는다.

    <ASSET_ROOT>/<slot>/parent/*.cbin     ← v1 (리포에 커밋된 실물)
    <ASSET_ROOT>/<slot>/v<N>/*.cbin       ← 편집 결과 (런타임 생성)

🔴 매니페스트의 `contract` 는 **지금 이 프로세스의 계약 상수**를 그대로 싣는다.
   손으로 적지 않는다 — 적으면 계약이 올라갈 때 조용히 갈라지고, 받는 쪽의
   `assert_contract_compatible` 이 틀린 값으로 통과시킨다.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from deltacontract import (  # type: ignore[import-not-found]
    CONTRACT_CONSTANTS,
    chunk_uri,
)
from deltacontract.schemas import (  # type: ignore[import-not-found]
    ChunkEntry,
    ChunkManifest,
    ContractInfo,
)

__all__ = ["AssetStore", "STORE", "AssetNotFound", "VersionNotFound"]


class AssetNotFound(KeyError):
    """모르는 asset_id. **비슷한 것을 대신 주지 않는다.**"""


class VersionNotFound(KeyError):
    """그 자산에 그 판본이 없다. 옛 판본으로 대신 답하지 않는다 — 다른 물체가 된다."""


@dataclass(frozen=True)
class _Version:
    n: int
    path: Path


class AssetStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or os.environ.get(
            "ASSET_STORE_ROOT", str(Path(__file__).resolve().parent.parent / "assets")))
        self._lock = threading.Lock()
        self._extra: Dict[str, Dict[int, Path]] = {}   # 런타임 판본 (편집 결과)

    # ── 조회 ────────────────────────────────────────────────────────
    def _slots(self) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            m = d / "manifest.json"
            if m.is_file():
                try:
                    aid = json.loads(m.read_text()).get("asset_id")
                except (OSError, ValueError):
                    continue
                if aid:
                    out[aid] = d
        return out

    def asset_ids(self) -> List[str]:
        return sorted(self._slots())

    def _dir(self, asset_id: str) -> Path:
        d = self._slots().get(asset_id)
        if d is None:
            raise AssetNotFound(f"모르는 자산이다: {asset_id!r}. 아는 것: {self.asset_ids()}")
        return d

    def versions(self, asset_id: str) -> Dict[int, Path]:
        base = self._dir(asset_id)
        out = {1: base / "parent"}
        for d in sorted(base.glob("v*")):
            if d.is_dir() and d.name[1:].isdigit():
                out[int(d.name[1:])] = d
        out.update(self._extra.get(asset_id, {}))
        return {k: v for k, v in sorted(out.items()) if v.is_dir()}

    def latest(self, asset_id: str) -> int:
        return max(self.versions(asset_id))

    def _vdir(self, asset_id: str, version: int) -> Path:
        vs = self.versions(asset_id)
        if version not in vs:
            raise VersionNotFound(
                f"{asset_id} 에 v{version} 이 없다. 있는 것: {sorted(vs)}")
        return vs[version]

    # ── 청크 ────────────────────────────────────────────────────────
    def chunk(self, asset_id: str, key: str, version: int) -> bytes:
        """한 청크의 바이트. **판본이 없으면 거부한다** — 옛것으로 대신 답하지 않는다."""
        d = self._vdir(asset_id, version)
        p = d / f"{key}.cbin"
        if not p.is_file():
            # 편집 판본은 **바뀐 청크만** 들고 있다. 나머지는 부모에서 승계한다.
            for lower in sorted((v for v in self.versions(asset_id) if v < version),
                                reverse=True):
                q = self._vdir(asset_id, lower) / f"{key}.cbin"
                if q.is_file():
                    return q.read_bytes()
            raise VersionNotFound(f"{asset_id} v{version} 에 청크 {key} 가 없다")
        return p.read_bytes()

    def blobs(self, asset_id: str, version: int) -> Dict[str, bytes]:
        """그 판본의 **전체 세트** (승계분 포함)."""
        out: Dict[str, bytes] = {}
        for v in sorted(v for v in self.versions(asset_id) if v <= version):
            for p in sorted(self._vdir(asset_id, v).glob("*.cbin")):
                out[p.stem] = p.read_bytes()
        return out

    # ── 매니페스트 ──────────────────────────────────────────────────
    def manifest(self, asset_id: str, version: Optional[int] = None) -> ChunkManifest:
        v = self.latest(asset_id) if version is None else version
        blobs = self.blobs(asset_id, v)
        if not blobs:
            raise VersionNotFound(f"{asset_id} v{v} 이 비어 있다")
        return ChunkManifest(
            asset_id=asset_id, version=v,
            # 🔴 계약 상수를 **그대로**. 손으로 적지 않는다.
            contract=ContractInfo(**CONTRACT_CONSTANTS),
            chunks={k: self._entry(asset_id, k, blobs[k], v) for k in sorted(blobs)},
        )

    @staticmethod
    def _entry(asset_id: str, key: str, blob: bytes, version: int) -> ChunkEntry:
        from deltacontract import decode  # 지연 임포트 — 임포트 시점에 계약을 안 건다

        m = decode(blob)
        return ChunkEntry(
            uri=chunk_uri(asset_id, key, version),
            hash=hashlib.sha256(blob).hexdigest(),
            byte_length=len(blob),
            vertex_count=int(len(m.positions)),
            index_count=int(len(m.indices)),
            voxel_count=int(getattr(m, "voxel_count", 0) or 0),
            version=version,
        )

    # ── 쓰기 ────────────────────────────────────────────────────────
    def put_version(self, asset_id: str, version: int,
                    blobs: Dict[str, bytes]) -> Path:
        """편집 결과를 새 판본으로 쓴다. **바뀐 청크만** 넣는다 (나머지는 승계)."""
        with self._lock:
            d = self._dir(asset_id) / f"v{version}"
            d.mkdir(parents=True, exist_ok=True)
            for f in d.glob("*.cbin"):
                f.unlink()
            for k, b in blobs.items():
                (d / f"{k}.cbin").write_bytes(b)
            self._extra.setdefault(asset_id, {})[version] = d
            return d


STORE = AssetStore()
