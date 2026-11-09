"use strict";
function lineDiff(a, b) {
  const x = (a || '').split('\n'), y = (b || '').split('\n');
  const n = x.length, m = y.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) { if (x[i] === y[j]) { out.push([' ', x[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['-', x[i++]]); else out.push(['+', y[j++]]); }
  while (i < n) out.push(['-', x[i++]]); while (j < m) out.push(['+', y[j++]]);
  return out;
}
function diffEl(a, b) {
  const box = h('pre', { class: 'box' });
  lineDiff(a, b).forEach(([k, t]) => box.append(h('div', { style: { background: k === '+' ? 'var(--ok-bg)' : k === '-' ? 'var(--bad-bg)' : '' } }, k + ' ' + t)));
  return box;
}

views.versions = {
  title: 'Versions',
  async render(root) {
    needAgent();
    const vs = await api(A() + '/versions');
    const out = [lookFor('A version is one saved prompt plus settings (model, tools). The Auditor tells runs apart by version, so you can see whether a change helped or hurt. Pick two to compare them.')];
    const act1 = h('button', { class: 'btn primary', type: 'button', text: 'Register a version', onclick: async () => {
      const v = await formDialog({ title: 'Register a version', intro: 'Record a prompt and settings so runs can be tagged with it. Identical content is the same version.', fields: [{ name: 'label', label: 'Name', required: true, placeholder: 'v2-shorter-answers' }, { name: 'prompt', label: 'Prompt', type: 'textarea' }, { name: 'config', label: 'Settings (JSON)', type: 'textarea', placeholder: '{"model": "gemini-3.5-flash"}' }, { name: 'activate', label: 'Make it the active version', type: 'checkbox' }], submit: 'Register' });
      if (!v) return;
      let config = {}; try { config = v.config ? JSON.parse(v.config) : {}; } catch (e) { return toast('The settings are not valid JSON.', true); }
      act('Version registered.', () => post(A() + '/versions', { label: v.label, prompt: v.prompt || null, config, activate: v.activate }));
    } });
    out.push(card({ title: 'Versions of ' + state.agent, actions: [act1], body: table([
      { label: 'Version', render: (v) => v.label }, { label: 'State', render: (v) => (v.is_active ? badge('ok', 'active') : badge('mute', 'inactive')) }, { label: 'Created', render: (v) => fmt.date(v.created_at) },
      { label: 'Settings', clip: true, render: (v) => fmt.short(Object.entries(v.config || {}).filter(([k]) => !['features', 'extras', 'tools'].includes(k)).map(([k, x]) => k + ': ' + x).join(', '), 80) },
      { label: 'Prompt', clip: true, render: (v) => fmt.short(v.prompt || '', 70) }], vs, { onclick: (v) => showVersion(v), empty: 'No versions registered. The chat registers one when it starts.' }) }));
    if (vs.length >= 2) {
      const sel = (id, i) => h('select', { class: 'sel', id, 'aria-label': id }, vs.map((v) => h('option', { value: v.label, text: v.label, selected: v === vs[i] })));
      const a = sel('cmpA', 1), b = sel('cmpB', 0), res = h('div', { class: 'stack' });
      const run = async () => {
        res.replaceChildren(loading('Comparing…'));
        const va = vs.find((v) => v.label === a.value), vb = vs.find((v) => v.label === b.value);
        try {
          const c = await api(A() + '/compare?baseline=' + q(a.value) + '&candidate=' + q(b.value) + '&min_samples=10');
          res.replaceChildren(h('div', { class: 'row' }, h('b', { text: 'Verdict:' }), verdictBadge(c.overall_verdict), h('span', { class: 'muted', text: (c.notes || [])[0] || '' })),
            table([{ label: 'Check', render: (s) => s.scorer.replace(/_/g, ' ') }, { label: a.value, num: true, render: (s) => fmt.pct(s.baseline_pass_rate) + ' (n=' + s.baseline_n + ')' }, { label: b.value, num: true, render: (s) => fmt.pct(s.candidate_pass_rate) + ' (n=' + s.candidate_n + ')' }, { label: 'Change', num: true, render: (s) => (s.delta == null ? '' : (s.delta > 0 ? '+' : '') + (s.delta * 100).toFixed(1) + ' pts') }, { label: 'Verdict', render: (s) => verdictBadge(s.verdict) }, { label: 'Why', clip: true, render: (s) => s.reason }], c.scorers),
            h('div', { class: 'sect' }, h('h3', { text: 'Prompt changes' }), (va.prompt || vb.prompt) ? diffEl(va.prompt, vb.prompt) : h('div', { class: 'muted', text: 'Neither version has a prompt.' })));
        } catch (e) { res.replaceChildren(errorBox(e, run)); }
      };
      out.push(h('div', { style: { height: '14px' } }), card({ title: 'Compare two versions', sub: 'Baseline on the left, candidate on the right. Needs quality checks to have been run on both.', actions: [a, h('span', { text: 'vs' }), b, h('button', { class: 'btn primary', type: 'button', text: 'Compare', onclick: run })], body: res }));
    }
    root.replaceChildren(...out);
  },
};
function showVersion(v) {
  const dlg = h('dialog', { 'aria-label': 'Version ' + v.label });
  dlg.append(h('form', { method: 'dialog' }, h('h2', { text: v.label }), h('dl', { class: 'kv' }, h('dt', { text: 'State' }), h('dd', { text: v.is_active ? 'active' : 'inactive' }), h('dt', { text: 'Created' }), h('dd', { text: fmt.date(v.created_at) }), h('dt', { text: 'Fingerprint' }), h('dd', { class: 'mono', text: v.content_hash.slice(0, 16) })),
    h('div', { class: 'sect' }, h('h3', { text: 'Prompt' }), h('pre', { class: 'box', text: v.prompt || '(none)' })), h('div', { class: 'sect' }, h('h3', { text: 'Settings' }), h('pre', { class: 'box', text: JSON.stringify(v.config, null, 2) })),
    h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'submit', text: 'Close' }))));
  document.body.append(dlg); dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
}

