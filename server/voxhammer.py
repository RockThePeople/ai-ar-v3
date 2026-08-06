"""형태 편집(`replace_region` · `add` · `remove`) → `<EDIT_HOST>` VoxHammer.

`recolor` 는 3090 이 로컬로 한다 (기하 불변 · GPU 불필요 · D24). 형태를 바꾸는 op 는
디코더를 돌려야 하므로 상류로 넘긴다 — `dispatch.py` 의 CONSUMERS 표가 정본이고,
여기서 그 표를 다시 쓰지 않는다.

🔴 **op 를 갈아끼우지 않는다.** 상류가 거절하면 거절로 보고한다. 자동 강등
(`replace_region` → `recolor`)은 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를
재게 만든다 (D26).

⚠️ 받은 바이트는 **저장 직전에 계약 판본을 검증한다** (`contractguard`). 상류 헬스의
   선언을 믿지 않는다 — 선언과 산출이 갈린 실례가 있다 (W26).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Dict

from deltacontract import CONTRACT_CONSTANTS, chunk_uri  # type: ignore[import-not-found]
from deltacontract.schemas import (  # type: ignore[import-not-found]
    ChunkEntry,
    ContractInfo,
    PatchPackage,
)
from deltacontract.uris import staging_chunk_uri  # type: ignore[import-not-found]

from .assetstore import STORE
from .contractguard import verify_blobs
from .upstreamerr import parse_upstream_error, upstream_call

__all__ = ["run_voxhammer_edit", "UpstreamEditFailed"]


class UpstreamEditFailed(RuntimeError):
    error_code = "UPSTREAM_EDIT_FAILED"


def _client():
    import httpx

    url, key = os.environ.get("BLOCKEDIT_B_URL"), os.environ.get("BLOCKEDIT_B_KEY")
    if not url or not key:
        raise UpstreamEditFailed("상류 자격증명이 없다 (환경변수 미설정)")
    return httpx.Client(base_url=url.rstrip("/"),
                        headers={"X-Blockedit-Key": key}, timeout=120.0)


#: 조건 이미지용 구도 강제. **바꾼 뒤의 모습 전체**를 그리게 한다.
#: ⚠️ 이 꼬리말은 자산 종류를 가정한다(측면·전신). 다른 자산군이 오면 여기가 틀린다 —
#:    지금은 moto-b 한 자산이라 그대로 두고, 늘어나면 자산별로 갈라야 한다.
CONDITION_TEMPLATE = os.environ.get(
    "EDIT_CONDITION_TEMPLATE",
    "{subject}, strict side view profile, full vehicle and rider fully visible, "
    "product photography, studio lighting, plain white background, centered, "
    "entire subject fully visible with margin, no text, no watermark")


def _local_asset_dir(asset_id: str):
    """3090 쪽 자산 원본 디렉터리 (slat·input 이 있는 곳). 없으면 None."""
    from pathlib import Path as _P

    root = _P(os.environ.get("ASSET_SOURCE_ROOT",
                             str(_P.home() / "ai-ar-v3-assets")))
    for d in (root / asset_id, root / asset_id.replace("v3-", "")):
        if (d / "slat.safetensors").is_file():
            return d
    return None


def _condition_prompt(spec, req) -> str:
    """편집 지시 → 조건 이미지 프롬프트. **바뀐 뒤 전체**를 묘사해야 한다."""
    return CONDITION_TEMPLATE.replace("{subject}", spec.target_prompt or req.raw_prompt)


def _explain_version_conflict(asset_id: str, base_version: int, detail: str) -> str:
    """409 를 받은 **뒤에** 무엇이 필요한지 알려 준다.

    🔴 사전 조회로 판단하지 않는다. 전 판본은 `committed.json` 을 GET 해서 404 면
    "커밋 안 됨" 으로 읽었는데, **그 엔드포인트가 없어서** 404 였다 — 실제로는
    커밋돼 있었고 편집도 성공한 자산이었다. 없는 것을 근거로 추측하면 멀쩡한
    경로를 막는다. 그래서 **상류가 실제로 409 를 준 뒤에만** 이 경로를 탄다.

    ⚠️ `recolor` 는 3090 로컬이라 그 결과 판본이 상류에 **아예 없다.** 그 위에 형태
       편집을 걸면 여기로 온다 — 그때는 재료 검사 결과가 "무엇을 밀어야 하는가" 다.
    """
    from .versionsync import RequiredFilesMissing, check_v1_payload

    src = _local_asset_dir(asset_id)
    if src is None:
        return (f"{detail} · 3090 에도 밀어 넣을 재료가 없다 — 이 판본은 로컬 편집"
                f"(recolor)으로 만든 것일 수 있고, 그때 상류는 그 기하를 본 적이 없다")
    try:
        n = len(STORE.blobs(asset_id, base_version))
    except Exception:                                    # noqa: BLE001
        n = 0
    try:
        # 🔴 밀기 **전에** 검사한다. 없는 채로 밀면 라우트는 202 를 주고 워커에서 죽고
        #    ("요청은 성공, 결과는 없음"), 상류에 **막다른 판본**이 남는다.
        info = check_v1_payload(src, n_chunks=n)
    except RequiredFilesMissing as e:
        return f"{detail} · 밀어 넣을 재료가 불완전하다: {e}"
    return (f"{detail} · 3090 재료는 갖췄다 (청크 {info['n_chunks']} · "
            f"{info['slat']} · {info['image']}) — 상류가 이 판본을 받아 커밋해야 한다")


def run_voxhammer_edit(asset_id: str, req, spec, progress) -> dict:
    """`BEditRequest` → 상류 → 청크 검증 → 새 판본 → `PatchPackage`.

    ⚠️ 상류 `/v2/trellis/edit` 는 **JSON 만** 받는다 (asset_id · base_version ·
       prompt · mask · seed · idempotency_key). **조건 이미지 슬롯이 없다** —
       조건은 텍스트 프롬프트가 전부다. 이미지 조건이 필요하면 계약 변경이다.
    """
    # ── 조건 이미지 (D40). **조건이 프롬프트를 이긴다** — 조건 없이 돌리면 원본
    #    머리로 회귀한다. 그래서 형태 편집에는 조건을 붙이는 것이 기본이다.
    #
    #    Unity→3090 구간은 자연어 그대로다. 조건 이미지는 **3090 이 만든다** —
    #    사용자에게 이미지를 요구하지 않는다.
    #
    #    ⚠️ 규격(A5000 EDIT-API-SPEC): **바꾼 뒤의 모습 전체**다. 머리만 크롭하면 안 된다 —
    #       조건은 전신 실루엣으로 읽힌다. 1024² · 단순 배경 · 알파 불필요(rembg 가 덮는다).
    #    ⚠️ W11: **조건 이미지 좌표로 마스크를 판단하지 마라.** 비율이 다르다
    #       (조건의 목 35% vs 자산 17%). 마스크는 클라가 준 복셀이 유일한 진실이다.
    cond_png = None
    if os.environ.get("EDIT_CONDITION_IMAGE", "1") != "0":
        try:
            from .t2i import render_rgba

            progress(0.1, "condition", "조건 이미지 생성 (바뀐 뒤 전신)")
            cond_png, _ = render_rgba(_condition_prompt(spec, req), seed=int(req.seed))
        except Exception as exc:                        # noqa: BLE001
            # 🔴 조건을 못 만들면 **조건 없이 몰래 진행하지 않는다.** D40 상 그건
            #    원본 머리로 회귀하는 길이고, 결과만 보면 "편집이 약하다" 로 오독된다.
            raise UpstreamEditFailed(
                f"조건 이미지를 못 만들었다: {exc}. 조건 없이 돌리면 원본으로 회귀한다 "
                f"(D40) — 조용히 진행하지 않는다. 끄려면 EDIT_CONDITION_IMAGE=0") from exc

    body = {
        "asset_id": asset_id,
        "base_version": int(req.base_version),
        # 🔴 원문을 그대로 넘긴다. 여기서 프롬프트를 다시 쓰면 상류가 무엇을 받았는지
        #    화면과 로그가 갈린다.
        "prompt": spec.target_prompt or req.raw_prompt,
        "mask": req.mask.model_dump(mode="json"),
        "seed": int(req.seed),
        "idempotency_key": req.idempotency_key,
    }
    with _client() as c:
        progress(0.2, "submit", f"<EDIT_HOST> 편집 제출 (op={spec.op})"
                 + (" · 조건 이미지 동봉" if cond_png else " · 조건 없음"))
        if cond_png:
            # multipart — `meta`(BEditRequest JSON) + `image`. generate 와 같은 관례다.
            import json as _json

            r = upstream_call(lambda: c.post(
                "/v2/trellis/edit", data={"meta": _json.dumps(body)},
                files={"image": ("condition.png", cond_png, "image/png")}),
                action="편집 제출")
        else:
            r = upstream_call(lambda: c.post("/v2/trellis/edit", json=body),
                              action="편집 제출")
        if r.status_code >= 400:
            # 🔴 상류 사유를 **그대로** 올린다. "실패" 로 뭉뚱그리면 사용자가 재시도만
            #    하고, 재시도로는 절대 안 고쳐지는 오류가 많다.
            err = parse_upstream_error(r.status_code, r.text, action="편집 제출")
            if "VERSION_CONFLICT" in err.error_code:
                # 판본 동기화가 필요한 자리다. **무엇을 밀어야 하는지**까지 적는다 —
                # "커밋을 잊어서 409" 는 사유만으로는 고칠 수 없다.
                raise UpstreamEditFailed(
                    _explain_version_conflict(asset_id, int(req.base_version), str(err))
                ) from err
            raise err
        job_id = r.json().get("job_id")
        if not job_id:
            raise UpstreamEditFailed(f"job_id 가 없다: {r.text[:200]}")

        progress(0.3, "running", f"상류 잡 {job_id}")
        deadline = time.time() + 900
        payload = None
        while time.time() < deadline:
            p = c.get(f"/v2/trellis/jobs/{job_id}")
            if p.status_code >= 400:
                # 🔴 `raise_for_status()` 를 쓰지 않는다 — httpx 의 예외 메시지에
                #    **URL 이 통째로 들어간다**(호스트·포트). 그게 잡 상태로 올라가면
                #    앱 화면·로그에 공인 IP 가 찍힌다 (§7 위반). 실측으로 겪었다.
                raise parse_upstream_error(p.status_code, p.text, action="잡 조회")
            payload = p.json()
            # 완료 판정은 **응답 모양**이다 (state 로 판정하면 그 필드를 안 채우는
            # 판본에서 영원히 돈다 — generate 경로와 같은 규약).
            if isinstance(payload, dict) and "chunks" in payload and "to_version" in payload:
                break
            st = payload.get("state")
            if st in ("failed", "cancelled"):
                raise UpstreamEditFailed(f"상류 잡 {st}: {payload.get('error')}")
            progress(0.3 + 0.4 * float(payload.get("progress") or 0),
                     payload.get("stage") or "running", payload.get("stage_detail") or "")
            time.sleep(2)
        if not (payload and "chunks" in payload):
            raise UpstreamEditFailed(f"상류 잡이 제한 시간 안에 안 끝났다: {job_id}")

        to_version = int(payload["to_version"])
        entries = payload.get("chunks") or []
        removed = list(payload.get("removed_chunk_ids") or [])

        progress(0.75, "chunk", f"청크 {len(entries)} 수신")
        blobs: Dict[str, bytes] = {}
        failed = []
        for e in entries:
            key = e.get("chunk_id") or Path(e["uri"]).name.split(".")[0]
            g = c.get(staging_chunk_uri(asset_id, job_id, key))
            if g.status_code != 200:
                failed.append(f"{key}({g.status_code})")
                continue
            if e.get("hash") and hashlib.sha256(g.content).hexdigest() != e["hash"]:
                failed.append(f"{key}(해시 불일치)")
                continue
            blobs[key] = g.content
        if failed:
            raise UpstreamEditFailed(
                f"청크 {len(failed)}/{len(entries)} 를 못 받았다 "
                f"(예: {', '.join(failed[:5])}). 부분 저장하지 않는다")

    # 🔴 가드 — 상류가 무엇을 선언했든 **바이트**로 판정한다.
    progress(0.9, "verify", "계약 판본 검증 (바이트)")
    counts = verify_blobs(blobs, where="<EDIT_HOST>")

    progress(0.95, "store", "새 판본 저장")
    STORE.put_version(asset_id, to_version, blobs)

    changed = {}
    for k, b in blobs.items():
        from deltacontract import decode  # noqa: PLC0415

        d = decode(b)
        changed[k] = ChunkEntry(
            uri=chunk_uri(asset_id, k, to_version),
            hash=hashlib.sha256(b).hexdigest(), byte_length=len(b),
            vertex_count=int(len(d.positions)), index_count=int(len(d.indices)),
            voxel_count=int(getattr(d, "voxel_count", 0) or 0), version=to_version)

    patch = PatchPackage(
        asset_id=asset_id, from_version=int(req.base_version), to_version=to_version,
        contract=ContractInfo(**CONTRACT_CONSTANTS),
        changed_chunks=changed, removed_chunk_ids=removed,
        mask_fingerprint=payload.get("mask_fingerprint"),
        mask_voxels_used=len(req.mask.voxels or []), op="edit")
    return {"patch": patch, "asset_id": asset_id,
            "manifest": STORE.manifest(asset_id, to_version),
            "stage_detail": f"청크 {len(blobs)} · 제거 {len(removed)} · 판본 {counts}"}
