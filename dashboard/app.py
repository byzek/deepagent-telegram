"""Local metrics dashboard.

Read-only view over the agent's Postgres metrics tables. No privileged access:
it never touches the Docker socket or other containers' /proc — the agent writes
network samples into the DB and this just visualizes them.

Served on the internal port 8000; compose publishes it to 127.0.0.1 only.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "pgvector")

_pool: AsyncConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
    await _pool.open()
    yield
    await _pool.close()


app = FastAPI(title="deepagent dashboard", lifespan=lifespan)

_AGG = """
    count(*)                        AS prompts,
    coalesce(sum(prompt_tokens),0)  AS prompt_tokens,
    coalesce(sum(completion_tokens),0) AS completion_tokens,
    coalesce(sum(total_tokens),0)   AS total_tokens,
    coalesce(sum(llm_seconds),0)    AS llm_seconds,
    coalesce(avg(nullif(tokens_per_sec,0)),0) AS avg_tokens_per_sec
"""


async def _rows(sql: str, params=()):
    async with _pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


def _num(v):
    return float(v) if v is not None else 0.0


async def _network():
    rows = await _rows(
        "SELECT ts, rx_bytes, tx_bytes, rx_packets, tx_packets "
        "FROM metrics_net ORDER BY ts DESC LIMIT 120"
    )
    rows = list(reversed(rows))  # ascending
    history = []
    for prev, cur in zip(rows, rows[1:]):
        dt = (cur["ts"] - prev["ts"]).total_seconds()
        if dt <= 0:
            continue

        def rate(a, b):
            d = b - a
            return d / dt if d >= 0 else 0.0  # ignore counter resets

        history.append(
            {
                "t": cur["ts"].isoformat(),
                "rx_bps": rate(prev["rx_bytes"], cur["rx_bytes"]),
                "tx_bps": rate(prev["tx_bytes"], cur["tx_bytes"]),
                "rx_pps": rate(prev["rx_packets"], cur["rx_packets"]),
                "tx_pps": rate(prev["tx_packets"], cur["tx_packets"]),
            }
        )
    latest = rows[-1] if rows else None
    last = history[-1] if history else {"rx_bps": 0, "tx_bps": 0, "rx_pps": 0, "tx_pps": 0}
    return {
        "since_start": {
            "rx_bytes": latest["rx_bytes"] if latest else 0,
            "tx_bytes": latest["tx_bytes"] if latest else 0,
            "rx_packets": latest["rx_packets"] if latest else 0,
            "tx_packets": latest["tx_packets"] if latest else 0,
        },
        "rate": {
            "rx_bps": last["rx_bps"],
            "tx_bps": last["tx_bps"],
            "rx_pps": last["rx_pps"],
            "tx_pps": last["tx_pps"],
        },
        "history": history,
    }


@app.get("/api/stats")
async def stats():
    totals = (await _rows(f"SELECT {_AGG} FROM metrics_llm"))[0]
    per_user = await _rows(
        f"SELECT user_id, {_AGG} FROM metrics_llm GROUP BY user_id ORDER BY total_tokens DESC"
    )
    per_backend = await _rows(
        f"SELECT memory_backend, {_AGG} FROM metrics_llm GROUP BY memory_backend ORDER BY total_tokens DESC"
    )
    per_model = await _rows(
        f"SELECT model, {_AGG} FROM metrics_llm GROUP BY model ORDER BY total_tokens DESC"
    )
    return JSONResponse(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "memory_backend_current": MEMORY_BACKEND,
            "totals": totals,
            "per_user": per_user,
            "per_backend": per_backend,
            "per_model": per_model,
            "network": await _network(),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_HTML)


_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>deepagent dashboard</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#58a6ff;--acc2:#3fb950}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
  header h1{font-size:16px;margin:0}
  header .mut{color:var(--mut);font-size:12px}
  main{padding:24px;display:grid;gap:24px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
  .panel h2{margin:0 0 12px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:600;font-size:12px}
  .kpis{display:flex;gap:20px;flex-wrap:wrap}
  .kpi{min-width:120px}
  .kpi .v{font-size:22px;font-weight:700}
  .kpi .l{color:var(--mut);font-size:12px}
  .full{grid-column:1/-1}
  svg{width:100%;height:80px;display:block}
  .lg{display:flex;gap:16px;font-size:12px;color:var(--mut);margin-top:6px}
  .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
  code{color:var(--acc)}
</style></head>
<body>
<header>
  <h1>deepagent dashboard</h1>
  <span class="mut">memory backend: <code id="backend">…</code></span>
  <span class="mut">updated <span id="updated">…</span></span>
</header>
<main>
  <div class="panel full">
    <h2>Totals</h2>
    <div class="kpis" id="totals"></div>
  </div>

  <div class="panel full">
    <h2>Agent-container network (whole container — not per user; see README)</h2>
    <div class="kpis" id="netkpis"></div>
    <svg id="spark" viewBox="0 0 600 80" preserveAspectRatio="none"></svg>
    <div class="lg">
      <span><span class="sw" style="background:#58a6ff"></span>rx (down)</span>
      <span><span class="sw" style="background:#3fb950"></span>tx (up)</span>
    </div>
  </div>

  <div class="panel"><h2>Per user</h2><table id="per_user"></table></div>
  <div class="panel"><h2>Per memory backend</h2><table id="per_backend"></table></div>
  <div class="panel"><h2>Per model</h2><table id="per_model"></table></div>
</main>

<script>
const fmtInt = n => Math.round(n).toLocaleString();
const fmtF = n => (Math.round(n*10)/10).toLocaleString();
function bytes(n){const u=['B','KB','MB','GB','TB'];let i=0;n=+n||0;while(n>=1024&&i<u.length-1){n/=1024;i++}return (Math.round(n*10)/10)+' '+u[i];}
function kpi(label,val){return `<div class="kpi"><div class="v">${val}</div><div class="l">${label}</div></div>`;}

function table(el, rows, cols){
  const head = '<tr>'+cols.map(c=>`<th>${c.h}</th>`).join('')+'</tr>';
  const body = rows.map(r=>'<tr>'+cols.map(c=>`<td>${c.f(r[c.k])}</td>`).join('')+'</tr>').join('');
  el.innerHTML = head+body;
}

function spark(hist){
  const svg=document.getElementById('spark');
  const W=600,H=80,pad=4;
  if(!hist.length){svg.innerHTML='';return;}
  const max=Math.max(1,...hist.map(h=>Math.max(h.rx_bps,h.tx_bps)));
  const x=i=>pad+i*(W-2*pad)/Math.max(1,hist.length-1);
  const y=v=>H-pad-(v/max)*(H-2*pad);
  const path=k=>hist.map((h,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(h[k]).toFixed(1)).join(' ');
  svg.innerHTML=`<path d="${path('rx_bps')}" fill="none" stroke="#58a6ff" stroke-width="1.5"/>`
              +`<path d="${path('tx_bps')}" fill="none" stroke="#3fb950" stroke-width="1.5"/>`;
}

async function refresh(){
  try{
    const s = await (await fetch('/api/stats')).json();
    document.getElementById('backend').textContent = s.memory_backend_current;
    document.getElementById('updated').textContent = new Date(s.generated_at).toLocaleTimeString();
    const t=s.totals;
    document.getElementById('totals').innerHTML =
      kpi('prompts',fmtInt(t.prompts))+kpi('prompt tokens',fmtInt(t.prompt_tokens))
      +kpi('completion tokens',fmtInt(t.completion_tokens))+kpi('total tokens',fmtInt(t.total_tokens))
      +kpi('avg tokens/sec',fmtF(t.avg_tokens_per_sec));
    const n=s.network;
    document.getElementById('netkpis').innerHTML =
      kpi('down / s',bytes(n.rate.rx_bps))+kpi('up / s',bytes(n.rate.tx_bps))
      +kpi('pkts down/s',fmtInt(n.rate.rx_pps))+kpi('pkts up/s',fmtInt(n.rate.tx_pps))
      +kpi('rx total',bytes(n.since_start.rx_bytes))+kpi('tx total',bytes(n.since_start.tx_bytes))
      +kpi('pkts rx',fmtInt(n.since_start.rx_packets))+kpi('pkts tx',fmtInt(n.since_start.tx_packets));
    spark(n.history);

    const cols=[{h:'',k:'__k',f:v=>v??'—'},{h:'prompts',k:'prompts',f:fmtInt},
      {h:'prompt tok',k:'prompt_tokens',f:fmtInt},{h:'compl tok',k:'completion_tokens',f:fmtInt},
      {h:'total tok',k:'total_tokens',f:fmtInt},{h:'avg tok/s',k:'avg_tokens_per_sec',f:fmtF}];
    const withKey=(rows,key)=>rows.map(r=>({...r,__k:r[key]}));
    table(document.getElementById('per_user'), withKey(s.per_user,'user_id'), cols);
    table(document.getElementById('per_backend'), withKey(s.per_backend,'memory_backend'), cols);
    table(document.getElementById('per_model'), withKey(s.per_model,'model'), cols);
  }catch(e){console.error(e);}
}
refresh(); setInterval(refresh, 3000);
</script>
</body></html>
"""
