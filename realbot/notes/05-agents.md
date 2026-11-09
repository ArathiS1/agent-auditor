# Agents and tools


An agent loop is: think, call a tool, read the result, and repeat until the task is done. Set a maximum of 8 steps per run so an agent cannot loop forever.

Every tool call needs a timeout; I use 10 seconds as the default. Retry rate-limit errors (HTTP 429) with exponential backoff: wait 1, 2, then 4 seconds.

Tool descriptions are prompts too; vague descriptions make the model pick the wrong tool. LangGraph models an agent as a graph of nodes and edges with shared state.
