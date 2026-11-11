# I tried to ship three changes. My canary promoted a slower model.

I built Agent Auditor, an open-source tool that compares a new version of an AI agent with the live one before it goes out.
It records runs, scores them, and answers one of four things: promote, hold, rollback or continue.
Until this week I had one real verdict from it, and it was a hold. So I ran all three outcomes on real models and wrote down
what came back, including the part I did not expect.

## The setup

Same 24 questions every time. A baseline model runs as "production", a candidate runs as "canary", and Agent Auditor
decides from the recorded runs: error rate, the built-in quality checks, answer time, failure causes and drift.

The two models were Indos `deepseek-v4-flash` (fast) and AIRouter `gpt-5-nano` (slower).

| Check | Baseline | Candidate | Median | 95th percentile | Verdict |
|---|---|---|---|---|---|
| A slower candidate | deepseek-v4-flash | gpt-5-nano | 2.6 s vs 4.7 s | 5.0 s vs 7.6 s | **promote** |
| A broken candidate | deepseek-v4-flash | same model, wrong address | 2.6 s vs 2.1 s | 5.0 s vs 2.1 s | **rollback** |
| A faster candidate | gpt-5-nano | deepseek-v4-flash | 4.3 s vs 1.9 s | 8.6 s vs 4.2 s | **promote** |

For the broken candidate, the times are how long the failed calls took. None of them returned an answer.

## What worked

**Rollback.** I broke the candidate on purpose by sending the same model to a wrong address, so all 24 calls failed.
This is a planted fault, a stand-in for a bad config push, and not a model that failed by itself. The tool rolled it back:
four quality checks failed, and a new failure cause, `llm_error`, went from 0% to 100% of runs.

**Promote.** The faster model was 0.47x the baseline's answer time, nothing got worse, and it was promoted.

## What I did not expect

I expected the first check to be a hold. Two days earlier the same two models had given me one:
`gpt-5-nano` was 3.16x slower on the latency check (medians 1.7 s against 4.4 s) and the check failed.

This time the latency check measured 1.78x (medians 2.6 s against 4.7 s). My default policy fails latency only at 2x or worse.
Between 1.3x and 2x it raises a **warning**, and warnings do not stop a promote. So the tool promoted a model that was
about 78% slower, with a latency warning and a not-significant quality difference sitting in the report.

Nothing was broken. The tool did what I wrote it to do. But "promote" is not what I would have decided by hand.
Two things came out of that:

1. **The same pair of models gave two different verdicts in two days.** Provider speed seems to have moved. With 24 runs per side,
   a fixed line at 2x is a thin basis for a hard decision, and I should say so.
2. **The tool now shows both verdicts.** For each check, `bench/verdicts.py` judges the same runs again with every warning set
   to block. Under that strict setting, the first and third checks both come back as hold, and the broken one stays rollback.

The strict setting is blunt, and that is a finding too. The drift check warns when a candidate is *faster* as well as slower,
so a strict policy can hold an improvement. A warning that knows which direction is bad is next on my list.

## What the canary still cannot see

It judges errors, speed, the built-in checks and failure causes. It cannot tell that an answer is wrong but well formed.
In an earlier test with fake model servers, a candidate that answered "I am not sure" to everything was promoted.
That is the next thing I am building: a scorer that checks answers against expectations.

## Try it

```
pip install agent-auditor
agent-auditor demo
```

The demo opens the dashboard with sample data, including a good candidate and a bad one.
Source, the live demo and the full report are at https://github.com/ArathiS1/agent-auditor.
If you run it on your own agent, I would like to hear where it breaks.
