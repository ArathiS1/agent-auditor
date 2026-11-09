# Prompting


Put instructions first and the retrieved context after them, and separate sections with clear labels. Temperature 0 gives the most repeatable answers, so use it for question answering.

Few-shot examples teach the output format; 2 or 3 examples are usually enough. Ask for JSON output with a schema when another program reads the answer.

Chain-of-thought prompting asks the model to reason step by step before the final answer. Keep the system prompt under 500 words, since long prompts hide the important rules.
