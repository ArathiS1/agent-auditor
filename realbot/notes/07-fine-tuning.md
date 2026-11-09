# Fine-tuning


Fine-tuning changes model behaviour and style; RAG is better for adding new facts. LoRA trains small adapter matrices instead of all weights, so it needs far less memory.

You need roughly 500 to 1000 clean examples for a useful instruction fine-tune. Hold out 15 percent of the data as a validation set to catch overfitting.

Quantization stores weights in 4 or 8 bits to shrink a model for cheaper serving. Always compare the fine-tuned model with the base model prompted well, on the same test set.
