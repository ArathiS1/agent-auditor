# Running LLM apps in production


Cache identical questions for 24 hours to cut cost and latency. Log every run with its prompt version, so a bad answer can be traced back to a change.

A canary release sends 10 percent of traffic to the new version before everyone gets it. Keep a rollback plan: the previous prompt version must be one click away.

Set a monthly spending cap on API keys and alert at 80 percent. Never put API keys in code or git; use environment variables or a secrets manager.
