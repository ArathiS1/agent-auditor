## What is Agent Auditor (start here)
Agent Auditor watches an AI agent (for example your Auditor Chat) and tells you when it gets worse. Every question the agent answers is saved as a "run" with all the steps it took. The Auditor scores each run with automatic checks, explains why failed runs failed, turns real failures into regression tests, and lets you try a new version on a small share of traffic (a canary) before you switch to it. Nothing is changed automatically: a person approves every release.
Beginner path: open Overview, read the banner, open Failures, click a failed run, read "What to change".

## Which agent am I looking at (chat-agent, demo-, validation-)
The agent picker at the top selects whose runs every page shows. "chat-agent" is your real Auditor Chat: every question you type at 127.0.0.1:8001 appears there. Agents that start with "demo-" come from Setup, "Load demo data": invented support-bot runs so you can see a full dashboard. Agents that start with "validation-" come from the self-check (python validation\selfcheck.py or run-all.ps1 -Check): a made-up support bot with a toy knowledge base. Its failures are created ON PURPOSE (rate limits, timeouts, empty retrieval) to prove the Auditor catches them, so there is nothing to fix there. To see your own chatbot, choose chat-agent in the picker.

## Why are there so many failures
First look at which agent is selected in the picker at the top. If it starts with "validation-" or "demo-", the failures are test data made on purpose (rate limits, timeouts, empty searches) to prove the Auditor catches them, so there is nothing to fix. Choose "chat-agent" to see your real chat. For chat-agent, open Root causes: the most common cause is usually a model limit (429 quota, 503 overload) or a model with no credit, and the fix is a working free key or another model. A question that loops on a tool can also use up a free daily quota quickly.

## Overview page
The banner says whether things are healthy. Tiles: Runs (how many questions in the period), Runs with problems (errored runs plus runs whose answer failed a check), Quality checks passed, Median answer time, and Not scored yet (runs that no check has looked at; press "Score and diagnose now"). The tape shows one tick per run from oldest to newest: green is ok, orange is failed, taller means slower, a dashed line marks a new version going live. Action needed lists the most urgent findings with a button to the right page.

## Failures page
Every run that errored or answered badly, newest first, each with a likely cause, the checks that failed, and "What to change". Filter by cause. Open trace shows every step. Use "Make a regression test" so the same mistake cannot return unnoticed. Failure types you will see: llm error (the model refused or was down: rate limit, quota, overload), retrieval low relevance or empty (search found little), tool error or timeout, wrong answer, user rejected (a thumbs down), empty output.

## Runs page
All runs, not only failed ones. Filter by version, status or search text, and page through. Click a run to open its drawer: input, answer, error, tags, the step waterfall with timings, and quality checks.

## Quality page
Pass rate for each automatic check (run succeeded, no step errors, output present, retrieval returned documents, retrieval score, latency within budget, answer grounded in retrieved text, expectations met), per version, with a 95% range. A short range means a trustworthy number; a long range means too few runs.

## Root causes page
Groups failed runs by their most likely cause. Strong evidence means an error pointed straight at it; weak means a guess from symptoms. Click a cause for what it means and the first thing to try. If a cause is much more common in one version, that points to a recent change.

## Drift page
Compares recent behaviour with the week before: answer length, speed, failure rate, tool use, questions asked. Press Check now. Drift is not automatically bad; it says "something changed, look". Small sample sizes make drift unreliable.

## Versions page
A version is a saved prompt plus settings. You can register one, compare two (pass rates side by side with the prompt difference), and see which is active. Registering is idempotent: the same content gives the same version.

## Regression tests page
Real failures saved as tests with expectations such as "answer contains 30 days". Replaying them against a new version proves a fix works and nothing old broke.

## Fixes page
Proposes concrete changes for the most common causes (a firmer prompt rule, a longer timeout, a setting). They are proposals only. Register one as a new version, then verify it against the regression tests, then canary it.

## Benchmark page
The same questions asked of several models, each answer checked with plain text rules (no model judges another). Run python bench\run.py (or run-all.ps1 -Bench) to fill it. It ranks models by quality, speed and errors, by category (facts, math, tools, instructions, safety, reasoning). Compare models in the chat also feeds it.

## Gap analysis page
Compares your chat's features with ChatGPT, Claude, Gemini, Perplexity, Copilot and Blackbox and ranks what to build next by how many others have it and how much it matters. The feature table is kept by hand and may be out of date. Copilot and Blackbox have no public chat API, so they only appear here.

