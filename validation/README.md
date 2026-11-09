# Check that Agent Auditor really works

This folder tests the project against a small support bot whose faults are **planted**, so the right answer is always known.
It answers four questions with numbers:

1. Does a canary roll back a version that is really worse, and name the right cause?
2. Does it stay quiet when the change is harmless (false alarms)?
3. When it labels a failure, how often is the label right?
4. What does it miss?

It does not need an API key or any paid service. All commands below are for PowerShell in `G:\agent-auditor` with the virtual
environment active (`(.venv)` shown in the prompt).

## Step 1. Quick check with no server (about 30 seconds)

```powershell
python validation/run_validation.py --dry-run
```

This builds about 3,000 runs from the support bot and runs the project's own scoring, root-cause and canary code on them,
inside this one process. When it finishes it prints a short table and the path of `report.md`. Open that file.

## Step 2. The real check, through the server and database

Start the server in one terminal (or use Docker Compose), then in a second terminal:

```powershell
$env:AUDITOR_API_KEY = "<the same key the server uses>"     # skip this line if the server has no key
python validation/run_validation.py
```

It sends the runs to the API as a new agent called `validation-<number>`, asks the server for 35 canary verdicts, then reads
back the tool's root-cause label for every run. Use `--quick` for a smaller run (about a minute).
Then open `http://127.0.0.1:8000/dashboard` and pick that agent. The dashboard shows the last canary it evaluated;
every canary is also listed at `/docs` under `GET /v1/agents/{name}/canary`.

The server run should match the dry run closely, because the traffic is identical (same seed) and the server uses the same code.
**A difference between the two is itself a finding**: tell me what differs and we will find out why.

## Step 3. Read the report

`validation/results/<time>/report.md` has, in plain language:

| Section | The question it answers |
|---|---|
| 2. Did the canary catch the planted faults? | Per fault: how many of 5 canaries said rollback, and was the named cause right |
| 3. False alarms | How often a harmless change was wrongly stopped (10 tries) |
| 4. Root-cause labels | For each true cause: how many runs were flagged, and how many labels were right |
| 5. What it missed | The faults it could not see, and why |

What I expect from the dry run (so you can compare; yours should be close):

| Fault | Expected |
|---|---|
| harmless change (control) | promote 10 of 10 |
| tool timeout, model error, slow tool | rollback 5 of 5, correct cause named |
| half the articles missing from the index | rollback 3 of 5. Two canaries are missed because other articles take their place with decent scores |
| out-of-date facts | rollback 0 of 5. The tool cannot see wrong facts. This is a blind spot, not a bug in the test |

Do not tune the faults until the tool looks good. The misses are the useful result.

## Step 4. Label 30 failures yourself

Open `labeling_sheet.csv` from the same results folder in Excel. For each row, read only the question, the answer and the
steps, decide the cause yourself, and type it in `your_label` (the allowed names are listed at the end of `report.md`; use
`none` if you think it was not a failure). Then:

```powershell
python validation/run_validation.py --score-labels validation/results/<time>/labeling_sheet.csv
```

It prints how often you and the tool agree, and which rows differ. Look at each disagreement: sometimes the tool is wrong,
sometimes your label is, and sometimes a run has two causes at once.

## Step 5. Time how long a diagnosis takes

Pick one failing version from the report (for example `timeout-r1`). Start a timer and find out *why is it failing?* twice:

1. Using only the raw data (the JSON from `GET /v1/runs?agent=...`, or the database).
2. Using the dashboard: the banner, "Why runs fail", and the canary panel.

Write both times in section 7 of the report. This is the number that says whether the tool saves anyone time.

## Step 6 (optional). A real model instead of the stand-in

```powershell
$env:ANTHROPIC_API_KEY = "<your key>"
python validation/run_validation.py --quick --llm
```

The answers then come from the Claude API and the step durations are real. It prints the number of API calls first and asks
before spending anything. The faults stay planted the same way.

## Step 7 (the real test). Your own agent

The support bot proves the machinery against known faults. To learn whether the tool is useful on something real:

1. Connect your own agent with the SDK (see the README, "SDK in 20 lines"), so each run is sent to the server.
2. Run 100-300 real questions through version 1 as the baseline.
3. Make one deliberately worse version 2 (a weaker prompt, a lower `top_k`), send some traffic to it as `environment="canary"`, and run
   `POST /v1/agents/{name}/canary/evaluate`.
4. Repeat with a harmless change and check it says promote.
5. Fill in `docs/case-study.md` with the numbers.

## How the pieces work

| File | Role |
|---|---|
| `kb.py` | The fictional help center: 30 facts, 130 questions with known right answers, a small search index |
| `support_bot.py` | The support bot and its fault settings (timeouts, rate limits, missing articles, slow tool, stale facts) |
| `offline.py` | Runs the project's pure scoring, root-cause and canary code with no server |
| `harness.py` | Plans the traffic, runs it, and works out the numbers |
| `report.py` | Writes `report.md` |
| `run_validation.py` | The command you run |

## What this does not prove

The faults are planted and simple, durations are simulated, and the default bot answers by returning the best article. It shows the
logic is sound and where it is blind. It does not show the tool is useful on a messy real agent; Step 7 does.
