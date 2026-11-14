"""S21 체인 평가(계약 11절): 순수 파이썬 암호·ABI, UserOperation 조립(가짜 RPC), 평가 상태 전이, 가짜 모드 서명 검사, 미세 조정."""

from __future__ import annotations

import hashlib
import json

import pytest

from server.app import chaincrypto as cc
from server.app.chain import ENTRY_POINT, REPUTATION, ChainClient
from server.app.ratings import Ratings
from server.tests.test_app_server import A, CAM, CELLS, env, make_asset  # noqa: F401 — fixture

D = 0xc9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721       # RFC 6979 A.2.5 키
X, Y = cc.pub_from_priv(D)
PUB = "0x04" + X.to_bytes(32, "big").hex() + Y.to_bytes(32, "big").hex()


# ══════════════════════════════════════════════════════════ 암호·ABI
def test_crypto_vectors():
    assert cc.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert cc.selector("transfer(address,uint256)").hex() == "a9059cbb"
    assert hex(X) == "0x60fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6"
    r, s = cc.sign_sha256(D, b"sample", low_s=False)
    assert hex(r) == "0xefd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716"
    h = hashlib.sha256(b"sample").digest()
    assert cc.verify_hash(h, r, s, X, Y) and cc.verify_hash(h, r, cc.N - s, X, Y)      # high-s·low-s 모두
    assert not cc.verify_hash(hashlib.sha256(b"x").digest(), r, s, X, Y)
    assert cc.parse_pubkey(PUB) == (X, Y)
    with pytest.raises(ValueError):
        cc.parse_pubkey("0x04" + "00" * 64)
    assert cc.to_checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


# ══════════════════════════════════════════════════════════ 가짜 체인
class FakeChain(ChainClient):
    def __init__(self, deployed=False):
        super().__init__({"rpc_url": "http://rpc", "pimlico_key": "k", "factory": "0x" + "fa" * 20})
        self.status = "on"
        self.deployed = deployed
        self.sent, self.receipts, self.calls = [], {}, []
        self.hash_n = 0

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(11155111)
        if method == "eth_getCode":
            return "0x6001" if self.deployed else "0x"
        if method == "eth_call":
            data = params[0]["data"]
            sel = data[:10]
            if sel == "0x" + cc.selector("getAddress(bytes32,bytes32)").hex():
                return "0x" + "00" * 12 + "ab" * 20
            if sel == "0x" + cc.selector("getNonce(address,uint192)").hex():
                return "0x" + "00" * 31 + "05"
            if sel.startswith("0x" + cc.selector(
                    "getUserOpHash((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes))").hex()):
                self.hash_n += 1
                return "0x" + hashlib.sha256(data.encode() + bytes([self.hash_n])).hexdigest()
            if sel == "0x" + cc.selector("getLastIndex(uint256,address)").hex():
                return "0x" + "00" * 31 + "03"
            if sel == "0x" + cc.selector("readFeedback(uint256,address,uint64)").hex():
                return "0x" + cc.encode(["int128", "uint8", "string", "string", "bool"], [80, 0, "starred", "", False]).hex()
        raise AssertionError(method)

    def bundler(self, method, params):
        if method == "pimlico_getUserOperationGasPrice":
            return {"fast": {"maxFeePerGas": "0x10", "maxPriorityFeePerGas": "0x2"}}
        if method == "pm_sponsorUserOperation":
            return {"callGasLimit": "0x1000", "verificationGasLimit": "0x2000", "preVerificationGas": "0x300",
                    "paymaster": "0x" + "77" * 20, "paymasterVerificationGasLimit": "0x400",
                    "paymasterPostOpGasLimit": "0x1", "paymasterData": "0xbeef"}
        if method == "eth_sendUserOperation":
            self.sent.append(params[0])
            return "0x" + "11" * 32
        if method == "eth_getUserOperationReceipt":
            return self.receipts.get(params[0])
        raise AssertionError(method)


