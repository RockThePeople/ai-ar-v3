"""앱 서버 시험 도구 — 시험용 `.cbin`, 프로세스 안 가짜 백엔드, 가짜 A5000·인페인팅 HTTP 워커.

가짜 A5000 은 아직 A5000 에 없는 엔드포인트(`/generate`·`/discard`·`DELETE`·`slat_coords`)를
내부 규약 3절 모양대로 흉내 낸다(A1·A2 병합 전 시험용).
"""

from __future__ import annotations

import io
import json
import re
import struct
import threading
import zlib
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

import numpy as np

from deltacontract.chunkbin import canonicalize, encode  # type: ignore[import-not-found]

from server.app.backends import StageError


def cbin(key: str, tag: int = 0) -> bytes:
    cx, cy, cz = (int(x) for x in key.split("_"))
    o = np.array([cx * 4 / 64 - 0.5, cy * 4 / 64 - 0.5, cz * 4 / 64 - 0.5], np.float32)
    pos = np.array([[0, 0, 0], [0.01 + tag * 1e-3, 0, 0], [0, 0.01, 0]], np.float32) + o
    col = np.array([[200, 10, 10, 255]] * 3, np.uint8)
    return encode(canonicalize((cx, cy, cz), pos, np.array([0, 1, 2], np.uint32), colors=col, voxel_count=1 + tag))


