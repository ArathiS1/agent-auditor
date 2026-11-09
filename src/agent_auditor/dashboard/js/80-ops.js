"use strict";
views.audit = {
  title: 'Audit log',
  async render(root) {
    const f = { prefix: '' }, body = h('div', { class: 'stack' });
    root.replaceChildren(lookFor('Every change the Auditor makes or is asked to make is written here: versions registered, tests created, releases approved, promoted or rolled back, thumbs given. It is append-only, so it answers who did what, and when.'), body);
    const draw = async () => {
      const sel = h('select', { class: 'sel', 'aria-label': 'Kind of event', onchange: (e) => { f.prefix = e.target.value; draw(); } }, [['', 'All events'], ['release', 'Releases'], ['version', 'Versions'], ['test', 'Tests'], ['run', 'Runs and feedback'], ['eval', 'Quality checks'], ['canary', 'Canary']].map(([v, l]) => h('option', { value: v, text: l, selected: v === f.prefix })));
      body.replaceChildren(sel, loading());
      try { const ev = await api('/v1/audit?limit=200' + (f.prefix ? '&action_prefix=' + f.prefix : '')); body.replaceChildren(sel, card({ title: ev.length + ' events', body: table([{ label: 'When', render: (e) => fmt.date(e.created_at) }, { label: 'Who', key: 'actor' }, { label: 'Action', render: (e) => e.action }, { label: 'On', render: (e) => e.entity_type + ' ' + e.entity_id.slice(0, 8) }, { label: 'Detail', clip: true, render: (e) => fmt.short(JSON.stringify(e.detail), 120) }], ev, { empty: 'No events yet.' }) })); }
      catch (e) { body.replaceChildren(sel, errorBox(e, draw)); }
    };
    draw();
  },
};

