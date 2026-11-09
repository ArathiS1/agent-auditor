"use strict";
views.failures = {
  title: 'Failures',
  async render(root) {
    needAgentOptional();
    const f = { env: '', cause: null, kind: null };
    const body = h('div', { class: 'stack' });
    root.replaceChildren(lookFor('Each card is one run that went wrong. The orange tag is the likely cause; the box says what to change first. Open the trace to see every step.'), body);
    const draw = async () => {
      body.replaceChildren(loading());
      const qs = (state.agent ? 'agent=' + q(state.agent) + '&' : '') + 'hours=' + winHours() + '&limit=100' + (f.env ? '&environment=' + f.env : '');
      const d = await api('/v1/insights/failures?' + qs);
      const items = d.items.filter((x) => (!f.cause || x.cause === f.cause) && (!f.kind || x.error_kind === f.kind));
      const chip = (label, n, key, val) => h('button', { class: 'toggle', type: 'button', 'aria-pressed': String(f[key] === val), text: fmt.cause(label) + ' (' + n + ')', onclick: () => { f[key] = f[key] === val ? null : val; draw(); } });
      const envSel = h('select', { class: 'sel', 'aria-label': 'Environment', onchange: (e) => { f.env = e.target.value; draw(); } }, ['', 'production', 'canary', 'test'].map((v) => h('option', { value: v, text: v || 'All environments', selected: v === f.env })));
      const bar = h('div', { class: 'row' }, envSel, h('button', { class: 'btn', type: 'button', text: 'Turn failures into regression tests', onclick: () => genTests() }), h('button', { class: 'btn', type: 'button', text: 'Propose fixes', onclick: () => go('fixes') }), h('button', { class: 'btn', type: 'button', text: 'Refresh', onclick: draw }));
      const chips = h('div', { class: 'stack' },
        Object.keys(d.causes).length ? h('div', { class: 'pill-row' }, h('span', { class: 'muted', text: 'Cause:' }), Object.entries(d.causes).map(([k, n]) => chip(k, n, 'cause', k))) : null,
        Object.keys(d.error_kinds).length ? h('div', { class: 'pill-row' }, h('span', { class: 'muted', text: 'Error type:' }), Object.entries(d.error_kinds).map(([k, n]) => chip(k, n, 'kind', k))) : null);
      const list = items.length ? items.map((x) => { const c = failureCard(x, { full: true }); if (state.assistant.enabled && state.agent) c.append(h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', text: 'Ask the AI helper for a fix', onclick: () => suggestFix() }))); return c; })
        : [emptyState(d.count ? 'No failures match these filters' : 'No failures in this period', d.count ? 'Clear a filter above.' : 'Nothing failed in the last ' + WINDOWS[state.win][0] + '. Try a longer period, or run the chat and ask something hard.')];
      body.replaceChildren(bar, chips, h('div', { class: 'muted', text: d.count + ' failed run' + (d.count === 1 ? '' : 's') + ' in the last ' + WINDOWS[state.win][0] + (d.count >= 100 ? ' (showing the newest 100)' : '') }), ...list);
    };
    try { await draw(); } catch (e) { body.replaceChildren(errorBox(e, draw)); }
  },
};
const needAgentOptional = () => {};
async function genTests() {
  needAgent();
  const ok = await confirmDialog('Make regression tests', 'Each distinct failure becomes a saved test (input plus what a good answer must satisfy). Passwords and keys are redacted.', 'Create tests');
  if (!ok) return;
  const r = await act(null, () => post(A() + '/tests/generate', { environment: 'production', since_hours: winHours(), limit: 100 }), false);
  if (r) toast(r.created.length + ' test' + (r.created.length === 1 ? '' : 's') + ' created, ' + r.merged_duplicates + ' duplicates merged.');
}
async function suggestFix() {
  needAgent();
  const dlg = h('dialog', { 'aria-label': 'Suggested fix' }), out = h('div', { class: 'stack' }, loading('Asking the AI helper…'));
  dlg.append(h('form', { method: 'dialog' }, h('h2', { text: 'Suggested fix' }), out, h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'submit', text: 'Close' }))));
  document.body.append(dlg); dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
  try { const r = await post(A() + '/suggest-fix', { environment: 'production', since_hours: winHours() }); out.replaceChildren(h('pre', { class: 'box', text: r.suggestion }), h('p', { class: 'muted', text: r.note || '' })); }
  catch (e) { out.replaceChildren(errorBox(e)); }
}