def png(w: int = 1024, h: int = 1024, v: int = 128) -> bytes:
    raw = b"".join(b"\x00" + bytes([v]) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


BASE_KEYS = ["8_8_8", "8_8_9", "9_8_8"]


class FakeBackend:
    """프로세스 안. 생성 = 청크 3개, 편집 = 8_8_8 바뀜 + 9_8_8 제거 + 10_8_8 신규."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.fail: Dict[str, str] = {}

    def _f(self, what: str) -> None:
        self.calls.append((what,))
        if what in self.fail:
            raise StageError(self.fail[what], f"fake {what} 실패")

    def health(self):
        return {"a5000": "up", "inpaint": "up"}

    def generate(self, asset_id, prompt, seed, progress):
        self._f("generate")
        progress("generating", 0.5)
        return {"blobs": {k: cbin(k) for k in BASE_KEYS}, "slat_coords": [[32, 32, 32], [33, 32, 32]]}

    def render(self, asset_id, base_version, camera, cells):
        self._f("render")
        return {"render_id": f"r-{len(self.calls)}", "render_png": png(v=10), "mask_png": png(v=255), "record": {}}

    def inpaint(self, render_png, mask_png, prompt, seed, info=None):
        if info is not None:
            info.update({"form": {"prompt": prompt, "seed": str(seed)}, "settings": {"steps": 50}, "settings_source": "fake"})
        self._f("inpaint")
        self.calls.append(("inpaint_args", prompt, seed))
        return png(v=200 + seed)

    def edit3d(self, asset_id, base_version, render, edit_png, cells, idem, progress):
        self._f("edit3d")
        return {"to_version": base_version + 1, "worker_job_id": "wj-1", "stats": {}, "raw": {"meta": {"seed": 0}, "text": "텍스트 입력 없음(이미지 조건)"},
                "blobs": {"8_8_8": cbin("8_8_8", base_version), "10_8_8": cbin("10_8_8")}, "removed": ["9_8_8"]}

    def commit(self, asset_id, to_version, idem):
        self._f("commit")

    def discard(self, asset_id, wj):
        self._f("discard")

    def delete(self, asset_id):
        self._f("delete")

    def slat_coords(self, asset_id, version):
        return [[1, 2, 3]]


# ══════════════════════════════════════════════════════════ HTTP 가짜 워커
def _multipart(headers, body: bytes) -> Dict[str, bytes]:
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in msg.iter_parts()}


class FakeWorkers:
    """A5000(`/v2/trellis/*`, `/v2/assets/*`) + 인페인팅(`/release` `/reload` `/inpaint` `/jobs`) 를 한 포트에서."""

    def __init__(self) -> None:
        self.log: List[str] = []
        self.assets: Dict[str, Dict[int, Dict[str, bytes]]] = {}
        self.staging: Dict[str, dict] = {}
        self.gpu_loaded = True
        fw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, raw=None, ctype="application/json"):
                b = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                p = self.path.split("?")[0]
                fw.log.append(f"GET {p}")
                if p in ("/health", "/v2/trellis/health"):
                    return self._send(200, {"ok": True})
                if p.startswith("/jobs/") and p.endswith("/2d_edit.png"):
                    return self._send(200, raw=png(v=222), ctype="image/png")
                m = re.fullmatch(r"/v2/trellis/renders/([\w-]+)/(2d_render|2d_mask)\.png", p)
                if m:
                    return self._send(200, raw=png(v=1 if m.group(2) == "2d_render" else 255), ctype="image/png")
                m = re.fullmatch(r"/v2/trellis/jobs/([\w-]+)", p)
                if m:
                    return self._send(200, fw.staging[m.group(1)]["resp"])
                m = re.fullmatch(r"/v2/assets/([\w-]+)/staging/([\w-]+)/chunks/([\d_]+)\.cbin", p)
                if m:
                    return self._send(200, raw=fw.staging[m.group(2)]["blobs"][m.group(3)], ctype="application/octet-stream")
                m = re.fullmatch(r"/v2/trellis/assets/([\w-]+)/chunks/([\d_]+)\.v(\d+)\.cbin", p)
                if m:
                    return self._send(200, raw=fw.assets[m.group(1)][int(m.group(3))][m.group(2)],
                                      ctype="application/octet-stream")
                m = re.fullmatch(r"/v2/trellis/assets/([\w-]+)/v(\d+)/slat_coords\.json", p)
                if m:
                    return self._send(200, {"coords": [[5, 5, 5], [6, 5, 5]]})
                m = re.fullmatch(r"/v2/trellis/assets/([\w-]+)/v(\d+)/manifest\.json", p)
                if m:
                    a, n = m.group(1), int(m.group(2))
                    return self._send(200, {"asset_id": a, "version": n, "chunks": {
                        k: {"uri": f"/v2/assets/{a}/chunks/{k}.v{n}.cbin"} for k in fw.assets[a][n]}})
                return self._send(404, {"error_code": "NOT_FOUND", "message": p})

            def do_POST(self):
                p = self.path.split("?")[0]
                fw.log.append(f"POST {p}")
                b = self._body()
                if p == "/release":
                    fw.gpu_loaded = False
                    return self._send(200, {"gpu_loaded": False, "vram_mib": 300})
                if p == "/reload":
                    fw.gpu_loaded = True
                    return self._send(200, {"gpu_loaded": True, "load_s": 4.0})
                if p == "/inpaint":
                    f = _multipart(self.headers, b)
                    fw.log.append(f"inpaint prompt={f['prompt'].decode()} seed={f['seed'].decode()}")
                    return self._send(200, {"job_id": "ij1", "state": "done"})
                if p == "/v2/trellis/render":
                    req = json.loads(b)
                    fw.log.append(f"render camera={'camera' in req} view={'view' in req}")
                    resp = {"render_id": "r-fake1", "asset_id": req["asset_id"], "base_version": req["base_version"],
                            "view": None if "camera" in req else 3, "render_sha256": "a" * 64, "mask_sha256": "b" * 64}
                    if "camera" in req:     # A1: camera 렌더는 view 가 null 이고 camera_used 를 준다
                        resp["camera_used"] = {"position": [0, -1.8, 0], "fov_deg": 49.1}
                    return self._send(200, resp)
                if p == "/v2/trellis/generate":
                    f = _multipart(self.headers, b)
                    meta = json.loads(f["meta"])
                    a = meta["asset_id"]
                    fw.assets[a] = {1: {k: cbin(k) for k in BASE_KEYS}}
                    jid = "gj-1"
                    fw.staging[jid] = {"resp": {"asset_id": a, "to_version": 1, "is_initial": True,
                                                "chunks": [{"chunk_id": k} for k in BASE_KEYS]}, "blobs": {}}
                    return self._send(202, {"job_id": jid})
                if p == "/v2/trellis/edit":
                    f = _multipart(self.headers, b)
                    meta = json.loads(f["meta"])
                    assert {"2d_render", "2d_mask", "2d_edit"} <= set(f), f.keys()
                    jid = f"ej-{len(fw.staging)}"
                    to = meta["base_version"] + 1
                    fw.staging[jid] = {"resp": {"asset_id": meta["asset_id"], "to_version": to,
                                                "chunks": [{"chunk_id": "8_8_8"}], "removed_chunk_ids": ["9_8_8"]},
                                       "blobs": {"8_8_8": cbin("8_8_8", to)}}
                    return self._send(202, {"job_id": jid})
                m = re.fullmatch(r"/v2/trellis/assets/([\w-]+)/(commit|discard)", p)
                if m:
                    return self._send(200, {"ok": True})
                return self._send(404, {"error_code": "NOT_FOUND", "message": p})

            def do_DELETE(self):
                fw.log.append(f"DELETE {self.path}")
                self.send_response(204)
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
