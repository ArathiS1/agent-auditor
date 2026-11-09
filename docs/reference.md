# Agent Auditor: full reference

The front page is the [README](../README.md). This file keeps every phase's detail.

An open-source reliability layer for AI agents. It observes agent runs, turns production failures
into regression tests, diagnoses root causes, and gates changes with verified canaries, with audit
trails and rollback.

## Status: all 9 phases written, plus Phase 10 (friendlier dashboard, AI helper) and Phase 11 (change attribution, verified fixes)

| Phase | Scope | State |
|---|---|---|
| 1 | Data model, FastAPI ingestion, versioned prompts/configs, audit events | done |
| 2 | Python SDK (plain Python + LangGraph/LangChain adapter) | done |
| 3 | Eval engine + version-to-version regression tracking | done |
| 4 | Drift detection | done |
| 5 | Failure-to-test conversion + replay | done |
| 6 | Root-cause classification | done |
| 7 | Canary comparison (reuses Phases 3-6) | done |
| 8 | Approval gates + rollback | done |
| 9a | Docker Compose + Postgres, Alembic, CI, background worker, architecture and deployment docs | done |
| 9b | Dashboard at `/dashboard`, Swagger Authorize button, final docs, case-study template | done |
| 10 | Plain-language dashboard (health banner, release buttons), AI helper, suggested fixes | written; needs first run on your machine |
| 11 | Change attribution (prompt / model / retrieval setting / source data), suspect documents, fixes proven against the regression suite | written; logic tested, server parts need `pytest` on your machine |

## Install and test

```bash
python -m venv .venv          # then activate it
pip install -e ".[dev]"
pytest
uvicorn agent_auditor.api.main:app --reload     # http://127.0.0.1:8000  (redirects to /docs)
```
LangGraph support is optional: `pip install -e ".[dev,langgraph]"`.

**Docker (API + worker + PostgreSQL):** `cp .env.example .env`, set the two secrets, `docker compose up --build`.
See [docs/deployment.md](deployment.md) and [docs/architecture.md](architecture.md).

## Dashboard

Open `http://127.0.0.1:8000/dashboard` (`run-all.ps1` opens it already signed in via `#key=`; the key is kept for that tab
only and removed from the address bar). It is plain HTML, CSS and JS served by the API: no build step, no CDN.
Fifteen pages, grouped by what you are trying to do:

| Group | Pages |
|---|---|
| Watch | **Overview** (health verdict, "the tape": one tick per run, orange = failed, dashed line = new version), **Failures** (every failed or badly answered run with its cause and what to change), **Runs** |
| Understand | **Quality**, **Root causes**, **Drift** |
| Improve | **Versions** (register, compare, prompt diff), **Regression tests**, **Fixes**, **Benchmark** (models ranked on the same questions), **Gap analysis** (your chat against ChatGPT, Claude, Gemini, Perplexity, Copilot, Blackbox) |
| Ship safely | **Canary**, **Releases** (propose, approve, promote, roll back) |
| Govern | **Audit log**, **Setup** (connection checks, key, demo data) |

Click any run for a drawer with its steps, checks, diagnosis, and buttons to make a regression test. Every chart has a
table view; dark mode follows your system or the Dark button. "Ask for help" explains every term (and answers questions
if `ANTHROPIC_API_KEY` is set on the server).

**Gap analysis** compares the feature list in `src/agent_auditor/data/feature_matrix.json` with what the chat app
reports it can do. That matrix is kept by hand (as of 2026-06) and some cells may be out of date: check the sources it lists.
Copilot and Blackbox have no standard public chat API, so they appear in the gap checklist only, not in the Benchmark.

**Benchmark.** `python bench/run.py` (or `run-all.ps1 -Bench`) asks every model whose key is in `.env` the same
questions and records the runs in the `test` environment, scored by plain text checks. `--quick` runs a short set,
`--models gemini,groq` limits it, `--list` shows what would run.

**Acting from the page.** Releases have Approve, Reject, Promote, Cancel and Roll back buttons, plus Propose a
release. Each asks for your name (a label you type, not a verified login) and explains what it will do first. The
server still enforces the gate, so a button can never skip a check. Promote only marks a version active; it deploys
nothing.

