"""S21 2.6 운영자 화면 `GET /admin`(HTML, 인증 없음 — 사용자 지시). 에이전트 등록 [Register]·평가 목록·조정 기록."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .chain import EXPLORER, IDENTITY, ChainError


def build_admin(st) -> APIRouter:
    r = APIRouter()
    cache: dict = {"t": 0.0}

    def operator() -> dict:
        R = st.engine.ratings
        if R.mock or not R.client.operator_key:
            return {"address": None, "balance_eth": None}
        if time.time() - cache["t"] > 10:
            try:
                a = R.client.operator_address()
                cache.update(t=time.time(), address=a, balance_eth=R.client.balance(a) / 1e18)
            except (ChainError, Exception) as e:              # noqa: BLE001
                cache.update(t=time.time(), address=None, balance_eth=None, error=str(e)[:200])
        return {k: cache.get(k) for k in ("address", "balance_eth", "error")}

    @r.get("/admin/api")
    def admin_api(req: Request):
        R = st.engine.ratings
        rows = R.all_ratings()
        aid = R.agent_id()
        clients = sorted({(o.get("chain") or {}).get("account") for o in rows
                          if (o.get("chain") or {}).get("status") == "confirmed" and (o.get("chain") or {}).get("account")})
        summary = None
        if aid is not None and clients and R.client.status == "on":
            try:
                summary = R.client.summary(aid, clients)
            except ChainError as e:
                summary = {"error": str(e)[:200]}
        return {"chain": R.health(), "operator": operator(), "register": R.reg_status(), "summary": summary,
                "registration_url": "/app/v1/agent-registration.json", "explorer": EXPLORER, "identity": IDENTITY,
                "ratings": [{"rating_id": o["rating_id"], "t": o.get("created_at"), "asset_id": o.get("asset_id"),
                             "stars": o.get("stars"), "review": o.get("review"), "chain": o.get("chain"),
                             "feedback_uri": o.get("feedback_uri"), "tuning": o.get("tuning")} for o in rows],
                "tuning": R.tuning_rows(), "tuning_lines": R.tuning_lines(), "tuning_version": R.tuning_version()}

    @r.post("/admin/agent/register")
    async def admin_register(req: Request):
        again = (req.query_params.get("again") == "1")
        return st.engine.ratings.start_register(str(req.base_url), again=again)

    @r.get("/admin/agent/status")
    def admin_status():
        return st.engine.ratings.reg_status()

    @r.get("/admin", response_class=HTMLResponse)
    def admin_page():
        return PAGE

    return r


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VHL Agent Admin</title><style>
body{font-family:system-ui,sans-serif;margin:16px;color:#222;background:#fafafa}h1{font-size:20px}h2{font-size:16px;margin-top:24px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:8px 0}.big{font-size:32px;font-weight:700}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid #eee;padding:4px;text-align:left;vertical-align:top}
button{font-size:16px;padding:8px 18px;border-radius:6px;border:1px solid #2563eb;background:#2563eb;color:#fff;cursor:pointer}
button.small{font-size:12px;padding:3px 8px;background:#fff;color:#b91c1c;border-color:#b91c1c}.cap{color:#666;font-size:12px}
.ok{color:#15803d}.bad{color:#b91c1c}.mono{font-family:ui-monospace,monospace;font-size:11px;word-break:break-all}</style></head>
<body><h1>VoxHammer-Light 3D Edit Agent — 운영자 화면</h1>
<div class="card" id="reg"></div>
<h2>평가 목록</h2><div class="card" id="sum"></div><div class="card"><table id="rt"></table></div>
<h2>조정 기록(피드백 기반 미세 조정)</h2><div class="card" id="tune"></div>
<script>
const E=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let D=null, fast=false;
function ts(t){return t?new Date(t*1000).toLocaleString():''}
function tx(h){return h?`<a class="mono" target="_blank" href="${D.explorer}/tx/${h}">${h.slice(0,10)}…</a>`:''}
function addr(a){return a?`<a class="mono" target="_blank" href="${D.explorer}/address/${a}">${a.slice(0,8)}…${a.slice(-4)}</a>`:''}
function regView(){const c=D.chain,o=D.operator,g=D.register;let h=`<div>네트워크 <b>Ethereum Sepolia (${c.chain_id})</b> · 체인 상태 <b class="${c.status==='on'?'ok':'bad'}">${E(c.status)}</b> ${c.error?'<span class="bad">'+E(c.error)+'</span>':''}</div>
<div>IdentityRegistry <a class="mono" target="_blank" href="${D.explorer}/address/${D.identity}">${D.identity}</a> · ReputationRegistry <span class="mono">${E(c.reputation_registry)}</span> · 계정 팩토리 ${addr(c.factory)}</div>
<div>운영자 ${addr(o.address)} · 잔액 ${o.balance_eth==null?'-':o.balance_eth.toFixed(5)+' ETH'} · 등록 파일 <a target="_blank" href="${D.registration_url}">agent-registration.json</a></div>`;
 const aid=c.agent_id;
 if(g.state==='sending'||g.state==='pending'){h+=`<div class="big">등록 중…</div><div>${g.state==='sending'?'tx 보내는 중':'tx 보냄 '+tx(g.tx_hash)+' → 블록 대기'}</div>`}
 else if(aid!=null){h+=`<div class="cap">등록됨</div><div class="big">agentId = ${aid}</div><div><a target="_blank" href="https://sepolia.etherscan.io/token/${D.identity}?a=${aid}">토큰 보기</a>${g.tx_hash?' · 등록 tx '+tx(g.tx_hash)+(g.seconds?' · 확정까지 '+g.seconds+' s':''):''}</div>
 <div style="margin-top:8px"><button class="small" onclick="again()">새로 등록</button></div>`}
 else{h+=`<div style="margin-top:10px"><button onclick="reg()">Register</button></div>`}
 if(g.state==='error')h+=`<div class="bad">실패: ${E(g.error)}</div>`;
 const hist=(g.history||[]);if(hist.length)h+=`<div class="cap">기록: ${hist.map(x=>'agentId '+x.agent_id+' ('+ts(x.t)+')').join(' · ')}</div>`;
 document.getElementById('reg').innerHTML=h;}
function rtView(){const s=D.summary;document.getElementById('sum').innerHTML=s?(s.error?'<span class="bad">getSummary 오류 '+E(s.error)+'</span>':`getSummary(agentId, 평가 계정 전부, "starred", "") → 건수 <b>${s.count}</b> · 값 <b>${s.value}</b> (소수 ${s.decimals})`):'<span class="cap">확정된 체인 평가 없음</span>';
 document.getElementById('rt').innerHTML='<tr><th>시각</th><th>자산</th><th>별점</th><th>리뷰</th><th>계정</th><th>user_op_hash</th><th>상태</th><th>tx</th><th>index</th><th>파일</th><th>체인 읽기</th><th>조정</th></tr>'+D.ratings.map(o=>{const c=o.chain||{},rd=c.onchain_read;
 return `<tr><td>${ts(o.t)}</td><td class="mono">${E(o.asset_id)}</td><td>${'★'.repeat(o.stars||0)}</td><td>${E(o.review)}</td><td>${addr(c.account)}</td><td class="mono">${E((c.user_op_hash||'').slice(0,12))}</td><td class="${c.status==='confirmed'?'ok':(c.status==='failed'?'bad':'')}">${E(c.status)}${c.error?'<div class="bad cap">'+E(c.error)+'</div>':''}</td><td>${tx(c.tx_hash)}</td><td>${c.feedback_index??''}</td><td><a target="_blank" href="${o.feedback_uri}">json</a></td><td>${rd?(rd.error?'<span class="bad">'+E(rd.error)+'</span>':(rd.match?'<span class="ok">일치</span>':'<span class="bad">불일치</span>')+' '+rd.value+'·'+rd.decimals+'·'+E(rd.tag1)):''}</td><td class="cap">${o.tuning?(o.tuning.applied?'판 '+o.tuning.version+' '+E(o.tuning.target):E(o.tuning.reason||'none')):''}</td></tr>`}).join('');}
function tuneView(){const L=D.tuning_lines;document.getElementById('tune').innerHTML=`<div>현재 판 <b>${D.tuning_version}</b> · masking ${L.masking.length}줄 · refine ${L.refine.length}줄</div>`+
 `<table><tr><th>판</th><th>대상</th><th>동작</th><th>전</th><th>후</th><th>근거 평가</th><th>시각</th></tr>`+D.tuning.map(t=>`<tr><td>${t.version}</td><td>${E(t.target)}</td><td>${E(t.action)}</td><td class="cap">${E(t.before)}</td><td class="cap">${E(t.after)}</td><td class="mono">${E(t.rating_id)}</td><td>${ts(t.t)}</td></tr>`).join('')+'</table>';}
async function load(){D=await (await fetch('/admin/api')).json();regView();rtView();tuneView();fast=['sending','pending'].includes(D.register.state)}
async function poll(){if(!fast)return;const g=await (await fetch('/admin/agent/status')).json();D.register=g;if(g.agent_id!=null)D.chain.agent_id=g.agent_id;regView();fast=['sending','pending'].includes(g.state);if(!fast)load()}
async function reg(){await fetch('/admin/agent/register',{method:'POST'});fast=true;await load();fast=true}
async function again(){const n=D.ratings.filter(o=>(o.chain||{}).agent_id===D.chain.agent_id&&(o.chain||{}).status==='confirmed').length;
 if(!confirm((n?'⚠ 이 agentId 에 확정된 평가가 '+n+'건 있습니다. ':'')+'새 agentId 를 발급받아 현재 값으로 바꿉니다. 계속할까요?'))return;
 await fetch('/admin/agent/register?again=1',{method:'POST'});fast=true;await load();fast=true}
load();setInterval(poll,1000);setInterval(()=>{if(!fast)load()},10000);
</script></body></html>"""
