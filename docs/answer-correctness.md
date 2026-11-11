# Catching wrong answers when you have no answer key

A canary compares a new version of an agent with the live one. Errors and slowness are easy to see. A wrong answer is
harder: a candidate that replies "I am not sure." to everything, or one that replies fluently and gets the facts wrong,
throws no error and is not slow. This page says what Agent Auditor can catch, how, and what it still cannot.

## What catches what

| The candidate... | Expected answers on the runs (`tags.expect`) | No expected answers (most real traffic) | Judge on (`answer_judge`) |
|---|---|---|---|
| errors or times out | caught | caught | caught |
| is much slower | caught | caught | caught |
| answers "I am not sure" or "I can't help with that" | caught (`expectations_met`) | caught (`gives_an_answer`) | caught |
| answers fluently and wrongly | caught (`expectations_met`) | **not caught** | caught, as well as the judge grades |
| is wrong in a way only a domain expert would notice | only if you wrote that rule | not caught | not caught reliably |

The middle column is the honest limit. With no answer key and no judge, a confident wrong answer looks like any other answer.

## 1. Give the runs an answer key

If you have questions with known answers (a regression suite, a benchmark, a support FAQ), tag each run with what a good
answer must contain. This is exact, free and needs no model:

```python
with auditor.run(input=question, tags={"expect": [{"type": "output_contains", "value": "Canberra"}]}):
    ...
```

The rule types are in `agent_auditor/evals/expect.py` (contains, does not contain, contains any, matches a pattern, word
limits). The scorer is `expectations_met`. `bench/canary.py` does this for its 14 questions.

## 2. `gives_an_answer`: non-answers, with no labels

On by default. For a short answer (30 words or fewer, a setting) it flags "I am not sure", "I don't know", "I can't help with
that", "as an AI" and similar, and the common Spanish and French equivalents ("no estoy seguro", "no lo sé", "no puedo
ayudar", "je ne sais pas", "je ne peux pas vous aider", "désolé, mais"). Longer answers pass, because a long answer with one hedge is usually still an answer. Runs that
carry `tags.expect` are skipped, because `expectations_met` knows the right answer.

What to expect from it:

- It is a **change detector**. A good agent also refuses or says "I don't know" sometimes, and rightly. In the offline demo
  below, 2 of the baseline's 14 questions are ones where refusing is the correct answer, and they count as non-answers on both
  sides. That is fine, because the canary compares the rate between baseline and candidate. A jump from 14% to 100% is the signal.
- A failed check is diagnosed as the cause `wrong_answer`, so the Causes check agrees with the Evals check.
- It does nothing about a confident wrong answer.

## 3. `answer_judge`: a model grades each answer

Off by default, because it costs money, sends each run's question and answer to the model you choose, and can be wrong.
Turn it on by pointing the Auditor server at any OpenAI-compatible chat endpoint:

```
AUDITOR_JUDGE_BASE_URL=https://api.openai.com/v1
AUDITOR_JUDGE_API_KEY=...
AUDITOR_JUDGE_MODEL=gpt-5-mini
```

Then ask for it by name, in an evaluation (`"scorers": ["answer_judge"]`) or in a canary check:

```python
report = auditor.evaluate_canary(baseline="v1", candidate="v2",
                                 scorers=[*default_scorer_names(), "answer_judge"])
```

or, with the chat benchmark, `python bench/canary.py --baseline a --candidate b --unlabeled --judge`.

How it behaves:

- It grades from 0 to 1 and passes at 0.5 or more (`threshold` is a parameter). A run tagged with `tags.reference` is graded
  against that reference answer. Without one, the judge uses its own knowledge and is told to score 0.5 or lower when it cannot
  tell, so it is better at common knowledge than at your private data.
- The question and answer are passed as data and the judge is told to ignore instructions inside them, but a judge model can
  still be misled by an adversarial answer. Do not treat its grade as proof.
- If the judge is not configured, times out or replies in a form that cannot be read, the failure is reported as a scorer
  error. The canary report then carries a `scorers` warning ("answer_judge: not configured...") instead of quietly leaving the
  judge out. With `warnings_block` on, that warning holds the release.
- Each run is graded once and the grade is stored. It runs when you ask for it, over unscored runs, not on every request.

## See it work (offline, about 5 seconds)

```
python bench/wrong_answers.py
```

It plays two scripted candidates against a good baseline, 28 runs each, with the expected answers hidden:

```
candidate A: answers "I am not sure." to everything
  built-in checks before this change             PROMOTE
  built-in checks now (adds gives_an_answer)     ROLLBACK    caught by: gives_an_answer
  built-in checks plus answer_judge              ROLLBACK    caught by: answer_judge, gives_an_answer

candidate B: confident, fluent, and wrong
  built-in checks before this change             PROMOTE
  built-in checks now (adds gives_an_answer)     PROMOTE
  built-in checks plus answer_judge              ROLLBACK    caught by: answer_judge
```

"Before" is the same runs scored with the checks that existed before `gives_an_answer`. These are scripted models, and the
judge here is a scripted stand-in that grades with the benchmark's own answer key, so it shows how the verdict behaves, not
how well a real judge model grades. Candidate B's `PROMOTE` in the middle row is the point: no heuristic sees it.

## What is still open

- A real judge has not been measured against human grades here. Before you let it block releases, read a sample of its
  grades next to the answers.
- 24 to 30 runs per side is a small sample. A non-answer jump from 0% to 100% is unmistakable; a move from 5% to 15% is not.
- The judge sends data to a third party. Use a judge you are allowed to send that data to.