**AI helper (optional).** Set `ANTHROPIC_API_KEY` where the server runs (`.env` for Docker Compose) and an
"Ask for help" button appears. It answers questions about what the page shows, using only the numbers on the page,
and cannot change anything. "Suggest a fix" on the failure panel drafts a prompt or settings change from the top
failure causes; it is a draft for a person to review, and it must go through a canary. Each question sends the
current dashboard figures and your question to the Claude API. Without a key the helper is off and everything else works.
Model: `AUDITOR_ASSISTANT_MODEL` (default `claude-sonnet-5-5`). Questions are limited to 20 per minute per server. To see it populated, run `python examples/release_demo.py`
against a running server. In Swagger (`/docs`) use the **Authorize** button once to set the key.

## Evals (Phase 3)

Scorers look at stored runs and say pass / fail / not applicable. Built-ins need no API key:

| Scorer | Passes when |
|---|---|
| `run_succeeded` | the run did not end in an error |
| `no_step_errors` | no tool / LLM / retrieval step failed |
| `output_present` | the run produced a non-empty output |
| `retrieval_nonempty` | every retrieval step returned documents |
| `retrieval_top_score` | the best retrieval score is at least the threshold (default 0.5) |
| `latency_within_budget` | the run finished within `budget_ms` (default 10 000) |
| `grounded_overlap` | most of the answer's terms appear in the retrieved text (a weak heuristic) |

"Not applicable" (for example a retrieval scorer on a run that never retrieved) is stored but never
counted as a pass or a failure.

```bash
python examples/eval_demo.py      # simulates v1 vs a worse v2 and prints the verdict
```
Or use the API (`/docs`):
- `POST /v1/evals/run` score runs (idempotent; `only_unevaluated` skips finished work)
- `GET  /v1/runs/{id}/evals` per-run results with reasons
- `GET  /v1/agents/{name}/eval-summary` pass rates per version, with 95% intervals
- `GET  /v1/agents/{name}/compare?baseline=v1&candidate=v2` regression verdict
- `GET  /v1/evals/scorers` available scorers and their parameters

From the SDK: `auditor.run_evals()` and `auditor.compare_versions("v1", "v2")`.

### How the verdict works
Per scorer, pass rates of the two versions are compared with Fisher's exact test (valid for small
samples). A scorer is a **regression** if its pass rate dropped by at least `min_effect` (default
5 points) and the result is significant; **improvement** is the mirror image; a big but
non-significant change is **inconclusive** (collect more runs); fewer than `min_samples` (default
20) runs on either side gives **insufficient_data**. Because several scorers are tested together,
the significance level is split across them (Bonferroni) so you are not told about regressions
that are just noise.

### Bring your own judge
```python
from agent_auditor.evals import make_judge_scorer, register_scorer
register_scorer(make_judge_scorer("helpfulness", my_llm_judge, threshold=0.7))
```
Judges are opt-in (`"scorers": ["helpfulness"]`). Note: a scorer registered in your own Python
process is only available to a server running in that same process; registering judges from a
separate client over HTTP is planned with the dashboard phase.

## Drift detection (Phase 4)

Compares a **reference window** (default: the previous 7 days) with a **current window** (default: the
last 24 h) of production runs and reports which behaviours moved.

| Signal family | Signals | Test | Severity from |
|---|---|---|---|
| numeric | latency, input/output chars, step count, retrieval top score, retrieved doc count | KS two-sample | KS D |
| rate | run error, step error, empty retrieval, eval pass rates | Fisher exact | absolute change |
| term mix | input terms, tool calls | permutation test on Jensen-Shannon divergence | JS distance |

Alpha (default 0.01) is Bonferroni-split across testable signals. Severity depends on effect size, not
just p-values, so a tiny but "significant" shift stays low. Thresholds are heuristics (`Thresholds`).

```
POST /v1/drift/check              GET /v1/agents/{name}/drift      GET /v1/drift/reports/{id}
auditor.check_drift(...)          # SDK
python examples/drift_demo.py     # server running; seeds 280 baseline + 60 drifted runs
```

Power: with 280 reference vs 60 current runs, a 0.8 sd shift is caught ~92% of the time and 1 sd or
more ~100%; subtle shifts (+10% latency) are caught only sometimes. Validation on synthetic data:
0/80 false alarms on null windows.

## Failure-to-test conversion (Phase 5)

Production failures become regression tests, and any new version is replayed against them.

