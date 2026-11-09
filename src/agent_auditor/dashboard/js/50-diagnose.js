"use strict";
views.causes = {
  title: 'Root causes',
  async render(root) {
    needAgent();
    const tax = await taxonomy();
    const rc = await post(A() + '/root-causes', { environment: 'production', since_hours: winHours(), diagnose_first: true });
    const out = [lookFor('For every failed run the Auditor picks the most likely cause (a model error, a tool timeout, empty retrieval, a wrong answer, a thumbs-down...). A cause that is common is where one fix helps most. "Strong" evidence means an error pointed straight at it; "weak" means a guess from the symptoms.')];
    out.push(h('div', { class: 'tiles' }, tile('Runs looked at', fmt.int(rc.total_runs)), tile('Failed', fmt.int(rc.failed_runs), fmt.pct(rc.failure_rate, 1) + ' of runs', rc.failure_rate > 0.1 ? 'bad' : ''), tile('Healthy', fmt.int(rc.healthy_runs), '', 'ok')));
    if (!rc.causes.length) out.push(emptyState('No failures to explain', 'Every run in this period looks healthy.'));
    else {
      out.push(twinCard({ title: 'Causes, most common first', sub: 'Click a bar for what it means and what to change.', chart: () => barList(rc.causes.map((c) => ({ label: fmt.cause(c.cause), value: c.count, kind: 'bad', text: c.count + ' (' + fmt.pct(c.share_of_failures) + ')', onclick: () => causeDetail(c) })), {}),
        tableEl: () => table([{ label: 'Cause', render: (c) => fmt.cause(c.cause) }, { label: 'Runs', num: true, key: 'count' }, { label: 'Share of failures', num: true, render: (c) => fmt.pct(c.share_of_failures) }, { label: 'Evidence', render: (c) => Object.entries(c.strength).map(([k, n]) => k + ' ' + n).join(', ') }, { label: 'Where', render: (c) => c.top_steps.map((s) => s.name).join(', ') }], rc.causes, { onclick: causeDetail }) }));
    }
    const assoc = (rc.version_associations && rc.version_associations.associations) || [];
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'Is a cause tied to one version?', sub: 'A cause that is much more common in one version than the rest points at a recent change.', body: assoc.length ? table([{ label: 'Version', key: 'version' }, { label: 'Cause', render: (a) => fmt.cause(a.cause) }, { label: 'Direction', render: (a) => (a.direction || '').replace(/_/g, ' ') }, { label: 'In this version', num: true, render: (a) => fmt.pct(a.rate_in_version) }, { label: 'Elsewhere', num: true, render: (a) => fmt.pct(a.rate_elsewhere) }], assoc) : h('div', { class: 'muted', text: (rc.version_associations && rc.version_associations.note) || 'No cause stands out in one version.' }) }));
    if ((rc.suspect_sources || []).length) out.push(h('div', { style: { height: '14px' } }), card({ title: 'Documents that appear in failing runs', body: table([{ label: 'Source', key: 'source' }, { label: 'In failed runs', num: true, key: 'failed_runs' }, { label: 'Share of failed', num: true, render: (s) => fmt.pct(s.failed_share) }], rc.suspect_sources) }));
    (rc.notes || []).forEach((n) => out.push(h('p', { class: 'muted', text: n })));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'All the causes the Auditor can name', sub: 'Reference list.', body: table([{ label: 'Cause', render: (t) => fmt.cause(t.cause) }, { label: 'Meaning', key: 'meaning' }, { label: 'First thing to try', render: (t) => (t.advice || [])[0] || '' }], Object.values(tax)) }));
    root.replaceChildren(...out);
  },
};
function causeDetail(c) {
  const dlg = h('dialog', { 'aria-label': fmt.cause(c.cause) });
  dlg.append(h('form', { method: 'dialog' }, h('h2', { text: fmt.cause(c.cause) + ' (' + c.count + ' runs)' }), h('p', { text: c.meaning }),
    h('div', { class: 'sect' }, h('h3', { text: 'What to change' }), h('ul', null, (c.advice || []).map((a) => h('li', { text: a })))),
    c.top_steps.length ? h('p', { class: 'muted', text: 'Where it happens: ' + c.top_steps.map((s) => s.name + ' (' + s.count + ')').join(', ') }) : null,
    h('div', { class: 'sect' }, h('h3', { text: 'Example runs' }), h('div', { class: 'pill-row' }, c.example_run_ids.map((id) => h('button', { class: 'btn small', type: 'button', text: id.slice(0, 8), onclick: () => { dlg.close(); openRun(id); } })))),
    h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'submit', text: 'Close' }))));
  document.body.append(dlg); dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
}

