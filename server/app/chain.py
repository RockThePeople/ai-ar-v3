"""S21 체인 평가 — Ethereum Sepolia JSON-RPC(httpx), ERC-4337 v0.7 UserOperation 조립(Pimlico 번들러·paymaster),
ERC-8004 Reputation/Identity 레지스트리 호출. 계약 `services/common/app-api-v1.md` 11절.

- 새 의존성 없음: keccak·ABI·P-256 은 `chaincrypto`(순수 파이썬), 운영자 EOA tx(에이전트 등록·자기평가 시험)는 foundry `cast send`
  를 하위 프로세스로 부른다(키는 환경변수로만 넘김, 기록하지 않음).
- 기동 때 `eth_chainId` 가 11155111 이 아니면 체인 기능을 끈다(사용자 처음 URL 이 mainnet 이었다 — 결과가 바뀌는 규칙).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Optional

from . import chaincrypto as cc

CHAIN_ID = 11155111
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
IDENTITY = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
REPUTATION = "0x8004B663056A597Dffe9eCcC1965A193B7388713"
AGENT_REGISTRY = f"eip155:{CHAIN_ID}:{IDENTITY}"
EXPLORER = "https://sepolia.etherscan.io"
DUMMY_SIG = "0x" + "00" * 31 + "01" + "00" * 31 + "01"          # r = s = 1(가스 추정·후원 요청용)
USEROP_TTL_S = 8 * 60                                              # paymaster 유효시간보다 짧게


class ChainError(Exception):
    pass


def _hx(v) -> str:
    return hex(int(v)) if not isinstance(v, str) else v


def _b32(v: int) -> str:
    return "0x" + int(v).to_bytes(32, "big").hex()


class ChainClient:
    def __init__(self, env: dict, http=None) -> None:
        self.rpc_url = env.get("rpc_url") or ""
        self.pimlico_key = env.get("pimlico_key") or ""
        self.operator_key = env.get("operator_key") or ""
        self.factory = env.get("factory") or ""
        self.policy_id = env.get("policy_id") or ""
        self.http = http
        self.status, self.error = "off", None
        self.bundler_url = (f"https://api.pimlico.io/v2/sepolia/rpc?apikey={self.pimlico_key}"
                            if self.pimlico_key else "")
        self._id = 0

    # ── JSON-RPC ──────────────────────────────────────────────────
    def _post(self, url: str, method: str, params: list, timeout: float = 20.0):
        import httpx
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            client = self.http or httpx.Client(timeout=timeout)
            try:
                r = client.post(url, json=body)
            finally:
                if self.http is None:
                    client.close()
        except httpx.HTTPError as e:
            raise ChainError(f"{method}: 연결 실패 {type(e).__name__}") from e
        try:
            j = r.json()
        except ValueError as e:
            raise ChainError(f"{method}: HTTP {r.status_code}") from e
        if j.get("error"):
            err = j["error"]
            raise ChainError(f"{method}: {err.get('message') or err}"[:400])
        return j.get("result")

    def rpc(self, method: str, params: list):
        return self._post(self.rpc_url, method, params)

    def bundler(self, method: str, params: list):
        return self._post(self.bundler_url, method, params, timeout=30.0)

    def check(self) -> None:
        """chainId 11155111 이고 번들러·팩토리가 있으면 on. 아니면 off + 이유(health chain.error)."""
        if not self.rpc_url:
            self.status, self.error = "off", "CHAIN_RPC_URL 없음"
            return
        try:
            cid = int(self.rpc("eth_chainId", []), 16)
        except ChainError as e:
            self.status, self.error = "off", f"RPC 확인 실패: {e}"
            return
        if cid != CHAIN_ID:
            self.status, self.error = "off", f"chainId {cid} ≠ {CHAIN_ID}(Sepolia) — 체인 기능 끔"
            print(f"[chain] !!! chainId {cid} 는 Sepolia 가 아니다 — 체인 기능을 끈다 !!!", flush=True)
            return
        if not self.bundler_url:
            self.status, self.error = "off", "PIMLICO_API_KEY 없음"
            return
        if not self.factory:
            self.status, self.error = "off", "CHAIN_FACTORY 없음"
            return
        self.status, self.error = "on", None

    def call(self, to: str, data: str) -> str:
        return self.rpc("eth_call", [{"to": to, "data": data}, "latest"])

    # ── 읽기 ──────────────────────────────────────────────────────
    def account_for(self, x: int, y: int) -> str:
        out = self.call(self.factory, cc.call_data("getAddress(bytes32,bytes32)", ["bytes32", "bytes32"],
                                                   [x.to_bytes(32, "big"), y.to_bytes(32, "big")]))
        return cc.to_checksum("0x" + out[-40:])

    def has_code(self, addr: str) -> bool:
        return (self.rpc("eth_getCode", [addr, "latest"]) or "0x") not in ("0x", "0x0")

    def nonce(self, account: str) -> int:
        out = self.call(ENTRY_POINT, cc.call_data("getNonce(address,uint192)", ["address", "uint256"], [account, 0]))
        return int(out, 16)

    def last_index(self, agent_id: int, client: str) -> int:
        out = self.call(REPUTATION, cc.call_data("getLastIndex(uint256,address)", ["uint256", "address"],
                                                 [agent_id, client]))
        return int(out, 16)

    def read_feedback(self, agent_id: int, client: str, index: int) -> dict:
        out = self.call(REPUTATION, cc.call_data("readFeedback(uint256,address,uint64)",
                                                 ["uint256", "address", "uint256"], [agent_id, client, index]))
        w = cc.decode_words(out)
        value = w[0] - (1 << 256) if w[0] >= 1 << 255 else w[0]
        raw = bytes.fromhex(out.removeprefix("0x"))
        def _str(off_word: int) -> str:
            off = w[off_word]
            n = int.from_bytes(raw[off:off + 32], "big")
            return raw[off + 32:off + 32 + n].decode(errors="replace")
        tag1 = _str(2) if len(w) > 3 else ""
        return {"value": value, "decimals": w[1], "tag1": tag1}

    def summary(self, agent_id: int, clients: list) -> dict:
        """getSummary(agentId, clients, "starred", "") → (count, summaryValue, decimals) 앞 세 단어."""
        # address[] 동적 배열: head = [agentId, off_arr, off_t1, off_t2], 배열 = [len, addr…], 문자열 둘
        n = len(clients)
        head = 4 * 32
        arr = cc._word(n) + b"".join(cc._word(int(a, 16)) for a in clients)
        t1 = cc._word(len("starred")) + cc._pad(b"starred")
        t2 = cc._word(0)
        data = (cc.selector("getSummary(uint256,address[],string,string)") + cc._word(agent_id) + cc._word(head)
                + cc._word(head + len(arr)) + cc._word(head + len(arr) + len(t1)) + arr + t1 + t2)
        out = self.call(REPUTATION, "0x" + data.hex())
        w = cc.decode_words(out)
        v = w[1] - (1 << 256) if len(w) > 1 and w[1] >= 1 << 255 else (w[1] if len(w) > 1 else 0)
        return {"count": w[0] if w else 0, "value": v, "decimals": w[2] if len(w) > 2 else 0}

    def receipt(self, user_op_hash: str) -> Optional[dict]:
        return self.bundler("eth_getUserOperationReceipt", [user_op_hash])

    def balance(self, addr: str) -> int:
        return int(self.rpc("eth_getBalance", [addr, "latest"]), 16)

    # ── UserOperation(v0.7) ──────────────────────────────────────
    def feedback_call(self, agent_id: int, value: int, feedback_uri: str, feedback_keccak: str) -> str:
        inner = cc.call_data("giveFeedback(uint256,int128,uint8,string,string,string,string,bytes32)",
                             ["uint256", "int128", "uint8", "string", "string", "string", "string", "bytes32"],
                             [agent_id, value, 0, "starred", "", "/app/v1/edits", feedback_uri, feedback_keccak])
        return cc.call_data("execute(address,uint256,bytes)", ["address", "uint256", "bytes"], [REPUTATION, 0, inner])

    def build_user_op(self, x: int, y: int, call_data: str) -> dict:
        """계정 주소·(필요하면 factory)·nonce·가스값(pimlico fast)·paymaster 후원 → user_op, user_op_hash."""
        account = self.account_for(x, y)
        op = {"sender": account, "nonce": hex(self.nonce(account)), "callData": call_data, "signature": DUMMY_SIG}
        if not self.has_code(account):
            op["factory"] = cc.to_checksum(self.factory)
            op["factoryData"] = cc.call_data("createAccount(bytes32,bytes32)", ["bytes32", "bytes32"],
                                             [x.to_bytes(32, "big"), y.to_bytes(32, "big")])
        gp = self.bundler("pimlico_getUserOperationGasPrice", [])["fast"]
        op["maxFeePerGas"], op["maxPriorityFeePerGas"] = gp["maxFeePerGas"], gp["maxPriorityFeePerGas"]
        ctx = [{"sponsorshipPolicyId": self.policy_id}] if self.policy_id else []
        sp = self.bundler("pm_sponsorUserOperation", [op, ENTRY_POINT] + ctx)
        for k in ("callGasLimit", "verificationGasLimit", "preVerificationGas", "paymaster",
                  "paymasterVerificationGasLimit", "paymasterPostOpGasLimit", "paymasterData"):
            if k in sp:
                op[k] = sp[k]
        op["signature"] = "0x"
        h = self.user_op_hash(op)
        return {"account": account, "user_op": op, "user_op_hash": h, "deployed": "factory" not in op}

    @staticmethod
    def pack(op: dict) -> list:
        """v0.7 PackedUserOperation 칸 값(getUserOpHash 인자)."""
        def b(h):
            return bytes.fromhex((h or "0x").removeprefix("0x"))
        init = b(op.get("factory")) + b(op.get("factoryData")) if op.get("factory") else b""
        agl = int(op["verificationGasLimit"], 16).to_bytes(16, "big") + int(op["callGasLimit"], 16).to_bytes(16, "big")
        fees = int(op["maxPriorityFeePerGas"], 16).to_bytes(16, "big") + int(op["maxFeePerGas"], 16).to_bytes(16, "big")
        pmd = b""
        if op.get("paymaster"):
            pmd = (b(op["paymaster"]) + int(op.get("paymasterVerificationGasLimit") or "0x0", 16).to_bytes(16, "big")
                   + int(op.get("paymasterPostOpGasLimit") or "0x0", 16).to_bytes(16, "big") + b(op.get("paymasterData")))
        return [op["sender"], int(op["nonce"], 16), init, b(op["callData"]), agl, int(op["preVerificationGas"], 16),
                fees, pmd, b(op.get("signature"))]

    def user_op_hash(self, op: dict) -> str:
        """EntryPoint getUserOpHash(packed) eth_call(직접 계산하지 않는다)."""
        types = ["address", "uint256", "bytes", "bytes", "bytes32", "uint256", "bytes32", "bytes", "bytes"]
        sel = cc.selector("getUserOpHash((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes))")
        data = sel + cc._word(32) + cc.encode(types, self.pack(op))
        return self.call(ENTRY_POINT, "0x" + data.hex())

    def send_user_op(self, op: dict) -> str:
        return self.bundler("eth_sendUserOperation", [op, ENTRY_POINT])

    # ── 운영자 EOA tx(cast) ──────────────────────────────────────
    def cast(self, *args: str, timeout: float = 120.0) -> str:
        exe = shutil.which("cast") or os.path.expanduser("~/.foundry/bin/cast")
        env = dict(os.environ, ETH_RPC_URL=self.rpc_url)
        r = subprocess.run([exe, *args, "--rpc-url", self.rpc_url, "--private-key", self.operator_key],
                           capture_output=True, text=True, timeout=timeout, env=env)
        out = (r.stdout + r.stderr).replace(self.operator_key, "<key>") if self.operator_key else r.stdout + r.stderr
        if r.returncode != 0:
            raise ChainError(f"cast {args[0]} 실패: {out.strip()[-300:]}")
        return r.stdout.strip()

    def operator_address(self) -> str:
        exe = shutil.which("cast") or os.path.expanduser("~/.foundry/bin/cast")
        r = subprocess.run([exe, "wallet", "address", "--private-key", self.operator_key], capture_output=True,
                           text=True, timeout=30)
        if r.returncode != 0:
            raise ChainError("운영자 주소 계산 실패")
        return r.stdout.strip()

    def send_register(self, agent_uri: str) -> str:
        """IdentityRegistry register(string agentURI) — tx 를 보내고 hash 만 돌려준다(확정은 tx_receipt 로)."""
        return self.cast("send", "--async", IDENTITY, "register(string)", agent_uri)

    def tx_receipt(self, tx_hash: str) -> Optional[dict]:
        return self.rpc("eth_getTransactionReceipt", [tx_hash])

    @staticmethod
    def registered_agent_id(receipt: dict) -> Optional[int]:
        """Registered(uint256 indexed agentId, string agentURI, address indexed owner) 로그 → agentId."""
        topic = "0x" + cc.keccak256(b"Registered(uint256,string,address)").hex()
        for lg in receipt.get("logs") or []:
            if (lg.get("address") or "").lower() == IDENTITY.lower() and lg.get("topics") and lg["topics"][0] == topic:
                if len(lg["topics"]) > 1:
                    return int(lg["topics"][1], 16)
                return int((lg.get("data") or "0x0")[:66], 16)
        return None


def explorer_tx(tx_hash: str) -> str:
    return f"{EXPLORER}/tx/{tx_hash}"


def now_iso(t: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