1. **Generate**: `auditor.generate_tests()` scores recent runs, finds the ones that failed a scorer, and
   stores each distinct failure as a *regression case*: the input, what the failing run answered, and
   an expectation per failed scorer ("`retrieval_nonempty` must pass", with the same thresholds the
   failure was judged by). Repeats are merged: identical inputs by fingerprint, near-identical inputs
   (content-word overlap of at least `similarity`, default 0.8) when they failed the same scorers. A case
   keeps an `occurrences` count, so you can see which failure is most common. Re-running is idempotent.
2. **Review**: `GET /v1/agents/{name}/tests`. Disable a noisy case with `PATCH /v1/tests/{id}`, add your own
   checks (`output_contains`, `output_not_contains`, extra scorers), or pin one run with
   `POST /v1/runs/{id}/to-test`. Cases are never deleted, only disabled.
3. **Replay**: `auditor.replay_tests(my_agent, version="v2")` calls *your* agent on every enabled case, records
   the runs in the `test` environment under that version (so they never mix with production data), and the
   server grades them. The verdict is `pass`, `fail` or `incomplete`; per case you get `pass`, `fail`,
   `unverified` (e.g. the agent no longer retrieves, so the original check cannot be evaluated) or
   `missing`. The summary lists `fixed`, `still_failing` and `newly_failing` (passed in the previous replay,
   fails now), which is what Phase 8 will gate on.

```
POST /v1/agents/{name}/tests/generate     GET /v1/agents/{name}/tests     GET|PATCH /v1/tests/{id}
POST /v1/runs/{id}/to-test                POST /v1/agents/{name}/replays  GET /v1/agents/{name}/replays
GET  /v1/replays/{id}
python examples/regression_demo.py        # server running; v1 fails, v2/v3/v4 are graded
```

Things to know:
- The server never runs your agent. Replay happens in your process (the SDK), the server only grades.
  Runs are matched to cases by a token the SDK adds; you can also post `results: [{case_id, run_id}]` yourself.
- `grounded_overlap` (a heuristic) is not turned into a test unless you pass `include_weak=true`.
- Inputs and outputs of production runs are copied into test cases. `redact=true` masks emails, phone
  numbers, card-like numbers, SSNs and key-like strings. It is best effort and does **not** find names or
  addresses; review cases before sharing a database.
- Each case is replayed once. An agent with a nondeterministic LLM can pass or fail a case by chance, so
  treat a single failing case in a large suite with suspicion and re-run before blocking a release.

## Root-cause classification (Phase 6)

"Something is failing" becomes "this cause, in this step, since this version".

`auditor.root_causes()` classifies every run and reports, for the failures: the cause ranking, the steps
and sub-causes involved, example run ids, suggested first checks, and which causes cluster in one version.
`auditor.diagnose_run(run_id)` explains a single run. Regression cases from Phase 5 now carry their cause.

Causes: `retrieval_error`, `retrieval_empty`, `retrieval_low_relevance`, `tool_timeout`, `tool_error`,
`llm_error`, `agent_exception`, `ungrounded_answer`, `slow_run`, `empty_output`, `unknown`
(`GET /v1/root-causes/taxonomy` has the meaning and advice for each).

How it decides (deterministic rules, no model call, no API key):
1. **Errors dominate.** If a step raised, the *earliest* failing step is the cause (tool, LLM or retrieval),
   refined by its error text into a sub-cause: timeout, rate_limit, context_length, auth, connection,
   server_error, not_found, code_or_input. A run that failed with no failing step is an `agent_exception`.
2. **Otherwise, upstream quality symptoms first:** empty retrieval, then weak matches, then an answer not
   supported by the retrieved text, then slowness (blamed on the slowest step), then empty output.
3. Everything else that fired is kept as a contributing finding, never dropped.

`strength` (strong / moderate / weak) says how direct the evidence is (an error message, an empty
result = strong; a threshold = moderate; a heuristic scorer = weak). It is **not a probability**: on the
synthetic benchmark it does not track accuracy closely.

```
POST /v1/agents/{name}/diagnose     POST /v1/agents/{name}/root-causes     GET /v1/runs/{id}/diagnosis
GET  /v1/root-causes/taxonomy
python examples/rootcause_demo.py                    # server running; v2 breaks refunds, v1 is mostly fine
python -m agent_auditor.rootcause.benchmark          # no server needed
```

