"use strict";
const whoami = () => store.get('aa.name') || '';
async function askWho(extra) {
  return formDialog(extra);
}
views.canary = {
  title: 'Canary',
  async render(root) {
    needAgent();
    const [vs, hist] = await Promise.all([api(A() + '/versions'), api(A() + '/canary?limit=10').catch(() => [])]);
    const out = [lookFor('A canary sends a small share of real traffic to the new version. The Auditor then compares it with the current one on error rate, quality and speed, and says: safe to continue, hold, or roll back. A verdict needs enough runs on both sides; with too few it says so instead of guessing.')];
    if (vs.length < 2) { out.push(emptyState('A canary needs two versions', 'Register a second version (a changed prompt or model) and send some traffic through it.', h('button', { class: 'btn primary', type: 'button', text: 'Go to Versions', onclick: () => go('versions') }))); root.replaceChildren(...out); return; }
    const sel = (i, label) => h('select', { class: 'sel', 'aria-label': label }, vs.map((v) => h('option', { value: v.label, text: v.label + (v.is_active ? ' (active)' : ''), selected: v === (i === 0 ? vs.find((x) => x.is_active) || vs[vs.length - 1] : vs.find((x) => !x.is_active) || vs[0]) })));
    const base = sel(0, 'Baseline'), cand = sel(1, 'Candidate');
    const env = (label, v) => h('select', { class: 'sel', 'aria-label': label }, ['production', 'canary', 'test'].map((e) => h('option', { value: e, text: e, selected: e === v })));
    const be = env('Baseline environment', 'production'), ce = env('Candidate environment', 'production');
    const minRuns = h('input', { type: 'number', class: 'sel', value: '20', min: '1', style: { width: '80px' }, 'aria-label': 'Minimum runs per side' });
    const res = h('div', { class: 'stack' });
    const run = async () => {
      res.replaceChildren(loading('Comparing…'));
      const n = Number(minRuns.value) || 20;
      try { res.replaceChildren(...canaryReport(await post(A() + '/canary/evaluate', { baseline: base.value, candidate: cand.value, baseline_environment: be.value, candidate_environment: ce.value, since_hours: winHours(), evaluate_first: true, diagnose_first: true, save: true, policy: { min_canary_runs: n, min_baseline_runs: n, min_eval_samples: n } }))); }
      catch (e) { res.replaceChildren(errorBox(e, run)); }
    };
    out.push(card({ title: 'Compare a candidate with the current version', sub: 'Your chat runs every model in production, so production for both is right. Use canary only if you send a candidate through the canary environment on purpose. Each model in the chat is its own version, so this compares two models.', body: h('div', { class: 'stack' }, h('div', { class: 'row' }, h('span', { text: 'Baseline' }), base, be, h('span', { text: 'Candidate' }), cand, ce, h('span', { text: 'Min runs' }), minRuns, h('button', { class: 'btn primary', type: 'button', text: 'Evaluate', onclick: run })), res) }));
    out.push(h('div', { style: { height: '14px' } }), card({ title: 'Past canary checks', body: table([{ label: 'When', render: (r) => fmt.date(r.created_at) }, { label: 'Baseline', key: 'baseline' }, { label: 'Candidate', key: 'candidate' }, { label: 'Verdict', render: (r) => verdictBadge(r.verdict) }, { label: 'Why', clip: true, render: (r) => (r.blocking[0] || r.reasons[0] || '') }], hist, { onclick: async (r) => { res.replaceChildren(loading()); try { res.replaceChildren(...canaryReport(await api('/v1/canary/' + q(r.id)))); scrollTo(0, 0); } catch (e) { res.replaceChildren(errorBox(e)); } }, empty: 'No canary checks saved yet.' }) }));
    root.replaceChildren(...out);
  },
};
function canaryReport(r) {
  const kind = { continue: 'warn', promote: 'ok', hold: 'warn', rollback: 'bad' }[r.verdict] || 'info';
  const out = [h('div', { class: 'verdict ' + (kind === 'ok' ? '' : kind === 'bad' ? 'bad' : 'warn') }, h('div', { class: 'mark', 'aria-hidden': 'true', text: kind === 'ok' ? '✓' : kind === 'bad' ? '✕' : '!' }), h('div', null, h('h2', { text: 'Verdict: ' + String(r.verdict).replace(/_/g, ' ') }), h('p', { text: r.candidate.version + ' (' + r.candidate.runs + ' runs) against ' + r.baseline.version + ' (' + r.baseline.runs + ' runs)' })))];
  if ((r.blocking || []).length) out.push(callout('bad', 'Blocking', h('ul', null, r.blocking.map((x) => h('li', { text: x })))));
  if ((r.warnings || []).length) out.push(callout('warn', 'Warnings', h('ul', null, r.warnings.map((x) => h('li', { text: x })))));
  out.push(table([{ label: 'Check', render: (c) => c.name.replace(/_/g, ' ') }, { label: 'Result', render: (c) => verdictBadge(c.status) }, { label: 'Why', render: (c) => c.reason }], r.checks));
  if (r.caveat) out.push(h('p', { class: 'muted', text: r.caveat }));
  return out;
}

