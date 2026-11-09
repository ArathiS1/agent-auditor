"use strict";
const CHAT = 'chat-agent';
const benchAgent = () => (state.agent && !/^demo-/.test(state.agent) ? state.agent : CHAT);
views.benchmark = {
  title: 'Benchmark',
  async render(root) {
    let d;
    try { d = await api('/v1/insights/benchmark?agent=' + q(benchAgent())); }
    catch (e) { if (e.status !== 404) throw e; d = { targets: [], advice: [], categories: [], questions: [] }; }
    const out = [lookFor('The same questions are asked of several AI models and each answer is checked against what a good answer must contain. This shows which model suits your questions, where your current one is weak, and what to change. Small question sets give wide uncertainty ranges: treat gaps under about 10 points as a tie.')];
    if (!d.targets.length) {
      out.push(emptyState('No benchmark results yet', 'Run the benchmark from a terminal. It asks every question of each model whose key you have set, and sends the answers here.',
        cmd('python bench\\run.py                 # every model whose key is in your .env\npython bench\\run.py --quick         # a short set, two questions per topic\npython bench\\run.py --models gemini,openai,anthropic'), h('p', { class: 'muted', text: 'Add keys such as OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, PERPLEXITY_API_KEY to your .env first.' })));
      root.replaceChildren(...out); return;
    }
    if (d.advice.length) out.push(card({ title: 'What to change', sub: 'Plain recommendations from the numbers below.', body: h('div', { class: 'stack' }, d.advice.map((a) => h('div', { class: 'callout ' + (a.severity === 'high' ? 'bad' : a.severity === 'medium' ? 'warn' : 'info') }, h('div', { class: 'row' }, badge(a.severity === 'high' ? 'bad' : a.severity === 'medium' ? 'warn' : 'info', a.area), h('b', { text: a.title })), h('p', { text: a.detail }), h('div', { class: 'muted', text: 'Evidence: ' + a.evidence })))) }), h('div', { style: { height: '14px' } }));
    out.push(twinCard({ title: 'Leaderboard', sub: 'Share of answers that met their expectation. The line is the 95% range.', chart: () => leaderboard(d.targets),
      tableEl: () => table([{ label: 'Model', render: (t) => t.target + (t.current ? ' (current)' : '') }, { label: 'Passed', num: true, render: (t) => t.quality.passed + '/' + t.quality.n }, { label: 'Rate', num: true, render: (t) => fmt.pct(t.quality.rate) }, { label: '95% range', num: true, render: (t) => fmt.pct(t.quality.low) + ' to ' + fmt.pct(t.quality.high) }, { label: 'Median time', num: true, render: (t) => fmt.ms(t.p50_ms) }, { label: 'Slowest 5%', num: true, render: (t) => fmt.ms(t.p95_ms) }, { label: 'Errors', num: true, render: (t) => t.errors + '/' + t.questions }, { label: 'Avg words', num: true, render: (t) => (t.avg_words == null ? '' : Math.round(t.avg_words)) }, { label: 'People', render: (t) => '👍 ' + t.feedback.up + '  👎 ' + t.feedback.down }], d.targets) }));
    out.push(h('div', { style: { height: '14px' } }));
    if (d.categories.length) out.push(card({ title: 'Where each model is strong or weak', sub: 'Pass rate by kind of question.', body: heat(d) }), h('div', { style: { height: '14px' } }));
    out.push(card({ title: 'Question by question', sub: 'Click a cell to read the answer and why it passed or failed.', body: questionGrid(d) }));
    root.replaceChildren(...out);
  },
};
function leaderboard(ts) {
  const W = 760, rowH = 38, pl = 210, pr = 70, H = ts.length * rowH + 30;
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': 'Pass rate per model with 95 percent ranges' });
  const x = (v) => pl + (W - pl - pr) * v;
  [0, 0.25, 0.5, 0.75, 1].forEach((v) => svg.append(sv('line', { x1: x(v), x2: x(v), y1: 4, y2: H - 22, stroke: 'var(--grid)' }), sv('text', { x: x(v), y: H - 6, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--muted)', text: v * 100 + '%' })));
  ts.forEach((t, i) => {
    const y = 8 + i * rowH + rowH / 2, qv = t.quality;
    const g = sv('g', { tabindex: '0' });
    g.append(sv('text', { x: pl - 10, y: y + 4, 'text-anchor': 'end', 'font-size': 13, fill: 'var(--ink)', 'font-weight': t.current ? 700 : 400, text: fmt.short(t.target, 28) + (t.current ? ' ★' : '') }));
    if (qv.n) {
      g.append(sv('line', { x1: x(qv.low), x2: x(qv.high), y1: y, y2: y, stroke: 'var(--series)', 'stroke-width': 2 }), sv('line', { x1: x(qv.low), x2: x(qv.low), y1: y - 6, y2: y + 6, stroke: 'var(--series)', 'stroke-width': 2 }), sv('line', { x1: x(qv.high), x2: x(qv.high), y1: y - 6, y2: y + 6, stroke: 'var(--series)', 'stroke-width': 2 }),
        sv('circle', { cx: x(qv.rate), cy: y, r: 6, fill: 'var(--series)', stroke: 'var(--raised)', 'stroke-width': 2 }), sv('text', { x: W - pr + 10, y: y + 4, 'font-size': 13, fill: 'var(--ink)', text: fmt.pct(qv.rate) }));
    } else g.append(sv('text', { x: pl, y: y + 4, 'font-size': 12, fill: 'var(--muted)', text: 'no answers were checked' }));
    g.append(sv('rect', { x: 0, y: y - rowH / 2, width: W, height: rowH, fill: 'transparent' }));
    withTip(g, () => [t.target, [qv.n ? qv.passed + ' of ' + qv.n + ' passed (range ' + fmt.pct(qv.low) + ' to ' + fmt.pct(qv.high) + ')' : 'nothing checked', 'median ' + fmt.ms(t.p50_ms) + ', errors ' + t.errors + '/' + t.questions]]);
    svg.append(g);
  });
  return svg;
}
function heat(d) {
  const grid = h('div', { class: 'heat', style: { gridTemplateColumns: 'minmax(160px,1.2fr) repeat(' + d.categories.length + ', minmax(70px,1fr))' } });
  grid.append(h('div', { class: 'h', text: 'Model' }), ...d.categories.map((c) => h('div', { class: 'h', text: c })));
  d.targets.forEach((t) => {
    grid.append(h('div', { class: 'h', text: t.target }));
    d.categories.forEach((c) => { const v = t.categories[c]; const r = v && v.rate; grid.append(h('div', { class: 'c ' + (v == null ? 'na' : r >= 0.8 ? 'ok' : r < 0.5 ? 'bad' : 'na'), title: v ? v.passed + '/' + v.n : 'no data', text: v ? fmt.pct(r) : '–', style: v && r >= 0.5 && r < 0.8 ? { background: 'var(--warn-bg)', color: 'var(--warn)' } : {} })); });
  });
  return h('div', { class: 'tw' }, grid);
}
function questionGrid(d) {
  const ts = d.targets;
  const head = ['Question', ...ts.map((t) => t.target)];
  const tb = h('table', { class: 't' }, h('thead', null, h('tr', null, head.map((x) => h('th', { text: x })))), h('tbody', null, d.questions.map((qn) => h('tr', null, h('td', { class: 'clip', title: qn.question }, h('span', { class: 'badge mute', text: qn.category }), ' ', fmt.short(qn.question, 70)), ts.map((t) => {
    const r = qn.results[t.version]; if (!r) return h('td', { class: 'muted', text: '–' });
    const b = r.status === 'error' ? badge('bad', 'error') : r.passed === true ? badge('ok', 'pass') : r.passed === false ? badge('bad', 'fail') : badge('mute', 'unchecked');
    const td = h('td'); const bt = h('button', { class: 'btn quiet small', type: 'button', onclick: () => answerDialog(qn, t, r) }, b); td.append(bt); return td;
  })))));
  return h('div', { class: 'tw' }, tb);
}
function answerDialog(qn, t, r) {
  const dlg = h('dialog', { 'aria-label': 'Answer' });
  dlg.append(h('form', { method: 'dialog' }, h('h2', { text: t.target }), h('p', { text: qn.question }), r.reason ? callout(r.passed ? 'info' : 'bad', null, h('span', { text: r.reason })) : null, r.error ? h('pre', { class: 'box', style: { color: 'var(--bad)' }, text: r.error }) : h('pre', { class: 'box', text: r.answer || '(no answer)' }),
    h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'button', text: 'Open the run', onclick: () => { dlg.close(); openRun(r.run_id); } }), h('button', { class: 'btn', type: 'submit', text: 'Close' }))));
  document.body.append(dlg); dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
}

