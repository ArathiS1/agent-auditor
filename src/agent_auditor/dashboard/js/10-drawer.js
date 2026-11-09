"use strict";
/* ---------- chart card with a table twin ---------- */
function twinCard({ title, sub, chart, tableEl, actions }) {
  const view = h('div'); let asTable = false;
  const tgl = h('button', { class: 'btn quiet small', type: 'button', 'aria-pressed': 'false', text: 'Table view' });
  const draw = () => { hideTip(); view.replaceChildren(asTable ? tableEl() : chart()); };
  tgl.addEventListener('click', () => { asTable = !asTable; tgl.setAttribute('aria-pressed', String(asTable)); tgl.textContent = asTable ? 'Chart view' : 'Table view'; draw(); });
  draw();
  return card({ title, sub, actions: [...(actions || []), tgl], body: view });
}

/* ---------- taxonomy (cause meanings and advice), loaded once ---------- */
let _tax = null;
async function taxonomy() { if (!_tax) { const l = await api('/v1/root-causes/taxonomy'); _tax = Object.fromEntries(l.map((t) => [t.cause, t])); } return _tax; }

/* ---------- the run drawer ---------- */
function closeDrawer() { const d = $('#drawer'); if (d) d.remove(); const v = $('#veil'); if (v) v.remove(); document.removeEventListener('keydown', drawerKey); }
function drawerKey(e) { if (e.key === 'Escape') closeDrawer(); }
async function openRun(id) {
  closeDrawer();
  const bd = h('div', { class: 'bd' }, loading('Loading the run…'));
  const title = h('h2', { text: 'Run ' + id.slice(0, 8) });
  const drawer = h('aside', { id: 'drawer', role: 'dialog', 'aria-label': 'Run details' }, h('header', null, title, h('button', { class: 'btn', type: 'button', text: 'Close', onclick: closeDrawer })), bd);
  document.body.append(h('div', { id: 'veil', onclick: closeDrawer }), drawer);
  document.addEventListener('keydown', drawerKey);
  try {
    const [run, evals, diag, tax] = await Promise.all([api('/v1/runs/' + q(id)), api('/v1/runs/' + q(id) + '/evals').catch(() => []), api('/v1/runs/' + q(id) + '/diagnosis').catch(() => null), taxonomy().catch(() => ({}))]);
    bd.replaceChildren(...runSections(run, evals, diag, tax));
  } catch (e) { bd.replaceChildren(errorBox(e, () => openRun(id))); }
}
function runSections(run, evals, diag, tax) {
  const out = [];
  const meta = h('dl', { class: 'kv' });
  [['Status', statusBadge(run.status)], ['Agent', run.agent], ['Version', run.version || '(none)'], ['Environment', run.environment], ['Started', fmt.date(run.started_at) + ' (' + fmt.ago(run.started_at) + ')'], ['Took', fmt.ms(run.latency_ms)],
    ...Object.entries(run.tags || {}).filter(([k]) => !['conversation', 'turn_id'].includes(k)).map(([k, v]) => ['Tag ' + k, typeof v === 'object' ? JSON.stringify(v) : String(v)])]
    .forEach(([k, v]) => meta.append(h('dt', { text: k }), h('dd', null, v)));
  out.push(h('div', { class: 'sect' }, meta));
  out.push(h('div', { class: 'sect' }, h('h3', { text: 'Question' }), h('pre', { class: 'box', text: fmt.text(run.input) || '(empty)' })));
  out.push(h('div', { class: 'sect' }, h('h3', { text: 'Answer' }), h('pre', { class: 'box', text: fmt.text(run.output) || '(no answer was produced)' })));
  if (run.error) out.push(h('div', { class: 'sect' }, h('h3', { text: 'Error' }), h('pre', { class: 'box', style: { color: 'var(--bad)' }, text: run.error })));
  if (diag && diag.status === 'failed') {
    const t = tax[diag.cause] || {};
    out.push(callout('bad', 'Why it failed: ' + fmt.cause(diag.cause) + (diag.strength ? ' (' + diag.strength + ' evidence)' : ''),
      h('p', { text: diag.explanation || t.meaning || '' }),
      (t.advice && t.advice.length) ? h('div', null, h('b', { text: 'What to change' }), h('ul', null, t.advice.map((a) => h('li', { text: a })))) : null));
  } else if (diag) out.push(callout('info', 'No failure found', h('p', { text: 'The checks and steps for this run look healthy.' })));
  const ran = evals.filter((x) => x.applicable);
  if (ran.length) out.push(h('div', { class: 'sect' }, h('h3', { text: 'Checks' }), table([
    { label: 'Check', render: (x) => x.scorer.replace(/_/g, ' ') }, { label: 'Result', render: (x) => (x.passed ? badge('ok', 'passed') : badge('bad', 'failed')) }, { label: 'Why', render: (x) => x.reason || '' }], ran)));
  if (run.steps && run.steps.length) out.push(waterfall(run));
  const acts = h('div', { class: 'row' });
  acts.append(h('button', { class: 'btn', type: 'button', text: 'Make a regression test from this run', onclick: async () => { const r = await act(null, () => post('/v1/runs/' + q(run.id) + '/to-test', {}), false); if (r) toast(r.created ? 'Regression test created.' : 'A test for this failure already exists.'); } }),
    h('button', { class: 'btn', type: 'button', text: 'Check again', onclick: () => openRun(run.id) }));
  if (run.tags && run.tags.turn_id) {
    const fb = (rating) => act(rating === 'up' ? 'Marked as a good answer.' : 'Marked as a bad answer.', () => post('/v1/feedback', { agent: run.agent, turn_id: run.tags.turn_id, rating }), false).then(() => openRun(run.id));
    acts.append(h('button', { class: 'btn', type: 'button', text: '👍 Good answer', onclick: () => fb('up') }), h('button', { class: 'btn', type: 'button', text: '👎 Bad answer', onclick: () => fb('down') }));
  }
  out.push(acts);
  return out;
}
function waterfall(run) {
  const t0 = new Date(run.started_at).getTime(), total = Math.max(run.latency_ms || 1, ...run.steps.map((s) => (s.started_at && s.latency_ms ? new Date(s.started_at).getTime() - t0 + s.latency_ms : 0)), 1);
  const wrap = h('div', { class: 'wf' });
  run.steps.forEach((s) => {
    const off = s.started_at ? Math.max(0, new Date(s.started_at).getTime() - t0) : 0, w = s.latency_ms || 0;
    const detail = h('div', { hidden: true, class: 'stack' },
      s.error ? h('pre', { class: 'box', style: { color: 'var(--bad)' }, text: s.error }) : null,
      h('pre', { class: 'box', text: 'in: ' + fmt.short(JSON.stringify(s.input), 600) + '\nout: ' + fmt.short(JSON.stringify(s.output), 900) + (Object.keys(s.attributes || {}).length ? '\n' + JSON.stringify(s.attributes) : '') }));
    const row = h('div', { class: 's click', tabindex: '0', role: 'button', 'aria-expanded': 'false' },
      h('span', { class: 'nm', title: s.name }, s.status === 'error' ? badge('bad', s.name) : s.name),
      h('span', { class: 'tr' }, h('span', { class: 'b ' + (s.status === 'error' ? 'bad' : s.kind === 'llm' ? '' : 'tool'), style: { left: (off / total * 100) + '%', width: Math.max(0.8, w / total * 100) + '%' } })),
      h('span', { class: 'muted num', text: fmt.ms(s.latency_ms) }));
    const tg = () => { detail.hidden = !detail.hidden; row.setAttribute('aria-expanded', String(!detail.hidden)); };
    row.addEventListener('click', tg); row.addEventListener('keydown', (e) => { if (e.key === 'Enter') tg(); });
    wrap.append(row, detail);
  });
  return h('div', { class: 'sect' }, h('h3', { text: 'Steps (' + run.steps.length + ')' }), wrap);
}
