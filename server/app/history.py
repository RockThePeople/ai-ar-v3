"""S17 app-api 1.0.6 3.8 — 자산 AI 기록. 서버 작업 기록(시도·상태)에 있는 값만 꺼낸다(계산·LLM 호출 없음).

S17 전 작업은 시도별 `refine`·`accepted` 가 저장되지 않았다. 그때는 저장된 값에서만 꺼낸다:
- refine: 그 시도의 new_scene_prompt 가 작업의 마지막 보정 문장과 같으면 그 보정, 아니면 null.
- accepted: 거절 기록이 있으면 false, 마지막 시도이고 작업이 3D 단계 이후로 갔으면 true, 그 밖에는 null.
- refine_retried: S11+ 시도 기록의 style.retried / S8 refine_retry 가 있으면 그 값, 없으면 null.
"""

from __future__ import annotations

from typing import Optional

from .engine import prompts_of

OUTCOME = {"committed": "keep", "discarded": "discard", "cancelled": "cancelled", "needs_redraw": "needs_redraw",
           "failed": "failed"}
AFTER_2D = {"editing_3d", "awaiting_3d_review", "committed", "discarded"}
REFINE_KEYS = ("target_element", "replacement", "op", "new_scene_prompt")


def _job_prompts(j: dict) -> dict:
    p = prompts_of(j)
    out = {k: p.get(k) for k in ("input", "detailed", "style", "mask_ratio", "region_fit")}
    if j.get("prompts") is None:                       # S15 전 작업: 저장된 기록에서만
        if j.get("kind") == "generate":
            out["detailed"] = ((j.get("t2i") or {}).get("timing") or {}).get("prompt_effective")
            st = j.get("style")
            if isinstance(st, dict) and not j.get("raw_prompt"):
                out["style"] = st.get("style") or "realistic"
        else:
            last = next((a.get("style") for a in reversed(j.get("attempts") or []) if isinstance(a.get("style"), dict)), None)
            if last:
                out["mask_ratio"] = last.get("mask_ratio")
                out["region_fit"] = last.get("region_fit")
                if last.get("mode"):
                    out["style"] = last.get("style") or "realistic"
    if j.get("raw_prompt"):                             # 4.4 그대로 모드 규칙
        out.update(style=None, mask_ratio=None, region_fit=None)
        if j.get("kind") == "generate":
            out["detailed"] = out["input"]
    return out


def _attempts(j: dict) -> list:
    if j.get("kind") != "edit":
        return []
    final = j.get("refine") or {}
    atts = sorted(j.get("attempts") or [], key=lambda a: a.get("attempt") or 0)
    out = []
    for i, a in enumerate(atts):
        last = i == len(atts) - 1
        if "refine" in a:                               # S17 이후 저장
            refine = a["refine"]
        elif j.get("raw_prompt"):
            refine = None
        elif final and a.get("new_scene_prompt") and a.get("new_scene_prompt") == final.get("new_scene_prompt"):
            refine = {k: final.get(k) for k in REFINE_KEYS}
        else:
            refine = None
        if "refine_retried" in a:
            rr = a["refine_retried"]
        elif j.get("raw_prompt"):
            rr = None
        elif isinstance(a.get("style"), dict) and "retried" in a["style"]:
            rr = bool(a["style"]["retried"] or a.get("refine_retry"))
        elif "refine_retry" in a:
            rr = bool(a["refine_retry"])
        else:
            rr = None
        review = ({"reason": a.get("reason"), "category": a.get("category"), "guidance": a.get("guidance")}
                  if a.get("rejected") else None)
        if a.get("accepted") is not None:
            acc: Optional[bool] = a["accepted"]
        elif a.get("rejected"):
            acc = False
        elif last and j.get("state") in AFTER_2D:
            acc = True
        else:
            acc = None
        out.append({"attempt": a.get("attempt"), "refine": refine, "inpaint": a.get("prompt"),
                    "refine_retried": rr, "review": review, "accepted": acc})
    return out


def asset_history(engine, db, asset_id: str) -> dict:
    ids = [r["job_id"] for r in db.q("SELECT job_id FROM jobs WHERE asset_id=? ORDER BY created_at", (asset_id,))]
    jobs = []
    for jid in ids:
        j = engine.get(jid)
        if j is None:
            continue
        jobs.append({"job_id": j["job_id"], "kind": j["kind"], "created_at": j["created_at"], "state": j["state"],
                     "prompts": _job_prompts(j), "attempts": _attempts(j), "outcome": OUTCOME.get(j["state"]),
                     "error_code": j.get("error_code") if j["state"] == "failed" else None})
    jobs.sort(key=lambda x: x["created_at"])
    return {"asset_id": asset_id, "jobs": jobs}
