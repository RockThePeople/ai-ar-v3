"""3090 판본을 `<EDIT_HOST>` 에 **수납(ingest)** 시킨다 — 결정 7.

────────────────────────────────────────────────────────────────────────
왜 필요한가
────────────────────────────────────────────────────────────────────────
`recolor` 는 3090 **로컬**이라 그 결과 판본이 상류에 아예 없다. 그 위에 형태 편집을
걸면 `409 VERSION_CONFLICT` 가 난다 (W27 실측).

W30 조사에서 우회(“v1 slat 으로 대신 제출”)가 **기하는 되지만 부기가 깨진다**는 것을
확인했다 — `to_version` 충돌이 구조적이고(상류는 언제나 `base+1`), 마스크가 겹치면
색이 통째로 사라진다. 그래서 **우회를 만들지 않고**(결정 8) 수납으로 간다.

────────────────────────────────────────────────────────────────────────
🔴 보내기 **전에** 검사한다
────────────────────────────────────────────────────────────────────────
`versionsync.check_v1_payload()` 를 진입점에 건다. 없는 채로 밀면 라우트는 201 을 주고
**워커에서** 죽어 "요청은 성공, 결과는 없음" 이 되고, 상류에는 **막다른 판본**이 남는다.
A5000 도 자기 진입점에 같은 검사를 걸었다 — **양쪽에서 막힌다.**

⚠️ recolor 판본의 slat 은 **부모 것을 그대로 쓴다.** recolor 는 slat 을 만들지 않고
   (`recolor.py` 에 언급 0건) 기하도 안 바꾼다 — 기하 바이트 동일률 89/89 = 100% 로
   확인했다. 그래서 v1 의 slat 이 v2 에 대해서도 정본이다. **새로 만들지 않는다** —
   만들면 바이트가 갈린다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Dict, Optional

from .assetstore import STORE
from .upstreamerr import parse_upstream_error, upstream_call
from .versionsync import check_v1_payload

__all__ = ["IngestFailed", "ingest_version"]


class IngestFailed(RuntimeError):
    error_code = "UPSTREAM_INGEST_FAILED"


def _client():
    import httpx

    url, key = os.environ.get("BLOCKEDIT_B_URL"), os.environ.get("BLOCKEDIT_B_KEY")
    if not url or not key:
        raise IngestFailed("상류 자격증명이 없다 (환경변수 미설정)")
    return httpx.Client(base_url=url.rstrip("/"),
                        headers={"X-Blockedit-Key": key}, timeout=600.0)


def _chunks_tar(blobs: Dict[str, bytes]) -> bytes:
    """청크를 tar.gz 로 묶는다. 이름은 `{key}.cbin` — 계약 파일명 그대로다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for key in sorted(blobs):
            data = blobs[key]
            info = tarfile.TarInfo(f"{key}.cbin")
            info.size = len(data)
            # 재현 가능하게 — mtime 을 넣으면 같은 내용이 다른 바이트가 된다.
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def ingest_version(asset_id: str, version: int, source_dir: Path,
                   progress=None) -> dict:
    """그 판본 전체를 상류에 수납시킨다.

    Args:
        source_dir: `slat.safetensors` · `input.png`(또는 `source.png`) 가 있는
            3090 쪽 자산 원본 디렉터리.

    Raises:
        RequiredFilesMissing: 재료가 부족하다 — **보내기 전에** 멈춘다.
        IngestFailed: 상류가 거부했다.
    """
    def note(p: float, stage: str, detail: str = "") -> None:
        if progress:
            progress(p, stage, detail)

    blobs = STORE.blobs(asset_id, version)
    note(0.1, "ingest", f"재료 검사 (청크 {len(blobs)})")
    # 🔴 진입점 검사. 부족하면 여기서 끝난다.
    info = check_v1_payload(source_dir, n_chunks=len(blobs))

    manifest = STORE.manifest(asset_id, version).model_dump(mode="json")
    slat = (source_dir / "slat.safetensors").read_bytes()
    img_name = info["image"]
    image = (source_dir / img_name).read_bytes()
    tar = _chunks_tar(blobs)

    note(0.4, "ingest", f"업로드 {len(tar):,}B (청크) + slat {len(slat):,}B")
    with _client() as c:
        r = upstream_call(lambda: c.post(
            f"/v2/trellis/assets/{asset_id}/ingest",
            data={"meta": json.dumps({"version": int(version)})},
            files={
                "manifest": ("manifest.json",
                             json.dumps(manifest).encode(), "application/json"),
                "slat": ("slat.safetensors", slat, "application/octet-stream"),
                "input": (img_name, image, "image/png"),
                "chunks": ("chunks.tar.gz", tar, "application/gzip"),
            }), action="판본 수납")
    if r.status_code >= 400:
        raise parse_upstream_error(r.status_code, r.text, action="판본 수납")

    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    note(0.6, "ingest", f"수납 완료 {r.status_code} · {body.get('verified') or ''}")
    return {"status": r.status_code, "version": version, "n_chunks": len(blobs),
            "tar_bytes": len(tar), "sha256_tar": hashlib.sha256(tar).hexdigest()[:16],
            "response": body}