views.releases = {
  title: 'Releases',
  async render(root) {
    needAgent();
    const [vs, rels] = await Promise.all([api(A() + '/versions'), api(A() + '/releases?limit=15')]);
    const out = [lookFor('A release is a request to make a new version the live one. It needs the canary evidence to be fresh and at least one person other than the proposer to approve. After promotion you can roll back in one click. Every step is written to the audit log.')];
    const propose = h('button', { class: 'btn primary', type: 'button', text: 'Propose a release', onclick: async () => {
      const cands = vs.filter((v) => !v.is_active);
      if (!cands.length) return toast('Register a second version first.', true);
      const v = await formDialog({ title: 'Propose a release', intro: 'This does not change what is live. It opens the gate: evidence is checked and approvals are collected first.', fields: [{ name: 'candidate', label: 'Candidate version', type: 'select', options: cands.map((c) => c.label) }, { name: 'name', label: 'Your name', required: true, value: whoami() }, { name: 'approvals', label: 'Approvals needed', type: 'number', value: 1, min: 1, max: 10 }, { name: 'self', label: 'The proposer may approve their own release', type: 'checkbox' }, { name: 'notes', label: 'Why', type: 'textarea' }], submit: 'Propose' });
      if (!v) return; store.set('aa.name', v.name);
      act('Release proposed.', () => post(A() + '/releases', { candidate: v.candidate, proposed_by: v.name, notes: v.notes || null, policy: { required_approvals: v.approvals || 1, allow_self_approval: v.self } }));
    } });
    out.push(card({ title: 'Releases of ' + state.agent, sub: 'Active version: ' + ((vs.find((v) => v.is_active) || {}).label || 'none'), actions: [propose], body: rels.length ? h('div', { class: 'stack' }, rels.map(releaseCard)) : emptyState('No releases yet', 'Propose one when a new version is ready.') }));
    root.replaceChildren(...out);
  },
};
function releaseCard(r) {
  const gate = r.gate, kind = { proposed: 'info', promoted: 'ok', rolled_back: 'bad', rejected: 'bad', cancelled: 'mute' }[r.state] || 'info';
  const el = h('article', { class: 'callout ' + (r.state === 'proposed' ? 'info' : '') },
    h('div', { class: 'row' }, h('b', { text: r.candidate }), h('span', { class: 'muted', text: 'replaces ' + (r.baseline || 'nothing') }), verdictBadge(r.state), h('span', { class: 'muted', style: { marginLeft: 'auto' }, text: 'proposed by ' + r.proposed_by + ', ' + fmt.ago(r.created_at) })),
    r.notes ? h('p', { text: r.notes }) : null, r.recommendation ? h('p', null, h('b', { text: 'Recommendation: ' }), r.recommendation) : null);
  if (gate) el.append(h('div', { class: 'sect' }, h('h3', { text: 'Gate: ' + (gate.ready ? 'ready to promote' : 'not ready') }), h('ul', null, gate.checks.map((c) => h('li', null, c.passed ? badge('ok', 'pass') : badge('bad', 'fail'), ' ', c.name.replace(/_/g, ' ') + ': ' + c.reason)))));
  if (r.approvals.length) el.append(h('div', { class: 'sect' }, h('h3', { text: 'Decisions' }), h('ul', null, r.approvals.map((a) => h('li', { text: a.approver + ' ' + a.decision + (a.comment ? ': ' + a.comment : '') + ' (' + fmt.ago(a.created_at) + ')' })))));
  const row = h('div', { class: 'row' });
  const decide = async (decision) => {
    const v = await formDialog({ title: decision === 'approve' ? 'Approve this release' : 'Reject this release', fields: [{ name: 'name', label: 'Your name', required: true, value: whoami() }, { name: 'comment', label: 'Comment', type: 'textarea' }], submit: decision === 'approve' ? 'Approve' : 'Reject', danger: decision !== 'approve' });
    if (!v) return; store.set('aa.name', v.name);
    act(decision === 'approve' ? 'Approval recorded.' : 'Release rejected.', () => post('/v1/releases/' + q(r.id) + '/approve', { approver: v.name, decision, comment: v.comment || null }));
  };
  if (r.state === 'proposed') {
    row.append(h('button', { class: 'btn', type: 'button', text: 'Approve', onclick: () => decide('approve') }), h('button', { class: 'btn', type: 'button', text: 'Reject', onclick: () => decide('reject') }),
      h('button', { class: 'btn primary', type: 'button', text: 'Promote', disabled: gate && !gate.ready, title: gate && !gate.ready ? 'The gate is not ready' : '', onclick: async () => { const v = await formDialog({ title: 'Make ' + r.candidate + ' the active version', intro: 'Live traffic will use this version from now on. You can roll back afterwards.', fields: [{ name: 'name', label: 'Your name', required: true, value: whoami() }], submit: 'Promote' }); if (!v) return; store.set('aa.name', v.name); act(r.candidate + ' is now the active version.', () => post('/v1/releases/' + q(r.id) + '/promote', { actor: v.name })); } }),
      h('button', { class: 'btn quiet', type: 'button', text: 'Cancel release', onclick: async () => { const v = await formDialog({ title: 'Cancel this release', fields: [{ name: 'name', label: 'Your name', required: true, value: whoami() }, { name: 'reason', label: 'Reason' }], submit: 'Cancel release' }); if (!v) return; act('Release cancelled.', () => post('/v1/releases/' + q(r.id) + '/cancel', { actor: v.name, reason: v.reason || null })); } }));
  }
  if (r.state === 'promoted') row.append(h('button', { class: 'btn danger', type: 'button', text: 'Roll back', onclick: async () => { const v = await formDialog({ title: 'Roll back', intro: 'Puts ' + (r.previous_active || 'the previous version') + ' back as the active version.', fields: [{ name: 'name', label: 'Your name', required: true, value: whoami() }, { name: 'reason', label: 'Why are you rolling back?', required: true }], submit: 'Roll back', danger: true }); if (!v) return; act('Rolled back.', () => post('/v1/releases/' + q(r.id) + '/rollback', { actor: v.name, reason: v.reason })); } }));
  const trail = h('div', { class: 'stack', hidden: true });
  row.append(h('button', { class: 'btn quiet', type: 'button', text: 'Audit trail', onclick: async () => { trail.hidden = !trail.hidden; if (!trail.hidden) { trail.replaceChildren(loading()); try { const ev = await api('/v1/releases/' + q(r.id) + '/audit'); trail.replaceChildren(table([{ label: 'When', render: (e) => fmt.date(e.created_at) }, { label: 'Who', key: 'actor' }, { label: 'What', key: 'action' }, { label: 'Detail', clip: true, render: (e) => fmt.short(JSON.stringify(e.detail), 100) }], ev)); } catch (e) { trail.replaceChildren(errorBox(e)); } } } }));
  el.append(row, trail);
  return el;
}