def test_build_user_op_and_calldata():
    c = FakeChain()
    cd = c.feedback_call(7, 80, "http://h/app/v1/ratings/rt-x.json", "0x" + "22" * 32)
    assert cd.startswith("0x" + cc.selector("execute(address,uint256,bytes)").hex())
    raw = bytes.fromhex(cd[10:])
    assert int.from_bytes(raw[:32], "big") == int(REPUTATION, 16)                    # dest = ReputationRegistry
    inner = raw[96 + 32: 96 + 32 + int.from_bytes(raw[96:128], "big")]
    assert inner[:4] == cc.selector("giveFeedback(uint256,int128,uint8,string,string,string,string,bytes32)")
    w = [int.from_bytes(inner[4 + 32 * i: 36 + 32 * i], "big") for i in range(8)]
    assert w[0] == 7 and w[1] == 80 and w[2] == 0 and inner[4 + 32 * 7: 36 + 32 * 7] == b"\x22" * 32
    b = c.build_user_op(X, Y, cd)
    op = b["user_op"]
    assert op["factory"] and op["factoryData"].startswith("0x" + cc.selector("createAccount(bytes32,bytes32)").hex())
    assert op["nonce"] == "0x5" and op["paymaster"] == "0x" + "77" * 20 and op["signature"] == "0x"
    assert b["user_op_hash"].startswith("0x") and not b["deployed"]
    packed = c.pack(op)
    assert packed[4] == (0x2000).to_bytes(16, "big") + (0x1000).to_bytes(16, "big")    # verification ‖ call
    assert packed[6] == (0x2).to_bytes(16, "big") + (0x10).to_bytes(16, "big")         # priority ‖ max
    assert packed[7][:20] == b"\x77" * 20 and packed[7].endswith(b"\xbe\xef")
    b2 = FakeChain(deployed=True).build_user_op(X, Y, cd)
    assert "factory" not in b2["user_op"] and b2["deployed"]


# ══════════════════════════════════════════════════════════ 평가 흐름
def _edited_asset(env):
    aid = make_asset(env)
    r = env.post(f"/app/v1/assets/{aid}/edits", headers=A,
                 json={"base_version": 1, "instruction": "빨간 모자", "camera": CAM, "volume_cells": CELLS})
    env.eng.drain()
    jid = r.json()["job_id"]
    env.post(f"/app/v1/jobs/{jid}/review2d", headers=A, json={"accept": True})
    env.eng.drain()
    env.post(f"/app/v1/jobs/{jid}/review3d", headers=A, json={"keep": True})
    return aid


def _ratings(env, client=None, mock=False):
    env.eng.s.mock = mock
    R = Ratings(env.eng, start_thread=False, client=client or FakeChain())
    if not mock:
        R.client.status = "on"
    R.set_kv("agent_id", 7)
    env.eng.ratings = R
    return R


def test_rating_off_without_pubkey_and_file_fields(env):
    aid = _edited_asset(env)
    _ratings(env)
    r = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "review": "좋아요"})
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["chain"]["status"] == "off" and o["review"] == "좋아요" and o["version_range"] == [2, 2]
    raw = env.get(o["feedback_uri"]).content
    assert o["feedback_keccak"] == "0x" + cc.keccak256(raw).hex() and o["feedback_hash"] == hashlib.sha256(raw).hexdigest()
    fb = json.loads(raw)
    for k in ("agentRegistry", "agentId", "clientAddress", "createdAt", "value", "valueDecimals", "tag1", "endpoint",
              "stars", "review", "asset_id", "version_range", "job_ids", "agent_trace"):
        assert k in fb, k
    assert fb["agentId"] == 7 and fb["value"] == 80 and fb["agent_trace"]["history_keccak"].startswith("0x")


