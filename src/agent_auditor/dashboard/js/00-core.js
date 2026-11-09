"use strict";
/* ---------- tiny DOM helpers: text only ever enters the page through textContent ---------- */
const h = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'style') Object.assign(n.style, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c)));
  return n;
};
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs, ...kids) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) { if (k === 'text') n.textContent = v; else n.setAttribute(k, v); }
  for (const c of kids.flat()) if (c) n.append(c);
  return n;
};
const $ = (s, r) => (r || document).querySelector(s);
const q = encodeURIComponent;
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
  sget(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
  sset(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
};

/* ---------- formatting ---------- */
const fmt = {
  pct: (x, d = 0) => (x == null ? 'n/a' : (x * 100).toFixed(d) + '%'),
  ms: (x) => (x == null ? 'n/a' : x >= 1000 ? (x / 1000).toFixed(x >= 10000 ? 0 : 1) + ' s' : Math.round(x) + ' ms'),
  int: (x) => (x == null ? 'n/a' : Number(x).toLocaleString()),
  cause: (c) => (c ? String(c).replace(/_/g, ' ') : 'none'),
  short: (s, n = 90) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; },
  text: (v) => (v == null ? '' : typeof v === 'string' ? v : (v.text || v.question || v.message || v.content) && typeof (v.text || v.question || v.message || v.content) === 'string' ? (v.text || v.question || v.message || v.content) : JSON.stringify(v)),
  date: (d) => { const t = new Date(d); return isNaN(t) ? '' : t.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); },
  ago: (d) => {
    const s = (Date.now() - new Date(d).getTime()) / 1000;
    if (isNaN(s)) return '';
    if (s < 60) return 'just now'; if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago'; return Math.floor(s / 86400) + ' d ago';
  },
};

/* ---------- state and API ---------- */
const state = { agent: store.get('aa.agent'), win: store.get('aa.win') || '7d', agents: [], assistant: { enabled: false }, failures: 0, view: 'overview' };
const WINDOWS = { '1h': ['1 hour', 1], '24h': ['24 hours', 24], '7d': ['7 days', 168], '30d': ['30 days', 720], '90d': ['90 days', 2160] };
const winHours = () => WINDOWS[state.win][1];
const sinceIso = () => new Date(Date.now() - winHours() * 3600e3).toISOString();

async function api(path, opts) {
  const headers = { Accept: 'application/json' };
  const key = store.sget('aa.key');
  if (key) headers['X-API-Key'] = key;
  if (opts && opts.body) headers['Content-Type'] = 'application/json';
  let r;
  try { r = await fetch(path, { ...(opts || {}), headers, cache: 'no-store' }); }
  catch (e) { const err = new Error('Cannot reach the Auditor server. Is it running? (' + location.origin + ')'); err.network = true; throw err; }
  if (r.status === 401) { const e = new Error('The API key was rejected'); e.auth = true; throw e; }
  if (!r.ok) {
    let d = ''; try { d = (await r.json()).detail; } catch (e) { /* not json */ }
    const msg = typeof d === 'string' ? d : d && d.message ? d.message + (d.blockers ? ': ' + d.blockers.join('; ') : '') : Array.isArray(d) ? d.map((x) => (x.loc || []).slice(1).join('.') + ' ' + x.msg).join('; ') : JSON.stringify(d || r.statusText);
    const err = new Error(msg || String(r.status)); err.status = r.status; throw err;
  }
  return r.json();
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
const patch = (path, body) => api(path, { method: 'PATCH', body: JSON.stringify(body || {}) });
const A = () => '/v1/agents/' + q(state.agent);
const needAgent = () => {
  if (!state.agent && (state.agents || []).length) {  // "All agents" cannot be shown on this page: use your chat, else the first real agent
    const list = state.agents, pick = list.find((a) => a.name === 'chat-agent') || list.find((a) => !/^(demo|validation)-/.test(a.name)) || list[0];
    state.agent = pick.name; try { store.set('aa.agent', pick.name); } catch (e) { /* storage may be blocked */ }
    if (typeof drawChrome === 'function') drawChrome();
  }
  if (!state.agent) throw Object.assign(new Error('Pick an agent first.'), { noAgent: true });
};

/* ---------- toasts, tooltips ---------- */
function toast(text, bad) {
  const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), role: bad ? 'alert' : 'status', text });
  $('#toasts').append(t); setTimeout(() => t.remove(), bad ? 7000 : 3200);
}
const tip = h('div', { class: 'tip', hidden: true, role: 'tooltip' });
function showTip(x, y, title, rows) {
  tip.replaceChildren(h('b', { text: title }), ...(rows || []).map((r) => h('div', { class: 'm', text: r })));
  tip.hidden = false;
  const w = tip.offsetWidth, ht = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(innerWidth - w - 8, x + 14)) + 'px';
  tip.style.top = Math.max(8, Math.min(innerHeight - ht - 8, y + 14)) + 'px';
}
const hideTip = () => { tip.hidden = true; };
function withTip(el, content) {
  const at = (e) => { const [t, r] = content(); showTip(e.clientX, e.clientY, t, r); };
  el.addEventListener('pointermove', at); el.addEventListener('pointerleave', hideTip);
  el.addEventListener('focus', () => { const b = el.getBoundingClientRect(); const [t, r] = content(); showTip(b.left, b.top, t, r); });
  el.addEventListener('blur', hideTip);
  return el;
}

