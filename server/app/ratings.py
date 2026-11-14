"""S21 체인 평가 서비스(계약 11절) — 평가 저장·피드백 파일·UserOperation 준비/제출/감시·가짜 번들러·에이전트 등록·미세 조정.

상태: awaiting_signature → submitted → confirmed | failed, 서명 전 만료 → expired, 체인 없음 → off.
prepare 는 awaiting_signature·expired·failed 에서만(S21 5절: failed 도 — 새 nonce·가스, 같은 rating_id), 그 밖은 409.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import traceback
from typing import Optional

from . import chaincrypto as cc
from .chain import (AGENT_REGISTRY, CHAIN_ID, ENTRY_POINT, IDENTITY, REPUTATION, USEROP_TTL_S, ChainClient, ChainError,
                    explorer_tx, now_iso)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS agent_tuning (id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER, target TEXT,
  action TEXT, idx INTEGER, before TEXT, after TEXT, rating_id TEXT, t REAL);
"""
ACTIVE_WATCH = ("submitted",)
WATCH_MAX_S = 300.0
MOCK_CONFIRM_S = 5.0
TUNE_MAX_LINES = 8
TUNE_MAX_CHARS = 200
REG_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"


class RatingError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Ratings:
    def __init__(self, eng, start_thread: bool = True, client: Optional[ChainClient] = None) -> None:
        from .config import chain_env
        self.eng, self.s, self.db = eng, eng.s, eng.db
        with self.db.lock:
            self.db.c.executescript(SCHEMA)
        self.mock = self.s.mock
        self.client = client or ChainClient(chain_env())
        if self.s.chain and not self.mock:
            self.client.check()
        elif self.mock:
            self.client.status, self.client.error = "mock", None
        else:
            self.client.status, self.client.error = "off", "APP_CHAIN=0"
        self.lock = threading.RLock()
        self.reg: dict = {"state": "idle"}                     # /admin 등록 진행 상태
        self._apply_notes()
        if start_thread:
            threading.Thread(target=self._watch_loop, daemon=True, name="chain-watch").start()

    # ── 상태 저장 ─────────────────────────────────────────────────
    def kv(self, k: str, default=None):
        r = self.db.q("SELECT v FROM chain_state WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def set_kv(self, k: str, v) -> None:
        self.db.x("INSERT OR REPLACE INTO chain_state VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))

    def agent_id(self) -> Optional[int]:
        if self.mock:
            return int(self.kv("agent_id", 1))
        a = self.kv("agent_id")
        if a is None and os.environ.get("CHAIN_AGENT_ID"):          # 8090 에서 등록한 값을 8083 에 넘길 때(app.env)
            a = os.environ["CHAIN_AGENT_ID"]
        return int(a) if a is not None else None

    def on(self) -> bool:
        return self.client.status in ("on", "mock")

    def health(self) -> dict:
        return {"chain_id": CHAIN_ID, "entry_point": ENTRY_POINT, "factory": self.client.factory or None,
                "identity_registry": IDENTITY, "reputation_registry": REPUTATION, "agent_id": self.agent_id(),
                "bundler_url": (self.client.bundler_url if not self.mock else "/mock-bundler") or None,
                "status": self.client.status, **({"error": self.client.error} if self.client.error else {})}

    # ── 공개 주소 ─────────────────────────────────────────────────
    def public_base(self, req_base: str) -> str:
        h, p = os.environ.get("PUBLIC_HOST"), os.environ.get("PUBLIC_PORT")
        if h and not self.mock:
            return f"http://{h}:{p}" if p else f"http://{h}"
        return req_base.rstrip("/")

    # ── 평가 저장 ─────────────────────────────────────────────────
    def _body(self, rid: str) -> dict:
        r = self.db.q("SELECT body FROM ratings WHERE rating_id=?", (rid,))
        if not r:
            raise RatingError(404, "RATING_NOT_FOUND", f"평가 없음: {rid}")
        return json.loads(r[0]["body"])

    def _save(self, o: dict) -> None:
        self.db.x("UPDATE ratings SET body=? WHERE rating_id=?", (json.dumps(o, ensure_ascii=False), o["rating_id"]))

    def _set_field(self, rid: str, key: str, value) -> None:
        """다른 스레드(조정·감시)가 늦게 쓰는 칸만 새로 읽은 본문에 넣는다 — 오래된 사본으로 chain 상태를 덮지 않게."""
        with self.lock:
            o = self._body(rid)
            o[key] = value
            self._save(o)

    def _set_chain(self, rid: str, ch: dict, expect: str) -> bool:
        """chain 이 아직 expect 상태일 때만 바꾼다(감시 스레드용). 바뀌었으면 False."""
        with self.lock:
            o = self._body(rid)
            if (o.get("chain") or {}).get("status") != expect:
                return False
            o["chain"] = ch
            self._save(o)
            return True

    def _feedback_file(self, o: dict, account: Optional[str]) -> bytes:
        """ERC-8004 필수 칸 + 서비스 칸(계약 11.4) + 1.0.x 칸(호환). sort_keys 로 바이트 고정."""
        aid = o["asset_id"]
        hist = None
        try:
            from .history import asset_history
            hist = asset_history(self.eng, self.db, aid)
        except Exception:                                  # noqa: BLE001
            hist = None
        hraw = json.dumps(hist, ensure_ascii=False, sort_keys=True).encode() if hist is not None else b""
        lo, hi = o["version_range"]
        job_ids = [r["job_id"] for r in self.db.q(
            "SELECT job_id FROM commits WHERE asset_id=? AND version>=? AND version<=? ORDER BY version", (aid, lo, hi))]
        fb = {"agentRegistry": AGENT_REGISTRY, "agentId": self.agent_id(),
              "clientAddress": f"eip155:{CHAIN_ID}:{account}" if account else None,
              "createdAt": now_iso(o["created_at"]), "value": o["value"], "valueDecimals": 0,
              "tag1": "starred", "tag2": "", "endpoint": "/app/v1/edits",
              "stars": o["stars"], "review": o.get("review") or "", "asset_id": aid, "version_range": o["version_range"],
              "job_ids": job_ids,
              "agent_trace": {"history_uri": f"/app/v1/assets/{aid}/history",
                              "history_keccak": "0x" + cc.keccak256(hraw).hex() if hraw else None},
              # 1.0.x 칸(호환)
              "agent": "vhl-edit-service", "client": o["device"], "created_at": o["created_at"]}
        return json.dumps(fb, ensure_ascii=False, sort_keys=True).encode()

    def _write_file(self, o: dict, account: Optional[str]) -> None:
        raw = self._feedback_file(o, account)
        p = self.s.data_dir / "ratings" / f"{o['rating_id']}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        o["feedback_hash"] = hashlib.sha256(raw).hexdigest()
        o["feedback_keccak"] = "0x" + cc.keccak256(raw).hex()

    def create(self, asset_id: str, device: str, stars: int, review: Optional[str], signer_pubkey: Optional[str],
               req_base: str) -> dict:
        from .engine import AppError
        eng = self.eng
        with eng.lock:
            a = self.db.live_asset(asset_id)
            if a is None or (a["version"] == 0 and not a["is_sample"]):
                raise AppError(404, "ASSET_NOT_FOUND", f"자산 없음: {asset_id}")
            if a["is_sample"]:
                raise AppError(403, "READ_ONLY", "샘플 자산은 평가하지 않는다")
            rng = eng.rate_range(asset_id, device)
            if rng is None:
                raise AppError(409, "NOTHING_TO_RATE", "직전 평가 이후 커밋된 편집이 없다")
            rid = f"rt-{secrets.token_hex(6)}"
            o = {"rating_id": rid, "asset_id": asset_id, "device": device, "stars": stars, "value": stars * 20,
                 "decimals": 0, "tag1": "starred", "version_range": rng, "review": (review or "")[:1000],
                 "feedback_uri": f"/app/v1/ratings/{rid}.json", "created_at": eng.clock(),
                 "public_base": self.public_base(req_base), "signer_pubkey": signer_pubkey,
                 "tuning_version": self.tuning_version()}
            self.db.x("INSERT INTO ratings(rating_id,asset_id,device,stars,v_from,v_to,created_at,body) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, asset_id, device, stars, rng[0], rng[1], o["created_at"], "{}"))
        o["chain"] = self._prepare_chain(o, signer_pubkey)
        self._save(o)
        if o.get("review") and self.s.agent_tune:
            threading.Thread(target=self._tune_safe, args=(rid,), daemon=True, name="tune").start()
        return self.view(o)

    def _prepare_chain(self, o: dict, signer_pubkey: Optional[str]) -> dict:
        """체인 기록 준비. 공개키 없음·형식 오류·체인 꺼짐·에이전트 미등록 → off(저장만)."""
        if not signer_pubkey:
            self._write_file(o, None)
            return {"status": "off"}
        try:
            x, y = cc.parse_pubkey(signer_pubkey)
        except ValueError as e:
            self._write_file(o, None)
            return {"status": "off", "error": str(e)}
        if not self.on():
            self._write_file(o, None)
            return {"status": "off", "error": self.client.error or "체인 꺼짐"}
        if self.agent_id() is None:
            self._write_file(o, None)
            return {"status": "off", "error": "에이전트 미등록(/admin 에서 Register)"}
        if self.mock:
            return self._mock_chain(o, x, y)
        account = self.client.account_for(x, y)
        self._write_file(o, account)
        uri = f"{o['public_base']}{o['feedback_uri']}"
        try:
            cd = self.client.feedback_call(self.agent_id(), o["value"], uri, o["feedback_keccak"])
            b = self.client.build_user_op(x, y, cd)
        except ChainError as e:
            return {"status": "failed", "error": f"user_op 준비 실패: {e}", "account": account}
        return {"status": "awaiting_signature", "chain_id": CHAIN_ID, "entry_point": ENTRY_POINT,
                "account": b["account"], "agent_id": self.agent_id(), "bundler_url": self.client.bundler_url,
                "user_op": b["user_op"], "user_op_hash": b["user_op_hash"],
                "expires_at": round(self.eng.clock() + USEROP_TTL_S, 1), "feedback_uri_onchain": uri}

    def _mock_chain(self, o: dict, x: int, y: int) -> dict:
        """가짜 모드(계약 7절): 결정적 가짜 계정, 무작위 user_op_hash, 번들러 = 이 서버 /mock-bundler."""
        account = cc.to_checksum("0x" + cc.keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big")).hex()[-40:])
        self._write_file(o, account)
        h = "0x" + secrets.token_hex(32)
        op = {"sender": account, "nonce": "0x0", "callData": "0x", "callGasLimit": "0x30000",
              "verificationGasLimit": "0x60000", "preVerificationGas": "0xc000", "maxFeePerGas": "0x3b9aca00",
              "maxPriorityFeePerGas": "0x3b9aca00", "paymaster": "0x" + "77" * 20, "paymasterVerificationGasLimit": "0x8000",
              "paymasterPostOpGasLimit": "0x1", "paymasterData": "0x", "signature": "0x"}
        return {"status": "awaiting_signature", "chain_id": CHAIN_ID, "entry_point": ENTRY_POINT, "account": account,
                "agent_id": self.agent_id(), "bundler_url": f"{o['public_base']}/mock-bundler", "user_op": op,
                "user_op_hash": h, "expires_at": round(self.eng.clock() + USEROP_TTL_S, 1), "mock": True}

    # ── 응답 모양 ─────────────────────────────────────────────────
    def view(self, o: dict) -> dict:
        ch = dict(o.get("chain") or {"status": "off"})
        for k in ("feedback_uri_onchain", "mock", "submitted_at", "gas_used", "confirm_s", "onchain_read", "last_error",
                  "bundler_hash", "submitted_by"):
            ch.pop(k, None)
        return {k: o.get(k) for k in ("rating_id", "asset_id", "stars", "value", "decimals", "tag1", "version_range",
                                      "review", "feedback_uri", "feedback_hash", "feedback_keccak")} | {"chain": ch}

    def _expire(self, o: dict) -> bool:
        ch = o.get("chain") or {}
        if ch.get("status") == "awaiting_signature" and self.eng.clock() > float(ch.get("expires_at") or 0):
            ch["status"] = "expired"
            self._set_chain(o["rating_id"], ch, "awaiting_signature")
            return True
        return False

    def get(self, rid: str) -> dict:
        o = self._body(rid)
        self._expire(o)
        return self.view(o)

    def prepare(self, rid: str, signer_pubkey: Optional[str]) -> dict:
        """expired·awaiting_signature·failed 에서만(S21 5절). 새 nonce·가스·expires. 공개키가 오면 새 계정·피드백 파일."""
        with self.lock:
            o = self._body(rid)
            self._expire(o)
            st = (o.get("chain") or {}).get("status")
            if st not in ("awaiting_signature", "expired", "failed"):
                raise RatingError(409, "WRONG_STATE", f"지금 상태: {st}")
            pk = signer_pubkey or o.get("signer_pubkey")
            if signer_pubkey:
                o["signer_pubkey"] = signer_pubkey
            o["chain"] = self._prepare_chain(o, pk)
            o.setdefault("prepares", 0)
            o["prepares"] += 1
            self._save(o)
            return self.view(o)

    def submitted(self, rid: str, user_op_hash: Optional[str], signature: Optional[str]) -> dict:
        with self.lock:
            o = self._body(rid)
            self._expire(o)
            ch = o.get("chain") or {}
            if ch.get("status") != "awaiting_signature":
                raise RatingError(409, "WRONG_STATE", f"지금 상태: {ch.get('status')}")
            if signature:
                ok = self._verify(o, signature)
                if self.mock:
                    if not ok:
                        ch.update(status="failed", error="signature invalid")
                        self._save(o)
                        return self.view(o)
                else:
                    op = dict(ch["user_op"], signature=signature)
                    try:
                        h = self.client.send_user_op(op)
                    except ChainError as e:
                        ch.update(status="failed", error=f"번들러 제출 실패: {e}")
                        self._save(o)
                        return self.view(o)
                    ch["user_op"] = op
                    if h and h.lower() != ch["user_op_hash"].lower():
                        ch["bundler_hash"] = h
                ch["submitted_by"] = "server"
            else:
                if (user_op_hash or "").lower() != (ch.get("user_op_hash") or "").lower():
                    raise RatingError(400, "INVALID_REQUEST", "user_op_hash 가 이 평가의 값과 다르다")
                ch["submitted_by"] = "device"
            ch["status"] = "submitted"
            ch["submitted_at"] = self.eng.clock()
            self._save(o)
            return self.view(o)

    def _verify(self, o: dict, signature: str) -> bool:
        try:
            x, y = cc.parse_pubkey(o.get("signer_pubkey") or "")
        except ValueError:
            return False
        h = bytes.fromhex(o["chain"]["user_op_hash"].removeprefix("0x"))
        return cc.verify_sha256(h, signature, x, y)

    # ── 가짜 번들러(JSON-RPC) ──────────────────────────────────────
    def mock_bundler(self, req: dict) -> dict:
        rid_, method, params = req.get("id"), req.get("method"), req.get("params") or []
        def err(code, msg):
            return {"jsonrpc": "2.0", "id": rid_, "error": {"code": code, "message": msg}}
        if method == "eth_chainId":
            return {"jsonrpc": "2.0", "id": rid_, "result": hex(CHAIN_ID)}
        if method == "eth_supportedEntryPoints":
            return {"jsonrpc": "2.0", "id": rid_, "result": [ENTRY_POINT]}
        if method != "eth_sendUserOperation" or not params:
            return err(-32601, f"method not supported: {method}")
        op = params[0]
        rows = self.db.q("SELECT body FROM ratings ORDER BY created_at DESC LIMIT 200")
        for r in rows:
            o = json.loads(r["body"] or "{}")
            ch = o.get("chain") or {}
            if ch.get("status") == "awaiting_signature" and (ch.get("account") or "").lower() == (op.get("sender") or "").lower():
                if not self._verify(o, op.get("signature") or ""):
                    return err(-32507, "AA24 signature error")
                return {"jsonrpc": "2.0", "id": rid_, "result": ch["user_op_hash"]}
        return err(-32602, "unknown user operation(평가 대기 중인 계정이 없다)")

    # ── 감시 ─────────────────────────────────────────────────────
    def _watch_loop(self) -> None:
        while True:
            time.sleep(3)
            try:
                self.watch_once()
            except Exception:                              # noqa: BLE001
                traceback.print_exc()

    def watch_once(self) -> None:
        rows = self.db.q("SELECT rating_id, body FROM ratings ORDER BY created_at DESC LIMIT 200")
        for r in rows:
            try:
                o = json.loads(r["body"] or "{}")
            except ValueError:
                continue
            ch = o.get("chain") or {}
            if ch.get("status") == "awaiting_signature":
                self._expire(o)
            if ch.get("status") != "submitted":
                continue
            age = self.eng.clock() - float(ch.get("submitted_at") or 0)
            if self.mock:
                if age >= MOCK_CONFIRM_S:
                    n = int(self.kv("mock_feedback_index", 0)) + 1
                    self.set_kv("mock_feedback_index", n)
                    tx = "0x" + secrets.token_hex(32)
                    ch.update(status="confirmed", tx_hash=tx, block_number=9000000 + n, feedback_index=n,
                              explorer_url=explorer_tx(tx))
                    self._set_chain(o["rating_id"], ch, "submitted")
                continue
            h = ch.get("bundler_hash") or ch.get("user_op_hash")
            try:
                rec = self.client.receipt(h)
            except ChainError as e:
                ch["last_error"] = str(e)
                self._set_chain(o["rating_id"], ch, "submitted")
                continue
            if rec:
                tr = rec.get("receipt") or {}
                if rec.get("success"):
                    ch.update(status="confirmed", tx_hash=tr.get("transactionHash"),
                              block_number=int(tr.get("blockNumber") or "0x0", 16),
                              explorer_url=explorer_tx(tr.get("transactionHash")),
                              gas_used=int(rec.get("actualGasUsed") or "0x0", 16),
                              confirm_s=round(age, 1))
                    try:
                        ch["feedback_index"] = self.client.last_index(int(ch["agent_id"]), ch["account"])
                        rd = self.client.read_feedback(int(ch["agent_id"]), ch["account"], ch["feedback_index"])
                        ch["onchain_read"] = rd | {"match": rd["value"] == o["value"] and rd["tag1"] == "starred"}
                    except ChainError as e:
                        ch.setdefault("feedback_index", None)
                        ch["onchain_read"] = {"error": str(e)[:200]}
                else:
                    ch.update(status="failed", error=f"실행 실패: {rec.get('reason') or 'reverted'}",
                              tx_hash=tr.get("transactionHash"))
                self._set_chain(o["rating_id"], ch, "submitted")
            elif age > WATCH_MAX_S:
                ch.update(status="failed", error="5분 안에 영수증 없음")
                self._set_chain(o["rating_id"], ch, "submitted")

    # ── 에이전트 등록(/admin) ─────────────────────────────────────
    def registration(self, req_base: str) -> dict:
        base = self.public_base(req_base)
        hist = self.kv("agent_history", [])
        regs = [{"agentId": self.agent_id(), "agentRegistry": AGENT_REGISTRY}] if self.agent_id() is not None else []
        return {"type": REG_TYPE, "name": "VoxHammer-Light 3D Edit Agent",
                "description": ("AR 3D asset edit agent. One instruction plus a lasso volume: an orchestrator (Claude) "
                                "refines the volume, writes the image prompt, edits the best-visible view with Gemini, "
                                "grades the result, retries, then lifts it to 3D with TRELLIS and commits."),
                "image": None, "services": [{"name": "web", "endpoint": f"{base}/app/v1"}],
                "registrations": regs, "supportedTrust": ["reputation"], "active": True,
                "tools": ["auto_volume_mask", "prompt_refine", "flux_inpaint", "trellis_edit"],
                "previous_agent_ids": [h["agent_id"] for h in hist if h.get("agent_id") != self.agent_id()]}

    def start_register(self, req_base: str, again: bool = False) -> dict:
        with self.lock:
            if self.reg.get("state") in ("sending", "pending"):
                return self.reg
            if self.agent_id() is not None and not again:
                return {"state": "done", "agent_id": self.agent_id()}
            if self.mock or not self.on():
                self.reg = {"state": "error", "error": self.client.error or "가짜 모드에서는 등록하지 않는다"}
                return self.reg
            uri = f"{self.public_base(req_base)}/app/v1/agent-registration.json"
            self.reg = {"state": "sending", "agent_uri": uri, "t0": time.time()}
        threading.Thread(target=self._register_run, args=(uri,), daemon=True, name="register").start()
        return self.reg

    def _register_run(self, uri: str) -> None:
        try:
            tx = self.client.send_register(uri)
            self.reg.update(state="pending", tx_hash=tx, explorer_url=explorer_tx(tx))
            for _ in range(120):
                time.sleep(2)
                rec = self.client.tx_receipt(tx)
                if rec:
                    if int(rec.get("status") or "0x0", 16) != 1:
                        raise ChainError("register tx 실패(status 0)")
                    aid = ChainClient.registered_agent_id(rec)
                    if aid is None:
                        raise ChainError("Registered 이벤트에서 agentId 를 찾지 못했다")
                    hist = self.kv("agent_history", [])
                    hist.append({"agent_id": aid, "tx_hash": tx, "t": time.time(), "block": int(rec["blockNumber"], 16)})
                    self.set_kv("agent_history", hist)
                    self.set_kv("agent_id", aid)
                    self.reg.update(state="done", agent_id=aid, block_number=int(rec["blockNumber"], 16),
                                    seconds=round(time.time() - self.reg["t0"], 1),
                                    token_url=f"https://sepolia.etherscan.io/token/{IDENTITY}?a={aid}")
                    return
            raise ChainError("4분 안에 확정되지 않았다")
        except Exception as e:                             # noqa: BLE001
            self.reg.update(state="error", error=str(e)[:300])

    def reg_status(self) -> dict:
        out = dict(self.reg)
        out["agent_id"] = out.get("agent_id") or self.agent_id()
        out["history"] = self.kv("agent_history", [])
        return out

    # ── 미세 조정(2.5) ────────────────────────────────────────────
    def tuning_lines(self) -> dict:
        return self.kv("tuning_lines", {"masking": [], "refine": []})

    def tuning_version(self) -> int:
        return int(self.kv("tuning_version", 0))

    def _apply_notes(self) -> None:
        lines = self.tuning_lines()
        for ad in (self.eng.llm, self.eng.agent_llm):
            ad.notes = {k: list(v) for k, v in lines.items()}

    def _tune_safe(self, rid: str) -> None:
        try:
            self.tune(rid)
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()

    def tune(self, rid: str) -> dict:
        """평가 1건당 최대 1줄. 대상별 8줄, 줄당 200자. 별점 4–5 이고 구체적 문제가 없으면 none(코드가 강제)."""
        o = self._body(rid)
        lines = self.tuning_lines()
        ctx = self._tune_context(o)
        fn = getattr(self.eng.agent_llm, "agent_tune", None)
        if fn is None:
            return {"action": "none"}
        out = fn(o["stars"], o.get("review") or "", ctx, lines, job_id=rid)
        tgt, act = out.get("target"), out.get("action")
        line = " ".join(str(out.get("line") or "").split())[:TUNE_MAX_CHARS]
        rec = {"rating_id": rid, "proposal": out, "applied": False}
        if tgt not in ("masking", "refine") or act not in ("add", "replace", "remove") or \
                (o["stars"] >= 4 and not out.get("specific_problem")):
            rec["reason"] = "none(규칙)"
            self._set_field(rid, "tuning", rec)
            return rec
        cur = list(lines.get(tgt) or [])
        before = list(cur)
        i = int(out.get("index") or 0)
        if act == "add":
            if len(cur) >= TUNE_MAX_LINES or not line:
                rec["reason"] = "8줄 상한" if line else "빈 줄"
                self._set_field(rid, "tuning", rec)
                return rec
            cur.append(line)
        elif act == "replace" and 0 <= i < len(cur) and line:
            cur[i] = line
        elif act == "remove" and 0 <= i < len(cur):
            cur.pop(i)
        else:
            rec["reason"] = "index 범위 밖"
            self._set_field(rid, "tuning", rec)
            return rec
        lines[tgt] = cur
        ver = self.tuning_version() + 1
        self.set_kv("tuning_lines", lines)
        self.set_kv("tuning_version", ver)
        self.db.x("INSERT INTO agent_tuning(version,target,action,idx,before,after,rating_id,t) VALUES(?,?,?,?,?,?,?,?)",
                  (ver, tgt, act, i, json.dumps(before, ensure_ascii=False), json.dumps(cur, ensure_ascii=False), rid,
                   self.eng.clock()))
        self._apply_notes()
        rec.update(applied=True, version=ver, target=tgt, action=act, line=line)
        self._set_field(rid, "tuning", rec)
        return rec

    def _tune_context(self, o: dict) -> list:
        lo, hi = o["version_range"]
        out = []
        for r in self.db.q("SELECT job_id FROM commits WHERE asset_id=? AND version>=? AND version<=? ORDER BY version",
                           (o["asset_id"], lo, hi)):
            j = self.eng.get(r["job_id"]) or {}
            if j.get("mode") == "agent":
                out.append({"instruction": j.get("instruction"),
                            "events": [e.get("text") for e in (j.get("events") or [])][-12:],
                            "verdict": (j.get("agent") or {}).get("verdict")})
        return out

    def tuning_rows(self) -> list:
        return [dict(r) for r in self.db.q("SELECT * FROM agent_tuning ORDER BY id DESC LIMIT 50")]

    def all_ratings(self) -> list:
        out = []
        for r in self.db.q("SELECT body FROM ratings ORDER BY created_at DESC LIMIT 200"):
            try:
                o = json.loads(r["body"] or "{}")
            except ValueError:
                continue
            if o.get("rating_id"):
                out.append(o)
        return out
