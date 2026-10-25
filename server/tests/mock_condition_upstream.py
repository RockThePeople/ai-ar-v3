"""**테스트 전용** 모의 서버 둘 — 계약 3.28.0 조건 이미지 경로(Voxhammer-light R9 R-7).

A5000 의 R8a 워커(`/v2/trellis/render` · multipart `/v2/trellis/edit`)가 아직 없어서, 규약
(Voxhammer-light `services/common/http-contract.md` §3)대로 응답하는 모의 서버로 오케스트레이터를
개발·시험한다. 실제 종단 시험은 A5000 워커가 뜬 뒤에 한다 — 이 모의 서버가 통과한다는 것은
**3090 쪽이 규약을 지킨다**는 뜻이지 A5000 과 맞물린다는 뜻이 아니다.

- `MockA5000`: 조건 렌더 · 렌더 파일 · 조건 이미지 편집(multipart, §3.3 거절 사유 전부) · 잡 · staging 청크
- `MockInpaint`: 3090 R9 워커(§2)의 `/inpaint?wait=1` · `/jobs/<id>/2d_edit.png`

GPU·PIL·네트워크 밖 접근이 없다(127.0.0.1, 임의 포트). PNG 는 표준 라이브러리로 만든다.

손으로 띄워 보기:  python -m server.tests.mock_condition_upstream   (포트를 출력한다)
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
import uuid
import zlib
from email.parser import BytesParser
from email.policy import default as _policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

SIZE = 1024
KEY = "test-key"


def png(rgb: tuple, size: int = SIZE, box: Optional[tuple] = None, box_rgb: tuple = (255, 255, 255)) -> bytes:
    """단색(+선택 사각형) RGB PNG. PIL 없이 만든다."""
    row = bytes(rgb) * size
    rows = []
    for y in range(size):
        if box and box[1] <= y < box[3]:
            r = bytearray(row)
            r[box[0] * 3:box[2] * 3] = bytes(box_rgb) * (box[2] - box[0])
            rows.append(b"\x00" + bytes(r))
        else:
            rows.append(b"\x00" + row)
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def png_size(b: bytes) -> Optional[tuple]:
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def parse_multipart(ctype: str, body: bytes) -> Dict[str, bytes]:
    msg = BytesParser(policy=_policy).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = part.get_payload(decode=True)
    return out


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):          # 테스트 출력을 더럽히지 않는다
        pass

    @property
    def S(self):
        return self.server.owner

    def _send(self, code: int, body, ctype="application/json"):
        b = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _err(self, code: int, error_code: str, message: str, detail: Optional[dict] = None):
        body = {"error_code": error_code, "message": message}
        if detail:
            body["detail"] = detail
        self._send(code, body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))


# ══════════════════════════════════════════════════════════ A5000 (R8a 규약)
class MockA5000(_Server):
    """조절 손잡이:
        forget_renders: int    ③ 이 이만큼 RENDER_UNKNOWN 을 준다(기록을 지운 것처럼)
        change_render_on_retry 다시 렌더하면 다른 바이트를 낸다(비결정 렌더)
        mismatch: bool         ③ 이 RENDER_MISMATCH 를 준다
        render_base_version_offset  렌더 응답의 base_version 을 어긋나게 한다
    관찰:
        calls: [(method, path)] · edits: [{"meta", "parts"}] · renders: {render_id: rec}
    """

    def __init__(self, chunk_blobs: Dict[str, bytes], latest_version: int = 1):
        super().__init__(_A5000Handler)
        self.chunk_blobs = chunk_blobs
        self.latest_version = latest_version
        self.forget_renders = 0
        self.change_render_on_retry = False
        self.mismatch = False
        self.render_base_version_offset = 0
        self.calls: List[tuple] = []
        self.edits: List[dict] = []
        self.renders: Dict[str, dict] = {}
        self.files: Dict[str, Dict[str, bytes]] = {}
        self.jobs: Dict[str, dict] = {}
        self._n_render = 0


class _A5000Handler(_H):
    def _auth(self) -> bool:
        if self.headers.get("X-Blockedit-Key") != KEY:
            self._err(401, "UNAUTHORIZED", "키가 없다")
            return False
        return True

    def do_GET(self):
        S = self.S
        S.calls.append(("GET", self.path))
        if not self._auth():
            return
        m = re.fullmatch(r"/v2/trellis/renders/([\w-]+)/(2d_render|2d_mask)\.png", self.path)
        if m:
            f = S.files.get(m.group(1), {}).get(m.group(2))
            return self._send(200, f, "image/png") if f else self._err(404, "NOT_FOUND", self.path)
        m = re.fullmatch(r"/v2/trellis/jobs/([\w-]+)", self.path)
        if m and m.group(1) in S.jobs:
            return self._send(200, S.jobs[m.group(1)])
        m = re.fullmatch(r"/v2/assets/([\w-]+)/staging/([\w-]+)/chunks/([\w]+)\.cbin", self.path)
        if m and m.group(3) in S.chunk_blobs:
            return self._send(200, S.chunk_blobs[m.group(3)], "application/octet-stream")
        self._err(404, "NOT_FOUND", self.path)

    def do_POST(self):
        S = self.S
        S.calls.append(("POST", self.path))
        if not self._auth():
            return
        if self.path == "/v2/trellis/render":
            return self._render(json.loads(self._body()))
        if self.path == "/v2/trellis/edit":
            return self._edit()
        self._err(404, "NOT_FOUND", self.path)

    def _render(self, req: dict):
        S = self.S
        for k in ("asset_id", "base_version", "mask"):
            if k not in req:
                return self._err(422, "INVALID_REQUEST", f"{k} 가 없다")
        view = int(req.get("view", 0))
        if not 0 <= view <= 4:
            return self._err(422, "INVALID_REQUEST", "view 는 0–4")
        if int(req["base_version"]) != S.latest_version:
            return self._err(409, "VERSION_CONFLICT", "최신이 아니다",
                             {"latest_version": str(S.latest_version)})
        S._n_render += 1
        shade = 100 + view * 10 + (S._n_render if S.change_render_on_retry else 0)
        render = png((shade, shade, shade), box=(400, 300, 600, 500), box_rgb=(30, 30, 30))
        mask = png((0, 0, 0), box=(380, 280, 620, 520))
        rid = uuid.uuid4().hex
        S.files[rid] = {"2d_render": render, "2d_mask": mask}
        rec = {"render_id": rid, "asset_id": req["asset_id"],
               "base_version": int(req["base_version"]) + S.render_base_version_offset,
               "view": view, "render_sha256": sha(render), "mask_sha256": sha(mask),
               "renderer": {"name": "mock", "resolution": SIZE}}
        S.renders[rid] = rec
        self._send(200, rec)

    def _edit(self):
        S = self.S
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            # 옛 JSON 경로(조건 없음). §4 결정 2 — 바로 끊는다.
            return self._err(422, "CONDITION_IMAGES_REQUIRED", "조건 이미지가 없다",
                             {"missing": "2d_render,2d_mask,2d_edit"})
        parts = parse_multipart(ctype, self._body())
        meta = json.loads(parts.get("meta") or b"{}")
        S.edits.append({"meta": meta, "parts": {k: v for k, v in parts.items() if k != "meta"}})
        missing = [n for n in ("2d_render", "2d_mask", "2d_edit") if not parts.get(n)]
        if missing:
            return self._err(422, "CONDITION_IMAGES_REQUIRED", "조건 이미지가 빠졌다",
                             {"missing": ",".join(missing)})
        for n in ("2d_render", "2d_mask", "2d_edit"):
            if png_size(parts[n]) != (SIZE, SIZE):
                return self._err(400, "BAD_CONDITION_IMAGE", f"{n} 가 PNG {SIZE}² 가 아니다")
        rid = meta.get("render_id")
        if S.forget_renders > 0:
            S.forget_renders -= 1
            S.renders.pop(rid, None)
        rec = S.renders.get(rid)
        if rec is None:
            return self._err(404, "RENDER_UNKNOWN", f"render_id 기록이 없다: {rid}")
        got = {"render_sha256": sha(parts["2d_render"]), "mask_sha256": sha(parts["2d_mask"])}
        for k, v in got.items():
            if S.mismatch or rec[k] != v or meta.get(k) != v:
                return self._err(409, "RENDER_MISMATCH", "렌더 해시가 기록과 다르다",
                                 {"expected": rec[k], "received": v})
        if int(meta.get("base_version", -1)) != rec["base_version"]:
            return self._err(409, "VERSION_CONFLICT", "렌더 때와 판본이 다르다",
                             {"latest_version": str(rec["base_version"])})
        jid = uuid.uuid4().hex
        S.jobs[jid] = {"job_id": jid, "to_version": S.latest_version + 1, "removed_chunk_ids": [],
                       "chunks": [{"chunk_id": k, "hash": sha(b)} for k, b in S.chunk_blobs.items()]}
        self._send(202, {"job_id": jid, "state": "queued"})


# ══════════════════════════════════════════════════════════ 3090 R9 워커 (§2)
class MockInpaint(_Server):
    """관찰: requests: [{"prompt", "seed", "image_sha256", "mask_sha256"}]. 조절: fail: bool"""

    def __init__(self):
        super().__init__(_InpaintHandler)
        self.requests: List[dict] = []
        self.out: Dict[str, bytes] = {}
        self.fail = False


class _InpaintHandler(_H):
    def do_POST(self):
        S = self.S
        u = urlparse(self.path)
        if u.path != "/inpaint":
            return self._err(404, "NOT_FOUND", self.path)
        parts = parse_multipart(self.headers.get("Content-Type", ""), self._body())
        for k in ("image", "mask", "prompt"):
            if not parts.get(k):
                return self._err(400, "MISSING_FIELD", f"필드가 없다: {k}")
        req = {"prompt": parts["prompt"].decode(), "seed": int(parts.get("seed") or b"0"),
               "image_sha256": sha(parts["image"]), "mask_sha256": sha(parts["mask"]),
               "wait": parse_qs(u.query).get("wait") == ["1"]}
        S.requests.append(req)
        jid = uuid.uuid4().hex
        if S.fail:
            return self._send(500, {"job_id": jid, "state": "failed",
                                    "error": {"error_code": "INPAINT_FAILED", "message": "모의 실패"}})
        h = int(sha(parts["image"] + parts["prompt"])[:2], 16)
        edit = png((h, 0, 255 - h), box=(400, 300, 600, 500), box_rgb=(200, 50, 50))
        S.out[jid] = edit
        self._send(200, {"job_id": jid, "state": "done",
                         "result": {"sha256": {"2d_edit": sha(edit)},
                                    "spans": [{"name": "inpaint", "seconds": 0.01}]}})

    def do_GET(self):
        m = re.fullmatch(r"/jobs/([0-9a-f]{32})/2d_edit\.png", self.path)
        if m and m.group(1) in self.S.out:
            return self._send(200, self.S.out[m.group(1)], "image/png")
        self._err(404, "NOT_FOUND", self.path)


def moto_b_chunks(n: int = 3) -> Dict[str, bytes]:
    """리포 자산 moto-b 의 실제 .cbin 몇 개 — `verify_blobs` 를 통과하는 진짜 바이트."""
    root = Path(__file__).resolve().parents[2] / "assets" / "moto-b" / "parent"
    return {p.stem: p.read_bytes() for p in sorted(root.glob("*.cbin"))[:n]}


if __name__ == "__main__":       # pragma: no cover — 손으로 띄워 보기
    import time

    with MockA5000(moto_b_chunks()) as a, MockInpaint() as w:
        print(f"MockA5000 {a.url}  (X-Blockedit-Key: {KEY})\nMockInpaint {w.url}")
        while True:
            time.sleep(3600)
