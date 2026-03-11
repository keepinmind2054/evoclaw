"""
EvoClaw Dashboard — Full SPA with sidebar navigation.

Sections:
  1. 狀態監控 — Container status, active agents, memory, session stats
  2. 日誌查看 — Real-time SSE log stream with level filter
  3. Agent 管理 — Task CRUD, container stop
  4. 系統設定 — .env viewer/editor, CLAUDE.md editor

No external dependencies — pure stdlib.
"""
import base64
import http.server
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config

# ─────────────────────────────────────────────────────────────────────────────
# SPA Shell HTML
# ─────────────────────────────────────────────────────────────────────────────
_SHELL = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EvoClaw Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f1a;color:#e0e0e0;font-family:'Courier New',monospace;font-size:13px;display:flex;flex-direction:column;height:100vh;overflow:hidden}
#topbar{background:#16213e;border-bottom:1px solid #2d2d4e;padding:8px 16px;display:flex;align-items:center;gap:24px;flex-shrink:0}
#topbar .logo{color:#a78bfa;font-size:16px;font-weight:bold;letter-spacing:2px}
#topbar .meta{display:flex;flex-direction:column}
#topbar .label{color:#4b5563;font-size:10px;text-transform:uppercase}
#topbar .value{color:#a78bfa;font-weight:bold}
#topbar .clock{margin-left:auto;color:#6b7280;font-size:12px}
#layout{display:flex;flex:1;overflow:hidden}
#sidebar{width:160px;background:#13132a;border-right:1px solid #2d2d4e;display:flex;flex-direction:column;flex-shrink:0;padding-top:8px}
.nav-item{padding:12px 16px;cursor:pointer;color:#6b7280;display:flex;align-items:center;gap:8px;border-left:3px solid transparent;transition:all 0.15s;user-select:none}
.nav-item:hover{color:#e0e0e0;background:#1a1a2e}
.nav-item.active{color:#a78bfa;border-left-color:#a78bfa;background:#1a1a2e}
.nav-item .icon{font-size:16px}
#main{flex:1;overflow-y:auto;padding:16px}
.section-title{font-size:16px;color:#a78bfa;letter-spacing:2px;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #2d2d4e}
.card{background:#16213e;border:1px solid #2d2d4e;border-radius:6px;padding:16px;margin-bottom:12px}
.card h3{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.grid-4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px}
.stat-card{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:6px;padding:12px}
.stat-card .stat-label{color:#4b5563;font-size:10px;text-transform:uppercase;letter-spacing:1px}
.stat-card .stat-value{color:#a78bfa;font-size:22px;font-weight:bold;margin-top:4px}
.stat-card .stat-sub{color:#6b7280;font-size:10px;margin-top:2px}
table{width:100%;border-collapse:collapse;margin-top:4px}
th{text-align:left;color:#4b5563;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;padding:6px 8px;border-bottom:1px solid #2d2d4e}
td{padding:6px 8px;border-bottom:1px solid #0f0f1a;vertical-align:top;word-break:break-all}
tr:last-child td{border-bottom:none}
tr:nth-child(even) td{background:#1a1a2e}
.badge{display:inline-block;padding:1px 8px;border-radius:3px;font-size:10px;font-weight:bold;letter-spacing:0.5px}
.b-green{background:#064e3b;color:#34d399}
.b-yellow{background:#451a03;color:#fbbf24}
.b-red{background:#450a0a;color:#f87171}
.b-blue{background:#1e3a5f;color:#60a5fa}
.b-gray{background:#1f2937;color:#9ca3af}
.b-purple{background:#3b0764;color:#c084fc}
.na{color:#374151;font-style:italic}
.empty{color:#374151;text-align:center;padding:24px;font-style:italic}
/* Log viewer */
#log-toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.filter-btn{padding:4px 12px;border:1px solid #2d2d4e;border-radius:4px;cursor:pointer;background:#13132a;color:#6b7280;font-family:inherit;font-size:11px;transition:all 0.15s}
.filter-btn.active{background:#3b0764;color:#c084fc;border-color:#7c3aed}
.filter-btn:hover{color:#e0e0e0}
#log-box{background:#0a0a14;border:1px solid #2d2d4e;border-radius:4px;height:500px;overflow-y:auto;padding:8px;font-size:11px;line-height:1.6}
.log-line{padding:1px 0;border-bottom:1px solid #0f0f1a}
.log-DEBUG{color:#4b5563}
.log-INFO{color:#60a5fa}
.log-WARNING{color:#f59e0b}
.log-ERROR{color:#f87171}
.log-CRITICAL{color:#ff0000;font-weight:bold}
/* Management */
.btn{padding:5px 12px;border:1px solid #2d2d4e;border-radius:4px;cursor:pointer;font-family:inherit;font-size:11px;transition:all 0.15s}
.btn-danger{background:#450a0a;color:#f87171;border-color:#7f1d1d}
.btn-danger:hover{background:#7f1d1d}
.btn-primary{background:#1e3a5f;color:#60a5fa;border-color:#1d4ed8}
.btn-primary:hover{background:#1d4ed8}
.btn-success{background:#064e3b;color:#34d399;border-color:#065f46}
.btn-success:hover{background:#065f46}
.btn-sm{padding:3px 8px;font-size:10px}
/* Settings */
.env-row{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #1a1a2e}
.env-key{color:#a78bfa;min-width:200px;font-size:11px}
.env-val{flex:1;background:#0a0a14;border:1px solid #2d2d4e;border-radius:3px;padding:3px 8px;color:#e0e0e0;font-family:inherit;font-size:11px}
.env-val:focus{outline:none;border-color:#7c3aed}
.env-save{padding:3px 10px;background:#3b0764;color:#c084fc;border:1px solid #7c3aed;border-radius:3px;cursor:pointer;font-family:inherit;font-size:10px}
.claude-editor{width:100%;height:300px;background:#0a0a14;border:1px solid #2d2d4e;border-radius:4px;padding:8px;color:#e0e0e0;font-family:'Courier New',monospace;font-size:11px;resize:vertical}
.claude-editor:focus{outline:none;border-color:#7c3aed}
#status-msg{position:fixed;bottom:16px;right:16px;padding:8px 16px;border-radius:6px;font-size:12px;display:none;z-index:9999}
.msg-ok{background:#064e3b;color:#34d399;border:1px solid #065f46}
.msg-err{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
/* Loading */
.loading{color:#4b5563;text-align:center;padding:32px;font-style:italic}
/* DevEngine */
.dev-textarea{width:100%;background:#0a0a14;border:1px solid #2d2d4e;border-radius:4px;padding:8px;color:#e0e0e0;font-family:'Courier New',monospace;font-size:12px;resize:vertical;line-height:1.5}
.dev-textarea:focus{outline:none;border-color:#7c3aed}
.dev-select{background:#0a0a14;border:1px solid #2d2d4e;border-radius:4px;padding:4px 8px;color:#e0e0e0;font-family:inherit;font-size:11px;cursor:pointer}
.dev-select:focus{outline:none;border-color:#7c3aed}
.dev-terminal{background:#0a0a14;border:1px solid #2d2d4e;border-radius:4px;height:220px;overflow-y:auto;padding:8px;font-size:11px;line-height:1.7;font-family:'Courier New',monospace}
.dev-terminal-line{padding:1px 0;white-space:pre-wrap;word-break:break-all}
.log-success{color:#34d399}
.stage-badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:4px;font-size:11px;border:1px solid #2d2d4e}
.stage-pending{background:#1a1a2e;color:#4b5563}
.stage-running{background:#451a03;color:#fbbf24;border-color:#78350f;animation:pulse 1.2s infinite}
.stage-paused{background:#1e3a5f;color:#60a5fa;border-color:#1d4ed8}
.stage-done{background:#064e3b;color:#34d399;border-color:#065f46}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
/* Toast */
#toast-container{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:8px;z-index:9999;pointer-events:none}
.toast{padding:10px 16px;border-radius:6px;font-size:12px;max-width:320px;pointer-events:all;transition:opacity 0.4s;word-break:break-word}
.toast-ok{background:#064e3b;color:#34d399;border:1px solid #065f46}
.toast-err{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
.toast-info{background:#1e3a5f;color:#60a5fa;border:1px solid #1d4ed8}
</style>
</head>
<body>

<div id="topbar">
  <div class="logo">🦀 EvoClaw</div>
  <div class="meta">
    <span class="label">Database</span>
    <span class="value" id="h-db">—</span>
  </div>
  <div class="meta">
    <span class="label">Port</span>
    <span class="value">PORT_PLACEHOLDER</span>
  </div>
  <div class="meta">
    <span class="label">Errors</span>
    <span class="value" id="h-errors" style="color:#f87171">—</span>
  </div>
  <div class="clock" id="clock"></div>
</div>

<div id="layout">
  <div id="sidebar">
    <div class="nav-item active" onclick="showTab('status')" id="nav-status">
      <span class="icon">📊</span><span>狀態監控</span>
    </div>
    <div class="nav-item" onclick="showTab('logs')" id="nav-logs">
      <span class="icon">📋</span><span>日誌查看</span>
    </div>
    <div class="nav-item" onclick="showTab('manage')" id="nav-manage">
      <span class="icon">🤖</span><span>Agent 管理</span>
    </div>
    <div class="nav-item" onclick="showTab('settings')" id="nav-settings">
      <span class="icon">⚙️</span><span>系統設定</span>
    </div>
    <div class="nav-item" onclick="showTab('messages')" id="nav-messages">
      <span class="icon">💬</span><span>對話訊息</span>
    </div>
    <div class="nav-item" onclick="showTab('evolution')" id="nav-evolution">
      <span class="icon">🧬</span><span>進化引擎</span>
    </div>
    <div class="nav-item" onclick="showTab('devengine')" id="nav-devengine">
      <span class="icon">🛠️</span><span>DevEngine</span>
    </div>
  </div>
  <div id="main">
    <div id="tab-status"></div>
    <div id="tab-logs" style="display:none"></div>
    <div id="tab-manage" style="display:none"></div>
    <div id="tab-settings" style="display:none"></div>
    <div id="tab-messages" style="display:none"></div>
    <div id="tab-evolution" style="display:none"></div>
    <div id="tab-devengine" style="display:none"></div>
  </div>
</div>

<div id="status-msg"></div>
<div id="toast-container"></div>

<script>
// ── Clock ──────────────────────────────────────────────────────────────────
function updateClock(){
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}
setInterval(updateClock, 1000); updateClock();

// ── Tab navigation ─────────────────────────────────────────────────────────
let _currentTab = 'status';
let _logEs = null;
let _autoRefresh = null;

function showTab(name) {
  ['status','logs','manage','settings','messages','evolution','devengine'].forEach(t => {
    document.getElementById('tab-'+t).style.display = t===name?'':'none';
    document.getElementById('nav-'+t).classList.toggle('active', t===name);
  });
  _currentTab = name;
  clearInterval(_autoRefresh);
  if (_logEs) { _logEs.close(); _logEs = null; }
  if (name==='status') { loadStatus(); _autoRefresh = setInterval(loadStatus, 5000); }
  else if (name==='logs') { initLogs(); }
  else if (name==='manage') { loadManage(); _autoRefresh = setInterval(loadManage, 8000); }
  else if (name==='settings') { loadSettings(); }
  else if (name==='messages') { loadMessages(); _autoRefresh = setInterval(loadMessages, 8000); }
  else if (name==='evolution') { loadEvolution(); _autoRefresh = setInterval(loadEvolution, 10000); }
  else if (name==='devengine') { loadDevEngine(); _autoRefresh = setInterval(loadDevEngine, 4000); }
}

// ── Fetch helper ───────────────────────────────────────────────────────────
async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch(e) { return null; }
}

function toast(msg, ok=true, dur=3500) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = 'toast ' + (ok === 'info' ? 'toast-info' : ok ? 'toast-ok' : 'toast-err');
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity='0'; setTimeout(()=>el.remove(), 420); }, dur);
}
function showMsg(msg, ok=true) { toast(msg, ok); }

function esc(s) {
  if (s==null) return '<span class="na">—</span>';
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}
function trunc(s, n=80) {
  if (s==null) return '<span class="na">—</span>';
  s=String(s); return s.length>n ? esc(s.slice(0,n))+'…' : esc(s);
}
function badge(txt, cls='gray') {
  const m={green:'b-green',yellow:'b-yellow',red:'b-red',blue:'b-blue',gray:'b-gray',purple:'b-purple'};
  return `<span class="badge ${m[cls]||'b-gray'}">${esc(txt)}</span>`;
}
function statusBadge(s) {
  if (!s) return badge('—');
  s=s.toLowerCase();
  if (s==='active'||s==='running'||s==='ok') return badge(s,'green');
  if (s==='paused'||s==='warning') return badge(s,'yellow');
  if (s==='error'||s==='failed') return badge(s,'red');
  if (s==='cancelled') return badge(s,'gray');
  return badge(s,'gray');
}
function fmtMs(ts) {
  if (!ts) return '<span class="na">—</span>';
  return new Date(ts).toLocaleString();
}
function fmtS(ts) {
  if (!ts) return '<span class="na">—</span>';
  return new Date(ts*1000).toLocaleString();
}

// ── Tab 1: 狀態監控 ────────────────────────────────────────────────────────
async function loadStatus() {
  const [stat, agents, containers, health] = await Promise.all([
    api('/api/stats'),
    api('/api/agents'),
    api('/api/containers'),
    api('/api/health'),
  ]);

  if (stat) {
    document.getElementById('h-db').textContent = stat.db_path || '—';
    document.getElementById('h-errors').textContent = stat.error_count || '0';
  }

  let html = '<div class="section-title">📊 狀態監控</div>';

  // Stat cards row
  html += '<div class="grid-4" style="margin-bottom:12px">';
  html += statCard('Active Agents', agents ? agents.length : '—', 'containers running');
  const mem = stat && stat.memory ? stat.memory : {};
  html += statCard('Memory', mem.rss_mb ? mem.rss_mb+'MB' : '—', 'RSS (process)');
  html += statCard('Sessions', stat ? stat.sessions : '—', 'active sessions');
  html += statCard('Messages', stat ? stat.messages_today : '—', 'messages today');
  html += '</div>';

  // Health check
  html += '<div class="card"><h3>Health Checks</h3>';
  if (health && health.checks) {
    html += '<table><thead><tr><th>Component</th><th>Status</th></tr></thead><tbody>';
    for (const [k,v] of Object.entries(health.checks)) {
      const ok = v==='ok';
      html += `<tr><td>${esc(k)}</td><td>${statusBadge(ok?'ok':'error')}</td></tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">Health data unavailable</div>';
  }
  html += '</div>';

  // Active agents — with parent-child hierarchy and real-time activity
  html += '<div class="card"><h3>🐳 Active Agent Containers</h3>';
  if (agents && agents.length > 0) {
    // Sort: primary agents first, then subagents grouped under their parent
    const primary = agents.filter(a => !a.parent_container);
    const subs = agents.filter(a => a.parent_container);
    const sorted = [];
    for (const p of primary) { sorted.push(p); sorted.push(...subs.filter(s => s.parent_container === p.name)); }
    // Any orphan subagents (parent already exited) at the end
    sorted.push(...subs.filter(s => !primary.find(p => p.name === s.parent_container)));

    html += '<table><thead><tr><th>Container</th><th>Group</th><th>Type</th><th>Running</th><th>Activity</th></tr></thead><tbody>';
    for (const a of sorted) {
      const elapsed = Math.round((Date.now()-a.started_at)/1000);
      const isSubagent = !!a.parent_container;
      const indent = isSubagent ? '&nbsp;&nbsp;↳&nbsp;' : '';
      const typeLabel = isSubagent ? 'subagent' : (a.is_scheduled ? 'scheduled' : 'message');
      const typeColor = isSubagent ? 'yellow' : (a.is_scheduled ? 'purple' : 'blue');
      const activity = a.current_activity || '...';
      const activityStyle = 'font-size:10px;color:#9ca3af;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      html += `<tr${isSubagent ? ' style="background:#1a1a28"' : ''}>
        <td><span style="color:#4b5563">${indent}</span><code style="font-size:10px">${esc(a.name.replace('evoclaw-',''))}</code></td>
        <td>${esc(a.folder)}</td>
        <td>${badge(typeLabel,typeColor)}</td>
        <td>${elapsed}s</td>
        <td style="${activityStyle}" title="${esc(activity)}">${esc(activity)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No containers running</div>';
  }
  html += '</div>';

  // Docker containers (all evoclaw-*)
  html += '<div class="card"><h3>🐳 Docker Process List (evoclaw-*)</h3>';
  if (containers && containers.length > 0) {
    html += '<table><thead><tr><th>Name</th><th>Status</th><th>Image</th><th>Created</th></tr></thead><tbody>';
    for (const c of containers) {
      html += `<tr>
        <td><code style="font-size:10px">${esc(c.name)}</code></td>
        <td>${statusBadge(c.status)}</td>
        <td style="font-size:10px">${esc(c.image)}</td>
        <td style="font-size:10px">${esc(c.created_at)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No Docker containers found</div>';
  }
  html += '</div>';

  // Groups & Sessions
  html += '<div class="grid-2">';
  const groups = stat && stat.groups ? stat.groups : [];
  html += '<div class="card"><h3>Registered Groups</h3>';
  if (groups.length) {
    html += '<table><thead><tr><th>Folder</th><th>JID</th><th>Main</th></tr></thead><tbody>';
    for (const g of groups) {
      html += `<tr><td>${esc(g.folder)}</td><td style="font-size:10px">${esc(g.jid)}</td><td>${g.is_main?badge('main','purple'):''}</td></tr>`;
    }
    html += '</tbody></table>';
  } else html += '<div class="empty">No groups</div>';
  html += '</div>';

  const sessions = stat && stat.session_list ? stat.session_list : [];
  html += '<div class="card"><h3>Sessions</h3>';
  if (sessions.length) {
    html += '<table><thead><tr><th>Group</th><th>Session ID</th></tr></thead><tbody>';
    for (const s of sessions) {
      html += `<tr><td>${esc(s.group_folder)}</td><td><code style="font-size:10px">${esc(s.session_id)}</code></td></tr>`;
    }
    html += '</tbody></table>';
  } else html += '<div class="empty">No sessions</div>';
  html += '</div>';
  html += '</div>'; // grid-2

  // Immune Threats
  const immune = await api('/api/immune');
  html += '<div class="card"><h3>🛡 免疫威脅 (count > 3 or blocked)</h3>';
  if (immune && immune.length > 0) {
    html += '<table><thead><tr><th>Sender JID</th><th>Threat Type</th><th>Count</th><th>Blocked</th><th>Last Seen</th></tr></thead><tbody>';
    for (const r of immune) {
      html += `<tr>
        <td style="font-size:11px">${esc(r.sender_jid)}</td>
        <td>${esc(r.threat_type)}</td>
        <td style="color:#f59e0b">${esc(r.count)}</td>
        <td>${r.blocked ? badge('BLOCKED','red') : badge('no','gray')}</td>
        <td style="font-size:10px;color:#6b7280">${esc(r.last_seen)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No threats detected</div>';
  }
  html += '</div>';

  document.getElementById('tab-status').innerHTML = html;
}

function statCard(label, value, sub) {
  return `<div class="stat-card">
    <div class="stat-label">${label}</div>
    <div class="stat-value">${value}</div>
    <div class="stat-sub">${sub}</div>
  </div>`;
}

// ── Tab 2: 日誌查看 ────────────────────────────────────────────────────────
let _logLevel = 'ALL';
let _logPaused = false;
let _logIdx = 0;

function initLogs() {
  const html = `<div class="section-title">📋 日誌查看</div>
  <div class="card">
    <div id="log-toolbar">
      <span style="color:#6b7280;font-size:11px">Level:</span>
      ${['ALL','DEBUG','INFO','WARNING','ERROR'].map(l =>
        `<button class="filter-btn${l===_logLevel?' active':''}" onclick="setLogLevel('${l}')">${l}</button>`
      ).join('')}
      <button class="filter-btn" onclick="clearLogs()">🗑 Clear</button>
      <button class="filter-btn" id="pause-btn" onclick="togglePause()">${_logPaused?'▶ Resume':'⏸ Pause'}</button>
      <span style="margin-left:auto;color:#4b5563;font-size:10px" id="log-count">0 lines</span>
    </div>
    <div id="log-box"></div>
  </div>`;
  document.getElementById('tab-logs').innerHTML = html;
  startLogStream();
}

function setLogLevel(level) {
  _logLevel = level;
  document.querySelectorAll('.filter-btn').forEach(b => {
    if (['ALL','DEBUG','INFO','WARNING','ERROR'].includes(b.textContent)) {
      b.classList.toggle('active', b.textContent === level);
    }
  });
  _logIdx = 0;
  document.getElementById('log-box').innerHTML = '';
  startLogStream();
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('log-count').textContent = '0 lines';
}

function togglePause() {
  _logPaused = !_logPaused;
  document.getElementById('pause-btn').textContent = _logPaused ? '▶ Resume' : '⏸ Pause';
}

function startLogStream() {
  if (_logEs) { _logEs.close(); _logEs = null; }
  _logEs = new EventSource(`/api/logs/stream?level=${_logLevel}`);
  _logEs.onmessage = function(e) {
    if (_logPaused) return;
    try {
      const entry = JSON.parse(e.data);
      _logIdx = entry.idx;
      const box = document.getElementById('log-box');
      if (!box) return;
      const line = document.createElement('div');
      line.className = `log-line log-${entry.level}`;
      line.textContent = entry.msg;
      box.appendChild(line);
      // Keep last 500 lines
      while (box.children.length > 500) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
      const cnt = document.getElementById('log-count');
      if (cnt) cnt.textContent = box.children.length + ' lines';
    } catch(e) {}
  };
}

// ── Tab 3: Agent 管理 ──────────────────────────────────────────────────────
async function loadManage() {
  const [tasks, agents] = await Promise.all([
    api('/api/tasks'),
    api('/api/agents'),
  ]);

  let html = '<div class="section-title">🤖 Agent 管理</div>';

  // Active containers with stop button
  html += '<div class="card"><h3>🐳 Running Containers</h3>';
  if (agents && agents.length > 0) {
    html += '<table><thead><tr><th>Container</th><th>Group</th><th>Running for</th><th>Action</th></tr></thead><tbody>';
    for (const a of agents) {
      const elapsed = Math.round((Date.now()-a.started_at)/1000);
      html += `<tr>
        <td><code style="font-size:10px">${esc(a.name)}</code></td>
        <td>${esc(a.folder)}</td>
        <td>${elapsed}s</td>
        <td><button class="btn btn-danger btn-sm" onclick="stopContainer('${a.name}')">⏹ Stop</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No containers running</div>';
  }
  html += '</div>';

  // Scheduled tasks with cancel/update
  html += '<div class="card"><h3>🗓 Scheduled Tasks</h3>';
  if (tasks && tasks.length > 0) {
    html += '<table><thead><tr><th>ID</th><th>Group</th><th>Type</th><th>Schedule</th><th>Next Run</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
    for (const t of tasks) {
      const tid = t.id;
      html += `<tr id="task-row-${esc(tid)}">
        <td><code style="font-size:10px">${esc(String(tid).slice(0,8))}</code></td>
        <td>${esc(t.group_folder)}</td>
        <td>${esc(t.schedule_type)}</td>
        <td><input id="sv-${esc(tid)}" value="${esc(t.schedule_value)}" style="background:#0a0a14;border:1px solid #2d2d4e;color:#e0e0e0;padding:2px 6px;border-radius:3px;font-family:inherit;font-size:11px;width:120px"></td>
        <td style="font-size:11px">${fmtMs(t.next_run)}</td>
        <td>${statusBadge(t.status)}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-primary btn-sm" onclick="updateTask('${esc(tid)}')">💾 Save</button>
          ${t.status==='active'?`<button class="btn btn-danger btn-sm" style="margin-left:4px" onclick="cancelTask('${esc(tid)}')">✕ Cancel</button>`:''}
        </td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No scheduled tasks</div>';
  }
  html += '</div>';

  // Task Run Logs
  const runLogs = await api('/api/task-run-logs');
  html += '<div class="card"><h3>📜 任務執行日誌 (最近 20 筆)</h3>';
  if (runLogs && runLogs.length > 0) {
    html += '<table><thead><tr><th>Task ID</th><th>執行時間</th><th>耗時</th><th>狀態</th><th>結果 / 錯誤</th></tr></thead><tbody>';
    for (const r of runLogs) {
      const ts = r.run_at ? new Date(r.run_at).toLocaleString() : '—';
      const status = r.status ? r.status.toLowerCase() : '';
      const statusBadgeHtml = (status==='ok'||status==='success'||status==='done')
        ? badge(r.status,'green')
        : (status==='error'||status==='failed'||status==='timeout')
          ? badge(r.status,'red') : badge(r.status||'—','gray');
      const resultText = r.result || r.error || '';
      html += `<tr>
        <td><code style="font-size:10px">${esc(String(r.task_id||'').slice(0,8))}</code></td>
        <td style="font-size:11px;white-space:nowrap">${esc(ts)}</td>
        <td style="color:#60a5fa">${r.duration_ms != null ? r.duration_ms+'ms' : '—'}</td>
        <td>${statusBadgeHtml}</td>
        <td style="font-size:11px;max-width:300px;word-break:break-word">${esc(resultText.slice(0,200))}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No run logs</div>';
  }
  html += '</div>';

  document.getElementById('tab-manage').innerHTML = html;
}

async function stopContainer(name) {
  if (!confirm(`Stop container ${name}?`)) return;
  const r = await api(`/api/containers/${encodeURIComponent(name)}/stop`, {method:'POST'});
  showMsg(r && r.ok ? `Stopped ${name}` : 'Failed to stop container', r && r.ok);
  setTimeout(loadManage, 1000);
}

async function cancelTask(id) {
  if (!confirm('Cancel this task?')) return;
  const r = await api(`/api/tasks/${encodeURIComponent(id)}/cancel`, {method:'POST'});
  showMsg(r && r.ok ? 'Task cancelled' : 'Failed', r && r.ok);
  loadManage();
}

async function updateTask(id) {
  const sv = document.getElementById('sv-'+id);
  if (!sv) return;
  const r = await api(`/api/tasks/${encodeURIComponent(id)}/update`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({schedule_value: sv.value})
  });
  showMsg(r && r.ok ? 'Task updated' : 'Failed to update', r && r.ok);
  loadManage();
}

// ── Tab 4: 系統設定 ────────────────────────────────────────────────────────
async function loadSettings() {
  const [envData, claudeMds] = await Promise.all([
    api('/api/env'),
    api('/api/claude-mds'),
  ]);

  let html = '<div class="section-title">⚙️ 系統設定</div>';

  // API Keys / Env
  html += '<div class="card"><h3>🔑 API Key 與環境變數</h3>';
  html += '<p style="color:#4b5563;font-size:10px;margin-bottom:8px">敏感欄位已遮罩。點擊 Save 更新 .env 檔案。</p>';
  if (envData && envData.vars) {
    for (const [k, v] of Object.entries(envData.vars)) {
      const masked = v.masked;
      html += `<div class="env-row">
        <span class="env-key">${esc(k)}</span>
        <input class="env-val" id="env-${esc(k)}" type="${masked?'password':'text'}" value="${esc(v.value)}" placeholder="${masked?'(masked — type to update)':''}">
        <button class="env-save" onclick="saveEnv('${esc(k)}')">Save</button>
      </div>`;
    }
  } else {
    html += '<div class="empty">Could not read .env file</div>';
  }
  html += '</div>';

  // CLAUDE.md editor
  html += '<div class="card"><h3>📝 CLAUDE.md 編輯器</h3>';
  if (claudeMds && claudeMds.files) {
    for (const f of claudeMds.files) {
      html += `<div style="margin-bottom:16px">
        <div style="color:#a78bfa;font-size:11px;margin-bottom:6px">📄 ${esc(f.path)}</div>
        <textarea class="claude-editor" id="claude-${btoa(f.path)}">${esc(f.content)}</textarea>
        <div style="margin-top:6px">
          <button class="btn btn-success btn-sm" onclick="saveClaude('${esc(f.path)}', '${btoa(f.path)}')">💾 Save</button>
        </div>
      </div>`;
    }
  } else {
    html += '<div class="empty">No CLAUDE.md files found</div>';
  }
  html += '</div>';

  document.getElementById('tab-settings').innerHTML = html;
}

async function saveEnv(key) {
  const input = document.getElementById('env-'+key);
  if (!input || !input.value) return;
  const r = await api('/api/env', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key, value: input.value})
  });
  showMsg(r && r.ok ? `Saved ${key}` : 'Failed to save', r && r.ok);
}

async function saveClaude(path, b64) {
  const ta = document.getElementById('claude-'+b64);
  if (!ta) return;
  const r = await api('/api/claude-mds', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path, content: ta.value})
  });
  showMsg(r && r.ok ? 'CLAUDE.md saved' : 'Failed to save', r && r.ok);
}

// ── Tab 5: 對話訊息 ────────────────────────────────────────────────────────
let _msgJid = '';

async function loadMessages() {
  const groups = await api('/api/stats');
  const groupList = groups && groups.groups ? groups.groups : [];

  // Build group selector
  const groupOptions = groupList.map(g =>
    `<option value="${esc(g.jid)}" ${_msgJid===g.jid?'selected':''}>${esc(g.folder)} (${esc(g.jid)})</option>`
  ).join('');

  const msgs = await api(`/api/messages?jid=${encodeURIComponent(_msgJid)}&limit=100`);

  let html = '<div class="section-title">💬 對話訊息</div>';

  // Filter bar
  html += `<div class="card" style="padding:10px 16px">
    <div style="display:flex;gap:12px;align-items:center">
      <span style="color:#6b7280;font-size:11px">群組：</span>
      <select id="msg-jid-select" onchange="_msgJid=this.value;loadMessages()"
        style="background:#0a0a14;border:1px solid #2d2d4e;border-radius:3px;padding:4px 8px;color:#e0e0e0;font-family:inherit;font-size:11px">
        <option value="">全部群組</option>
        ${groupOptions}
      </select>
      <span style="color:#4b5563;font-size:10px;margin-left:auto">${msgs ? msgs.length : 0} 筆訊息（最新 100 筆）</span>
    </div>
  </div>`;

  // Messages table
  html += '<div class="card">';
  if (msgs && msgs.length > 0) {
    html += `<table>
      <thead><tr>
        <th style="width:140px">時間</th>
        <th style="width:80px">方向</th>
        <th style="width:120px">發送者</th>
        <th style="width:100px">群組 JID</th>
        <th>訊息內容</th>
      </tr></thead>
      <tbody>`;
    for (const m of msgs) {
      const ts = m.timestamp ? new Date(m.timestamp).toLocaleString() : '—';
      const isBot = m.is_bot_message;
      const isMe = m.is_from_me;
      const direction = isBot
        ? badge('Bot 回覆', 'purple')
        : isMe ? badge('我', 'blue') : badge('用戶', 'green');
      const senderName = m.sender_name || m.sender || '—';
      const content = m.content || '';
      // Highlight bot messages differently
      const rowStyle = isBot ? "background:#1a0a2e" : "";
      html += `<tr style="${rowStyle}">
        <td style="font-size:10px;color:#6b7280;white-space:nowrap">${esc(ts)}</td>
        <td>${direction}</td>
        <td style="font-size:11px">${esc(senderName)}</td>
        <td style="font-size:10px;color:#60a5fa">${esc(m.chat_jid)}</td>
        <td style="font-size:12px;white-space:pre-wrap;word-break:break-word;max-width:500px">${esc(content)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">沒有訊息記錄</div>';
  }
  html += '</div>';

  document.getElementById('tab-messages').innerHTML = html;
}

// ── Tab 6: 進化引擎 ────────────────────────────────────────────────────────
async function loadEvolution() {
  const [genome, evoLog] = await Promise.all([
    api('/api/evolution/genome'),
    api('/api/evolution/log'),
  ]);

  let html = '<div class="section-title">🧬 進化引擎</div>';

  // Group Genome (Evolution Stats)
  html += '<div class="card"><h3>🔬 群組基因組 (Group Genome)</h3>';
  if (genome && genome.length > 0) {
    html += '<table><thead><tr><th>JID</th><th>回答風格</th><th>正式程度</th><th>技術深度</th><th>世代</th><th>更新時間</th></tr></thead><tbody>';
    for (const r of genome) {
      const styleColor = r.response_style==='concise' ? '#f59e0b' : r.response_style==='detailed' ? '#60a5fa' : '#9ca3af';
      const formality = r.formality != null ? (r.formality*100).toFixed(0)+'%' : '—';
      const tech = r.technical_depth != null ? (r.technical_depth*100).toFixed(0)+'%' : '—';
      html += `<tr>
        <td style="font-size:11px">${esc(r.jid)}</td>
        <td style="color:${styleColor}">${esc(r.response_style)}</td>
        <td>${formality}</td>
        <td>${tech}</td>
        <td style="color:#a78bfa">${esc(r.generation)}</td>
        <td style="font-size:10px;color:#4b5563">${esc(r.updated_at)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No genome data yet (need 10+ runs to trigger evolution)</div>';
  }
  html += '</div>';

  // Evolution Log
  const colorMap = {
    genome_evolved: '#4ade80',
    genome_unchanged: '#94a3b8',
    cycle_start: '#60a5fa',
    cycle_end: '#a78bfa',
    skipped_low_samples: '#f59e0b',
  };
  html += '<div class="card"><h3>📈 演化歷程日誌 (最近 30 筆)</h3>';
  if (evoLog && evoLog.length > 0) {
    html += '<table><thead><tr><th>時間</th><th>事件類型</th><th>JID</th><th>適應度</th><th>平均回應</th><th>世代</th><th>備注</th></tr></thead><tbody>';
    for (const e of evoLog) {
      const ts = e.timestamp ? String(e.timestamp).slice(0,19) : '—';
      const color = colorMap[e.event_type] || '#e2e8f0';
      const fitness = e.fitness_score != null ? e.fitness_score.toFixed(3) : '—';
      const ms = e.avg_response_ms != null ? e.avg_response_ms.toFixed(0)+'ms' : '—';
      const gen = e.generation_before != null ? `${e.generation_before}→${e.generation_after}` : '—';
      html += `<tr>
        <td style="color:#6b7280;font-size:10px;white-space:nowrap">${esc(ts)}</td>
        <td style="color:${color};font-size:11px">${esc(e.event_type)}</td>
        <td style="font-size:10px">${esc(e.jid)}</td>
        <td style="color:#fbbf24">${fitness}</td>
        <td style="color:#60a5fa">${ms}</td>
        <td style="color:#a78bfa">${gen}</td>
        <td style="font-size:10px;color:#6b7280">${esc(e.notes)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">No evolution events yet</div>';
  }
  html += '</div>';

  document.getElementById('tab-evolution').innerHTML = html;
}

// ── Tab 7: DevEngine ──────────────────────────────────────────────────────
const DEV_STAGES = ['analyze','design','implement','test','review','document','deploy'];
const DEV_ICONS  = {analyze:'🔍',design:'📐',implement:'💻',test:'🧪',review:'🔎',document:'📝',deploy:'🚀'};

let _devSelectedSession = null;
let _devLogInterval     = null;
let _devLogOffset       = 0;
let _devActiveId        = null;  // session being monitored in terminal

async function loadDevEngine() {
  const sessions = await api('/api/dev/sessions');

  let html = '<div class="section-title">🛠️ DevEngine — 7 階段自動化開發引擎</div>';

  // ── Start form ────────────────────────────────────────────────────────────
  html += `<div class="card">
    <h3>🚀 啟動新開發任務</h3>
    <textarea id="dev-prompt" class="dev-textarea" rows="3"
      placeholder="描述你想建立的功能，例如：在 Dashboard 新增 CPU 使用率折線圖..."></textarea>
    <div style="display:flex;align-items:center;gap:12px;margin-top:8px;flex-wrap:wrap">
      <label style="color:#6b7280;font-size:11px">模式：</label>
      <select id="dev-mode" class="dev-select">
        <option value="auto">🤖 Auto（全自動完成）</option>
        <option value="interactive">🔍 Interactive（每階段確認）</option>
      </select>
      <button class="btn btn-primary" onclick="devStart()">▶ 開始建立</button>
    </div>
  </div>`;

  // ── Active session monitor ────────────────────────────────────────────────
  const active = sessions && sessions.find(s => ['running','paused','pending'].includes(s.status));
  if (active) {
    const done  = active.stages_done || 0;
    const pct   = Math.round(done / 7 * 100);
    const sc    = active.status === 'paused' ? 'blue' : active.status === 'pending' ? 'gray' : 'yellow';

    // 7-stage badges
    let stageBadges = '';
    DEV_STAGES.forEach((s, i) => {
      const art  = i < done;
      const cur  = active.current_stage === s;
      let cls = 'stage-pending';
      let ico = '⬜';
      if (art) { cls = 'stage-done'; ico = '✅'; }
      else if (cur && active.status === 'running')  { cls = 'stage-running'; ico = '⏳'; }
      else if (cur && active.status === 'paused')   { cls = 'stage-paused';  ico = '⏸'; }
      stageBadges += `<div class="stage-badge ${cls}" title="${s}">${DEV_ICONS[s]} <span style="font-size:9px">${s}</span> ${ico}</div>`;
    });

    // Interactive confirm panel
    let confirmPanel = '';
    if (active.status === 'paused') {
      confirmPanel = `<div style="background:#1a1a28;border:1px solid #3b0764;border-radius:6px;padding:12px;margin-bottom:12px">
        <div style="color:#c084fc;font-size:12px;margin-bottom:8px">
          ⏸ 階段 <strong>${active.current_stage || ''}</strong> 已完成，等待確認才繼續
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-success" onclick="devResume('${esc(active.session_id)}')">▶ 繼續下一階段</button>
          <button class="btn btn-danger"  onclick="devCancel('${esc(active.session_id)}')">✕ 停止</button>
        </div>
      </div>`;
    }

    html += `<div class="card" id="dev-active-card">
      <h3>⏳ 執行中 &mdash; <code style="color:#a78bfa">${esc(active.session_id.replace('dev_',''))}</code>
        ${badge(active.status, sc)}</h3>
      <div style="color:#9ca3af;font-size:11px;margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
           title="${esc(active.prompt)}">${esc(active.prompt)}</div>

      <!-- 7-stage badges -->
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">${stageBadges}</div>

      <!-- Progress bar -->
      <div style="background:#1a1a2e;border-radius:4px;height:8px;margin-bottom:6px;overflow:hidden">
        <div style="width:${pct}%;background:${pct===100?'#34d399':'#7c3aed'};height:8px;border-radius:4px;transition:width 0.5s"></div>
      </div>
      <div style="font-size:11px;color:#6b7280;margin-bottom:12px">${done}/7 階段完成（${pct}%）</div>

      ${confirmPanel}

      <!-- Log terminal -->
      <h3 style="margin-bottom:6px">📟 即時執行日誌</h3>
      <div id="dev-log-terminal" class="dev-terminal">
        <div class="dev-terminal-line log-INFO">▶ Session ${esc(active.session_id)} 監控中...</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="devCancel('${esc(active.session_id)}')"
        style="margin-top:8px">✕ 取消此 Session</button>
    </div>`;

    // Start / restart log polling for this session
    if (_devActiveId !== active.session_id) {
      if (_devLogInterval) clearInterval(_devLogInterval);
      _devLogOffset = 0;
      _devActiveId  = active.session_id;
    }
    clearInterval(_devLogInterval);
    _devLogInterval = setInterval(() => devPollLogs(active.session_id), 2000);
    setTimeout(() => devPollLogs(active.session_id), 100);

  } else {
    // No active session — stop polling
    if (_devLogInterval) { clearInterval(_devLogInterval); _devLogInterval = null; }
    _devActiveId = null;
  }

  // ── Session list (completed / failed / cancelled) ─────────────────────────
  const pastSessions = sessions ? sessions.filter(s => !['running','paused','pending'].includes(s.status)) : [];
  html += `<div class="card"><h3>📋 歷史 Sessions（最近 30 筆）</h3>`;
  if (pastSessions.length > 0) {
    html += `<table><thead><tr>
      <th>Session ID</th><th>Prompt</th><th>Mode</th><th>Status</th>
      <th>Progress</th><th>Updated</th><th>操作</th>
    </tr></thead><tbody>`;
    for (const s of pastSessions) {
      const sc2  = {completed:'green',failed:'red',cancelled:'gray'}[s.status] || 'gray';
      const done2 = s.stages_done || 0;
      const pct2  = Math.round(done2 / 7 * 100);
      const bar2  = `width:${pct2}%;background:${pct2===100?'#34d399':'#7c3aed'};height:6px;border-radius:3px`;
      const ts   = s.updated_at ? new Date(s.updated_at*1000).toLocaleString() : '';
      const sid  = esc(s.session_id.replace('dev_','').substring(0,14));
      const act2 = `<button class="btn btn-sm" onclick="devShowDetail('${esc(s.session_id)}')"
        style="background:#1a1a2e;color:#9ca3af;border:1px solid #2d2d4e">🔍</button>`;
      html += `<tr>
        <td><code style="font-size:10px;color:#a78bfa">${sid}</code></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(s.prompt)}">${esc(s.prompt)}</td>
        <td>${badge(s.mode,'gray')}</td>
        <td>${badge(s.status,sc2)}</td>
        <td style="min-width:80px">
          <div style="background:#1a1a2e;border-radius:3px;height:6px">
            <div style="${bar2}"></div></div>
          <span style="font-size:10px;color:#6b7280">${done2}/7</span>
        </td>
        <td style="font-size:10px;color:#6b7280">${esc(ts)}</td>
        <td>${act2}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  } else {
    html += '<div class="empty">尚無歷史 session。</div>';
  }
  html += '</div>';

  // ── Session detail ────────────────────────────────────────────────────────
  if (_devSelectedSession) {
    const detail = await api('/api/dev/session?id=' + encodeURIComponent(_devSelectedSession));
    if (detail && !detail.error) {
      html += `<div class="card">
        <h3>🔍 Session 詳情 &mdash; <code style="color:#a78bfa">${esc(detail.session_id)}</code></h3>
        <p style="color:#9ca3af;font-size:11px;margin-bottom:10px">
          <strong>Prompt:</strong> ${esc(detail.prompt)}<br>
          <strong>Mode:</strong> ${esc(detail.mode)} &nbsp; <strong>Status:</strong> ${esc(detail.status)}
        </p>
        ${detail.error ? `<div style="background:#450a0a;border:1px solid #7f1d1d;padding:8px;border-radius:4px;
          color:#f87171;margin-bottom:8px;font-size:11px">❌ ${esc(detail.error)}</div>` : ''}
        <table><thead><tr><th>階段</th><th>狀態</th><th>Artifact 預覽（前 500 字）</th></tr></thead><tbody>`;
      for (const stage of DEV_STAGES) {
        const art  = detail.artifacts ? detail.artifacts[stage] : null;
        const cur  = detail.current_stage === stage && detail.status === 'running';
        const ico  = art ? '✅' : (cur ? '⏳' : '⬜');
        html += `<tr${cur ? ' style="background:#1a1a28"' : ''}>
          <td>${DEV_ICONS[stage]} ${stage}</td>
          <td>${ico}</td>
          <td style="font-size:10px;color:#9ca3af;max-width:400px;word-break:break-word">
            ${art ? `<pre style="margin:0;white-space:pre-wrap;font-family:inherit">${esc(art)}</pre>`
                  : '<span class="na">—</span>'}
          </td>
        </tr>`;
      }
      html += `</tbody></table>
        <button class="btn btn-sm" onclick="_devSelectedSession=null;loadDevEngine()"
          style="margin-top:8px;background:#1a1a2e;color:#6b7280;border:1px solid #2d2d4e">✕ 關閉</button>
      </div>`;
    }
  }

  document.getElementById('tab-devengine').innerHTML = html;
}

// Start a new DevEngine session from the Dashboard form
async function devStart() {
  const promptEl = document.getElementById('dev-prompt');
  const modeEl   = document.getElementById('dev-mode');
  const prompt   = promptEl ? promptEl.value.trim() : '';
  const mode     = modeEl   ? modeEl.value           : 'auto';
  if (!prompt) { toast('請輸入需求描述', false); return; }
  const r = await api('/api/dev/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, mode}),
  });
  if (r && r.ok) {
    toast(`🚀 DevEngine 啟動！Session: ${r.session_id}`);
    _devSelectedSession = null;
    _devLogOffset = 0;
    loadDevEngine();
  } else {
    toast('❌ 啟動失敗：' + (r && r.error || '未知錯誤'), false);
  }
}

// Resume a paused session (continue to next stage)
async function devResume(sessionId) {
  const r = await api('/api/dev/resume', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId}),
  });
  toast(r && r.ok ? '▶ 已繼續執行下一階段' : '❌ Resume 失敗', r && r.ok);
  setTimeout(loadDevEngine, 1200);
}

// Cancel / stop a session
async function devCancel(sessionId) {
  if (!confirm('確定要取消這個 DevEngine session？')) return;
  const r = await api('/api/dev/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId}),
  });
  toast(r && r.ok ? '✕ Session 已取消' : '❌ 取消失敗', r && r.ok);
  if (_devLogInterval) { clearInterval(_devLogInterval); _devLogInterval = null; }
  _devActiveId = null;
  loadDevEngine();
}

// Show artifact detail for a past session
function devShowDetail(sessionId) {
  _devSelectedSession = sessionId === _devSelectedSession ? null : sessionId;
  loadDevEngine();
}

// Poll new log lines from the server and append to terminal
async function devPollLogs(sessionId) {
  const terminal = document.getElementById('dev-log-terminal');
  if (!terminal) {
    clearInterval(_devLogInterval); _devLogInterval = null; return;
  }
  const lines = await api(`/api/dev/log/${encodeURIComponent(sessionId)}?offset=${_devLogOffset}`);
  if (lines && Array.isArray(lines) && lines.length > 0) {
    for (const line of lines) {
      const el = document.createElement('div');
      el.className = 'dev-terminal-line ' + (
        line.includes('❌') ? 'log-ERROR'   :
        line.includes('✅') ? 'log-success' :
        line.includes('⏸') ? 'log-WARNING' : 'log-INFO'
      );
      el.textContent = line;
      terminal.appendChild(el);
    }
    _devLogOffset += lines.length;
    terminal.scrollTop = terminal.scrollHeight;
  }
}

// Initial load
showTab('status');
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _open_db():
    db_path = config.STORE_DIR / "messages.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _fetch(query, params=()):
    try:
        conn = _open_db()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []

def _fetch_one(query, params=()):
    try:
        conn = _open_db()
        try:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None

def _write_db(query, params=()):
    """Execute a write query on the writable DB."""
    try:
        db_path = config.STORE_DIR / "messages.db"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute(query, params)
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# API data providers
# ─────────────────────────────────────────────────────────────────────────────

def _get_stats() -> dict:
    """Aggregate stats for the status tab."""
    from . import log_buffer
    groups = _fetch("SELECT folder, jid, is_main FROM registered_groups ORDER BY folder")
    sessions = _fetch("SELECT group_folder, session_id FROM sessions")
    today_start = int((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) * 1000)
    today_msgs = _fetch_one("SELECT COUNT(*) as c FROM messages WHERE timestamp >= ?", (today_start,))

    # Memory
    mem = {}
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem["rss_mb"] = round(usage.ru_maxrss / 1024, 1)
    except Exception:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        mem["rss_mb"] = round(kb / 1024, 1)
                        break
        except Exception:
            pass

    return {
        "db_path": str(config.STORE_DIR / "messages.db"),
        "groups": groups,
        "session_list": sessions,
        "sessions": len(sessions),
        "messages_today": today_msgs["c"] if today_msgs else 0,
        "memory": mem,
        "error_count": log_buffer.get_error_count(),
    }


def _get_containers() -> list:
    """Get running Docker containers matching evoclaw-* pattern."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=evoclaw-",
             "--format", '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}","created_at":"{{.CreatedAt}}"}'],
            capture_output=True, text=True, timeout=5
        )
        containers = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except Exception:
                    pass
        return containers
    except Exception:
        return []


def _get_active_agents() -> list:
    """Get agents currently being processed (in-process tracking)."""
    try:
        from .container_runner import get_active_containers
        return get_active_containers()
    except Exception:
        return []


def _get_health() -> dict:
    """Health check: DB + Docker."""
    checks = {}
    status = "ok"
    # DB
    try:
        db_path = config.STORE_DIR / "messages.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error"
        status = "degraded"
    # Docker
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
        checks["docker"] = "ok" if r.returncode == 0 else "error"
        if r.returncode != 0:
            status = "degraded"
    except Exception:
        checks["docker"] = "unavailable"
    return {"status": status, "checks": checks}


def _get_tasks() -> list:
    return _fetch("SELECT * FROM scheduled_tasks ORDER BY status, next_run")


def _get_env_vars() -> dict:
    """Read .env file, masking secret values."""
    sensitive = {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL"}
    env_path = config.BASE_DIR / ".env"
    result = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            masked = any(s in k.upper() for s in sensitive)
            result[k] = {"value": "••••••" if masked else v, "masked": masked}
    return {"vars": result}


def _get_claude_mds() -> dict:
    """Return all CLAUDE.md files content."""
    files = []
    for p in sorted(config.BASE_DIR.rglob("CLAUDE.md")):
        try:
            files.append({"path": str(p.relative_to(config.BASE_DIR)), "content": p.read_text(encoding="utf-8")})
        except Exception:
            pass
    return {"files": files}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress access log

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # client disconnected mid-response — ignore on Windows

    def _auth(self) -> bool:
        if not config.DASHBOARD_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
                return user == config.DASHBOARD_USER and pw == config.DASHBOARD_PASSWORD
            except Exception:
                pass
        return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EvoClaw Dashboard"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self):
        if not self._auth():
            self._require_auth(); return

        path = self.path.split("?")[0]
        query = self.path[len(path)+1:] if "?" in self.path else ""
        qs = {}
        for part in query.split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                qs[k] = v

        if path == "/" or path == "/index.html":
            shell = _SHELL.replace("PORT_PLACEHOLDER", str(config.DASHBOARD_PORT))
            body = shell.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/stats":
            self._json(_get_stats())
        elif path == "/api/containers":
            self._json(_get_containers())
        elif path == "/api/agents":
            self._json(_get_active_agents())
        elif path == "/api/health":
            h = _get_health()
            self._json(h, 200 if h["status"]=="ok" else 503)
        elif path == "/api/tasks":
            self._json(_get_tasks())
        elif path == "/api/env":
            self._json(_get_env_vars())
        elif path == "/api/claude-mds":
            self._json(_get_claude_mds())
        elif path == "/api/logs":
            from . import log_buffer
            since = int(qs.get("since", 0))
            level = qs.get("level", "ALL")
            limit = int(qs.get("limit", 200))
            self._json(log_buffer.get_logs(since, level, limit))

        elif path == "/api/messages":
            jid = qs.get("jid", "")
            limit = int(qs.get("limit", 100))
            if jid:
                rows = _fetch(
                    "SELECT id, chat_jid, sender, sender_name, content, timestamp, "
                    "is_from_me, is_bot_message FROM messages "
                    "WHERE chat_jid=? ORDER BY timestamp DESC LIMIT ?",
                    (jid, limit)
                )
            else:
                rows = _fetch(
                    "SELECT id, chat_jid, sender, sender_name, content, timestamp, "
                    "is_from_me, is_bot_message FROM messages "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                )
            self._json(rows)

        elif path == "/api/immune":
            rows = _fetch(
                "SELECT sender_jid, threat_type, count, blocked, last_seen "
                "FROM immune_threats WHERE blocked=1 OR count > 3 "
                "ORDER BY blocked DESC, count DESC"
            )
            self._json(rows)

        elif path == "/api/task-run-logs":
            rows = _fetch(
                "SELECT task_id, run_at, duration_ms, status, result, error "
                "FROM task_run_logs ORDER BY run_at DESC LIMIT 20"
            )
            self._json(rows)

        elif path == "/api/evolution/genome":
            rows = _fetch(
                "SELECT jid, response_style, formality, technical_depth, generation, updated_at "
                "FROM group_genome ORDER BY updated_at DESC"
            )
            self._json(rows)

        elif path == "/api/evolution/log":
            rows = _fetch(
                "SELECT timestamp, jid, event_type, fitness_score, avg_response_ms, "
                "generation_before, generation_after, notes "
                "FROM evolution_log ORDER BY timestamp DESC LIMIT 30"
            )
            self._json(rows)

        elif path == "/api/logs/stream":
            self._handle_sse_logs(qs.get("level", "ALL"))

        elif path == "/api/dev/sessions":
            try:
                from .dev_engine import list_sessions
                self._json(list_sessions(limit=30))
            except Exception as e:
                self._json({"error": str(e)})

        elif path == "/api/dev/session":
            session_id = qs.get("id", "")
            try:
                from .dev_engine import get_session_detail
                detail = get_session_detail(session_id) if session_id else None
                self._json(detail or {"error": "not found"})
            except Exception as e:
                self._json({"error": str(e)})

        elif path.startswith("/api/dev/log/"):
            # GET /api/dev/log/<session_id>?offset=N  → returns list of new log lines
            session_id = path[len("/api/dev/log/"):]
            try:
                offset = int(qs.get("offset", "0"))
                from .dev_engine import get_dev_logs
                self._json(get_dev_logs(session_id, offset))
            except Exception as e:
                self._json([])

        elif path == "/health":
            h = _get_health()
            self._json(h, 200 if h["status"]=="ok" else 503)
        elif path == "/metrics":
            self._handle_metrics()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self._auth():
            self._require_auth(); return

        path = self.path.split("?")[0]

        # POST /api/tasks/<id>/cancel
        if path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = path[len("/api/tasks/"):-len("/cancel")]
            ok = _write_db("UPDATE scheduled_tasks SET status='cancelled' WHERE id=?", (task_id,))
            self._json({"ok": ok})

        # POST /api/tasks/<id>/update
        elif path.startswith("/api/tasks/") and path.endswith("/update"):
            task_id = path[len("/api/tasks/"):-len("/update")]
            body = self._read_body()
            sv = body.get("schedule_value", "")
            if sv:
                ok = _write_db("UPDATE scheduled_tasks SET schedule_value=? WHERE id=?", (sv, task_id))
            else:
                ok = False
            self._json({"ok": ok})

        # POST /api/containers/<name>/stop
        elif path.startswith("/api/containers/") and path.endswith("/stop"):
            name = path[len("/api/containers/"):-len("/stop")]
            try:
                subprocess.run(["docker", "stop", name], timeout=10, capture_output=True)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        # POST /api/env
        elif path == "/api/env":
            body = self._read_body()
            key = body.get("key", "").strip()
            value = body.get("value", "").strip()
            if not key or not value:
                self._json({"ok": False, "error": "key and value required"}); return
            env_path = config.BASE_DIR / ".env"
            try:
                lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
                found = False
                new_lines = []
                for line in lines:
                    if line.strip().startswith(f"{key}="):
                        new_lines.append(f'{key}="{value}"')
                        found = True
                    else:
                        new_lines.append(line)
                if not found:
                    new_lines.append(f'{key}="{value}"')
                env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        # POST /api/dev/start — create a new DevEngine session and trigger via IPC
        elif path == "/api/dev/start":
            body = self._read_body()
            prompt = body.get("prompt", "").strip()
            mode   = body.get("mode", "auto")
            if not prompt:
                self._json({"ok": False, "error": "prompt required"}); return
            try:
                from .dev_engine import DevEngine, DevSession, save_session, _write_dev_log
                import uuid as _uuid
                session = DevSession(
                    session_id=f"dev_{int(time.time())}_{_uuid.uuid4().hex[:6]}",
                    prompt=prompt,
                    jid="dashboard",
                    mode=mode,
                )
                save_session(session)
                _write_dev_log(session.session_id, f"🚀 DevEngine 啟動（mode={mode}）")
                _write_dev_log(session.session_id, f"📝 Prompt: {prompt[:200]}")
                # Find main group and write IPC dev_task file to trigger the pipeline
                from . import db as _db
                groups = _db.get_all_registered_groups()
                main_group = next((g for g in groups if g.get("is_main")), None)
                if not main_group:
                    # No main group found – try first group
                    main_group = groups[0] if groups else None
                if main_group:
                    ipc_dir = config.DATA_DIR / "ipc" / main_group["folder"] / "tasks"
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    fname = ipc_dir / f"{int(time.time()*1000)}_devstart.json"
                    fname.write_text(json.dumps({
                        "type": "dev_task",
                        "session_id": session.session_id,
                        "prompt": "",  # session already has prompt
                        "mode": mode,
                    }), encoding="utf-8")
                    self._json({"ok": True, "session_id": session.session_id})
                else:
                    # No groups configured — return session_id anyway so dashboard shows it
                    self._json({"ok": True, "session_id": session.session_id,
                                "warning": "No group configured — pipeline cannot auto-start"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        # POST /api/dev/cancel
        elif path == "/api/dev/cancel":
            body = self._read_body()
            session_id = body.get("session_id", "")
            try:
                from .dev_engine import load_session, save_session
                session = load_session(session_id)
                if session:
                    session.status = "cancelled"
                    session.current_stage = None
                    save_session(session)
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "session not found"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        # POST /api/dev/resume (marks paused session for resumption — actual resume via IPC)
        elif path == "/api/dev/resume":
            body = self._read_body()
            session_id = body.get("session_id", "")
            try:
                from .dev_engine import load_session
                session = load_session(session_id)
                if session and session.status == "paused":
                    # Write a dev_task IPC file to the group's tasks dir to resume via ipc_watcher
                    ipc_dir = config.DATA_DIR / "ipc" / session.jid.replace(":", "-").replace("@", "-") / "tasks"
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    import json as _json, time as _time
                    fname = ipc_dir / f"{int(_time.time()*1000)}_devresume.json"
                    fname.write_text(_json.dumps({
                        "type": "dev_task",
                        "session_id": session_id,
                        "prompt": "",
                        "mode": session.mode,
                    }), encoding="utf-8")
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "session not found or not paused"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        # POST /api/claude-mds
        elif path == "/api/claude-mds":
            body = self._read_body()
            rel_path = body.get("path", "")
            content = body.get("content", "")
            if not rel_path:
                self._json({"ok": False, "error": "path required"}); return
            try:
                full = (config.BASE_DIR / rel_path).resolve()
                # Security: must be inside BASE_DIR
                full.relative_to(config.BASE_DIR.resolve())
                full.write_text(content, encoding="utf-8")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self.send_response(404); self.end_headers()

    def _handle_sse_logs(self, level: str = "ALL"):
        """Server-Sent Events endpoint for real-time log streaming."""
        from . import log_buffer
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_idx = 0
        try:
            while True:
                entries = log_buffer.get_logs(since_idx=last_idx, level=level, limit=50)
                for entry in entries:
                    data = json.dumps(entry, default=str)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    last_idx = entry["idx"]
                if entries:
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected

    def _handle_metrics(self):
        lines = []
        tables = ["messages", "scheduled_tasks", "registered_groups", "sessions", "evolution_runs", "immune_threats"]
        for t in tables:
            row = _fetch_one(f"SELECT COUNT(*) as c FROM {t}")
            if row:
                lines.append(f"evoclaw_{t}_total {row['c']}")
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def start_dashboard(stop_event=None):
    """Start the dashboard in a daemon background thread."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    if not config.DASHBOARD_PASSWORD:
        _log.warning(
            "DASHBOARD_PASSWORD not set — dashboard is unprotected. "
            "Set DASHBOARD_PASSWORD env var to enable authentication."
        )
    server = http.server.ThreadingHTTPServer((config.DASHBOARD_HOST, config.DASHBOARD_PORT), _Handler)

    def _run():
        _log.info(f"Dashboard started on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
        try:
            server.serve_forever()
        except Exception as exc:
            _log.warning(f"Dashboard server stopped: {exc}")

    t = threading.Thread(target=_run, name="evoclaw-dashboard", daemon=True)
    t.start()
    return t