views.runs = {
  title: 'Runs',
  async render(root) {
    const f = { status: '', env: '', version: '', text: '', offset: 0 }, PAGE = 40;
    const body = h('div', { class: 'stack' });
    let versions = [];
    try { if (state.agent) versions = (await api(A() + '/versions')).map((v) => v.label); } catch (e) { /* the table below shows errors */ }
    root.replaceChildren(lookFor('Every run the Auditor recorded. Use the filters to narrow down, then click a row to see the question, the answer and each step.'), body);
    const draw = async () => {
      const sel = (name, opts, key) => h('select', { class: 'sel', 'aria-label': name, onchange: (e) => { f[key] = e.target.value; f.offset = 0; draw(); } }, opts.map((o) => h('option', { value: o[0], text: o[1], selected: o[0] === f[key] })));
      const search = h('input', { class: 'sel', type: 'search', placeholder: 'Search question, answer or error', 'aria-label': 'Search runs', value: f.text, style: { minWidth: '260px' } });
      search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { f.text = search.value; f.offset = 0; draw(); } });
      const bar = h('div', { class: 'row' }, sel('Status', [['', 'Any status'], ['ok', 'ok'], ['error', 'failed']], 'status'), sel('Environment', [['', 'Any environment'], ['production', 'production'], ['canary', 'canary'], ['test', 'test']], 'env'), sel('Version', [['', 'Any version'], ...versions.map((v) => [v, v])], 'version'), search, h('button', { class: 'btn', type: 'button', text: 'Search', onclick: () => { f.text = search.value; f.offset = 0; draw(); } }));
      body.replaceChildren(bar, loading());
      try {
        const qs = ['limit=' + PAGE, 'offset=' + f.offset, state.agent && 'agent=' + q(state.agent), f.status && 'status=' + f.status, f.env && 'environment=' + f.env, f.version && 'version=' + q(f.version), f.text && 'q=' + q(f.text)].filter(Boolean).join('&');
        const runs = await api('/v1/runs?' + qs);
        const t = table([{ label: 'When', render: (r) => fmt.date(r.started_at) }, { label: 'Status', render: (r) => statusBadge(r.status) }, { label: 'Question', clip: true, render: (r) => fmt.short(fmt.text(r.input), 120) }, { label: 'Answer or error', clip: true, render: (r) => fmt.short(r.error || fmt.text(r.output), 120) },
          { label: 'Version', render: (r) => r.version || '' }, { label: 'Env', render: (r) => r.environment }, { label: 'Took', num: true, render: (r) => fmt.ms(r.latency_ms) }, { label: 'Steps', num: true, render: (r) => r.step_count }], runs, { onclick: (r) => openRun(r.id), empty: 'No runs match.' });
        const pager = h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', text: 'Newer', disabled: f.offset === 0, onclick: () => { f.offset = Math.max(0, f.offset - PAGE); draw(); } }), h('span', { class: 'muted', text: 'Showing ' + (runs.length ? f.offset + 1 : 0) + ' to ' + (f.offset + runs.length) }), h('button', { class: 'btn', type: 'button', text: 'Older', disabled: runs.length < PAGE, onclick: () => { f.offset += PAGE; draw(); } }));
        body.replaceChildren(bar, card({ title: 'Runs', body: t }), pager);
      } catch (e) { body.replaceChildren(bar, errorBox(e, draw)); }
    };
    draw();
  },
};