/* ---------- setup / connect ---------- */
const checkRow = (ok, title, detail, action) => h('div', { class: 'row', style: { padding: '8px 0', borderBottom: '1px solid var(--grid)' } }, ok === true ? badge('ok', 'ok') : ok === false ? badge('bad', 'problem') : badge('warn', 'optional'), h('div', { style: { flex: 1, minWidth: '240px' } }, h('b', { text: title }), h('div', { class: 'muted', text: detail })), action || null);
views.setup = {
  title: 'Setup',
  async render(root) {
    const out = [lookFor('This page checks that everything is wired together and tells you exactly what to fix. Green means working.')];
    const rows = h('div'); const checks = [];
    let health = null, keyState = 'unknown', agents = [], chat = null, assistant = { enabled: false }, overview = null;
    try { health = await (await fetch('/healthz', { cache: 'no-store' })).json(); } catch (e) { health = null; }
    try { await api('/v1/health'); keyState = 'ok'; } catch (e) { keyState = e.auth ? 'rejected' : e.network ? 'offline' : 'error'; }
    if (keyState === 'ok') {
      agents = await api('/v1/agents').catch(() => []);
      assistant = await api('/v1/assistant/status').catch(() => ({ enabled: false }));
      chat = await api('/v1/runs?agent=chat-agent&limit=1').catch(() => []);
      overview = await api('/v1/insights/overview?hours=168').catch(() => null);
    }
    checks.push(checkRow(!!health, 'The Auditor server is running', health ? 'Version ' + health.version + ' at ' + location.origin : 'Cannot reach ' + location.origin + '. Start it with run-all.ps1.'));
    checks.push(checkRow(keyState === 'ok', 'The API key works', { ok: 'The key you entered is accepted (or the server needs none).', rejected: 'The server rejected the key. Use the AUDITOR_API_KEY value from your .env file. If you changed it, restart the server so it picks up the new value.', offline: 'Cannot check: the server is not reachable.', error: 'The server answered with an error. Look at the server window.', unknown: '' }[keyState]));
    checks.push(checkRow(keyState === 'ok' ? agents.length > 0 : null, 'Agents are reporting', agents.length ? agents.map((a) => a.name).join(', ') : 'No agent has sent a run yet. Start the chat with run-all.ps1, or load demo data below.'));
    checks.push(checkRow(keyState === 'ok' ? !!(chat && chat.length) : null, 'The chat app is connected', chat && chat.length ? 'Last chat turn ' + fmt.ago(chat[0].started_at) : 'No runs from chat-agent yet. In the chat window, the bottom-left badge says "Recording to Auditor" when it is connected. Ask it something.'));
    checks.push(checkRow(keyState === 'ok' ? assistant.enabled : null, 'The AI helper in this dashboard', assistant.enabled ? 'On (' + assistant.model + ').' : 'Off. Optional. Set ANTHROPIC_API_KEY where the Auditor server runs to let it explain pages and suggest fixes. The word list in Ask for help works without it.'));
    if (overview) checks.push(checkRow(overview.unevaluated === 0, 'Runs are scored', overview.unevaluated ? overview.unevaluated + ' runs have not been checked yet.' : 'Every run has been checked.', overview.unevaluated ? h('button', { class: 'btn small', type: 'button', text: 'Score now', onclick: scoreNow }) : null));
    rows.append(...checks);
    out.push(card({ title: 'Connection check', actions: [h('button', { class: 'btn', type: 'button', text: 'Check again', onclick: () => refreshView() })], body: rows }));
    out.push(h('div', { style: { height: '14px' } }));
    const key = h('input', { type: 'password', class: 'sel', style: { width: 'min(420px,100%)' }, placeholder: 'Paste AUDITOR_API_KEY', 'aria-label': 'API key', value: store.sget('aa.key') || '', autocomplete: 'off' });
    out.push(card({ title: 'API key', sub: 'It lives in the .env file next to the project (AUDITOR_API_KEY). It is kept only in this browser tab, never saved to disk by this page.', body: h('div', { class: 'row' }, key, h('button', { class: 'btn primary', type: 'button', text: 'Save and test', onclick: async () => { store.sset('aa.key', key.value.trim() || null); await loadAgents(); refreshView(); } }), h('button', { class: 'btn quiet', type: 'button', text: 'Forget key', onclick: () => { store.sset('aa.key', null); refreshView(); } })) }));
    out.push(h('div', { style: { height: '14px' } }), h('div', { class: 'grid g2' },
      card({ title: 'Start everything (Windows)', body: h('div', { class: 'stack' }, cmd('powershell -ExecutionPolicy Bypass -File .\\run-all.ps1'), h('p', { class: 'muted', text: 'Starts this server and the chat together, opens both in your browser, and signs this dashboard in for you. Add -Check to also run the 23-point self-check, or -Test to run the unit tests.' })) }),
      card({ title: 'Compare other AI models', body: h('div', { class: 'stack' }, h('p', { class: 'muted', text: 'Add any of these keys to .env, restart, then use Compare models in the chat or run the benchmark:' }), cmd('OPENAI_API_KEY=...        # ChatGPT models\nANTHROPIC_API_KEY=...     # Claude\nGEMINI_API_KEY=...        # Gemini\nPERPLEXITY_API_KEY=...    # Perplexity (searches the web itself)\nDEEPSEEK_API_KEY=...  MISTRAL_API_KEY=...  XAI_API_KEY=...'), h('p', { class: 'muted', text: 'Microsoft Copilot and Blackbox do not offer a standard chat API; they appear in the Gap analysis checklist instead. Any service with an OpenAI-style API works through CHAT_PROVIDER=custom.' })) })));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'Demo data', sub: 'A made-up support bot with two versions, failures of several kinds, and a version that is worse than the other. Useful to see every page working.', body: h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', text: 'Load demo data', onclick: () => seedDemo() }), h('a', { href: '/docs', target: '_blank', rel: 'noopener noreferrer', text: 'Open the API reference' })) }));
    root.replaceChildren(...out);
  },
};