views.gap = {
  title: 'Gap analysis',
  async render(root) {
    const d = await api('/v1/insights/gap-analysis?agent=' + q(CHAT));
    const out = [lookFor('How the chat app compares, feature by feature, with other assistants people use. The chat reports what it can do; the other columns are a hand-kept checklist, so confirm in the product before you build. Gaps are ranked: features most assistants have first, then the biggest benefit, then the least work.')];
    if (!d.reported) out.push(callout('warn', 'The chat has not reported its features yet', h('p', { text: 'Start the chat with tracing on (run-all.ps1 does this). It registers its feature list with the Auditor, and this page fills in.' })));
    out.push(callout('info', 'Checklist dated ' + d.as_of, h('p', { text: d.note }), h('div', { class: 'pill-row' }, d.sources.map((s) => h('a', { href: s.url, target: '_blank', rel: 'noopener noreferrer', text: s.title })))));
    out.push(h('div', { class: 'tiles', style: { marginTop: '14px' } }, tile('Features the chat has', d.covered + ' of ' + d.total, 'among the ones compared', 'ok'), tile('Gaps to close', String(d.gaps.length), 'most assistants have these', d.gaps.length ? 'bad' : 'ok'), tile('Only here', String(d.unique.length), 'things other assistants do not offer')));
    out.push(card({ title: 'Build next', sub: 'Ranked by how common the feature is, then benefit, then effort (S small, M medium, L large).', body: d.gaps.length ? table([{ label: 'Feature', render: (g) => h('b', { text: g.name }) }, { label: 'Others with it', num: true, render: (g) => g.others_yes + ' of ' + g.of }, { label: 'Benefit', render: (g) => '★'.repeat(g.impact) + '☆'.repeat(3 - g.impact) }, { label: 'Effort', render: (g) => badge(g.effort === 'S' ? 'ok' : g.effort === 'M' ? 'warn' : 'bad', g.effort) }, { label: 'How', render: (g) => g.how }], d.gaps) : h('div', { class: 'muted', text: 'No gaps against this checklist.' }) }));
    out.push(h('div', { style: { height: '14px' } }));
    const groups = [...new Set(d.rows.map((r) => r.group))];
    const mx = h('table', { class: 'mx' }, h('thead', null, h('tr', null, h('th', { text: 'Feature' }), d.products.map((p) => h('th', { text: p })), h('th', { text: 'This chat' }))));
    const tb = h('tbody');
    groups.forEach((g) => { tb.append(h('tr', null, h('td', { colspan: d.products.length + 2, style: { fontWeight: 600, background: 'var(--grid)' }, text: g }))); d.rows.filter((r) => r.group === g).forEach((r) => tb.append(h('tr', { class: r.status === 'gap' ? 'gap' : '' }, h('td', { text: r.name }), d.products.map((p) => { const v = r.values[p]; return h('td', { class: { yes: 'y', no: 'n', partial: 'p', unknown: 'u' }[v], title: v, text: { yes: '✓', no: '–', partial: '◐', unknown: '?' }[v] }); }), h('td', { class: 'us', title: r.ours, text: r.ours === 'yes' ? '✓' : '✕' })))); });
    mx.append(tb);
    out.push(card({ title: 'Feature by feature', sub: '✓ has it, ◐ partly, – does not, ? not confirmed. An orange edge marks a gap.', body: h('div', { class: 'tw' }, mx) }));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'What only this setup offers', body: h('ul', null, d.unique.map((u) => h('li', null, h('b', { text: u.name + '. ' }), u.detail))) }));
    root.replaceChildren(...out);
  },
};
