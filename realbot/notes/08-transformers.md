# Transformers and tokens


Self-attention lets each token weigh every other token when building its representation. A context window is the maximum number of tokens a model can read in one request.

Tokens are word pieces; one token is about 4 characters of English on average. Positional encodings tell the model the order of tokens.

Decoder-only models such as GPT and Claude generate text one token at a time. The KV cache stores past attention keys and values so generation does not recompute them.