views.drift = {
  title: 'Drift',
  async render(root) {
    needAgent();
    const hist = await api(A() + '/drift?limit=14').catch(() => []);
    const out = [lookFor('Drift means the agent is behaving differently from before, even though nobody changed it: slower, longer answers, more errors, different quality. The Auditor compares a recent period with an earlier one and only flags changes that are statistically real.')];
    const res = h('div', { class: 'stack' });
    const cur = h('input', { type: 'number', class: 'sel', value: '24', min: '1', style: { width: '90px' }, 'aria-label': 'Recent hours' }), ref = h('input', { type: 'number', class: 'sel', value: '168', min: '2', style: { width: '90px' }, 'aria-label': 'Earlier hours' });
    const check = async () => {
      res.replaceChildren(loading('Comparing the two periods…'));
      try { res.replaceChildren(driftReport(await post('/v1/drift/check', { agent: state.agent, environment: 'production', current_hours: Number(cur.value) || 24, reference_hours: Number(ref.value) || 168, save: true }))); }
      catch (e) { res.replaceChildren(errorBox(e, check)); }
    };
    out.push(card({ title: 'Check for drift', sub: 'Recent period against the period before it.', actions: [h('span', { class: 'muted', text: 'Recent hours' }), cur, h('span', { class: 'muted', text: 'Earlier hours' }), ref, h('button', { class: 'btn primary', type: 'button', text: 'Check now', onclick: check })], body: res }));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'Past checks', body: table([{ label: 'When', render: (r) => fmt.date(r.created_at) }, { label: 'Result', render: (r) => verdictBadge(r.overall) }, { label: 'Runs (earlier / recent)', num: true, render: (r) => r.reference_runs + ' / ' + r.current_runs }, { label: 'What changed', render: (r) => (r.flagged || []).join(', ') }], hist, { onclick: async (r) => { res.replaceChildren(loading()); try { res.replaceChildren(driftReport(await api('/v1/drift/reports/' + q(r.id)))); scrollTo(0, 0); } catch (e) { res.replaceChildren(errorBox(e)); } }, empty: 'No saved checks yet.' }) }));
    root.replaceChildren(...out);
    if (hist.length) { try { res.replaceChildren(driftReport(await api('/v1/drift/reports/' + q(hist[0].id)))); } catch (e) { /* the button still works */ } } else res.append(h('div', { class: 'muted', text: 'Press Check now.' }));
  },
};
function driftReport(r) {
  const flagged = r.signals.filter((s) => s.significant);
  return h('div', { class: 'stack' }, h('div', { class: 'row' }, verdictBadge(r.overall), h('span', { class: 'muted', text: r.reference_runs + ' earlier runs compared with ' + r.current_runs + ' recent ones (' + fmt.date(r.as_of) + ')' })),
    (r.notes || []).map((n) => h('div', { class: 'muted', text: n })),
    table([{ label: 'Signal', render: (s) => s.name.replace(/_/g, ' ') }, { label: 'Result', render: (s) => (s.significant ? badge(s.severity === 'major' ? 'bad' : 'warn', s.severity) : badge('ok', 'steady')) }, { label: 'Earlier', clip: true, render: (s) => sigVal(s.reference) }, { label: 'Recent', clip: true, render: (s) => sigVal(s.current) }, { label: 'Why', render: (s) => s.reason }], r.signals.sort((a, b) => Number(b.significant) - Number(a.significant)), { empty: 'Not enough runs to compare.' }),
    flagged.length ? callout('warn', 'What to do', h('p', { text: 'Open Failures and Root causes for the same period, and look at what changed around the time the signals moved: a new version, a model update, different questions, or a data source.' })) : null);
}
const sigVal = (o) => { if (!o) return ''; const e = Object.entries(o).filter(([, v]) => typeof v === 'number' || typeof v === 'string').slice(0, 3); return e.map(([k, v]) => k + ' ' + (typeof v === 'number' ? Math.round(v * 1000) / 1000 : v)).join(', '); };