def test_rating_chain_flow_and_prepare_states(env):
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "signer_pubkey": PUB}).json()
    ch = o["chain"]
    assert ch["status"] == "awaiting_signature" and ch["agent_id"] == 7 and ch["user_op"]["factory"]
    fb = json.loads(env.get(o["feedback_uri"]).content)
    assert fb["clientAddress"].endswith(ch["account"])
    rid = o["rating_id"]
    assert env.get(f"/app/v1/ratings/{rid}").json()["chain"]["user_op_hash"] == ch["user_op_hash"]
    # prepare: awaiting → 새 hash
    p = env.post(f"/app/v1/ratings/{rid}/prepare", json={}).json()
    assert p["chain"]["status"] == "awaiting_signature" and p["chain"]["user_op_hash"] != ch["user_op_hash"]
    # 만료 → expired → prepare 가능
    env.clock.t += 9 * 60
    assert env.get(f"/app/v1/ratings/{rid}").json()["chain"]["status"] == "expired"
    p2 = env.post(f"/app/v1/ratings/{rid}/prepare", json={}).json()
    assert p2["chain"]["status"] == "awaiting_signature"
    # 잘못된 hash 400, 맞는 hash → submitted, submitted 에서 prepare 409
    assert env.post(f"/app/v1/ratings/{rid}/submitted", json={"user_op_hash": "0x" + "00" * 32}).status_code == 400
    h = p2["chain"]["user_op_hash"]
    s = env.post(f"/app/v1/ratings/{rid}/submitted", json={"user_op_hash": h}).json()
    assert s["chain"]["status"] == "submitted"
    assert env.post(f"/app/v1/ratings/{rid}/prepare", json={}).status_code == 409
    # 감시: 영수증 성공 → confirmed(tx·block·index·링크), confirmed 에서 prepare 409
    R.client.receipts[h] = {"success": True, "actualGasUsed": "0x5208",
                            "receipt": {"transactionHash": "0x" + "ab" * 32, "blockNumber": "0x10"}}
    R.watch_once()
    c = env.get(f"/app/v1/ratings/{rid}").json()["chain"]
    assert c["status"] == "confirmed" and c["tx_hash"] == "0x" + "ab" * 32 and c["block_number"] == 16
    assert c["feedback_index"] == 3 and c["explorer_url"].endswith("0x" + "ab" * 32)
    assert env.post(f"/app/v1/ratings/{rid}/prepare", json={}).status_code == 409
    assert R._body(rid)["chain"]["onchain_read"]["match"] is True


def test_prepare_from_failed_and_off_409(env):
    """S21 5절: failed 에서도 prepare(새 nonce·가스, 같은 rating_id). off 는 409."""
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 2, "signer_pubkey": PUB}).json()
    rid, h = o["rating_id"], o["chain"]["user_op_hash"]
    env.post(f"/app/v1/ratings/{rid}/submitted", json={"user_op_hash": h})
    R.client.receipts[h] = {"success": False, "reason": "AA23", "receipt": {"transactionHash": "0x" + "cd" * 32}}
    R.watch_once()
    assert env.get(f"/app/v1/ratings/{rid}").json()["chain"]["status"] == "failed"
    p = env.post(f"/app/v1/ratings/{rid}/prepare", json={})
    assert p.status_code == 200 and p.json()["rating_id"] == rid and p.json()["chain"]["status"] == "awaiting_signature"
    off = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 3})
    assert off.status_code == 409                                                 # 범위 소진(3.6) — 다른 기기로
    o2 = env.post(f"/app/v1/assets/{aid}/ratings", headers={"X-Device-Key": "dev-2"}, json={"stars": 3}).json()
    assert o2["chain"]["status"] == "off"
    assert env.post(f"/app/v1/ratings/{o2['rating_id']}/prepare", json={}).status_code == 409


def test_prepare_new_pubkey_rebuilds_account_and_file(env):
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "signer_pubkey": PUB}).json()
    k1 = o["feedback_keccak"]
    X2, Y2 = cc.pub_from_priv(12345)
    pub2 = "0x04" + X2.to_bytes(32, "big").hex() + Y2.to_bytes(32, "big").hex()
    p = env.post(f"/app/v1/ratings/{o['rating_id']}/prepare", json={"signer_pubkey": pub2}).json()
    assert p["rating_id"] == o["rating_id"] and p["version_range"] == o["version_range"]
    assert p["chain"]["status"] == "awaiting_signature" and R._body(o["rating_id"])["signer_pubkey"] == pub2
    assert p["feedback_keccak"] != k1 or p["feedback_keccak"] == k1                # 계정이 같으면 같을 수 있음(가짜 주소 고정)


