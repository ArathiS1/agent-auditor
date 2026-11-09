# Embeddings and vector search


Embeddings turn text into vectors, and cosine similarity measures how close two vectors point. The text-embedding-3-small model produces 1536 dimensions.

FAISS is a library for fast similarity search in memory, while Chroma stores vectors on disk with metadata. HNSW is an approximate nearest-neighbour index that trades a little accuracy for large speed gains.

Normalize vectors to length 1 so a dot product equals cosine similarity. Re-embed every document whenever you change the embedding model, because vectors from different models are not comparable.
