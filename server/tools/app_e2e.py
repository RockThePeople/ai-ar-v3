"""E1 — 폰 흉내 종단 시험(app-api 1.0.3). 폰이 보내는 요청을 그대로 흉내 낸다.

    python -m server.tools.app_e2e --base http://<PUBLIC_HOST>:<PUBLIC_PORT> --out <결과.json> [--long 10]

- 헤더 `X-Device-Key`(기기 A·B), 폴링 1 s, 계약 5절 멈춤 상태. 대상 주소는 인자로만 받는다(코드에 IP 없음).
- 청크는 `ChunkEntry.uri` 를 캐시 키로 받고 sha256 을 검사한다. 3D 확인 때 staging 청크를 받고, 되돌리기 뒤에는
  부모 판본 manifest·청크 해시가 처음과 같은지 본다(계약 3.5 편집 반영 순서).
- 단계(Voxhammer-light `ops/tasks/E1-e2e-3090.md`): 1 생성 → 2 편집 유지 → 3 quality 거절·되돌리기 → 4 prompt·region 거절
  → 5 B 잠금·판본 충돌 → 6 평가 → 8 장시간 N회 → 7 삭제. 장시간은 같은 자산에서 해야 해서 삭제(7) 앞에 둔다.
- 장시간 회차마다 3090 인페인팅 워커 RSS·VRAM(같은 장비일 때)과 A5000 health 의 GPU 메모리를 남긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

STOP = {"awaiting_2d_review", "awaiting_3d_review", "needs_redraw", "committed", "discarded", "cancelled", "failed"}
# X1 A-M1 phone 시점(반지름 1.8, 고도 15°, 세로 65°, aspect 0.46)의 NORMALIZED camera
PHONE_CAMERA = {"frame": "normalized_voxel", "position": [-1.231156, -1.230528, 0.465682],
                "forward": [0.683013, 0.683013, -0.258819], "up": [0.183013, 0.183013, 0.965926],
                "fov_y_deg": 65.0, "aspect": 0.46, "screen_px": [1080, 2340]}
COLORS = ["빨간", "파란", "노란", "초록", "보라", "주황", "하얀", "검은", "분홍", "은"]


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Device-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        try:
            return code, json.loads(raw) if raw else {}
        except ValueError:
            return code, {"_bytes": len(raw)}

    def raw(self, path: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers={"X-Device-Key": self.key}),
                                    timeout=60) as r:
            return r.read()

    def poll(self, job_id: str, until=STOP, limit_s: float = 900) -> dict:
        t0 = time.time()
        while time.time() - t0 < limit_s:
            code, j = self.call("GET", f"/app/v1/jobs/{job_id}")
            if code == 200 and j.get("state") in until:
                return j
            time.sleep(1.0)                               # 계약 5절: 1초 간격
        raise TimeoutError(f"{job_id} 가 {limit_s}s 안에 {until} 에 닿지 않았다")


class E2E:
    def __init__(self, a) -> None:
        self.a = a
        self.A, self.B = Client(a.base, "0x02e1deviceA"), Client(a.base, "0x03e1deviceB")
        self.cache: dict = {}                             # uri → bytes (폰의 청크 캐시)
        self.res: dict = {"base": a.base, "station": a.station, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "steps": [], "times": [], "long": []}

    # ── 기록 ───────────────────────────────────────────────────────
    def step(self, name: str, ok: bool, **detail) -> None:
        self.res["steps"].append({"step": name, "ok": bool(ok), **detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name} {json.dumps(detail, ensure_ascii=False)[:220]}", flush=True)

    def save(self) -> None:
        Path(self.a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(self.a.out).write_text(json.dumps(self.res, indent=1, ensure_ascii=False))

    # ── 청크 ───────────────────────────────────────────────────────
    def sync(self, c: Client, manifest_uri: str) -> dict:
        _, m = c.call("GET", manifest_uri)
        bad = 0
        for e in m["chunks"].values():
            if e["uri"] not in self.cache:
                b = c.raw(e["uri"])
                if hashlib.sha256(b).hexdigest() != e["hash"]:
                    bad += 1
                self.cache[e["uri"]] = b
            elif hashlib.sha256(self.cache[e["uri"]]).hexdigest() != e["hash"]:
                bad += 1
        return {"manifest": m, "n": len(m["chunks"]), "bad": bad}

    def staging(self, c: Client, result: dict) -> int:
        bad = 0
        for e in result["chunks"]:
            if hashlib.sha256(c.raw(e["uri"])).hexdigest() != e["hash"]:
                bad += 1
        return bad

    # ── 편집 한 건 ─────────────────────────────────────────────────
    def edit(self, c: Client, aid: str, base: int, instruction: str, cells: list):
        code, b = c.call("POST", f"/app/v1/assets/{aid}/edits",
                         {"base_version": base, "instruction": instruction, "camera": PHONE_CAMERA, "volume_cells": cells})
        return code, b

    def to_2d(self, c: Client, job_id: str, t0: float) -> tuple:
        j = c.poll(job_id)
        return j, round(time.time() - t0, 2)

    def mem(self) -> dict:
        out = {}
        try:
            pid = subprocess.run("ss -ltnpH 'sport = :8082' | grep -oP 'pid=\\K[0-9]+' | head -1", shell=True,
                                 capture_output=True, text=True).stdout.strip()
            if pid:
                out["inpaint_rss_gib"] = round(int(open(f"/proc/{pid}/statm").read().split()[1]) * 4096 / 2**30, 2)
            smi = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True).stdout.strip()
            out["gpu3090_mib"] = int(smi.splitlines()[0]) if smi else None
        except (OSError, ValueError):
            pass
        if self.a.a5000_health:
            try:
                h = json.loads(urllib.request.urlopen(self.a.a5000_health, timeout=5).read())
                out["a5000_gpu_mem_gb"] = h.get("gpu_mem_gb")
                out["a5000_queue"] = h.get("queue")
            except (OSError, ValueError):
                out["a5000_gpu_mem_gb"] = None
        return out

    # ── 시나리오 ───────────────────────────────────────────────────
    def run(self) -> None:
        A, B, st = self.A, self.B, self.a.station
        _, h = A.call("GET", "/app/v1/health")
        self.res["health"] = h
        # 1 생성
        code, b = A.call("GET", f"/app/v1/qr/{st}")
        self.step("1a QR 조회(빈 스테이션)", code == 404 and b.get("error_code") == "QR_EMPTY", http=code, code=b.get("error_code"))
        t0 = time.time()
        code, b = A.call("POST", f"/app/v1/qr/{st}/asset", {"prompt": self.a.prompt, "seed": 42})
        self.step("1b 생성 요청", code == 202, http=code, body=b)
        aid = b["asset_id"]
        j = A.poll(b["job_id"], until={"committed", "failed"})
        tg = round(time.time() - t0, 2)
        self.res["times"].append({"what": "생성", "s": tg})
        self.step("1c 생성 완료", j["state"] == "committed", s=tg, error=j.get("error_code"))
        code, q = A.call("GET", f"/app/v1/qr/{st}")
        sy = self.sync(A, q["manifest_uri"])
        _, sl = A.call("GET", q["slat_coords_uri"])
        self.step("1d 청크 전부 받기·해시", sy["bad"] == 0 and sy["n"] > 0, chunks=sy["n"], bad=sy["bad"],
                  slat_cells=sl.get("n_cells"), scene=q.get("scene_prompt"), is_creator=q.get("is_creator"))
        cells = self.box(sl["coords"])
        self.res["volume_cells"] = len(cells)

        # 2 편집 1: 유지
        v = q["version"]
        t0 = time.time()
        code, e = self.edit(A, aid, v, "타는 사람의 머리를 빨간 헬멧으로 바꿔줘", cells)
        j, t2 = self.to_2d(A, e["job_id"], t0)
        t1 = time.time()
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d", {"accept": True})
        j = A.poll(e["job_id"])
        t3 = round(time.time() - t1, 2)
        bad = self.staging(A, j["result"]) if j["state"] == "awaiting_3d_review" else -1
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review3d", {"keep": True})
        sy = self.sync(A, j["result"]["manifest_uri"])
        self.res["times"].append({"what": "편집1", "send_to_2d_s": t2, "accept_to_3d_s": t3})
        self.step("2 편집 유지", j["state"] == "committed" and bad == 0 and sy["bad"] == 0, send_to_2d_s=t2,
                  accept_to_3d_s=t3, staging_bad=bad, manifest_chunks=sy["n"], refine=j.get("refine"))
        v = j["result"]["to_version"]

        # 3 편집 2: quality 거절 → 재시도 → 승인 → 되돌리기
        _, m_before = A.call("GET", f"/v2/assets/{aid}/manifest.v{v}.json")
        t0 = time.time()
        code, e = self.edit(A, aid, v, "헬멧을 금색으로 칠해줘", cells)
        j, t2 = self.to_2d(A, e["job_id"], t0)
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d", {"accept": False, "reason": "이미지가 흐릿하고 뭉개졌어"})
        cat = (j.get("review") or {}).get("category")
        j = A.poll(e["job_id"])
        t1 = time.time()
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d", {"accept": True})
        j = A.poll(e["job_id"])
        t3 = round(time.time() - t1, 2)
        bad = self.staging(A, j["result"]) if j["state"] == "awaiting_3d_review" else -1
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review3d", {"keep": False})
        _, q = A.call("GET", f"/app/v1/qr/{st}")
        _, m_after = A.call("GET", f"/v2/assets/{aid}/manifest.v{v}.json")
        same = {k: x["hash"] for k, x in m_before["chunks"].items()} == {k: x["hash"] for k, x in m_after["chunks"].items()}
        cached_ok = all(hashlib.sha256(self.cache[x["uri"]]).hexdigest() == x["hash"] for x in m_after["chunks"].values()
                        if x["uri"] in self.cache)
        self.res["times"].append({"what": "편집2", "send_to_2d_s": t2, "accept_to_3d_s": t3})
        self.step("3 quality 거절·재시도·되돌리기", j["state"] == "discarded" and cat == "quality" and q["version"] == v
                  and same and cached_ok and bad == 0, category=cat, attempt=j.get("attempt"), version=q["version"],
                  manifest_same=same, parent_cache_ok=cached_ok, staging_bad=bad)

        # 4 편집 3: prompt 거절 → 재보정 → (5 B 잠금) → region 거절 → needs_redraw
        t0 = time.time()
        code, e = self.edit(A, aid, v, "헬멧 위에 뿔 두 개를 달아줘", cells)
        j, t2 = self.to_2d(A, e["job_id"], t0)
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d",
                      {"accept": False, "reason": "뿔이 아니라 토끼 귀처럼 나왔어. 날카로운 뿔이어야 해"})
        cat1 = (j.get("review") or {}).get("category")
        j = A.poll(e["job_id"])
        # 5 B: 조회·잠금
        code, qb = B.call("GET", f"/app/v1/qr/{st}")
        self.step("5a B 조회", code == 200 and qb["is_creator"] is False and qb["can_rate"] is True,
                  is_creator=qb.get("is_creator"), can_rate=qb.get("can_rate"), rate_range=qb.get("rate_range"),
                  active_job=qb.get("active_job"))
        code, bl = self.edit(B, aid, v, "의자를 추가해줘", cells)
        self.step("5b B 편집 → 잠금", code == 409 and bl.get("error_code") == "ASSET_LOCKED", http=code, code=bl.get("error_code"))
        _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d",
                      {"accept": False, "reason": "내가 고른 곳이 아니라 다른 데가 바뀌었어"})
        cat2 = (j.get("review") or {}).get("category")
        self.step("4 prompt → 재보정 → region → needs_redraw", cat1 == "prompt" and cat2 == "region" and j["state"] == "needs_redraw",
                  categories=[cat1, cat2], state=j["state"], attempt=j.get("attempt"), send_to_2d_s=t2,
                  reject_history_refine=(j.get("refine") or {}).get("replacement"))
        code, vc = self.edit(B, aid, v - 1, "의자를 추가해줘", cells)
        self.step("5c B 낡은 base_version → 판본 충돌", code == 409 and vc.get("error_code") == "VERSION_CONFLICT",
                  http=code, code=vc.get("error_code"), detail=vc.get("detail"))

        # 6 평가
        code, rb = B.call("POST", f"/app/v1/assets/{aid}/ratings", {"stars": 4})
        code2, rb2 = B.call("POST", f"/app/v1/assets/{aid}/ratings", {"stars": 4})
        code3, ra = A.call("POST", f"/app/v1/assets/{aid}/ratings", {"stars": 5})
        self.step("6 평가", code == 201 and code2 == 409 and rb2.get("error_code") == "NOTHING_TO_RATE" and code3 == 201,
                  b=[code, rb.get("version_range")], b_again=[code2, rb2.get("error_code")], a=[code3, ra.get("version_range")])
        self.save()

        # 8 장시간(같은 자산, 삭제 전)
        fails = 0
        for i in range(self.a.long):
            mem0 = self.mem()
            t0 = time.time()
            ok, info = False, {}
            try:
                code, e = self.edit(A, aid, v, f"헬멧을 {COLORS[i % len(COLORS)]}색으로 칠해줘", cells)
                j, t2 = self.to_2d(A, e["job_id"], t0)
                t1 = time.time()
                A.call("POST", f"/app/v1/jobs/{e['job_id']}/review2d", {"accept": True})
                j = A.poll(e["job_id"])
                t3 = round(time.time() - t1, 2)
                _, j = A.call("POST", f"/app/v1/jobs/{e['job_id']}/review3d", {"keep": True})
                sy = self.sync(A, j["result"]["manifest_uri"])
                ok = j["state"] == "committed" and sy["bad"] == 0
                v = j["result"]["to_version"] if ok else v
                info = {"send_to_2d_s": t2, "accept_to_3d_s": t3, "version": v, "changed": len(j["result"]["chunks"]),
                        "error": j.get("error_code")}
            except Exception as ex:                       # noqa: BLE001
                info = {"error": f"{type(ex).__name__}: {ex}"}
            fails += not ok
            row = {"round": i + 1, "ok": ok, "total_s": round(time.time() - t0, 2), **info, "mem_before": mem0, "mem_after": self.mem()}
            self.res["long"].append(row)
            print(f"[long {i + 1}] {'ok' if ok else 'FAIL'} {json.dumps(row, ensure_ascii=False)[:240]}", flush=True)
            self.save()
        if self.a.long:
            self.step(f"8 장시간 {self.a.long}회", fails == 0, fails=fails)

        # 7 삭제
        code, b = B.call("DELETE", f"/app/v1/qr/{st}")
        self.step("7a B 삭제 → 403", code == 403 and b.get("error_code") == "NOT_CREATOR", http=code, code=b.get("error_code"))
        code, _ = A.call("DELETE", f"/app/v1/qr/{st}")
        code2, b2 = A.call("GET", f"/app/v1/qr/{st}")
        code3, _ = A.call("GET", f"/v2/assets/{aid}/manifest.v{v}.json")
        code4, cl = A.call("GET", f"/clients/api/assets/{aid}")
        jobs = cl.get("jobs") or []
        self.step("7b A 삭제 → QR 404, /clients 에 보관", code == 204 and code2 == 404 and code3 == 404 and code4 == 200
                  and cl.get("status") == "삭제됨", delete=code, qr=[code2, b2.get("error_code")], manifest=code3,
                  clients_status=cl.get("status"), clients_jobs=len(jobs), clients_versions=cl.get("versions"))
        self.res["asset_id"] = aid
        self.res["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.res["summary"] = {"steps": len(self.res["steps"]), "ok": sum(s["ok"] for s in self.res["steps"])}
        self.save()
        print("SUMMARY", json.dumps(self.res["summary"], ensure_ascii=False))

    @staticmethod
    def box(coords: list) -> list:
        """A-M1 식 머리 상자: 점유 셀 맨 위 9층의 bbox 를 아래로 2, 위로 4 넓힌다(빈 공간 포함)."""
        zmax = max(c[2] for c in coords)
        top = [c for c in coords if c[2] >= zmax - 8]
        lo = [min(c[i] for c in top) for i in range(3)]
        hi = [max(c[i] for c in top) for i in range(3)]
        return [[i, j, k] for i in range(lo[0], hi[0] + 1) for j in range(lo[1], hi[1] + 1)
                for k in range(max(0, lo[2] - 2), min(63, hi[2] + 4) + 1)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="앱 서버 주소 http://<host>:<port>")
    ap.add_argument("--out", required=True)
    ap.add_argument("--station", default="st-e1")
    ap.add_argument("--prompt", default="검은 오토바이를 탄 남자")
    ap.add_argument("--long", type=int, default=10)
    ap.add_argument("--a5000-health", default="", help="A5000 워커 health URL(장시간 메모리 기록용, 선택)")
    E2E(ap.parse_args()).run()


if __name__ == "__main__":
    main()