views.tests = {
  title: 'Regression tests',
  async render(root) {
    needAgent();
    const [cases, replays] = await Promise.all([api(A() + '/tests?limit=200'), api(A() + '/replays?limit=10').catch(() => [])]);
    const out = [lookFor('A regression test is a real failure saved as a test: the input that failed plus what a good answer must contain. Before a new version ships, replaying these shows whether the old mistakes come back.')];
    out.push(card({ title: cases.length + ' test' + (cases.length === 1 ? '' : 's'), actions: [h('button', { class: 'btn primary', type: 'button', text: 'Create from failures', onclick: async () => { await genTests(); refreshView(); } })], body: table([
      { label: 'On', render: (c) => h('input', { type: 'checkbox', checked: c.enabled, 'aria-label': 'Enabled', onclick: (e) => { e.stopPropagation(); act(null, () => patch('/v1/tests/' + q(c.id), { enabled: e.target.checked }), false); } }) },
      { label: 'Test', clip: true, render: (c) => c.name }, { label: 'Input', clip: true, render: (c) => fmt.short(fmt.text(c.input), 80) }, { label: 'Must satisfy', clip: true, render: (c) => (c.expectations || []).map((e) => e.type + (e.value ? ' "' + fmt.short(e.value, 24) + '"' : '')).join(', ') },
      { label: 'Seen', num: true, render: (c) => c.occurrences + '×' }, { label: 'From version', render: (c) => c.origin_version || '' }], cases, { onclick: (c) => testDetail(c), empty: 'No tests yet. Press Create from failures.' }) }));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'Replay results', sub: 'Replays run from your agent’s own code, because only your code can call your agent.', body: [table([{ label: 'When', render: (r) => fmt.date(r.created_at) }, { label: 'Version', render: (r) => r.version || '' }, { label: 'Result', render: (r) => verdictBadge(r.verdict) }, { label: 'Summary', render: (r) => Object.entries(r.summary || {}).map(([k, v]) => k + ' ' + v).join(', ') }], replays, { empty: 'No replays yet.' }),
      h('p', { class: 'muted', text: 'To replay these tests against a version from Python:' }), cmd('aud = Auditor("http://127.0.0.1:8000", agent="' + state.agent + '", api_key=KEY)\nresult = aud.replay_tests(my_agent_function, version="v2")\nprint(result["verdict"])')] }));
    root.replaceChildren(...out);
  },
};
function testDetail(c) {
  const dlg = h('dialog', { 'aria-label': 'Test' });
  dlg.append(h('form', { method: 'dialog' }, h('h2', { text: c.name }), h('div', { class: 'sect' }, h('h3', { text: 'Input' }), h('pre', { class: 'box', text: JSON.stringify(c.input, null, 2) })), h('div', { class: 'sect' }, h('h3', { text: 'A good answer must' }), h('pre', { class: 'box', text: JSON.stringify(c.expectations, null, 2) })),
    c.reference_output != null ? h('div', { class: 'sect' }, h('h3', { text: 'The answer that failed' }), h('pre', { class: 'box', text: fmt.text(c.reference_output) })) : null,
    h('div', { class: 'row end' }, c.source_run_id ? h('button', { class: 'btn', type: 'button', text: 'Open the failed run', onclick: () => { dlg.close(); openRun(c.source_run_id); } }) : null, h('button', { class: 'btn', type: 'submit', text: 'Close' }))));
  document.body.append(dlg); dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
}

views.fixes = {
  title: 'Fixes',
  async render(root) {
    needAgent();
    const out = [lookFor('The Auditor looks at the most common causes of failure and proposes concrete changes: a firmer prompt rule, a longer timeout, a setting. They are only proposals. Register one as a new version, then prove it with a replay before any release.')];
    const res = h('div', { class: 'stack' });
    const ai = h('input', { type: 'checkbox', id: 'useai', disabled: !state.assistant.enabled });
    const go1 = async () => {
      res.replaceChildren(loading('Reading the failures…'));
      try { res.replaceChildren(...fixList(await post(A() + '/fixes/propose', { environment: 'production', since_hours: winHours(), use_ai: ai.checked && state.assistant.enabled }))); }
      catch (e) { res.replaceChildren(errorBox(e, go1)); }
    };
    out.push(card({ title: 'Propose fixes for ' + state.agent, actions: [h('label', { class: 'row', title: state.assistant.enabled ? '' : 'Needs ANTHROPIC_API_KEY on the Auditor server' }, ai, h('span', { text: 'Also ask the AI helper' })), h('button', { class: 'btn primary', type: 'button', text: 'Propose fixes', onclick: go1 })], body: res }));
    root.replaceChildren(...out);
    go1();
  },
};
function fixList(p) {
  const out = [];
  out.push(h('div', { class: 'row' }, badge(p.failed_runs ? 'bad' : 'ok', p.failed_runs + ' failed runs'), h('span', { class: 'muted', text: 'improving version ' + p.base_version.label })));
  (p.notes || []).forEach((n) => out.push(h('div', { class: 'muted', text: n })));
  if (!p.candidates.length) out.push(emptyState('No fixes to propose', 'Either nothing failed, or the failures do not have a rule-based fix yet. Open Root causes for manual advice.'));
  p.candidates.forEach((c) => {
    out.push(h('article', { class: 'callout info' }, h('div', { class: 'row' }, h('b', { text: c.title }), badge('info', c.kind), c.verifiable ? badge('ok', 'can be replayed') : badge('mute', 'manual check'), c.source === 'ai' ? badge('rec', 'AI draft') : null),
      h('p', { text: c.rationale }), h('ul', null, (c.changes || []).map((x) => h('li', null, h('b', { text: x.field + ': ' }), (x.from != null ? fmt.short(JSON.stringify(x.from), 60) + ' → ' : '') + fmt.short(typeof x.to === 'string' ? x.to : JSON.stringify(x.to), 140)))),
      c.prompt ? diffEl(p.base_version.prompt, c.prompt) : null,
      h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', text: 'Register as a new version', onclick: () => act('Version registered. It is not active until a release is approved.', () => post(A() + '/versions', { label: (p.base_version.label + '-fix-' + c.id.slice(0, 6)).slice(0, 100), prompt: c.prompt || p.base_version.prompt, config: { ...(p.base_version.config || {}), ...(c.config || {}) }, activate: false }), false) }))));
  });
  if (p.candidates.length) out.push(h('div', null, h('p', { class: 'muted', text: 'To prove the fixes work before releasing (replays every regression test against each candidate):' }), cmd('aud = Auditor("http://127.0.0.1:8000", agent="' + state.agent + '", api_key=KEY)\nreport = aud.verify_fixes(build_agent)   # build_agent(prompt=..., config=...) returns your agent function')));
  return out;
}