/* ---------- components ---------- */
const ICON = { ok: '✓', bad: '✕', warn: '▲', info: '●', mute: '•', rec: '●' };
const badge = (kind, text) => h('span', { class: 'badge ' + kind }, h('span', { 'aria-hidden': 'true', text: ICON[kind] || '' }), text);
const statusBadge = (s) => (s === 'ok' ? badge('ok', 'ok') : badge('bad', 'failed'));
const verdictBadge = (v) => {
  const map = { stable: 'ok', improvement: 'ok', no_change: 'ok', pass: 'ok', promoted: 'ok', approved: 'ok', ready: 'ok', healthy: 'ok', minor_drift: 'warn', warn: 'warn', inconclusive: 'warn', proposed: 'info', promote: 'ok', continue: 'info', hold: 'warn', insufficient_data: 'mute', skipped: 'mute',
    drift: 'bad', regression: 'bad', fail: 'bad', rollback: 'bad', rolled_back: 'bad', rejected: 'bad', failed: 'bad', block: 'bad', cancelled: 'mute' };
  return badge(map[v] || 'info', String(v || '').replace(/_/g, ' '));
};

function card({ title, sub, actions, body, cls }) {
  const hd = h('header', null, h('h2', { text: title }), ...(actions || []), sub ? h('div', { class: 'sub', text: sub }) : null);
  const bd = h('div', { class: 'bd' });
  if (body) bd.append(...[].concat(body));
  return h('section', { class: 'card ' + (cls || '') }, hd, bd);
}
function table(cols, rows, opts) {
  const o = opts || {};
  const thead = h('thead', null, h('tr', null, cols.map((c) => h('th', { class: c.num ? 'n' : '', text: c.label }))));
  const tbody = h('tbody');
  rows.forEach((r) => {
    const tr = h('tr', { class: o.onclick ? 'click' : '', tabindex: o.onclick ? '0' : null });
    cols.forEach((c) => { const v = c.render ? c.render(r) : r[c.key]; tr.append(h('td', { class: (c.num ? 'n ' : '') + (c.clip ? 'clip' : '') }, v == null ? '' : v)); });
    if (o.onclick) { tr.addEventListener('click', () => o.onclick(r)); tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') o.onclick(r); }); }
    tbody.append(tr);
  });
  if (!rows.length) tbody.append(h('tr', null, h('td', { colspan: cols.length, class: 'muted', text: o.empty || 'Nothing to show.' })));
  return h('div', { class: 'tw' }, h('table', { class: 't' }, thead, tbody));
}
/* horizontal bars: rows = [{label, value, text, kind, onclick, tip}] ; value 0..max */
function barList(rows, o) {
  const max = (o && o.max) || Math.max(1, ...rows.map((r) => r.value));
  const box = h('div', { class: 'bl' });
  rows.forEach((r) => {
    const row = h('div', { class: 'r' + (r.onclick ? ' click' : ''), tabindex: r.onclick ? '0' : null, role: r.onclick ? 'button' : null },
      h('span', { class: 'k', text: r.label, title: r.label }),
      h('span', { class: 'tr' }, h('span', { class: 'fl ' + (r.kind || ''), style: { width: Math.max(1, (r.value / max) * 100) + '%', display: 'block' } })),
      h('span', { class: 'vv', text: r.text != null ? r.text : String(r.value) }));
    if (r.tip) withTip(row, () => [r.label, r.tip]);
    if (r.onclick) { row.addEventListener('click', r.onclick); row.addEventListener('keydown', (e) => { if (e.key === 'Enter') r.onclick(); }); }
    box.append(row);
  });
  if (!rows.length) box.append(h('div', { class: 'muted', text: (o && o.empty) || 'Nothing to show.' }));
  return box;
}
const emptyState = (title, text, ...actions) => h('div', { class: 'empty' }, h('h3', { text: title }), h('p', { text }), h('div', { class: 'row', style: { justifyContent: 'center' } }, actions));
const callout = (kind, title, ...kids) => h('div', { class: 'callout ' + kind, role: kind === 'bad' ? 'alert' : null }, title ? h('h3', { text: title }) : null, ...kids);
const loading = (t) => h('div', { class: 'loading', text: t || 'Loading…' });
const lookFor = (text) => h('p', { class: 'look' }, h('b', { text: 'What to look for. ' }), text);
const cmd = (text) => h('code', { class: 'cmd', text });

/* an API error, shown where the content would be, with what to do next */
function errorBox(e, retry) {
  let title = 'Something went wrong', help = e.message;
  if (e.auth) { title = 'The server did not accept the API key'; help = 'Open Setup and paste the key from your .env file (AUDITOR_API_KEY).'; }
  else if (e.network) { title = 'Cannot reach the Auditor server'; help = e.message + ' Start it with run-all.ps1, or: python -m uvicorn agent_auditor.api.main:app --port 8000'; }
  else if (e.noAgent) { title = 'No agent selected'; help = 'Pick one in the top bar, or open Setup to connect your chat or load demo data.'; }
  else if (e.status === 404) { title = 'Not found'; }
  else if (e.status === 422) { title = 'The server could not use that request'; }
  else if (e.status >= 500) { title = 'The Auditor server hit an error'; help = e.message + ' Check the server window for the traceback.'; }
  return callout('bad', title, h('p', { text: help }), retry ? h('button', { class: 'btn', type: 'button', text: 'Try again', onclick: retry }) : null,
    e.auth ? h('button', { class: 'btn primary', type: 'button', text: 'Open Setup', onclick: () => go('setup') }) : null);
}

/* ---------- forms in a dialog ---------- */
function formDialog({ title, intro, fields, submit, danger }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', { 'aria-label': title });
    const inputs = {};
    const form = h('form', { method: 'dialog' }, h('h2', { text: title }), intro ? h('p', { class: 'muted', text: intro }) : null);
    fields.forEach((f) => {
      let inp;
      if (f.type === 'select') inp = h('select', { name: f.name }, f.options.map((o) => h('option', { value: o.value != null ? o.value : o, text: o.label || o })));
      else if (f.type === 'textarea') inp = h('textarea', { name: f.name, placeholder: f.placeholder || '' });
      else if (f.type === 'checkbox') inp = h('input', { type: 'checkbox', name: f.name });
      else inp = h('input', { type: f.type || 'text', name: f.name, placeholder: f.placeholder || '', step: f.step, min: f.min, max: f.max, required: f.required });
      if (f.value != null) { if (f.type === 'checkbox') inp.checked = !!f.value; else inp.value = f.value; }
      inputs[f.name] = inp;
      form.append(h('label', { class: 'fld' }, h('span', { text: f.label }), inp, f.help ? h('small', { text: f.help }) : null));
    });
    const out = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    form.append(h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => out(null) }), h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), type: 'submit', text: submit || 'OK' })));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const v = {}; fields.forEach((f) => { const i = inputs[f.name]; v[f.name] = f.type === 'checkbox' ? i.checked : f.type === 'number' ? (i.value === '' ? null : Number(i.value)) : i.value.trim(); });
      out(v);
    });
    dlg.addEventListener('cancel', () => out(null));
    dlg.append(form); document.body.append(dlg); dlg.showModal();
    const first = form.querySelector('input,select,textarea'); if (first) first.focus();
  });
}
const confirmDialog = (title, text, label, danger) => formDialog({ title, intro: text, fields: [], submit: label || 'Yes', danger }).then((v) => v != null);

/* run an action: toast the result, show errors, then refresh the view */
async function act(okText, fn, after) {
  try { const r = await fn(); if (okText) toast(okText); if (after !== false) refreshView(); return r; }
  catch (e) { toast(e.message, true); return null; }
}

/* views register themselves here; 90-main.js draws them */
const views = {};