## Canary page
Try a candidate version on a small share of traffic and compare it with the current one. The verdict is promote (looks safe), hold (not enough proof), rollback (worse) or continue (keep collecting runs). With few runs the verdict stays "hold" because the numbers could be luck.

## Releases page
A release is a request to make a version the active one. It needs the canary evidence to be fresh, and enough approvals from someone other than the proposer. Promote only marks the version active; it does not deploy code. Roll back puts the previous version back. Every step goes in the audit log.

## Audit log page
A permanent record of every decision: who proposed, approved, rejected, promoted, rolled back, and when, including refused attempts.

## Setup page
Connection checks (is the server reachable, is your key accepted), key entry, a "Load demo data" button, and copy-ready commands. The dashboard key is the AUDITOR_API_KEY in your .env file.

## Why does the chat say quota, rate limit or 429
429 means the provider refused because of limits. For Gemini's free tier each model has a daily quota that resets once a day. Another model has its own quota. 503 means the provider is overloaded; try again or switch models. 402 or "credit balance" means a paid provider has no money in the account. 401 means the key is wrong. The chat tries other models automatically and tells you what it did. Run python chatbot\check_keys.py to test every key.

## How do I add or fix an API key
Open .env in the project folder and add one line per provider with no spaces or quotes: GEMINI_API_KEY=..., OPENROUTER_API_KEY=..., MISTRAL_API_KEY=... Save, close the servers, and run run-all.ps1 again. Free options: Gemini (aistudio.google.com/apikey), OpenRouter (free models ending in :free), Mistral (small free tier), Ollama (runs on your PC, no key). Never paste a key into a chat or share a screenshot of it.

## How do failures get into the dashboard
The chat app sends every answer to the Auditor with the steps (model call, tools, searches), timing, the model name and the question. The Auditor stores it, runs the automatic checks, and classifies failures. A thumbs-down in the chat adds a "user rejected" failure. If nothing appears, check that the chat printed "Tracing ON" and the key in .env matches.

## Advanced: how scoring and diagnosis work
Scoring runs deterministic scorers (no model needed): run_succeeded, no_step_errors, output_present, retrieval_nonempty, retrieval_top_score, latency_within_budget, grounded_overlap (a weak word-overlap proxy for "answer supported by retrieved text"), expectations_met (text rules saved on the run), and gives_an_answer (a weak heuristic that flags short non-answers such as "I am not sure", for runs with no saved rules). An optional answer_judge scorer has a model grade each answer from 0 to 1; it is off by default and sends the question and answer to the model the server is configured with. Diagnosis is rule-based over error text and failed checks, with 13 causes. Pass rates use Wilson 95% intervals. Comparing versions uses an interval on the difference. Drift uses simple statistics over windows. Without saved rules or the judge, nothing here can tell that a fluent answer is wrong.

## Advanced: environments, tags and versions
Runs have an environment: production, canary or test. Benchmarks run in test with tags bench, target, question_id, category and expect. The chat tags runs with conversation, mode, turn_id and target (provider:model). Versions are matched by a content hash of the prompt and config. The chat registers one version per model; only the default is activated.

## Advanced: the API
Everything the dashboard does is in the REST API under /v1 with the X-API-Key header. See /docs for Swagger. Useful endpoints: /v1/runs, /v1/insights/overview, /v1/insights/failures, /v1/evals/run, /v1/agents/{name}/drift, /v1/feedback, /v1/insights/benchmark, /v1/insights/gap-analysis. The Python SDK (agent_auditor.sdk.Auditor) records runs with auditor.run(...), registers versions, and runs evals.

## Advanced: how to reduce failures
Look at Root causes first: fix the most common cause, not the loudest. For llm errors add retries with backoff, fall back to another model, and lower request volume. For retrieval problems check chunk size, the number of results (top_k), the embedding model and whether the answer exists in the documents. For wrong answers add the failing question as a regression test, tighten the prompt, and canary the change. For slow answers check which step takes the time in the trace.

## Troubleshooting: dashboard is blank
A blank page means the Auditor server is running old code or the script failed. Close the Auditor window and run run-all.ps1 again; it restarts an old server. If the key was rejected, the page asks for it again: use AUDITOR_API_KEY from .env.

## Troubleshooting: numbers look strange
The numbers cover the selected agent and period (top right). "Failures" in the menu counts runs with problems for the selected agent. Different agents have separate counts. A very high count for a validation- agent is expected test data.
