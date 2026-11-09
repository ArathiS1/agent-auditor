# Evaluating AI systems


Recall at k asks whether the correct passage appears in the top k retrieved results. Faithfulness checks that each claim in an answer is supported by the retrieved context.

A golden set of 50 to 100 question and answer pairs is enough for a first regression suite. LLM-as-judge scoring should be spot-checked by hand on at least 20 samples.

Track p95 latency, not just the average, because slow outliers hurt users most. Run the golden set again after every prompt or model change before shipping.
