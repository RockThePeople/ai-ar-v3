"""`/clients` — 앱 활동 기록 조회(운영자용, 앱 API 아님). Voxhammer-light `ops/tasks/S3-clients-3090.md`.

- 목록 `GET /clients`, 상세 `GET /clients/{asset_id}`. 삭제된 자산(1.0.2 9절 보관본)도 "삭제됨"으로 보인다.
- 인증 없음. 외부 공개(사용자 결정 2026-11-10). `CLIENTS_ALLOW_CIDR`(쉼표로 여러 대역)를 넣으면 그 밖의 요청에 403.
- GLB 는 요청 때 `glbbuild.build_glb` 로 만들고 캐시한다(개수 제한 없음 — 사용자 결정, 용량 무시). 되돌린 편집은 `archive/<job_id>/` 청크를
  부모 판본 위에 얹어 만든다.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from .engine import AppError


def _allowed(req: Request) -> None:
    raw = (os.environ.get("CLIENTS_ALLOW_CIDR") or "").strip()
    if not raw:
        return
    try:
        ip = ipaddress.ip_address(req.client.host if req.client else "0.0.0.0")
    except ValueError:
        raise AppError(403, "NOT_OWNER", "허용 대역 밖")
    nets = [ipaddress.ip_network(x.strip(), strict=False) for x in raw.split(",") if x.strip()]
    if not any(ip in n for n in nets):
        raise AppError(403, "NOT_OWNER", "허용 대역 밖(CLIENTS_ALLOW_CIDR)")


def _dur(log: List[dict], end: Optional[float]) -> Dict[str, float]:
    """stage_log([{t, stage}]) → 단계별 시간 합."""
    out: Dict[str, float] = {}
    for i, e in enumerate(log):
        t1 = log[i + 1]["t"] if i + 1 < len(log) else end
        if t1 is not None:
            out[e["stage"]] = round(out.get(e["stage"], 0.0) + (t1 - e["t"]), 2)
    return out


CROP_IMGS = ("crop_render", "crop_mask", "crop_edit")    # S7 잘라 인페인팅 이미지


def build_clients(S) -> APIRouter:
    r = APIRouter(prefix="/clients")
    data = S.settings.data_dir

    def guard(req: Request) -> None:
        _allowed(req)

    # ── JSON ───────────────────────────────────────────────────────
    def asset_rows() -> List[dict]:
        out = []
        for a in S.db.q("SELECT * FROM assets WHERE is_sample=0 ORDER BY created_at DESC"):
            aid = a["asset_id"]
            jobs = S.db.jobs("asset_id=?", (aid,))
            edits = [j for j in jobs if j["kind"] == "edit"]
            rs = S.db.q("SELECT stars FROM ratings WHERE asset_id=?", (aid,))
            status = "삭제됨" if a["deleted_at"] else ("생성 실패" if a["source"] == "generate_failed" else
                                                       ("생성 중" if a["version"] == 0 else "활성"))
            out.append({"qr_id": a["qr_origin"] or a["qr_id"], "asset_id": aid, "created_at": a["created_at"],
                        "creator": (a["creator"] or "")[:10], "version": a["version"],
                        "commits": sum(1 for j in edits if j["state"] == "committed"), "edits": len(edits),
                        "rating_avg": round(sum(x["stars"] for x in rs) / len(rs), 2) if rs else None,
                        "ratings": len(rs), "status": status, "deleted_at": a["deleted_at"],
                        "worker_kept": bool(a["worker_kept"]) if a["deleted_at"] else None,
                        "scene_prompt": a["scene_prompt"]})
        return out

    def job_view(j: dict) -> dict:
        end = j.get("finished_at") or S.engine.clock()
        res = j.get("result") or {}
        attempts = []
        for a in j.get("attempts", []):
            attempts.append({**{k: a.get(k) for k in ("attempt", "seed", "prompt", "new_scene_prompt", "instruction",
                                                      "render_id", "rejected", "reason", "category", "guidance",
                                                      "crop", "inpaint_s", "refine_retry", "refine_unchanged",
                                                      "inpaint_raw", "style", "render_reused")},
                             "images": {n: f"/clients/img/{j['job_id']}/{a['attempt']}/{n}.png" for n in
                                        ("render", "edit", "mask") + (CROP_IMGS if (a.get("crop") or {}).get("box") else ())}})
        outcome = {"committed": "유지", "discarded": "되돌리기" if "자동" not in (j.get("discard_note") or "") else "자동 폐기",
                   "cancelled": "취소", "failed": "실패", "needs_redraw": "영역 다시"}.get(j["state"], j["state"])
        return {"job_id": j["job_id"], "kind": j["kind"], "state": j["state"], "outcome": outcome,
                "error_code": j.get("error_code"), "error_message": j.get("error_message"),
                "created_at": j["created_at"], "finished_at": j.get("finished_at"),
                "elapsed_s": round(end - j["created_at"], 1), "device": (j.get("device") or "")[:10],
                "instruction": j.get("instruction"), "prompt": j.get("prompt"), "prompt_en": j.get("prompt_en"),
                "translate": j.get("translate"),
                "style": j.get("style"), "t2i_template": j.get("t2i_template"),   # S11 양식 판정(생성)
                "t2i_effective": ((j.get("t2i") or {}).get("timing") or {}).get("prompt_effective"),
                "seed": j.get("seed"), "t2i": bool(j.get("t2i")),
                "refine": j.get("refine"), "camera": j.get("camera"), "n_cells": len(j.get("cells") or []),
                "attempts": attempts, "base_version": j.get("base_version") or res.get("base_version"),
                "to_version": res.get("to_version"), "n_changed": len(res.get("chunks") or []),
                "n_removed": len(res.get("removed_chunk_ids") or []), "archive": bool(j.get("archive")),
                "stage_s": _dur(j.get("stage_log") or [], j.get("finished_at")), "queue_waits": j.get("queue_waits"),
                "history": j.get("history"), "discard_note": j.get("discard_note"),
                "llm_calls": [{k: c[k] for k in ("kind", "model", "ok", "retried", "latency_s", "in_tokens", "out_tokens",
                                                 "error", "input", "output", "attempts")} for c in S.db.llm_calls(j["job_id"])],
                # S9: 실제로 오간 입력 전문(앞으로의 작업부터 기록, 지난 작업은 있는 만큼)
                "raw_generate": j.get("raw_generate"), "raw_edit3d": j.get("raw_edit3d"),
                "render_record": (j.get("render") or {}).get("record"),
                "a5000_trace": j.get("a5000_trace"),            # S10: 접수·폴링 회차(실패 포함)
                "job_json": f"/clients/api/jobs/{j['job_id']}"}

    @r.get("/api/jobs/{job_id}")
    def api_job(job_id: str, req: Request):
        """S9: 작업 기록 전문 + LLM 호출 전문(요청 본문·응답 원문, 회차마다)."""
        guard(req)
        j = S.engine.get(job_id)
        if j is None:
            raise AppError(404, "JOB_NOT_FOUND", job_id)
        return {"job": j, "llm_calls": S.db.llm_calls(job_id)}

    @r.get("/api/assets")
    def api_assets(req: Request):
        guard(req)
        return asset_rows()

    @r.get("/api/assets/{asset_id}")
    def api_asset(asset_id: str, req: Request):
        guard(req)
        a = S.db.asset(asset_id)
        if a is None:
            raise AppError(404, "ASSET_NOT_FOUND", asset_id)
        row = next(x for x in asset_rows() if x["asset_id"] == asset_id)
        jobs = sorted(S.db.jobs("asset_id=?", (asset_id,)), key=lambda j: j["created_at"])
        jobs = [S.engine.get(j["job_id"]) or j for j in jobs]
        try:
            versions = sorted(S.store.versions(asset_id))
        except KeyError:
            versions = []
        ratings = [{"device": (x["device"] or "")[:10], "stars": x["stars"], "version_range": [x["v_from"], x["v_to"]],
                    "created_at": x["created_at"], "feedback_uri": json.loads(x["body"])["feedback_uri"]}
                   for x in S.db.q("SELECT * FROM ratings WHERE asset_id=? ORDER BY created_at", (asset_id,))]
        return {**row, "versions": versions, "jobs": [job_view(j) for j in jobs], "ratings_list": ratings}

    # ── 이미지 ─────────────────────────────────────────────────────
    @r.get("/img/{job_id}/region/{name}.png")
    def region_img(job_id: str, name: str, req: Request):
        """S13: 보정·분류에 보낸 라쏘 겹친 이미지·확대."""
        guard(req)
        if name not in ("overlay", "zoom") or S.engine.get(job_id) is None:
            raise AppError(404, "JOB_NOT_FOUND", job_id)
        p = data / "jobs" / job_id / "region" / f"{name}.png"
        if not p.is_file():
            raise AppError(404, "JOB_NOT_FOUND", "이미지 없음")
        return FileResponse(p, media_type="image/png")

    @r.get("/img/{job_id}/{attempt}/{name}.png")
    def img(job_id: str, attempt: int, name: str, req: Request):
        guard(req)
        if name not in ("render", "edit", "mask") + CROP_IMGS or S.engine.get(job_id) is None:
            raise AppError(404, "JOB_NOT_FOUND", job_id)
        p = data / "jobs" / job_id / str(int(attempt)) / f"{name}.png"
        if not p.is_file():
            raise AppError(404, "JOB_NOT_FOUND", "이미지 없음")
        return FileResponse(p, media_type="image/png")

    @r.get("/img/{job_id}/t2i.png")
    def t2i(job_id: str, req: Request):
        guard(req)
        if S.engine.get(job_id) is None:
            raise AppError(404, "JOB_NOT_FOUND", job_id)
        p = data / "jobs" / job_id / "t2i.png"
        if not p.is_file():
            raise AppError(404, "JOB_NOT_FOUND", "T2I 이미지 없음")
        return FileResponse(p, media_type="image/png")

    # ── GLB (캐시, 개수 제한 없음) ─────────────────────────────────
    def _glb(key: str, blobs: Dict[str, bytes]) -> Path:
        from ..glbbuild import build_glb
        cache = data / "glb_clients"
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / f"{key}.glb"
        if not out.is_file():
            with tempfile.TemporaryDirectory() as td:
                for k, b in blobs.items():
                    (Path(td) / f"{k}.cbin").write_bytes(b)
                build_glb(Path(td), out)
        return out

    @r.get("/glb/{asset_id}/v{version}.glb")
    def glb_version(asset_id: str, version: int, req: Request):
        guard(req)
        try:
            blobs = S.store.blobs(asset_id, version)
        except KeyError as e:
            raise AppError(404, "ASSET_NOT_FOUND", str(e))
        return FileResponse(_glb(f"{S.store._slug(asset_id)}-v{int(version)}", blobs), media_type="model/gltf-binary")

    @r.get("/glb/archive/{job_id}.glb")
    def glb_archive(job_id: str, req: Request):
        guard(req)
        j = S.engine.get(job_id)
        d = data / "archive" / job_id
        if j is None or not d.is_dir():
            raise AppError(404, "JOB_NOT_FOUND", "되돌린 결과 보관본 없음")
        blobs = dict(S.store.blobs(j["asset_id"], int(j["base_version"])))
        for k in (j.get("result") or {}).get("removed_chunk_ids") or []:
            blobs.pop(k, None)
        for p in d.glob("*.cbin"):
            blobs[p.stem] = p.read_bytes()
        return FileResponse(_glb(f"archive-{job_id}", blobs), media_type="model/gltf-binary")

    # ── HTML ───────────────────────────────────────────────────────
    @r.get("")
    def page_list(req: Request):
        guard(req)
        return HTMLResponse(LIST_PAGE)

    @r.get("/{asset_id}")
    def page_detail(asset_id: str, req: Request):
        guard(req)
        if S.db.asset(asset_id) is None:
            raise AppError(404, "ASSET_NOT_FOUND", asset_id)
        return HTMLResponse(DETAIL_PAGE)

    return r


_STYLE = """<style>
body{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
h1{font-size:18px} h2{font-size:16px;margin:18px 0 6px} table{border-collapse:collapse;width:100%;background:#fff}
td,th{border-bottom:1px solid #eee;padding:4px 6px;text-align:left;font-size:13px;vertical-align:top}
tr.row:hover{background:#f3f7ff;cursor:pointer} .del{color:#999} .bad{color:#b00} .ok{color:#070}
input{padding:4px 8px;width:260px} details{background:#fff;border:1px solid #ddd;margin:6px 0;padding:6px}
summary{cursor:pointer;font-weight:600} pre{white-space:pre-wrap;font:12px monospace;background:#f6f6f6;padding:6px;max-height:260px;overflow:auto}
.imgs img{width:31%;margin-right:1%;border:1px solid #ccc;background:#000} .views{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.view{height:360px;background:#223} .cap{font-size:12px;color:#555} details.raw{margin:3px 0;padding:3px 6px} details.raw summary{font-weight:400;font-size:12px} details.raw pre{max-height:520px} @media(max-width:900px){.views{grid-template-columns:1fr}}
</style>"""

LIST_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>앱 활동 기록</title>""" + _STYLE + """</head><body>
<h1>앱 활동 기록 <small>(QR·자산 단위, 최신순)</small></h1>
<input id="q" placeholder="qr_id 또는 asset_id 검색"> <span id="n"></span>
<table><thead><tr><th>qr_id</th><th>asset_id</th><th>생성 시각</th><th>생성자</th><th>판본</th><th>편집(커밋/전체)</th><th>평가</th><th>상태</th><th>장면 문장</th></tr></thead><tbody id="tb"></tbody></table>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const ts=t=>t?new Date(t*1000).toLocaleString('ko-KR'):'';let rows=[];
function draw(){const q=document.getElementById('q').value.trim();const f=rows.filter(r=>!q||String(r.qr_id).includes(q)||r.asset_id.includes(q));
 document.getElementById('n').textContent=f.length+'행';
 document.getElementById('tb').innerHTML=f.map(r=>`<tr class="row ${r.status==='삭제됨'?'del':''}" onclick="location='/clients/${r.asset_id}'"><td>${esc(r.qr_id)}</td><td>${r.asset_id}</td><td>${ts(r.created_at)}</td><td>${esc(r.creator)}</td><td>${r.version}</td><td>${r.commits} / ${r.edits}</td><td>${r.rating_avg??'-'} (${r.ratings})</td><td class="${r.status==='활성'?'ok':(r.status==='삭제됨'?'':'bad')}">${r.status}${r.deleted_at?(r.worker_kept?' <span class="cap">(A5000 보관)</span>':' <span class="cap">(A5000 저장소 지워짐)</span>'):''}</td><td>${esc(r.scene_prompt)}</td></tr>`).join('');}
fetch('/clients/api/assets').then(r=>r.json()).then(d=>{rows=d;draw()});document.getElementById('q').oninput=draw;
</script></body></html>"""

DETAIL_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자산 기록</title>""" + _STYLE + """</head><body>
<p><a href="/clients">← 목록</a></p><h1 id="title"></h1><div id="meta"></div>
<h2>생성</h2><div id="gen"></div>
<h2>3D 비교 <small class="cap">(카메라 공유)</small></h2>
<div><label>왼쪽 <select id="selA"></select></label> <label>오른쪽 <select id="selB"></select></label> <button id="cam">라쏘 카메라로 맞추기</button> <span id="camsrc" class="cap"></span></div>
<div class="views"><div><div id="vA" class="view"></div><div id="cA" class="cap"></div></div><div><div id="vB" class="view"></div><div id="cB" class="cap"></div></div></div>
<h2>편집</h2><div id="edits"></div>
<h2>평가</h2><div id="rates"></div>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function cropView(a){const c=a.crop;if(!c)return '';if(c.off)return '<div class="cap">잘라 인페인팅 꺼짐(APP_INPAINT_CROP=0)</div>';if(!c.box)return `<div class="cap">자르지 않음: ${esc(c.reason||'')}</div>`;
return `<div class="cap">잘라 인페인팅(S7): 상자 x=${c.box[0]} y=${c.box[1]} 한 변 ${c.box[2]} px → 1024² (×${c.scale}) · 마스크 상자 ${c.mask_bbox.join(',')} · 마스크 ${c.mask_px} px → 보낸 ${c.mask_px_sent} px · 자르기 ${c.pre_s}s · 붙이기 ${c.post_s}s · 인페인팅 전체 ${a.inpaint_s}s</div><div class="imgs">${['crop_render','crop_mask','crop_edit'].map(n=>`<a href="${a.images[n]}" target="_blank"><img src="${a.images[n]}" title="${n}"></a>`).join('')}</div><div class="cap">[자른 입력 | 자른 마스크 | 자른 결과(워커 출력)]</div>`}
const J1=o=>JSON.stringify(o,null,1).replace(/\\[\\s+([-\\d.,\\s]+?)\\s+\\]/g,(m,a)=>'['+a.replace(/\\s+/g,'')+']');  // 숫자 배열은 한 줄로
const RAW=(t,o)=>o==null?'':`<details class="raw"><summary>${t}</summary><pre>${esc(typeof o==='string'?o:J1(o))}</pre></details>`;
const thumb=(u,t)=>`<a href="${u}" target="_blank"><img src="${u}" title="${t}"></a>`;
function llmRaw(c){const at=c.attempts||[];if(!at.length)return '<span class="cap">(전문 기록 없음 — S9 이전 또는 가짜 LLM)</span>';
 return at.map(a=>{const q=a.request;if(!q)return `<div class="cap">회차 ${a.attempt}: 전문 없음(S9 이전) · stop ${esc(a.stop_reason)} · 원문 ${esc(a.text)}</div>`;
  const msgs=q.messages.map(m=>`[${m.role}]\\n${typeof m.content==='string'?m.content:JSON.stringify(m.content)}`).join('\\n\\n');
  const n=(q.system||'').length+q.messages.reduce((x,m)=>x+(typeof m.content==='string'?m.content.length:JSON.stringify(m.content).length),0);
  const u=(a.response||{}).usage||{};
  return `<div class="cap"><b>회차 ${a.attempt}</b> · stop_reason ${esc(a.stop_reason)} · usage in ${u.input_tokens??'?'} / out ${u.output_tokens??'?'} · 보낸 system+messages ${n}자${a.problem?' · 위반: '+esc(a.problem):''}</div>`+
   RAW((c.kind||'').startsWith('refine')?'보정 템플릿 — system 프롬프트 전문'+(((c.input||{}).relphrase)?' (S12 관계어)':''):'system 프롬프트 전문',q.system)+RAW('messages 전문(템플릿이 채워진 그대로)',msgs)+
   RAW('요청 본문 전체 JSON(model·max_tokens·output_config 포함, 키 없음)',q)+RAW('받은 응답 원문(파싱 전 content·stop_reason·usage)',a.response)+RAW('클라이언트 설정',a.client);}).join('');}
function llmTable(cs){return `<details><summary>LLM 호출 ${cs.length}건</summary><table><tr><th>종류</th><th>모델</th><th>성공</th><th>재시도</th><th>지연</th><th>토큰</th><th>입력(정리본)</th><th>출력(정리본)</th></tr>${cs.map(c=>`<tr><td>${c.kind}</td><td>${c.model}</td><td>${c.ok?'예':'아니오 '+esc(c.error)}</td><td>${c.retried?'예':''}</td><td>${c.latency_s}s</td><td>${c.in_tokens}/${c.out_tokens}</td><td><pre>${esc(JSON.stringify(c.input))}</pre></td><td><pre>${esc(JSON.stringify(c.output))}</pre></td></tr><tr><td colspan="8">${llmRaw(c)}</td></tr>`).join('')}</table></details>`;}
function inpRaw(a,j){const r=a.inpaint_raw;if(!r)return '<div class="cap">(인페인팅 요청 전문 기록 없음 — S9 이전)</div>';
 return `<div class="cap">인페인팅 요청: 문장 "${esc((r.form||{}).prompt)}" · seed ${esc((r.form||{}).seed)} · 워커 설정 ${esc(JSON.stringify(r.settings))} <i>(출처: ${esc(r.settings_source||'-')})</i></div>`+RAW('인페인팅 요청·응답 전문',r);}
function regionView(st,jid,a){if(!st||!st.region)return '';const iss=x=>(x||[]).map(v=>({subject_changed:'주어 바뀜',placement:'새 부위 자리'}[v]||v)).join(', ');
 const fit={ok:'ok',too_small:'<span class="bad">too_small</span>',wrong_part:'<span class="bad">wrong_part</span>'}[st.region_fit]||esc(st.region_fit);
 return `<div class="cap">라쏘 이미지(S13): mask_ratio ${st.mask_ratio??'-'} · selected_part <b>${esc(st.selected_part||'-')}</b> · region_fit ${fit} — ${esc(st.region_reason||'')}${(st.region_found||[]).length?` · 검사 ${esc(iss(st.region_found))} → 재요청 1회${(st.region_left||[]).length?` → <span class="bad">남음 ${esc(iss(st.region_left))}</span>`:' → 해결'}`:''}${a&&a.render_reused?' · 조건 렌더 재사용':''}</div><div class="imgs">${(st.images||[]).map(n=>thumb(`/clients/img/${jid}/region/${n}.png`,'보정에 보낸 '+n)).join('')}</div>`;}
function relView(st){if(!st||!st.relphrase)return '';const f=st.imperative_found||[],l=st.imperative_left||[];
 return `<div class="cap">보정 템플릿: S12 관계어(wearing·holding·with)${f.length?` · 명령형 ${esc(f.join(', '))} → 재요청 1회${l.length?` → <span class="bad">남음 ${esc(l.join(', '))}</span>`:' → 사라짐'}`:' · 명령형 없음'}</div>`;}
function styleView(st,gen){if(st&&!gen&&st.relphrase&&!st.mode)return relView(st);if(!st)return `<div class="cap">양식 판정: 기록 없음${gen?' (S11 이전 생성 또는 APP_REALSTYLE=0)':' (S11 이전·끔) → 기본 실물형으로 봄'}</div>`;
 const pre=st.style_source==='pre_s11_request'?' <span class="cap">(S11 이전 자산 — 생성 원문 기준)</span>':'';
 const m=(st.mode==='user'?`사용자 지정: <b>${esc(st.words||(gen?'?':'자산 기록'))}</b> → <b>${esc(st.style)}</b>`:'기본 실물형')+pre;
 const f=(st.banned_found||[]).length?` · 금지어 ${esc((st.banned_found||[]).join(', '))} → 재요청 1회${(st.removed||[]).length?` → 남은 <span class="bad">${esc(st.removed.join(', '))}</span> 삭제`:' → 사라짐'}`:(st.mode==='user'?'':' · 금지어 없음');
 return `<div>양식 판정: ${m}${f}${st.fallback?' <span class="cap">(LLM 실패·상세화 미지원 → 실물형)</span>':''}${st.retry_error?` <span class="bad">재요청 실패 ${esc(st.retry_error)}</span>`:''}</div>`;}
function trView(t,label){if(!t)return '';const s=t.submit||{},ps=t.polls||[],r=t.result||{},t0=s.t||(ps[0]||{}).t||0;
 const rows=ps.map(p=>`<tr${p.error||p.retry_wait_s?' class="bad"':''}><td>${p.n}</td><td>+${(p.t-t0).toFixed(2)}s</td><td>${p.status??''}</td><td>${esc(p.error||'')}${p.body?' '+esc(JSON.stringify(p.body)):''}</td><td>${esc(p.state||'')}${p.stage?' / '+esc(p.stage):''}${p.progress!=null?' '+p.progress:''}</td><td>${p.retry_wait_s?p.retry_wait_s+'s 뒤 재시도':''}${p.recovered_after_s?'회복('+p.recovered_after_s+'s 끊김)':''}</td></tr>`).join('');
 return `<details class="raw"${ps.some(p=>p.error||p.retry_wait_s)||r.failed?' open':''}><summary>A5000 ${label} 접수·폴링 ${ps.length}회${ps.filter(p=>p.retry_wait_s).length?' · 재시도 '+ps.filter(p=>p.retry_wait_s).length+'회':''}${r.failed?' · <span class="bad">실패 '+esc(r.failed)+'</span>':''}</summary><div class="cap">접수 ${s.t?ts(s.t):'-'} · HTTP ${s.status??'-'} · ${esc(JSON.stringify(s.response||''))}</div><table><tr><th>회</th><th>시각(접수 기준)</th><th>HTTP</th><th>오류</th><th>A5000 상태</th><th>재시도</th></tr>${rows}</table></details>`;}
function e3Raw(j){const r=j.raw_edit3d;if(!r)return '';const n=r.attempt,b=`/clients/img/${j.job_id}/${n}/`;
 return `<div><b>3D 편집 입력</b> (A5000 /v2/trellis/edit, 시도 ${n} 승인본) · ${esc(r.text||'')}<div class="imgs">${thumb(b+'render.png','2d_render')}${thumb(b+'mask.png','2d_mask')}${thumb(b+'edit.png','2d_edit')}</div><div class="cap">[2d_render | 2d_mask | 2d_edit]</div>`+RAW('meta JSON 전문',r.meta)+RAW('보낸 파일(이름·바이트·sha256)',r.files)+RAW('A5000 접수 응답',r.submit_response)+RAW('A5000 작업 결과 전문(설정·단계 시간·청크)',r.job_result)+`</div>`;}
const ts=t=>t?new Date(t*1000).toLocaleString('ko-KR'):'';const aid=location.pathname.split('/').pop();
// NORMALIZED(복셀 축) → three 월드: glbbuild 의 VOXEL_TO_GLB 와 같은 (x, z, −y). common.js:voxelToWorld 에서 xf 가 항등인 경우.
const toW=v=>new THREE.Vector3(v[0],v[2],-v[1]);
let D, cam, views=[], lassoCam=null;
function mkView(el){const r=new THREE.WebGLRenderer({antialias:true});r.setSize(el.clientWidth,el.clientHeight);el.appendChild(r.domElement);
 const s=new THREE.Scene();s.background=new THREE.Color(0x223344);s.add(new THREE.HemisphereLight(0xffffff,0x444444,1.1));const dl=new THREE.DirectionalLight(0xffffff,0.8);dl.position.set(2,3,1);s.add(dl);
 return {r,s,el,obj:null};}
function init3d(){const a=document.getElementById('vA');cam=new THREE.PerspectiveCamera(45,a.clientWidth/a.clientHeight,0.01,100);cam.position.set(1.2,0.9,1.4);
 views=[mkView(a),mkView(document.getElementById('vB'))];const ctl=views.map(v=>new THREE.OrbitControls(cam,v.r.domElement));
 ctl.forEach(c=>c.addEventListener('change',()=>ctl.forEach(o=>{if(o!==c)o.target.copy(c.target)})));window.ctl=ctl;
 (function loop(){requestAnimationFrame(loop);views.forEach(v=>v.r.render(v.s,cam));})();}
function load(i,url,cap){const v=views[i];if(!v)return;document.getElementById(i?'cB':'cA').textContent=cap+' 불러오는 중…';
 new THREE.GLTFLoader().load(url,g=>{if(v.obj)v.s.remove(v.obj);v.obj=g.scene;v.obj.traverse(o=>{if(o.isMesh){if(!o.geometry.attributes.normal)o.geometry.computeVertexNormals();o.material=new THREE.MeshStandardMaterial({vertexColors:!!o.geometry.attributes.color,roughness:0.85,metalness:0,side:THREE.DoubleSide})}});v.s.add(v.obj);document.getElementById(i?'cB':'cA').textContent=cap},undefined,e=>document.getElementById(i?'cB':'cA').textContent=cap+' 실패');}
function opts(){const o=D.versions.map(v=>({k:'v'+v,url:`/clients/glb/${aid}/v${v}.glb`,cap:`v${v}`}));
 D.jobs.filter(j=>j.archive).forEach(j=>o.push({k:'ar-'+j.job_id,url:`/clients/glb/archive/${j.job_id}.glb`,cap:`되돌린 결과 ${j.job_id} (v${j.base_version} 위)`,job:j}));return o;}
function pick(i){const o=opts().find(x=>x.k===document.getElementById(i?'selB':'selA').value);if(o)load(i,o.url,o.cap);}
function setLasso(j){lassoCam=j&&j.camera;document.getElementById('camsrc').textContent=lassoCam?('작업 '+j.job_id+' 의 camera'):'';}
document.getElementById('cam').onclick=()=>{if(!lassoCam)return;const c=lassoCam,p=toW(c.position),f=toW(c.forward),u=toW(c.up);
 cam.position.copy(p);cam.up.copy(u.normalize());const t=p.clone().add(f.normalize());cam.lookAt(t);cam.fov=c.fov_y_deg;cam.updateProjectionMatrix();window.ctl.forEach(o=>{o.target.copy(t);o.update()});};
function compare(j){const A=document.getElementById('selA'),B=document.getElementById('selB');A.value='v'+j.base_version;B.value=j.archive?'ar-'+j.job_id:('v'+(j.to_version||j.base_version));pick(0);pick(1);setLasso(j);document.getElementById('cam').click();window.scrollTo(0,document.getElementById('vA').offsetTop-80);}
fetch('/clients/api/assets/'+aid).then(r=>r.json()).then(d=>{D=d;
 document.getElementById('title').textContent=`${d.asset_id} · QR ${d.qr_id??'-'} · ${d.status}`;
 document.getElementById('meta').innerHTML=`생성 ${ts(d.created_at)} · 생성자 ${esc(d.creator)} · 판본 ${d.version} · 편집 ${d.commits}/${d.edits} · 평가 ${d.rating_avg??'-'} (${d.ratings})${d.deleted_at?' · 삭제 '+ts(d.deleted_at):''}<br>장면 문장: ${esc(d.scene_prompt)}`;
 const g=d.jobs.find(j=>j.kind==='generate');
 document.getElementById('gen').innerHTML=g?`<div>원문: <b>${esc(g.prompt)}</b> · seed ${g.seed} <span class="cap">(${g.translate?(g.translate.skipped?'영어라 그대로':(g.translate.ok?'LLM '+(g.translate.mode==='detail'?'상세화':(g.translate.mode==='detail_style'?'상세화+양식':'변환')):'LLM 실패 → 원문 사용: '+esc(g.translate.error))):'기록 없음'})</span></div><div>→ subject_detailed: ${esc(g.prompt_en)}</div>${styleView(g.style,true)}<div>→ 실제 T2I 문장: <span class="cap">${esc(g.t2i_effective||'(기록 없음 — S6 이전 생성)')}</span></div><div> · ${g.outcome}${g.error_code?' '+g.error_code:''}</div><div class="cap">단계 시간 ${esc(JSON.stringify(g.stage_s))} · GPU 대기 ${esc(JSON.stringify(g.queue_waits))}</div>${g.t2i?`<img src="/clients/img/${g.job_id}/t2i.png" style="width:240px;border:1px solid #ccc;background:#ccc">`:'<div class="cap">(T2I 이미지 없음 — 가져온 자산이거나 기록 전 생성)</div>'}${llmTable(g.llm_calls)}${g.raw_generate?RAW('T2I 실제 명령·seed·설정(스크립트에서 읽음)',g.raw_generate.t2i)+RAW('A5000 /generate 요청·결과 전문',g.raw_generate.a5000_generate)+trView((g.a5000_trace||{}).generate,'/generate'):trView((g.a5000_trace||{}).generate,'/generate')+'<div class="cap">(생성 요청 전문 기록 없음 — S9 이전)</div>'}<div class="cap"><a href="${g.job_json}" target="_blank">작업 JSON 전문</a></div>`:'<div class="cap">생성 작업 기록 없음(가져온 자산)</div>';
 try{init3d()}catch(e){document.querySelector('.views')?.insertAdjacentHTML('beforebegin','<div class="cap bad">3D 보기를 열 수 없다(WebGL 없음): '+esc(e.message)+'</div>')}const o=opts();['selA','selB'].forEach((id,i)=>{const s=document.getElementById(id);s.innerHTML=o.map(x=>`<option value="${x.k}">${esc(x.cap)}</option>`).join('');s.onchange=()=>pick(i);});
 if(o.length){document.getElementById('selA').value=o[0].k;document.getElementById('selB').value=o[o.length-1].k;pick(0);pick(1);}
 const qj=new URLSearchParams(location.search).get('job');
 document.getElementById('edits').innerHTML=d.jobs.filter(j=>j.kind==='edit').map(j=>`<details><summary>${ts(j.created_at)} · ${esc(j.instruction)} → <span class="${j.outcome==='유지'?'ok':(j.outcome==='실패'?'bad':'')}">${j.outcome}${j.error_code?' '+j.error_code:''}</span> · v${j.base_version}${j.to_version?' → v'+j.to_version:''} · 바뀐 ${j.n_changed} · 제거 ${j.n_removed}</summary>
  <div>보정: ${j.refine?`target <b>${esc(j.refine.target_element)}</b> · replacement <b>${esc(j.refine.replacement)}</b> · op ${esc(j.refine.op)}<br>new_scene_prompt: ${esc(j.refine.new_scene_prompt)}`:'-'}</div>
  <div class="cap">볼륨 셀 ${j.n_cells} · 기기 ${esc(j.device)} · 경과 ${j.elapsed_s}s · 단계 시간 ${esc(JSON.stringify(j.stage_s))} · GPU 대기 ${esc(JSON.stringify(j.queue_waits))}${j.discard_note?' · '+esc(j.discard_note):''}</div>
  ${(j.to_version||j.archive)?`<button onclick='compare(D.jobs.find(x=>x.job_id==="${j.job_id}"))'>이 편집 3D 비교</button>`:''}
  ${j.attempts.map(a=>`<div><b>시도 ${a.attempt}</b> · seed ${a.seed}${a.refine_retry?(a.refine_unchanged?' · <span class="bad">재보정 2회 모두 같은 문장(refine_unchanged)</span>':' · 같은 문장이라 재보정 1회 더 → 바뀜'):''}<div class="cap">지시 원문: ${esc(a.instruction||j.instruction)} → new_scene_prompt: ${esc(a.new_scene_prompt||'(기록 없음)')} → 실제 인페인팅 문장: ${esc(a.prompt)}</div>${styleView(a.style,false)}${a.style&&a.style.mode?relView(a.style):''}${regionView(a.style,j.job_id,a)}${a.rejected?` · 거절: "${esc(a.reason)}" → ${a.category} (${esc(a.guidance)})`:''}<div class="imgs">${['render','edit','mask'].map(n=>thumb(a.images[n],n)).join('')}</div>${cropView(a)}${inpRaw(a,j)}</div>`).join('')}
  <details><summary>camera JSON</summary><pre>${esc(JSON.stringify(j.camera,null,1))}</pre></details>
  ${llmTable(j.llm_calls)}
  ${RAW('A5000 렌더 요청·응답 전문(마지막 렌더)',j.render_record)}
  ${e3Raw(j)}
  ${trView((j.a5000_trace||{}).edit3d,'/edit')}
  <div class="cap"><a href="${j.job_json}" target="_blank">작업 JSON 전문</a></div>
  <details><summary>상태 이력</summary><pre>${esc(JSON.stringify(j.history,null,1))}</pre></details></details>`).join('')||'<div class="cap">편집 없음</div>';
 if(qj){const jj=d.jobs.find(x=>x.job_id===qj);if(jj)setTimeout(()=>compare(jj),300);}
 document.getElementById('rates').innerHTML=d.ratings_list.length?`<table><tr><th>기기</th><th>별점</th><th>version_range</th><th>시각</th><th>피드백</th></tr>${d.ratings_list.map(x=>`<tr><td>${esc(x.device)}</td><td>${x.stars}</td><td>${x.version_range}</td><td>${ts(x.created_at)}</td><td><a href="${x.feedback_uri}">파일</a></td></tr>`).join('')}</table>`:'<div class="cap">평가 없음</div>';
});
</script></body></html>"""
