# Validation report: validation-offline

Run on 2026-10-03 07:20. Mode: **dry run (no server)**. Agent answers: **retrieval only (best passage)**. Seed 1. 300 baseline runs, 80 runs per canary, 35 canaries in total.

Numbers in brackets are 95% intervals. With only a handful of canaries they are wide: read them as a range of what could be true.

## 1. The agent on its own

- Correct answers (judged against known right values, not by the tool): **86%**.
- Wrong answers that look fine to the tool (the run finished, no error): **42** of 300. Symptom-based checks cannot see these; only a ground-truth check or an LLM judge can.

## 2. Did the canary catch the planted faults?

Each row is a worse version (or, for `control`, a harmless change) tried as a canary several times with different random traffic.

| Scenario | What was changed | Wanted | Rollback | Hold | Promote | Caught (rollback) | Right cause named | Agent accuracy |
|---|---|---|---|---|---|---|---|---|
| control | harmless change: the prompt is reworded | promote | 0 | 0 | 10 | n/a | n/a | 84% |
| timeout | the policy API times out on 25% of runs | rollback | 5 | 0 | 0 | 5/5 (57%-100%) | 5/5 (57%-100%) | 63% |
| rate_limit | the model call is rate limited on 20% of runs | rollback | 5 | 0 | 0 | 5/5 (57%-100%) | 5/5 (57%-100%) | 71% |
| retrieval | half of the help-center articles are missing from the search index | rollback | 3 | 0 | 2 | 3/5 (23%-88%) | 3/3 (44%-100%) | 46% |
| slow | the policy API takes about 12 seconds on 30% of runs | rollback | 5 | 0 | 0 | 5/5 (57%-100%) | 5/5 (57%-100%) | 90% |
| stale | 40% of articles are out of date (right article, wrong fact) | rollback | 0 | 0 | 5 | 0/5 (0%-43%) | n/a | 52% |

## 3. False alarms

A harmless change was tried 10 times. The tool said promote 10 times and raised a false alarm (hold or rollback) **0/10 (0%-28%)** of the time.

## 4. Root-cause labels

Because the harness planted every fault, it knows the true cause of each failing run. "Flagged" means the tool called the run a failure at all; "label right" means that when it did, it named the correct cause.

| True cause | Runs | Flagged as a failure | Label right when flagged | Labels the tool used |
|---|---|---|---|---|
| tool timeout | 97 | 97/97 (96%-100%) | 97/97 (96%-100%) | tool_timeout 97 |
| model error | 65 | 65/65 (94%-100%) | 65/65 (94%-100%) | llm_error 65 |
| slow run | 113 | 106/113 (88%-97%) | 96/106 (83%-95%) | slow_run 96, retrieval_empty 7, retrieval_low_relevance 3 |
| retrieval | 200 | 83/200 (35%-48%) | 83/83 (96%-100%) | retrieval_empty 48, retrieval_low_relevance 35 |
| out-of-date fact | 149 | 2/149 (0%-5%) | n/a (no correct label exists) | retrieval_low_relevance 2 |
| no fault planted | 1676 | 173/1676 (9%-12%) | n/a | retrieval_empty 109, retrieval_low_relevance 64 |

Across the causes the classifier can see (timeout, model error, slow run, retrieval), it named the right cause for **341/475 (68%-76%)** of all affected runs (runs it did not flag at all count as misses).

The 173 flagged runs with no planted fault are mostly out-of-scope questions (nothing in the help center matches) and weak matches. The rule-based classifier reports those as retrieval problems. That is a fair reading of the symptom, but it is also noise a person has to learn to ignore.

## 5. What it missed

- **retrieval**: rolled back 3 of 5 times (accuracy fell to 46%). Missing articles were replaced by other plausible articles with decent scores, so retrieval looked healthy while answers were wrong.
- **stale**: rolled back 0 of 5 times (accuracy fell to 52%). The right article was found with a high score; only the fact inside it was wrong, and nothing in the built-in checks looks at facts.

These are the cases that need a ground-truth check, a regression suite built from real failures, or an LLM judge. Reporting them is part of the result, not a flaw in the report.

## 6. What this does and does not show

- It shows the scoring, root-cause and canary logic behave sensibly against faults whose true cause is known, and how often they are wrong.
- The faults are planted and simple. Real failures are messier, so treat these rates as a best case, not a forecast.
- Durations are simulated unless noted, and the agent's answers come from a retrieval-only bot unless you used `--llm`.
- This was an in-process check of the project's pure logic. The API, database and dashboard were not exercised; run it again without --dry-run for that.

## 7. Time to find the cause (fill in by hand)

Pick one failing version from the table. Start a timer, then answer: *why is it failing?*

| Method | Minutes to a confident answer | What you looked at |
|---|---|---|
| Reading raw logs / run data yourself | | |
| Using the dashboard (banner, Why runs fail, canary) | | |

## 8. Check the labels yourself

Open `labeling_sheet.csv`, fill the `your_label` column for each row using only the question, answer and steps, then run:

```
python validation/run_validation.py --score-labels validation/results/<this run>/labeling_sheet.csv
```

Allowed labels: agent_exception, empty_output, llm_error, retrieval_empty, retrieval_error, retrieval_low_relevance, slow_run, tool_error, tool_timeout, ungrounded_answer, unknown, none.