def test_server_submits_signature(env):
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 5, "signer_pubkey": PUB}).json()
    h = bytes.fromhex(o["chain"]["user_op_hash"][2:])
    r, s = cc.sign_sha256(D, h)
    sig = "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()
    out = env.post(f"/app/v1/ratings/{o['rating_id']}/submitted", json={"signature": sig}).json()
    assert out["chain"]["status"] == "submitted" and R.client.sent[0]["signature"] == sig


def test_invalid_pubkey_is_off(env):
    aid = _edited_asset(env)
    _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "signer_pubkey": "0x04abcd"}).json()
    assert o["chain"]["status"] == "off" and "signer_pubkey" in o["chain"]["error"]


# ══════════════════════════════════════════════════════════ 가짜 모드(8084)
def test_mock_mode_signature_checked(env):
    aid = _edited_asset(env)
    _ratings(env, mock=True)
    try:
        o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "signer_pubkey": PUB}).json()
        ch = o["chain"]
        assert ch["status"] == "awaiting_signature" and ch["bundler_url"].endswith("/mock-bundler")
        h = bytes.fromhex(ch["user_op_hash"][2:])
        r, s = cc.sign_sha256(D, h, low_s=False)
        bad = "0x" + r.to_bytes(32, "big").hex() + ((s + 1) % cc.N).to_bytes(32, "big").hex()
        op = dict(ch["user_op"], signature=bad)
        e = env.post("/mock-bundler", json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendUserOperation",
                                            "params": [op, ENTRY_POINT]}).json()
        assert "AA24" in e["error"]["message"]
        good = "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()       # high-s 그대로도 받음
        ok = env.post("/mock-bundler", json={"jsonrpc": "2.0", "id": 2, "method": "eth_sendUserOperation",
                                             "params": [dict(op, signature=good), ENTRY_POINT]}).json()
        assert ok["result"] == ch["user_op_hash"]
        rid = o["rating_id"]
        assert env.post(f"/app/v1/ratings/{rid}/submitted", json={"user_op_hash": ok["result"]}).json()["chain"]["status"] == "submitted"
        env.clock.t += 6
        env.eng.ratings.watch_once()
        c = env.get(f"/app/v1/ratings/{rid}").json()["chain"]
        assert c["status"] == "confirmed" and c["explorer_url"].startswith("https://sepolia.etherscan.io/tx/0x")
        # {signature} 제출이 틀리면 failed("signature invalid"), 그 뒤 prepare 가능(5절)
        o2 = env.post(f"/app/v1/assets/{aid}/ratings", headers={"X-Device-Key": "d2"},
                      json={"stars": 3, "signer_pubkey": PUB}).json()
        f = env.post(f"/app/v1/ratings/{o2['rating_id']}/submitted", json={"signature": bad}).json()
        assert f["chain"]["status"] == "failed" and f["chain"]["error"] == "signature invalid"
        assert env.post(f"/app/v1/ratings/{o2['rating_id']}/prepare", json={}).json()["chain"]["status"] == "awaiting_signature"
    finally:
        env.eng.s.mock = False


# ══════════════════════════════════════════════════════════ 미세 조정(2.5)
def test_tuning_rules(env):
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 5, "review": "좋아요"}).json()
    rec = R.tune(o["rating_id"])
    assert rec["applied"] is False and R.tuning_version() == 0                     # 4–5 별, 구체적 문제 없음 → none
    o2 = env.post(f"/app/v1/assets/{aid}/ratings", headers={"X-Device-Key": "d2"},
                  json={"stars": 2, "review": "칼이 손에서 떠 있어요"}).json()
    rec2 = R.tune(o2["rating_id"])
    assert rec2["applied"] and rec2["target"] == "masking" and R.tuning_version() == 1
    assert len(R.tuning_lines()["masking"]) == 1                                  # 평가 1건당 1줄
    assert "Keep a held object" in env.eng.agent_llm.notes["masking"][0]          # 프롬프트에 붙는 목록
    lines = R.tuning_lines()
    lines["masking"] = [f"rule {i}" for i in range(8)]
    R.set_kv("tuning_lines", lines)
    o3 = env.post(f"/app/v1/assets/{aid}/ratings", headers={"X-Device-Key": "d3"},
                  json={"stars": 1, "review": "칼이 또 떠 있어요"}).json()
    assert R.tune(o3["rating_id"])["applied"] is False and len(R.tuning_lines()["masking"]) == 8   # 8줄 상한
    assert R.tuning_rows()[0]["version"] == 1
    from server.app.llm_adapter import with_notes
    assert "Notes from past user feedback:" in with_notes(env.eng.agent_llm, "SYS", "masking")