What has and has not been measured:
- **Error-text matching** was measured on 270 realistic messages (30 for each of 9 kinds) written by a separate
  process that never saw the matcher. First version: 74%. After one round of fixes, a second independent set of 270
  messages, never used for tuning, scored **83%** (82% on messages that carry a signal). Misses are mostly
  long-tail vendor wording; teach your own with `add_error_pattern("timeout", r"softtimelimitexceeded")`.
  Both sets are in `tests/data/`.
- **Whole-run classification** was measured on synthetic failures with a known injected cause
  (`python -m agent_auditor.rootcause.benchmark`): about 97% overall, 100% on clear cases, about 89% on noisy
  ones (opaque error messages, borderline scores, missing instrumentation), 0% of healthy runs flagged. The
  generator was written alongside the classifier, so treat this as an **optimistic bound**, and the compound
  tier only checks that the stated precedence policy is implemented, not that the policy is right.
- **Not measured:** accuracy on real production incidents. Compare the diagnoses with failures you have
  root-caused by hand before trusting them for decisions.
- The version comparison finds *association* (a cause is much more common in one version), not proof: traffic
  or data can change at the same moment as a deploy.
- A tool timeout whose message does not say "timeout" is reported as `tool_error` (the most common mistake in
  the benchmark). If your agent records opaque errors, the sub-cause will often be missing.

## Canary comparison (Phase 7)

Give a candidate version a slice of live traffic, then ask: promote, hold, roll back, or keep collecting?

```python
router = auditor.canary_router(baseline=("v1", answer_v1), candidate=("v2", answer_v2), fraction=0.1)
answer = router.call(request_id, question)        # records "production"/v1 or "canary"/v2 runs

report = auditor.evaluate_canary(baseline="v1", candidate="v2", since_hours=24)
print(report["verdict"], report["reasons"])
```
`POST /v1/agents/{name}/canary/evaluate`, `GET /v1/agents/{name}/canary`, `GET /v1/canary/{id}`.
Every evaluation is saved and written to the audit trail as `canary.evaluate`.

| Check | Question | How |
|---|---|---|
| evals | Did pass rates fall? | Phase 3 comparison (Fisher exact, Bonferroni), alpha 0.01 |
| latency | Is it slower? | p50/p95 ratio plus a KS test; warn 1.3x, hold 2x, rollback 4x |
| causes | Did some failure cause become more common, or appear for the first time? | Phase 6 diagnoses, Fisher per cause |
| drift | Does canary traffic or behaviour look different? | Phase 4 detector; a warning only, since routing can explain it |
| replay | Does the candidate break a past failure's regression test? | latest Phase 5 replay of that version |

Verdicts: **rollback** if any check fails badly (a pass-rate or cause-rate increase of at least 10 points,
latency 4x, or 2+ regression cases newly failing); **hold** if a check fails less badly; **continue** if there are
too few canary or baseline runs (default 30 each) or no eval comparison is possible yet; otherwise **promote**.
Override any threshold with `policy={...}` (see `canary/checks.py: Policy`), for example
`{"min_canary_runs": 200, "require_replay": True, "warnings_block": True}`.

**What this does not give you**
- Repeated checks of one canary raise the false-alarm rate (peeking). The stricter default alpha helps; it does
  not remove the problem. Fix the traffic share and minimum sample size in advance.
- `router.choose(key)` is a sticky hash, not randomisation. If the key tracks user tier, region or time of day,
  the two arms are not comparable. Use a per-request key for an unbiased split.
- A `promote` means no check found a problem at this sample size, not that the candidate is better.
  A `rollback` is a recommendation here; acting on it (and approvals) arrives in Phase 8.
- Thresholds are defaults, not tuned on real traffic. The decision logic is unit-tested on constructed cases;
  the demo is a simulation.

Try it: `python examples/canary_demo.py` (server running). Expect v2 to be a rollback and v3 a promote.

**A canary on real models (Auditor Chat).** `python bench/canary.py --list` shows the models you have keys for. Then
`python bench/canary.py --baseline <model> --candidate <model>` asks both the same 24 questions, records the baseline in
`production` and the candidate in `canary`, scores the runs, and prints and saves the verdict (see the dashboard's Canary
page). If either side gets fewer runs than asked, for example because a key ran out of quota, the verdict is
"continue" and says why. It cannot see wrong-but-well-formed answers; use the Benchmark page for answer quality.

## Releases: approval gates and rollback (Phase 8)

A release asks to make a candidate version the *active* one. The gate decides whether that may happen.

