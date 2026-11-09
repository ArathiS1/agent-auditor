# RAG basics


A good starting chunk size for notes is 300 to 500 tokens, with an overlap of about 50 tokens. Retrieving the top 4 chunks is my default; going above 8 usually adds noise and cost.

Hybrid search combines keyword search (BM25) with vector search, and it helps most on exact terms like product codes. A reranker rescores the top 20 retrieved chunks with a cross-encoder before the best few go to the model.

Add the line 'answer only from the context, otherwise say you do not know' to the prompt to reduce made-up answers. Metadata filters such as document date or source let retrieval skip stale files.