views.quality = {
  title: 'Quality',
  async render(root) {
    needAgent();
    const [sum, scorers] = await Promise.all([api(A() + '/eval-summary?since=' + q(sinceIso())), api('/v1/evals/scorers')]);
    const out = [lookFor('Quality checks are small automatic tests run on every answer (did it fail, is it empty, is it grounded, does it meet the expected text). The bars show how many answers passed each check, with a 95% range: a short range means we can trust the number.')];
    const runBtn = h('button', { class: 'btn primary', type: 'button', text: 'Run checks now', onclick: async () => { const r = await act(null, () => post('/v1/evals/run', { agent: state.agent, limit: 500 }), false); if (r) { toast('Checked ' + r.evaluated_runs + ' runs (' + r.scored + ' results).'); refreshView(); } } });
    if (!sum.versions.length) out.push(emptyState('No quality results yet', 'Run the checks to score the recorded answers.', runBtn));
    sum.versions.forEach((v) => {
      const rows = v.scorers.filter((s) => s.n).map((s) => ({ label: s.scorer.replace(/_/g, ' '), value: s.pass_rate || 0, text: fmt.pct(s.pass_rate) + '  (' + s.passed + '/' + s.n + ')', kind: (s.pass_rate || 0) >= 0.9 ? 'ok' : (s.pass_rate || 0) >= 0.7 ? '' : 'bad', tip: ['95% range ' + fmt.pct(s.ci_low) + ' to ' + fmt.pct(s.ci_high)] }));
      out.push(twinCard({ title: 'Version ' + v.version, sub: v.runs + ' runs, ' + v.evaluated_runs + ' checked', actions: [runBtn.cloneNode(true)].map((b) => { b.addEventListener('click', () => runBtn.click()); return b; }), chart: () => barList(rows, { max: 1, empty: 'No checks apply to these runs.' }),
        tableEl: () => table([{ label: 'Check', render: (s) => s.scorer.replace(/_/g, ' ') }, { label: 'Passed', num: true, render: (s) => s.passed + '/' + s.n }, { label: 'Rate', num: true, render: (s) => fmt.pct(s.pass_rate) }, { label: '95% range', num: true, render: (s) => fmt.pct(s.ci_low) + ' to ' + fmt.pct(s.ci_high) }], v.scorers.filter((s) => s.n)) }), h('div', { style: { height: '14px' } }));
    });
    out.push(card({ title: 'Available checks', body: table([{ label: 'Check', render: (s) => s.name.replace(/_/g, ' ') }, { label: 'What it tests', render: (s) => s.description }, { label: 'Runs by default', render: (s) => (s.default_enabled ? badge('ok', 'yes') : badge('mute', 'only if asked')) }], scorers) }));
    root.replaceChildren(...out);
  },
};