```python
rel = auditor.propose_release("v2", proposed_by="alice", policy={"required_approvals": 1})
auditor.approve_release(rel["id"], approver="bob", comment="canary looks clean")
auditor.promote_release(rel["id"], actor="alice")      # HTTP 409 with the blockers if the gate says no
auditor.rollback_release(rel["id"], actor="bob", reason="timeouts doubled")
auditor.release_audit(rel["id"])                       # every step, oldest first
```

The gate passes only when all of these hold at the moment you promote:
- the newest canary report for the candidate says **promote**, is recent (default 24 h), and compared the candidate
  with the version that is **active now**;
- optionally (`require_replay`) the newest regression replay has no newly failing case;
- enough distinct approvers (default 1, not the proposer) whose approvals are still valid. **An approval goes stale
  when a newer canary report says anything other than promote**, so a person cannot approve on evidence that has
  since changed.

States: `proposed -> promoted -> rolled_back`, or `proposed -> rejected | cancelled`. Rollback reactivates the
version that was active before the promotion (or `to_version`) and needs a written reason. Refused approvals and
refused promotions are written to the audit trail too (`release.approval_refused`, `release.promote_refused`).
Endpoints: `POST /v1/agents/{name}/releases`, `GET /v1/agents/{name}/releases`, `GET /v1/releases/{id}`,
`POST /v1/releases/{id}/approve | promote | rollback | cancel`, `GET /v1/releases/{id}/audit`, `GET /v1/audit`.

**What this does not do**
- **It does not deploy anything.** Promotion and rollback change which version is marked active. Your service has to
  read it (`auditor.active_version()` returns the label, prompt and config) for the change to take effect.
- **Approver names are not authenticated.** The API has one shared key, so "two different approvers" means two
  different strings. The trail is a labelled record, not proof of who clicked. Put the API behind real user
  authentication before relying on four-eyes approval.
- Two requests racing on one release are not serialised beyond the database transaction. Fine for one operator at
  a time; a multi-user deployment needs row locking, which is not implemented (not even on PostgreSQL).
- There is no break-glass override. A blocked release stays blocked until its evidence is fixed, which is
  deliberate, but means an urgent fix needs a fresh canary run first.
- The gate checks canary evidence, not whether the candidate is *good*; see the canary limits above.

Try it: `python examples/release_demo.py` (server running).

## Change attribution and verified fixes (Phase 11)

**Which kind of change caused the regression?** Every canary report now has a `change` check. It compares the baseline and
candidate *versions* (prompt, model and sampling settings, retrieval settings, tool settings) and names the most likely
cause when the candidate got worse: `prompt_regression`, `model_change`, `retrieval_config_change`, `tool_config_change`,
`multiple_changes` (several things changed together, so it refuses to pick one), or `source_data_change` (nothing in the
prompt or settings changed, or retrieval got worse while retrieval settings did not, so the documents, index or traffic did).
It is informational and never changes the promote / hold / rollback verdict. Keep model settings such as `model` and
`temperature`, and retrieval settings such as `top_k` and `min_score`, in the version `config` so they can be told apart.

**Which document is behind the failures?** `POST /v1/agents/{name}/root-causes` now also returns `suspect_sources`:
documents that appear in many failed runs and rarely in healthy ones (Fisher exact test). It needs each retrieved item to
carry a `source` / `doc_id` / `url`. It is a lead for a person to check, not proof the document is wrong.

**Fixes that are proven before they are suggested.**

```python
result = auditor.verify_fixes(build_agent)   # build_agent(prompt=..., config=...) -> your agent function
print(result["recommended"])                 # only candidates that passed
```

1. The server proposes small candidates from the measured causes. Each changes one thing: a setting that already exists
   in your version config, or one added prompt rule. Code bugs and suspect documents become *manual* items.
   `use_ai=True` adds a Claude-written prompt rewrite (needs `ANTHROPIC_API_KEY` on the server); without it everything
   else still works.
2. Each candidate is registered as its own **non-active** version and your agent is rebuilt with it.
3. The candidate is replayed on every regression case (your real past failures) and graded by the server.
   `verified` = all pass, `improved` = at least half pass, otherwise `rejected`.
4. `guard=20` also runs the candidate on recent questions that worked and flags answers that changed a lot.
5. Nothing is activated. The next step is a canary and the approval gate.

