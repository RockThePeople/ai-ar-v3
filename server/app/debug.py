"""`GET /app/v1/debug` — 사용자 육안 확인 페이지(인증 없음, X-Device-Key 검사 안 함, S1 지시 9).

작업 목록·상태 이력·보정 결과·시도별 미리보기, 자산 목록·현재 판본 3D(GLB 는 `glbbuild` 로 만든다).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from .engine import AppError


def build_debug(S) -> APIRouter:
    r = APIRouter(prefix="/app/v1/debug")

    @r.get("")
    def page():
        return HTMLResponse(PAGE)

    @r.get("/api/jobs")
    def jobs():
        out = []
        for j in S.db.jobs(limit=50):
            j = S.engine.get(j["job_id"]) or j
            end = j.get("finished_at") or S.engine.clock()
            out.append({k: j.get(k) for k in ("job_id", "kind", "asset_id", "qr_id", "state", "stage", "error_code",
                                              "error_message", "attempt", "created_at")} |
                       {"elapsed_s": round(end - j["created_at"], 1)})
        return out

    @r.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = S.engine.get(job_id)
        if j is None:
            raise AppError(404, "JOB_NOT_FOUND", job_id)
        res = j.get("result") or {}
        return {k: j.get(k) for k in ("job_id", "kind", "asset_id", "state", "stage", "history", "refine", "review",
                                      "attempts", "error_code", "error_message", "instruction", "prompt",
                                      "discard_note", "edit_stats", "base_version")} | {
            "n_result_chunks": len(res.get("chunks") or []), "n_removed": len(res.get("removed_chunk_ids") or []),
            "to_version": res.get("to_version"), "device": (j.get("device") or "")[:10] + "…",
            "previews": [a["attempt"] for a in j.get("attempts", [])]}

    @r.get("/api/assets")
    def assets():
        return [dict(a) | {"creator": (a["creator"] or "")[:10]} for a in
                S.db.q("SELECT asset_id,qr_id,creator,scene_prompt,version,is_sample,sample_name FROM assets "
                       "ORDER BY is_sample, created_at DESC")]

    @r.get("/glb/{asset_id}/v{version}.glb")
    def glb(asset_id: str, version: int):
        from ..glbbuild import build_glb
        cache = S.settings.data_dir / "glb"
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / f"{S.store._slug(asset_id)}-v{int(version)}.glb"
        if not out.is_file():
            try:
                blobs = S.store.blobs(asset_id, version)
            except KeyError as e:
                raise AppError(404, "ASSET_NOT_FOUND", str(e))
            with tempfile.TemporaryDirectory() as td:
                for k, b in blobs.items():
                    (Path(td) / f"{k}.cbin").write_bytes(b)
                build_glb(Path(td), out)
        return FileResponse(out, media_type="model/gltf-binary")

    return r


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>앱 서버 디버그</title>
<style>
body{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
h2{margin:18px 0 6px;font-size:16px} table{border-collapse:collapse;width:100%;background:#fff}
td,th{border-bottom:1px solid #eee;padding:4px 6px;text-align:left;font-size:13px;vertical-align:top}
tr.sel{background:#eef4ff} tr:hover{background:#f3f7ff;cursor:pointer}
.st-failed{color:#b00}.st-committed{color:#070}.st-awaiting_2d_review,.st-awaiting_3d_review{color:#a60}
#detail{background:#fff;border:1px solid #ddd;padding:8px;margin-top:8px;white-space:pre-wrap;font:12px monospace;max-height:320px;overflow:auto}
.pv img{width:32%;margin-right:1%;border:1px solid #ccc;background:#000}
#view{width:100%;height:420px;background:#223;margin-top:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px} @media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1 style="font-size:18px">앱 서버 디버그 <small id="hl"></small></h1>
<div class="grid"><div>
<h2>작업 (최근 50)</h2><table id="jobs"><thead><tr><th>작업</th><th>종류</th><th>QR</th><th>상태</th><th>단계</th><th>경과 s</th><th>오류</th></tr></thead><tbody></tbody></table>
<div id="pv" class="pv"></div><div id="detail">작업을 누르면 이력·보정·미리보기가 나온다</div>
</div><div>
<h2>자산</h2><table id="assets"><thead><tr><th>자산</th><th>QR</th><th>판본</th><th>장면 문장</th></tr></thead><tbody></tbody></table>
<div id="view"></div><div id="vinfo"></div>
</div></div>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const B='/app/v1';const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(u){const r=await fetch(u);return r.json()}
async function load(){
 const h=await j(B+'/health');document.getElementById('hl').textContent=`app_api ${h.app_api} · mock ${h.mock} · a5000 ${h.workers.a5000} · inpaint ${h.workers.inpaint} · llm ${h.workers.llm}`;
 const js=await j(B+'/debug/api/jobs');document.querySelector('#jobs tbody').innerHTML=js.map(x=>`<tr onclick="job('${x.job_id}')"><td>${x.job_id}</td><td>${x.kind}</td><td>${esc(x.qr_id)}</td><td class="st-${x.state}">${x.state}</td><td>${x.stage}</td><td>${x.elapsed_s}</td><td>${esc(x.error_code)}</td></tr>`).join('');
 const as=await j(B+'/debug/api/assets');document.querySelector('#assets tbody').innerHTML=as.map(a=>`<tr onclick="view('${a.asset_id}',${a.version})"><td>${a.asset_id}${a.is_sample?' (샘플)':''}</td><td>${esc(a.qr_id)}</td><td>${a.version}</td><td>${esc(a.scene_prompt)}</td></tr>`).join('');
}
async function job(id){const d=await j(B+'/debug/api/jobs/'+id);
 document.getElementById('pv').innerHTML=d.previews.map(a=>`<div>시도 ${a}</div>`+['render','edit','mask'].map(n=>`<img src="${B}/jobs/${id}/preview/${a}/${n}.png" title="${n}">`).join('')).join('');
 document.getElementById('detail').textContent=JSON.stringify(d,null,1);}
let R,S,C,O,M;
function init3d(){const el=document.getElementById('view');R=new THREE.WebGLRenderer({antialias:true});R.setSize(el.clientWidth,el.clientHeight);el.appendChild(R.domElement);
 S=new THREE.Scene();S.background=new THREE.Color(0x223344);C=new THREE.PerspectiveCamera(40,el.clientWidth/el.clientHeight,0.01,100);C.position.set(1.2,0.9,1.4);
 S.add(new THREE.HemisphereLight(0xffffff,0x444444,1.1));const dl=new THREE.DirectionalLight(0xffffff,0.8);dl.position.set(2,3,1);S.add(dl);
 O=new THREE.OrbitControls(C,R.domElement);(function a(){requestAnimationFrame(a);O.update();R.render(S,C)})();}
function view(id,v){if(!v){document.getElementById('vinfo').textContent='판본 없음';return}if(!R)init3d();document.getElementById('vinfo').textContent=`${id} v${v} 불러오는 중…`;
 new THREE.GLTFLoader().load(`${B}/debug/glb/${id}/v${v}.glb`,g=>{if(M)S.remove(M);M=g.scene;M.traverse(o=>{if(o.isMesh){if(!o.geometry.attributes.normal)o.geometry.computeVertexNormals();o.material=new THREE.MeshStandardMaterial({vertexColors:!!o.geometry.attributes.color,roughness:0.85,metalness:0,side:THREE.DoubleSide})}});S.add(M);document.getElementById('vinfo').textContent=`${id} v${v}`},undefined,e=>document.getElementById('vinfo').textContent='GLB 실패: '+e);}
load();setInterval(load,3000);
const P=new URLSearchParams(location.search);if(P.get('job'))job(P.get('job'));if(P.get('asset'))view(P.get('asset'),+P.get('v')||1);
</script></body></html>"""

__all__ = ["build_debug", "json"]
