# Real canary verdicts

Run on 11 November 2026, with real models and 24 questions per side. The Auditor decided each verdict from the recorded runs; nothing here was edited by hand.

| Check | Baseline (production) | Candidate (canary) | Median answer time | 95th percentile | Verdict | Verdict if any warning blocks | What I expected |
|---|---|---|---|---|---|---|---|
| Hold: a slower candidate | `indos:Deepseek/deepseek-v4-flash` | `airouter:openai/gpt-5-nano` | 2.6 s vs 4.7 s | 5.0 s vs 7.6 s | **PROMOTE** | HOLD | hold (nothing broke, it is slower) |
| Rollback: a broken candidate | `indos:Deepseek/deepseek-v4-flash` | `indos-misconfigured:Deepseek/deepseek-v4-flash` (planted fault) | 2.6 s vs 2.1 s | 5.0 s vs 2.1 s | **ROLLBACK** | ROLLBACK | rollback |
| Promote: a faster candidate | `airouter:openai/gpt-5-nano` | `indos:Deepseek/deepseek-v4-flash` | 4.3 s vs 1.9 s | 8.6 s vs 4.2 s | **PROMOTE** | HOLD | promote (it is faster) |

## How each verdict was reached

### Hold: a slower candidate: PROMOTE

- no check failed: causes, drift, change
- not evaluated: replay

| Check | Result | Detail |
|---|---|---|
| evals | warn | pass rate moved but not significantly on expectations_met; more canary traffic would settle it |
| latency | warn | latency is 1.78x baseline (p50 2619->4668 ms, p95 4924->7627 ms, KS p=0.0029) |
| causes | pass | no failure cause became significantly more common |
| drift | pass | drift verdict: stable |
| replay | skipped | no regression replay found for the candidate version |
| change | pass | likely model change (moderate): only the model changed between the two versions |

Warnings the Auditor raised (they do not block a promote under the default policy):

- evals: pass rate moved but not significantly on expectations_met; more canary traffic would settle it
- latency: latency is 1.78x baseline (p50 2619->4668 ms, p95 4924->7627 ms, KS p=0.0029)

Answered / errored: baseline 24 / 0, candidate 24 / 0.

### Rollback: a broken candidate: ROLLBACK

- evals: pass rate regressed on expectations_met, no_step_errors, output_present, run_succeeded; worst is no_step_errors (pass rate fell 100.0% (p=6.2e-14))
- causes: more common in canary: llm_error (new); largest is llm_error 0.0% -> 100.0%

| Check | Result | Detail |
|---|---|---|
| evals | fail | pass rate regressed on expectations_met, no_step_errors, output_present, run_succeeded; worst is no_step_errors (pass rate fell 100.0% (p=6.2e-14)) |
| latency | pass | latency ratio 0.79x (KS p=1.79e-05) is within the allowed range |
| causes | fail | more common in canary: llm_error (new); largest is llm_error 0.0% -> 100.0% |
| drift | warn | canary traffic or behaviour differs from baseline on latency_ms:high, run_error:high, step_error:high; the difference may come from routing rather than the candidate |
| replay | skipped | no regression replay found for the candidate version |
| change | pass | likely model change (strong): only the model changed between the two versions; the causes that got worse (llm_error) fit that kind of change |

Warnings the Auditor raised (they do not block a promote under the default policy):

- drift: canary traffic or behaviour differs from baseline on latency_ms:high, run_error:high, step_error:high; the difference may come from routing rather than the candidate

Answered / errored: baseline 24 / 0, candidate 0 / 24.

### Promote: a faster candidate: PROMOTE

- no check failed: latency, causes, change
- not evaluated: replay

| Check | Result | Detail |
|---|---|---|
| evals | warn | pass rate moved but not significantly on expectations_met; more canary traffic would settle it |
| latency | pass | latency ratio 0.47x (KS p=8.19e-07) is within the allowed range |
| causes | pass | no failure cause became significantly more common |
| drift | warn | canary traffic or behaviour differs from baseline on latency_ms:high; the difference may come from routing rather than the candidate |
| replay | skipped | no regression replay found for the candidate version |
| change | pass | likely model change (moderate): only the model changed between the two versions |

Warnings the Auditor raised (they do not block a promote under the default policy):

- evals: pass rate moved but not significantly on expectations_met; more canary traffic would settle it
- drift: canary traffic or behaviour differs from baseline on latency_ms:high; the difference may come from routing rather than the candidate

Answered / errored: baseline 24 / 0, candidate 24 / 0.

## What the two verdict columns mean

The default policy fails a check only when a result is clearly worse (for example latency of 2x the baseline or more). A smaller change is a **warning**, and warnings alone do not stop a promote. The second column judges the same runs again with every warning treated as a block. Where the two columns differ, the default let a weaker candidate through.

## Read this before quoting it

- The rollback check uses a **planted fault**: the same model reached through a wrong address, so every call fails. It shows that the Auditor stops a broken release, not that a real model broke by itself.
- The canary judges errors, speed, the built-in quality checks and failure causes. It cannot tell that an answer is wrong but well formed.
- 24 questions per side is a small sample. Model speed also varies with the time of day and the provider's load.
