# Case studies on stock agents

`studies/run_study.py` compares two models on an agent you did not write and asks the canary for a verdict. The agent is a stock
[smolagents](https://github.com/huggingface/smolagents) `ToolCallingAgent` or a LangGraph agent from LangChain, with two tools.
Its code does not import Agent Auditor. The framework's own OpenInference instrumentation sends OpenTelemetry spans to the
Auditor's `/v1/traces`, so this also tests the [trace import](importing-traces.md) on spans written by other people's code.

Baseline and candidate are two model ids, recorded as two versions: the baseline as `production`, the candidate as `canary`.
Each question is asked of both, in the same order.

## Two numbers, kept apart

- **The answer key.** Every question has one right answer. The runner scores each answer itself and prints the counts and a
  Fisher exact test. The Auditor never sees these answers: imported runs carry none.
- **The canary's verdict.** What the Auditor concludes from the runs alone: errors, speed, retrieval, non-answers, and the
  model judge if you turn it on.

The report puts them side by side and says which case you are in: they agree, the answer key found a drop the canary missed,
or the canary flagged something the answer key did not show. The second case is expected for confident wrong answers when the
judge is off. It is reported as a limit of the canary, not hidden.

## Dry run first (no key)

```bash
pip install "agent-auditor[otel]" opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
pip install smolagents openinference-instrumentation-smolagents
python studies/run_study.py --fake --agent smolagents
```

`--fake` uses a scripted model server whose "weak" candidate misbehaves in a fixed way. It checks that your setup works. Its
report is labelled as a dry run, and its numbers describe the script, never a real model.

## Real run

```bash
# PowerShell
$env:OPENAI_API_KEY = "..."          # any OpenAI-compatible provider; set it in your shell, never in a file you commit
python studies/run_study.py --agent smolagents --baseline gpt-4o-mini --candidate gpt-4.1-nano --runs 30
python studies/run_study.py --agent langgraph  --baseline gpt-4o-mini --candidate gpt-4.1-nano --runs 30
```

For another provider, set `--base-url` and `--api-key-env` (for example Gemini's OpenAI-compatible endpoint and
`GEMINI_API_KEY`). Results go to `studies/results/<agent>-<baseline>-vs-<candidate>.md` and `.json`; the runs go to
`studies/study.db`, which you can open with `agent-auditor serve --db sqlite:///studies/study.db --api-key study-key`.

Add `--scorers answer_judge` (with the judge environment variables from [answer-correctness.md](answer-correctness.md)) to see
whether the judge catches what the free checks cannot.

## What this does not show

- 30 questions per side is small. Use `--runs 60` or more before drawing conclusions.
- The questions are easy and checkable on purpose, and the two tools are simple.
- It says nothing about the quality of the framework. A stock agent is a test subject, not a target.
- Models and endpoints change, so a result is true of the day it was run. The report records the date.
- If you publish a result, publish the JSON next to it so others can check the counts.
