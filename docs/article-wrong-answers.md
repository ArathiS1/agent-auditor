# My canary promoted a model that said "I am not sure" to everything

 "run it yourself": replace them with your own numbers if you run the real-model version.*

Last time I wrote about a canary check that promoted a model 78% slower than the one it replaced. This time it is worse.
I found a case where my tool promoted a model that answered "I am not sure." to every question.

The tool is Agent Auditor, an open-source project that compares a new version of an AI agent with the live one before it goes
out. It records runs, scores them, and answers promote, hold, rollback or continue. Until now it judged errors, speed,
retrieval and failure causes. It did not judge whether an answer was any good.

## The gap

I set up a fake candidate that replied "I am not sure." to all 28 questions, next to a baseline that answered properly.
No errors, no slowdown. Agent Auditor said **promote**.

There was a caveat, and it matters. My own benchmark attaches an expected answer to every question, and with those attached the
tool already rolled the candidate back. The gap was everything else. Real traffic has no answer key. Take the expected answers
away and the verdict went straight back to promote.

## What I added, and what each part can see

There are three ways to tell that an answer is wrong, and they cost different amounts:

1. **An answer key.** If you know the right answer, say so on the run (`output_contains: "Canberra"`). It is exact, free and
   needs no model. It only helps when you have the answers.
2. **A non-answer check, `gives_an_answer`.** On by default, no key, no model. For short answers it flags "I am not sure",
   "I don't know", "I can't help with that". It works as a change detector: a good agent refuses sometimes too, so what matters
   is the rate going from 14% to 100%, not the first number.
3. **A model judge, `answer_judge`.** Off by default. A model grades each answer from 0 to 1, against a reference answer if the
   run has one. It costs money, it sends each question and answer to the model you pick, and it can be wrong.

## What happened

I ran two bad candidates against a good baseline with the expected answers hidden, and judged the same runs three ways.
The models are scripted, and so is the judge (it grades with the benchmark's answer key), so this shows the wiring, not
how well a real judge grades:

| Candidate | Checks before | Checks now | Checks plus judge |
|---|---|---|---|
| Says "I am not sure." to everything | promote | **rollback** (`gives_an_answer`) | rollback |
| Confident, fluent and wrong | promote | **promote** | **rollback** (`answer_judge`) |

The second row is the honest one. A fluent wrong answer gets past every free check I have. Only a judge, or an answer key,
catches it. I would rather say that plainly than sell the free check as more than it is.

One more thing I changed along the way. If a judge is not configured or cannot be reached, the canary used to be able to
leave it out without telling you. Now the report carries a warning, and with warnings set to block, it holds the release.

## What I have not shown

- **A real judge.** The one in my demo is a stand-in. I have not measured a real model's grades against human grades, so I
  would not let one block a release on its own yet. Read a sample of its grades next to the answers first. *(Run it yourself
  with `python bench/canary.py --baseline <a> --candidate <b> --unlabeled --judge` and add your numbers here.)*
- **Small samples.** 28 runs per side is enough to see 14% turn into 100%. It is not enough to see 5% turn into 15%.
- **Honest refusals.** Two of the baseline's 14 questions are ones where refusing is the right answer. They count as
  non-answers on both sides. It is a comparison, so it cancels out, but it is a reason the check is a signal and not a grade.

## Try it

```
pip install agent-auditor
python bench/wrong_answers.py      # from a clone of the repo; offline, about five seconds
agent-auditor demo
```

Source, docs and the full table are at https://github.com/ArathiS1/agent-auditor. If you run the judge on your own agent,
I would like to hear how its grades compare with yours.
