"use strict";
function tapeEl(runs) {
  const list = runs.slice().reverse(); // oldest on the left, newest on the right
  const maxLat = Math.max(1, ...list.map((r) => r.latency_ms || 0));
  const strip = h('div', { class: 'strip', role: 'listbox', 'aria-label': 'Recent runs, oldest to newest', tabindex: '0' });
  let sel = -1, prevVersion = null; const ticks = [];
  list.forEach((r, i) => {
    const hgt = 14 + Math.round(Math.sqrt((r.latency_ms || 0) / maxLat) * 50);
    const tk = h('button', { class: 'tk' + (r.status === 'error' ? ' bad' : '') + (prevVersion !== null && r.version !== prevVersion ? ' dep' : ''), type: 'button', tabindex: '-1', style: { height: hgt + 'px' }, 'aria-label': (r.status === 'ok' ? 'ok' : 'failed') + ' run ' + fmt.date(r.started_at), onclick: () => openRun(r.id) });
    withTip(tk, () => [x0(r) + ' \u00b7 ' + fmt.ms(r.latency_ms), [fmt.date(r.started_at) + (r.version ? ' \u00b7 ' + r.version : ''), fmt.short(fmt.text(r.input), 80), r.error ? fmt.short(r.error, 90) : ''].filter(Boolean)]);
    prevVersion = r.version; ticks.push(tk); strip.append(tk);
  });
  const mark = (i) => { ticks.forEach((t, k) => t.classList.toggle('on', k === i)); if (ticks[i]) ticks[i].scrollIntoView({ block: 'nearest', inline: 'nearest' }); };
  strip.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') { sel = Math.max(0, (sel < 0 ? ticks.length : sel) - 1); mark(sel); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { sel = Math.min(ticks.length - 1, sel + 1); mark(sel); e.preventDefault(); }
    else if (e.key === 'Enter' && sel >= 0) openRun(list[sel].id);
  });
  requestAnimationFrame(() => { strip.scrollLeft = strip.scrollWidth; });
  const bad = list.filter((r) => r.status === 'error').length;
  return h('div', { class: 'tape' }, strip,
    h('div', { class: 'axis' }, h('span', { text: list.length ? fmt.date(list[0].started_at) : '' }), h('span', { text: list.length + ' runs, ' + bad + ' failed' }), h('span', { text: list.length ? fmt.date(list[list.length - 1].started_at) : '' })),
    h('div', { class: 'legend' }, h('span', null, h('i', { style: { background: 'var(--ok)' } }), 'ok'), h('span', null, h('i', { style: { background: 'var(--bad)' } }), 'failed'), h('span', { text: 'Taller means slower. A dashed line marks a new version going live. Click a tick to open the run.' })));
}
const x0 = (r) => (r.status === 'ok' ? '✓ ok' : '✕ failed');