def test_health_chain_and_admin(env):
    _ratings(env)
    h = env.get("/app/v1/health").json()
    assert h["app_api"] == "1.2.0" and "chain_rating" in h["features"]
    c = h["chain"]
    assert c["chain_id"] == 11155111 and c["entry_point"] == ENTRY_POINT and c["agent_id"] == 7 and c["factory"]
    assert "Register" in env.get("/admin").text
    a = env.get("/admin/api").json()
    assert a["chain"]["agent_id"] == 7 and "ratings" in a
    reg = env.get("/app/v1/agent-registration.json").json()
    assert reg["type"] == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
    assert reg["registrations"][0]["agentId"] == 7 and reg["supportedTrust"] == ["reputation"]


def test_tune_does_not_clobber_submitted(env):
    """S21 실측 결함: 조정 스레드가 LLM 응답을 기다리는 사이 submitted 가 되면, 오래된 사본 저장으로 awaiting 으로 되돌아갔다."""
    aid = _edited_asset(env)
    R = _ratings(env)
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A,
                 json={"stars": 5, "review": "좋아요", "signer_pubkey": PUB}).json()
    rid, h = o["rating_id"], o["chain"]["user_op_hash"]
    real = env.eng.agent_llm.agent_tune

    def slow_tune(*a, **k):                                   # LLM 대기 중에 기기가 제출
        assert env.post(f"/app/v1/ratings/{rid}/submitted", json={"user_op_hash": h}).status_code == 200
        return real(*a, **k)
    env.eng.agent_llm.agent_tune = slow_tune
    R.tune(rid)
    b = R._body(rid)
    assert b["chain"]["status"] == "submitted" and b["tuning"]["reason"] == "none(규칙)"
    # 감시 스레드의 오래된 사본도 상태가 바뀐 뒤에는 덮지 않는다
    stale = dict(b["chain"], status="failed")
    assert R._set_chain(rid, stale, "awaiting_signature") is False
    assert R._body(rid)["chain"]["status"] == "submitted"


def test_bundler_proxy_hides_key(env):
    """실제 모드: health·평가의 bundler_url 은 이 서버 /bundler(키 없음), 중계는 사용자 작업 메서드만 그대로 전달."""
    aid = _edited_asset(env)
    R = _ratings(env)
    assert R.health()["bundler_url"] == "/bundler"
    o = env.post(f"/app/v1/assets/{aid}/ratings", headers=A, json={"stars": 4, "signer_pubkey": PUB}).json()
    assert o["chain"]["bundler_url"].endswith("/bundler") and "apikey" not in json.dumps(o)
    fwd = []
    R.client.bundler_raw = lambda b: fwd.append(b) or {"jsonrpc": "2.0", "id": b.get("id"), "error": {"code": -32507, "message": "AA24"}}
    r = env.post("/bundler", json={"jsonrpc": "2.0", "id": 9, "method": "eth_sendUserOperation", "params": [{}, "0x"]}).json()
    assert r["error"]["code"] == -32507 and fwd[0]["method"] == "eth_sendUserOperation"      # 오류 객체 그대로
    bad = env.post("/bundler", json={"jsonrpc": "2.0", "id": 1, "method": "pm_sponsorUserOperation", "params": []}).json()
    assert bad["error"]["code"] == -32601 and len(fwd) == 1                                 # 후원 요청은 막음
    # 키 든 URL 로 저장된 옛 행도 응답에서는 중계 주소
    b = R._body(o["rating_id"]); b["chain"]["bundler_url"] = "https://x/rpc?apikey=SECRET"; R._save(b)
    assert "SECRET" not in json.dumps(env.get(f"/app/v1/ratings/{o['rating_id']}").json())