Needs regression cases first: `auditor.generate_tests()`. Endpoint: `POST /v1/agents/{name}/fixes/propose`.

**Limits.** The suite holds past failures only, so a passing fix may still hurt questions that used to work; the guard check is
a rough word-overlap comparison, not a quality score. Rule-based proposals only touch settings that exist in your config.
Attribution is evidence, not proof.

## Run everything with one command (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\run-all.ps1            # Auditor server + chatbot, connected, browser opened
powershell -ExecutionPolicy Bypass -File .\run-all.ps1 -Check     # the same, then the 23-point self-check
powershell -ExecutionPolicy Bypass -File .\run-all.ps1 -Test      # unit tests only (pip install -e ".[dev]" first)
```

The first run asks for your chat provider key once and saves it, plus a generated Auditor key, in `.env` (never committed).
The dashboard asks for the Auditor key: copy `AUDITOR_API_KEY` from `.env`.

## One-command self-check

With the Auditor server running:

```powershell
python validation\selfcheck.py
```

It creates a throwaway agent, sends it data with planted faults, and prints PASS or FAIL for each module: evals, root causes,
failure-to-test, drift, canary (a bad candidate is not promoted, a good one is), change attribution, the release gate
(self-approval refused, a failed candidate blocked, approve, promote and roll back), the audit trail, fix proposals, and the
SDK (trace, replay, and a real fix being verified while an irrelevant change is rejected). Results are saved in
`validation/results/`. It uses your `.env` for `AUDITOR_URL` and `AUDITOR_API_KEY`.

## Check that it works (validation kit)

`validation/` tests the project against a support bot whose faults are planted, so the right answer is known:
does a canary roll back a really worse version and name the cause, how often does it cry wolf, how often is a
root-cause label right, and what does it miss. `python validation/run_validation.py --dry-run` needs no server;
without `--dry-run` it exercises the API and database. Step-by-step guide: [validation/README.md](../validation/README.md);
an example report: [validation/sample_report_dry_run.md](../validation/sample_report_dry_run.md).
On the sample run, three of five planted faults were rolled back every time, half-missing search results were caught
3 times in 5, and out-of-date facts were never caught: the built-in checks look at symptoms, not facts.

## How the pieces fit
Runs flow in through the SDK, get scored and diagnosed by the worker, feed drift and canary checks, and a release
may only promote a version when the gate passes and enough people approve. Every decision, including refusals, is
in the audit trail. Diagram and component notes: [docs/architecture.md](architecture.md). To validate on your
own agent, follow [docs/case-study.md](case-study.md).

## SDK in 20 lines

```python
from agent_auditor.sdk import Auditor

auditor = Auditor("http://127.0.0.1:8000", agent="support-bot")
auditor.register_version("v1", prompt=PROMPT, config={"top_k": 4})

@auditor.observe(kind="retrieval")
def kb_search(q): ...

@auditor.observe_run()
def answer(question): ...
```
See `../examples/quickstart_plain.py` and `../examples/langgraph_demo.py`.

## Known limitations
- The AI helper and suggested fixes are only as good as the figures on the page; they can be wrong, so decisions stay with a person. Run data is sent to the Claude API when the helper is on.
- Approver names are self-asserted (one shared API key), and promotion only flips the active version; it deploys nothing.
- The dashboard has been checked in Chromium against fixtures, not yet against a long-running production server.
- Replay assumes the agent can be called with the stored inputs (a dict of arguments, or `call=` to adapt).
  Agents that need live user sessions or external state cannot be replayed faithfully.
- Drift checks run synchronously, sample the newest N runs per window (default max 2000), and use
  asymptotic KS p-values (close to exact for n >= 30). Drift needs at least `min_samples` runs per window.
- Evals and diagnosis can run inside a request (up to 500 runs per call). `agent-auditor-worker` does it in the
  background and is the recommended setup; it polls, so results lag by up to one interval.
- Built-in scorers detect symptoms, not answer quality. `grounded_overlap` is a cheap heuristic.
- Local use creates tables with `create_all`; Docker uses Alembic. The baseline migration is generated from the
  current models and must be made explicit before the first model change (see the note in the migration).
- The PostgreSQL CI job is experimental; SQLite is the tested backend until it is green.
- Two simultaneous first-ever runs for a brand-new agent name can race; retrying succeeds.
- The LangGraph adapter has been run against LangGraph on one machine only.