function activityChart(tl) {
  const b = tl.buckets, W = 760, H = 170, pl = 34, pb = 22, pt = 8;
  const max = Math.max(1, ...b.map((x) => x.ok + x.error)), bw = (W - pl) / Math.max(1, b.length);
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': 'Runs over time, stacked by ok and failed' });
  [0, 0.5, 1].forEach((f) => { const y = pt + (H - pb - pt) * (1 - f); svg.append(sv('line', { x1: pl, x2: W, y1: y, y2: y, stroke: 'var(--grid)' }), sv('text', { x: pl - 5, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--muted)', text: Math.round(max * f) })); });
  b.forEach((x, i) => {
    const hOk = (H - pb - pt) * (x.ok / max), hEr = (H - pb - pt) * (x.error / max), xx = pl + i * bw + 1, w = Math.max(1, bw - 2);
    const g = sv('g', { tabindex: '0' });
    g.append(sv('rect', { x: xx, y: H - pb - hOk, width: w, height: hOk, fill: 'var(--ok)', opacity: 0.75 }), sv('rect', { x: xx, y: H - pb - hOk - hEr - (hOk && hEr ? 2 : 0), width: w, height: hEr, fill: 'var(--bad)' }), sv('rect', { x: xx, y: pt, width: w, height: H - pb - pt, fill: 'transparent' }));
    withTip(g, () => [fmt.date(x.t), [x.ok + ' ok, ' + x.error + ' failed', x.p50_ms != null ? 'median ' + fmt.ms(x.p50_ms) : ''].filter(Boolean)]);
    svg.append(g);
  });
  if (b.length) svg.append(sv('text', { x: pl, y: H - 5, 'font-size': 11, fill: 'var(--muted)', text: fmt.date(b[0].t) }), sv('text', { x: W, y: H - 5, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--muted)', text: fmt.date(b[b.length - 1].t) }));
  return svg;
}

views.overview = {
  title: 'Overview',
  async render(root) {
    const a = state.agent ? '?agent=' + q(state.agent) + '&' : '?';
    root.replaceChildren(loading());
    const hrs = winHours();
    const [ov, tl, runs, fl] = await Promise.all([
      api('/v1/insights/overview' + a + 'hours=' + hrs), api('/v1/insights/timeline' + a + 'hours=' + Math.min(hrs, 168)),
      api('/v1/runs?limit=200' + (state.agent ? '&agent=' + q(state.agent) : '')), api('/v1/insights/failures' + a + 'hours=' + hrs + '&limit=5')]);
    state.failures = ov.problem_runs; drawNavCounts();
    const high = ov.alerts.filter((x) => x.severity === 'high').length, med = ov.alerts.filter((x) => x.severity === 'medium').length;
    const kind = high ? 'bad' : med ? 'warn' : 'ok';
    const out = [];
    if (!ov.runs && !ov.agents.length) { root.replaceChildren(onboarding()); return; }
    out.push(h('div', { class: 'verdict ' + kind }, h('div', { class: 'mark', 'aria-hidden': 'true', text: kind === 'ok' ? '✓' : kind === 'bad' ? '!' : '▲' }),
      h('div', null, h('h2', { text: kind === 'ok' ? 'All clear' : (high + med) + (high + med === 1 ? ' thing needs' : ' things need') + ' attention' }),
        h('p', { text: ov.runs ? ov.runs + ' runs in the last ' + WINDOWS[state.win][0] + ': ' + ov.errors + ' errored, ' + Math.max(0, ov.problem_runs - ov.errors) + ' more answered badly' + (ov.pass_rate != null ? '; ' + fmt.pct(ov.pass_rate) + ' of quality checks passed.' : '.') : 'No runs in this period yet.' })),
      h('div', { style: { marginLeft: 'auto' }, class: 'row' }, h('button', { class: 'btn', type: 'button', text: 'Score and diagnose now', onclick: () => scoreNow() }))));
    out.push(h('div', { class: 'tiles' },
      tile('Runs', fmt.int(ov.runs), ov.agents.length + ' agent' + (ov.agents.length === 1 ? '' : 's')),
      tile('Runs with problems', ov.problem_rate == null ? 'n/a' : fmt.pct(ov.problem_rate, 1), ov.errors + ' errored, ' + Math.max(0, ov.problem_runs - ov.errors) + ' bad answers', ov.problem_rate > 0.1 ? 'bad' : ''),
      tile('Quality checks passed', ov.pass_rate == null ? 'n/a' : fmt.pct(ov.pass_rate), ov.evaluated ? ov.passed + ' of ' + ov.evaluated : 'not scored yet', ov.pass_rate != null && ov.pass_rate < 0.8 ? 'bad' : ov.pass_rate != null ? 'ok' : ''),
      tile('Median answer time', fmt.ms(ov.p50_ms), 'slowest 5%: ' + fmt.ms(ov.p95_ms)),
      tile('Not scored yet', fmt.int(ov.unevaluated), ov.unevaluated ? 'press Score and diagnose' : 'all runs checked')));
    out.push(card({ title: 'The tape', sub: 'Every recent run, oldest to newest. This is your agent’s flight recorder.', body: runs.length ? tapeEl(runs) : h('div', { class: 'muted', text: 'No runs yet.' }), cls: 'pad' }));
    out.push(h('div', { style: { height: '14px' } }));
    out.push(card({ title: 'Action needed', sub: 'What the Auditor found, most urgent first.', body: ov.alerts.length ? h('div', { class: 'stack' }, ov.alerts.map((al) => h('div', { class: 'callout ' + (al.severity === 'high' ? 'bad' : al.severity === 'medium' ? 'warn' : 'info') }, h('div', { class: 'row' }, h('b', { text: al.title }), h('button', { class: 'btn small', type: 'button', style: { marginLeft: 'auto' }, text: 'Open ' + al.view.replace('-', ' '), onclick: () => go(al.view) })), al.detail ? h('div', { class: 'muted', text: al.detail }) : null))) : h('div', { class: 'muted', text: 'Nothing needs attention right now.' }), cls: 'pad' }));
    out.push(h('div', { style: { height: '14px' } }));
    const left = twinCard({ title: 'Runs over time', sub: 'Green is ok, orange is failed.', chart: () => activityChart(tl), tableEl: () => table([{ label: 'Period', render: (x) => fmt.date(x.t) }, { label: 'ok', num: true, key: 'ok' }, { label: 'failed', num: true, key: 'error' }, { label: 'median', num: true, render: (x) => fmt.ms(x.p50_ms) }], tl.buckets.filter((x) => x.ok || x.error)) });
    const causeRows = Object.entries(ov.causes).sort((x, y) => y[1] - x[1]).map(([c, n]) => ({ label: fmt.cause(c), value: n, kind: 'bad', onclick: () => go('causes') }));
    const kindRows = Object.entries(ov.error_kinds).sort((x, y) => y[1] - x[1]).map(([c, n]) => ({ label: fmt.cause(c), value: n, kind: 'bad', onclick: () => go('failures') }));
    const right = card({ title: 'What is going wrong', sub: 'Likely cause of each failed run, then the kind of error text.', body: h('div', { class: 'stack' }, h('div', null, h('b', { text: 'Causes' }), barList(causeRows, { empty: 'No diagnosed failures.' })), h('div', null, h('b', { text: 'Error messages' }), barList(kindRows, { empty: 'No error messages recorded.' }))) });
    out.push(h('div', { class: 'grid g2' }, left, right));
    out.push(h('div', { style: { height: '14px' } }));
    if (fl.items.length) out.push(card({ title: 'Latest failures', actions: [h('button', { class: 'btn small', type: 'button', text: 'See all', onclick: () => go('failures') })], body: h('div', { class: 'stack' }, fl.items.map((f) => failureCard(f))) }));
    out.push(h('div', { style: { height: '14px' } }));
    out.push(card({ title: 'Agents', sub: 'Click one to look at it alone.', body: table([{ label: 'Agent', render: (x) => x.name }, { label: 'Active version', render: (x) => x.active_version || '(none)' }, { label: 'Runs', num: true, render: (x) => fmt.int(x.runs) }, { label: 'Failed', num: true, render: (x) => x.errors }, { label: 'Failure rate', num: true, render: (x) => fmt.pct(x.error_rate, 1) }, { label: 'Drift', render: (x) => (x.drift ? verdictBadge(x.drift) : h('span', { class: 'muted', text: 'not checked' })) }, { label: 'Last run', render: (x) => (x.last_run ? fmt.ago(x.last_run) : '') }], ov.agents, { onclick: (x) => { selectAgent(x.name); } }) }));
    root.replaceChildren(...out);
  },
};
const tile = (l, v, s, k) => h('div', { class: 'tile ' + (k || '') }, h('div', { class: 'l', text: l }), h('div', { class: 'v', text: v }), h('div', { class: 's', text: s || '' }));

async function scoreNow() {
  try {
    toast('Scoring and diagnosing…');
    const body = state.agent ? { agent: state.agent, limit: 500 } : { limit: 500 };
    const ev = await post('/v1/evals/run', body);
    const targets = state.agent ? [state.agent] : state.agents.map((x) => x.name);
    for (const t of targets) await post('/v1/agents/' + q(t) + '/diagnose', { limit: 500 }).catch(() => null);
    toast('Checked ' + ev.evaluated_runs + ' runs.'); refreshView();
  } catch (e) { toast(e.message, true); }
}

function failureCard(f, opts) {
  const o = opts || {};
  const el = h('article', { class: 'fail' },
    h('div', { class: 'row' }, f.cause ? badge('bad', fmt.cause(f.cause)) : badge('bad', f.error_kind ? fmt.cause(f.error_kind) : 'failed'), f.error_kind && f.cause ? badge('mute', 'error: ' + fmt.cause(f.error_kind)) : null,
      h('span', { class: 'muted', text: fmt.ago(f.started_at) + ' · ' + f.agent + (f.version ? ' · ' + f.version : '') + ' · ' + f.environment }), h('button', { class: 'btn small', type: 'button', style: { marginLeft: 'auto' }, text: 'Open trace', onclick: () => openRun(f.run_id) })),
    h('div', { class: 'q', text: fmt.short(f.question, 200) || '(empty question)' }),
    f.error ? h('div', { class: 'err', text: f.error }) : (f.answer ? h('div', { class: 'ans', text: fmt.short(f.answer, 260) }) : null),
    f.failed_checks && f.failed_checks.length ? h('ul', null, f.failed_checks.map((c) => h('li', null, h('b', { text: c.scorer.replace(/_/g, ' ') + ': ' }), c.reason))) : null,
    f.advice && f.advice.length ? h('div', { class: 'todo' }, h('b', { text: 'What to change: ' }), f.advice[0], f.advice.length > 1 && o.full ? h('ul', null, f.advice.slice(1).map((x) => h('li', { text: x }))) : null) : null);
  return el;
}

function onboarding() {
  return h('div', { class: 'stack' },
    h('div', { class: 'empty' }, h('h3', { text: 'No agents are sending runs yet' }), h('p', { text: 'Nothing is wrong. The Auditor is running, and it is waiting for an agent to report to it.' }),
      h('div', { class: 'row', style: { justifyContent: 'center' } }, h('button', { class: 'btn primary', type: 'button', text: 'Connect the chat app', onclick: () => go('setup') }), h('button', { class: 'btn', type: 'button', text: 'Load demo data to explore', onclick: () => seedDemo() }))),
    lookFor('Demo data adds a made-up support bot with two versions, some failures, and a version that is worse than the other, so every page has something to show. You can ignore it later.'));
}