/* ---------- demo data ---------- */
async function seedDemo() {
  const ok = await confirmDialog('Load demo data', 'This adds an agent called demo-support-bot with about 140 made-up runs. It changes nothing else.', 'Load it');
  if (!ok) return;
  try {
    toast('Loading demo data…');
    const name = 'demo-support-bot', rnd = mulberry(42), pick = (a) => a[Math.floor(rnd() * a.length)];
    const P1 = 'You are a support assistant for an online shop. Answer using the policy documents. Be brief.';
    const P2 = 'You are a support assistant for an online shop. Answer in one sentence. Do not check the policy documents unless asked.';
    await post('/v1/agents', { name, description: 'Made-up support bot for the demo' });
    await post('/v1/agents/' + name + '/versions', { label: 'v1', prompt: P1, config: { model: 'demo-model-a', temperature: 0.2 }, activate: true });
    await post('/v1/agents/' + name + '/versions', { label: 'v2', prompt: P2, config: { model: 'demo-model-a', temperature: 0.7 }, activate: false });
    const QA = [['What is the refund window?', 'You can return items within 30 days.', '30 days'], ['How long does shipping take?', 'Standard shipping takes 3 to 5 business days.', 'business days'], ['Can I change my delivery address?', 'Yes, before the order ships, from your account page.', 'before'], ['Do you ship abroad?', 'Yes, to over 40 countries.', 'countries'], ['How do I reset my password?', 'Use the Forgot password link on the sign-in page.', 'Forgot password'], ['Is gift wrapping available?', 'Yes, for a small fee at checkout.', 'checkout']];
    const wrong = { 'What is the refund window?': 'Refunds are possible within 14 days.' };
    const now = Date.now(), runs = [];
    const mk = (version, env, tms, bad) => {
      const [qq, good, must] = pick(QA); const lat = 700 + rnd() * 500 + (bad ? 900 : 0);
      const t0 = new Date(tms), step = (i, ms) => new Date(tms + ms * i).toISOString();
      const v2 = version === 'v2', rate = v2 ? 0.42 : bad ? 0.28 : 0.08;
      const kind = rnd() < rate ? (v2 ? pick(['wrong', 'wrong', 'wrong', 'empty', 'ratelimit']) : pick(['ratelimit', 'ratelimit', 'timeout', 'empty', 'wrong'])) : 'ok';
      let status = 'ok', error = null, answer = good, steps = [{ name: 'search_policy', kind: 'retrieval', input: qq, output: [{ source: 'policy.md', text: good + ' This is our standard policy for all customers.' }], status: 'ok', attributes: { num_docs: 3, scores: [0.82, 0.6, 0.4], sources: ['policy.md'] }, started_at: t0.toISOString(), ended_at: step(1, 200) }, { name: 'model_call', kind: 'llm', input: qq, output: good, status: 'ok', attributes: { model: 'demo-model-a' }, started_at: step(1, 200), ended_at: new Date(tms + lat).toISOString() }];
      if (kind === 'ratelimit') { status = 'error'; error = '429 Too Many Requests: rate limit exceeded for model demo-model-a'; answer = null; steps[1] = { ...steps[1], status: 'error', error, output: null }; }
      if (kind === 'timeout') { status = 'error'; error = 'Timeout after 10s calling search_policy'; answer = null; steps[0] = { ...steps[0], status: 'error', error, output: null, attributes: {} }; steps = [steps[0]]; }
      if (kind === 'empty') { answer = 'I could not find that in our documents.'; steps[0] = { ...steps[0], attributes: { num_docs: 0, scores: [] } }; }
      if (kind === 'wrong') answer = wrong[qq] || 'I think that depends, please contact us.';
      return { agent: name, version, environment: env, input: qq, output: answer, status, error, started_at: t0.toISOString(), ended_at: new Date(tms + lat).toISOString(), tags: { demo: true, expect: [{ type: 'output_contains', value: must }] }, steps };
    };
    for (let i = 0; i < 95; i++) { const age = rnd() * 7 * 24 * 3600e3, recent = age < 24 * 3600e3; runs.push(mk('v1', 'production', now - age, recent)); }
    for (let i = 0; i < 45; i++) runs.push(mk('v2', 'canary', now - rnd() * 12 * 3600e3, true));
    for (let i = 0; i < runs.length; i += 10) await Promise.all(runs.slice(i, i + 10).map((r) => post('/v1/runs', r)));
    await post('/v1/evals/run', { agent: name, limit: 500 });
    await post('/v1/agents/' + name + '/diagnose', { limit: 500 }).catch(() => null);
    await loadAgents(); selectAgent(name, 'overview'); toast('Demo data loaded. Look at Failures, Drift and Canary.');
  } catch (e) { toast('Could not load demo data: ' + e.message, true); }
}
function mulberry(a) { return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
