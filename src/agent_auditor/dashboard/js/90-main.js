"use strict";
const NAV = [
  ['Watch', [['overview', 'Overview'], ['failures', 'Failures'], ['runs', 'Runs']]],
  ['Understand', [['quality', 'Quality'], ['causes', 'Root causes'], ['drift', 'Drift']]],
  ['Improve', [['versions', 'Versions'], ['tests', 'Regression tests'], ['fixes', 'Fixes'], ['benchmark', 'Benchmark'], ['gap', 'Gap analysis']]],
  ['Ship safely', [['canary', 'Canary'], ['releases', 'Releases']]],
  ['Govern', [['audit', 'Audit log'], ['setup', 'Setup']]],
];
const TITLES = Object.fromEntries(NAV.flatMap(([, l]) => l));
let renderToken = 0;

function go(name) { if (location.hash === '#/' + name) refreshView(); else location.hash = '#/' + name; }
function currentView() { const m = location.hash.match(/^#\/([a-z-]+)/); return m && views[m[1]] ? m[1] : 'overview'; }
function selectAgent(name, view) { state.agent = name || null; store.set('aa.agent', name || ''); drawChrome(); if (view) go(view); else refreshView(); }
function refreshView() { renderView(); }

async function renderView() {
  const name = currentView(), token = ++renderToken, root = $('#view');
  state.view = name; hideTip(); closeDrawer(); drawChrome();
  root.replaceChildren(loading());
  document.title = TITLES[name] + ' – Agent Auditor';
  try {
    const p = views[name].render(root);
    await p;
    if (token === renderToken) { root.setAttribute('aria-busy', 'false'); const note = testDataNote(); if (note) root.prepend(note); }
  } catch (e) {
    if (token !== renderToken) return;
    root.replaceChildren(errorBox(e, refreshView));
    if (e.auth && name !== 'setup') root.append(h('p', { class: 'muted', text: 'Tip: run-all.ps1 opens this page already signed in. If you started the server yourself, paste the key in Setup.' }));
  }
}

async function loadAgents() {
  try {
    state.agents = await api('/v1/agents'); state.needKey = false;
    state.assistant = await api('/v1/assistant/status').catch(() => ({ enabled: false }));
    if (!state.agent || (state.agent && !state.agents.some((a) => a.name === state.agent))) {
      const pref = state.agents.find((a) => a.name === 'chat-agent') || state.agents[0];
      state.agent = pref ? pref.name : null;
    }
    api('/v1/insights/overview?hours=24' + (state.agent ? '&agent=' + q(state.agent) : '')).then((o) => { state.failures = o.problem_runs; drawNavCounts(); }).catch(() => {});
  } catch (e) { state.agents = []; state.needKey = !!e.auth; state.netErr = e.network ? e : null; }
  drawChrome();
}

const isTestAgent = (n) => /^(demo|validation)-/.test(n || '');
function agentOptions() {
  const opt = (a) => h('option', { value: a.name, text: a.name, selected: a.name === state.agent });
  const real = state.agents.filter((a) => !isTestAgent(a.name)), test = state.agents.filter((a) => isTestAgent(a.name));
  return [...real.map(opt), test.length ? h('optgroup', { label: 'Test data (not your chat)' }, test.map(opt)) : null];
}
function testDataNote() {
  if (!isTestAgent(state.agent)) return null;
  const real = state.agents.find((a) => a.name === 'chat-agent');
  return h('div', { class: 'testnote', role: 'note' }, h('span', { text: 'This is test data from the self-check or demo, so its failures are made on purpose. Your own chat is “chat-agent”.' }),
    real ? h('button', { class: 'btn small', type: 'button', text: 'Show chat-agent', onclick: () => selectAgent('chat-agent') }) : null);
}
function drawNavCounts() { const c = $('#navFailures'); if (c) { c.textContent = state.failures || ''; c.hidden = !state.failures; } }
function drawChrome() {
  const rail = $('#rail'), cur = state.view;
  rail.replaceChildren(h('div', { class: 'brand' }, h('i'), 'Agent Auditor'));
  NAV.forEach(([g, items]) => {
    rail.append(h('div', { class: 'grp', text: g }));
    items.forEach(([id, label]) => rail.append(h('a', { class: 'nav', href: '#/' + id, 'aria-current': id === cur ? 'page' : null, onclick: () => document.body.classList.remove('nav') }, label, id === 'failures' ? h('span', { class: 'cnt', id: 'navFailures', hidden: !state.failures, text: state.failures || '' }) : null)));
  });
  rail.append(h('div', { class: 'spacer' }), h('div', { class: 'foot' }, h('a', { href: 'http://127.0.0.1:8001', target: '_blank', rel: 'noopener noreferrer', text: 'Open the chat app' }), h('a', { href: '/docs', target: '_blank', rel: 'noopener noreferrer', text: 'API reference' }), h('span', { text: 'Key: ' + (store.sget('aa.key') ? 'entered in this tab' : state.needKey ? 'needed' : 'not needed or not entered') })));
  const top = $('#top');
  const agentSel = h('select', { class: 'sel', 'aria-label': 'Agent', onchange: (e) => selectAgent(e.target.value) }, h('option', { value: '', text: 'All agents', selected: !state.agent }), ...agentOptions());
  const winSel = h('select', { class: 'sel', 'aria-label': 'Time period', onchange: (e) => { state.win = e.target.value; store.set('aa.win', state.win); refreshView(); } }, Object.entries(WINDOWS).map(([k, [l]]) => h('option', { value: k, text: 'Last ' + l, selected: k === state.win })));
  const dark = (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  top.replaceChildren(h('button', { class: 'btn', id: 'menu', type: 'button', 'aria-label': 'Menu', text: 'Menu', onclick: () => document.body.classList.toggle('nav') }), h('h1', { text: TITLES[cur] || 'Overview' }), agentSel, winSel,
    h('button', { class: 'btn', type: 'button', text: 'Refresh', onclick: refreshView }), h('button', { class: 'btn quiet', type: 'button', text: dark ? 'Light' : 'Dark', onclick: toggleTheme }));
}
function toggleTheme() { const dark = (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark'; const n = dark ? 'light' : 'dark'; document.documentElement.dataset.theme = n; store.set('aa.theme', n); drawChrome(); }

/* ---------- Ask for help ---------- */
const GLOSSARY = [
  ['Run', 'One question going in and one answer coming out, with every step the agent took in between.'], ['Trace', 'The recorded steps of a run: model calls, tool calls, searches, with how long each took.'], ['Version', 'A saved prompt plus settings. Runs are tagged with the version that produced them.'],
  ['Quality check', 'An automatic test on an answer: did it fail, is it empty, is it supported by the documents, does it contain what it should.'], ['Root cause', 'The most likely reason a run failed, such as a model error, a tool timeout, empty retrieval or a wrong answer.'], ['Drift', 'The agent behaving differently from before (slower, more errors, different quality) without anyone changing it.'],
  ['Regression test', 'A real failure saved as a test, so it cannot quietly come back.'], ['Canary', 'Trying a new version on a small share of traffic and comparing it with the current one before switching.'], ['Release gate', 'The checks and human approvals a new version must pass before it becomes the live one.'],
  ['Rollback', 'Putting the previous version back as the live one.'], ['95% range', 'The span that very likely contains the true rate. A short span means a trustworthy number; a long one means too few answers.'], ['Benchmark', 'The same questions asked of several models, each answer checked.'], ['Gap analysis', 'A comparison of the chat’s features with other assistants, ranked by what to build next.'], ['Tape', 'The strip of recent runs on the Overview: orange ticks are failures, taller means slower.'],
];
const HELP_LEVELS = [
  ['Beginner', ['What is this dashboard?', 'Which agent should I look at?', 'Why are there so many failures?', 'What does the Failures page show?']],
  ['Intermediate', ['How do I fix a failed run?', 'What is a canary?', 'Why does the chat say 429 or quota?', 'How do failures get into the dashboard?']],
  ['Advanced', ['How does scoring and diagnosis work?', 'How do I reduce failures?', 'How do releases and approvals work?', 'What can I do with the API?']],
];
let helpOpen = false; const helpHistory = [];
/* A tiny markdown reader built from DOM nodes only: paragraphs, "- " bullets, **bold** and `code`. */
function inlineMd(text) {
  const out = []; const re = /(\*\*[^*]+\*\*|`[^`]+`)/g; let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[0][0] === '*' ? h('b', { text: m[0].slice(2, -2) }) : h('code', { text: m[0].slice(1, -1) }));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function renderMd(text) {
  const box = h('div', { class: 'md' }); let list = null;
  String(text).split('\n').forEach((raw) => {
    const line = raw.trimEnd(), b = line.match(/^\s*[-*] (.*)$/);
    if (b) { if (!list) { list = h('ul'); box.append(list); } list.append(h('li', null, ...inlineMd(b[1]))); return; }
    list = null; if (line.trim()) box.append(h('p', null, ...inlineMd(line)));
  });
  return box;
}
function toggleHelp() {
  const old = $('#help'); if (old) { old.remove(); helpOpen = false; return; }
  helpOpen = true;
  const a = state.assistant || {}, ai = !!a.enabled;
  const log = h('div', { class: 'bd', role: 'log', 'aria-live': 'polite' });
  const input = h('textarea', { rows: '1', placeholder: 'Ask anything about this dashboard…', 'aria-label': 'Your question', maxlength: '1000' });
  const send = h('button', { class: 'btn primary', type: 'submit', text: 'Send' });
  let busy = false;
  const bubble = (role, content, source) => {
    const el = h('div', { class: 'm' + (role === 'user' ? ' me' : '') });
    if (role === 'user') el.textContent = content; else el.append(renderMd(content));
    if (source) el.append(h('div', { class: 'src', text: source === 'guide' ? 'From the built-in guide' : 'Answered by ' + source }));
    log.append(el); log.scrollTop = log.scrollHeight; return el;
  };
  const words = () => {
    bubble('assistant', '**Words used in this dashboard**\n' + GLOSSARY.map(([w, d]) => '- **' + w + ':** ' + d).join('\n'));
  };
  const welcome = () => {
    log.replaceChildren();
    bubble('assistant', 'Hi! Ask me anything about this dashboard, from "what is a run?" to "how do I stop timeouts?". Pick a question below or type your own.\n' +
      (ai ? '- AI answers are on (' + (a.providers || []).join(', ') + ').' : '- I am answering from the built-in guide. For free-form AI answers, add a free `GEMINI_API_KEY` to `.env` and restart.'));
  };
  const ask = async (question) => {
    question = (question || '').trim(); if (!question || busy) return;
    busy = true; send.disabled = true; input.value = ''; bubble('user', question);
    const wait = h('div', { class: 'm', text: 'Thinking…' }); log.append(wait); log.scrollTop = log.scrollHeight;
    try {
      const r = await post('/v1/assistant/ask', { question, context: { view: state.view, agent: state.agent, period: state.win, failed_runs: state.failures },
        history: helpHistory.slice(-6).map((t) => ({ role: t.role, content: t.content.slice(0, 1900) })) });
      wait.remove(); bubble('assistant', r.answer, r.source || 'guide');
      helpHistory.push({ role: 'user', content: question }, { role: 'assistant', content: r.answer });
    } catch (err) { wait.remove(); bubble('assistant', 'I could not answer: ' + (err.message || 'unknown error') + '. Check that the server is running, then try again.'); }
    busy = false; send.disabled = false; input.focus();
  };
  const chips = h('div', { class: 'chips' }, HELP_LEVELS.map(([lvl, qs]) => h('details', { class: 'lvl', open: lvl === 'Beginner' }, h('summary', { text: lvl }),
    h('div', { class: 'row' }, qs.map((x) => h('button', { type: 'button', class: 'chip', text: x, onclick: () => ask(x) }))))),
    h('button', { type: 'button', class: 'chip', text: 'Words used here', onclick: words }));
  const form = h('form', { onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, send);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 110) + 'px'; });
  const panel = h('div', { id: 'help', role: 'dialog', 'aria-label': 'Ask for help' },
    h('header', null, h('h2', { text: 'Ask for help' }),
      h('button', { class: 'btn quiet small', type: 'button', text: 'Clear', onclick: () => { helpHistory.length = 0; welcome(); } }),
      h('button', { class: 'btn quiet small', type: 'button', text: 'Close', onclick: toggleHelp })), log, chips, form);
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleHelp(); });
  welcome(); document.body.append(panel); input.focus();
}

/* ---------- boot ---------- */
(function boot() {
  const bm = document.getElementById('bootmsg'); if (bm) bm.remove();
  // run-all.ps1 opens the page as /dashboard#key=...: keep the key for this tab only, then remove it from the address bar
  const m = location.hash.match(/[#&]key=([^&]+)/);
  if (m) { store.sset('aa.key', decodeURIComponent(m[1])); history.replaceState(null, '', location.pathname + '#/overview'); }
  const th = store.get('aa.theme'); if (th) document.documentElement.dataset.theme = th;
  document.body.append(h('nav', { id: 'rail', 'aria-label': 'Sections' }), h('div', { id: 'main' }, h('header', { id: 'top' }), h('main', { id: 'view', tabindex: '-1' })), h('div', { id: 'toasts', 'aria-live': 'polite' }), tip,
    h('button', { id: 'helpFab', type: 'button', text: 'Ask for help', onclick: toggleHelp }));
  addEventListener('hashchange', renderView);
  addEventListener('keydown', (e) => { if (e.key === '?' && !/INPUT|TEXTAREA|SELECT/.test((e.target || {}).tagName || '')) toggleHelp(); });
  setInterval(() => { if (document.hidden || $('#drawer') || document.querySelector('dialog[open]') || !['overview', 'failures'].includes(state.view)) return; refreshView(); }, 30000);
  loadAgents().then(() => { if (state.needKey && currentView() !== 'setup') { location.hash = '#/setup'; return; } if (!location.hash.startsWith('#/')) location.hash = '#/overview'; else renderView(); });
  drawChrome();
})();
