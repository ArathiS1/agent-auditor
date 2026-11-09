# Auditor Chat: a ChatGPT-style assistant, watched by Agent Auditor

A chat window in your browser. It answers with Claude (paid key) or with a free provider: Google Gemini, Groq, or Ollama on your own PC.
- Streams answers as they are written, and you can press Stop.
- Remembers conversations (saved in `chatbot/chat.db`, a local file).
- **Attach files and images**: press + (or drag and drop, or paste an image). Reads txt, md, csv, json, code, PDF (needs `pip install pypdf`) and Word .docx; images need a model that can see (Claude, Gemini). Ask questions about the file; long files are searched passage by passage.
- **Web search**: the assistant searches the web and reads pages when a question needs current information, and cites its sources.
- **Deep research**: turn on the Deep research button for a multi-search, sourced report (slower, a few minutes).
- Other tools: calculator, current time, search your notes (`realbot/notes/`).
- Every chat turn can be sent to Agent Auditor as one run (a step per model call and per tool call).

Runs only on your computer (127.0.0.1). No web framework needed: Python standard library plus httpx.

## Run it (PowerShell, from G:\agent-auditor, venv active)
Pick ONE way to get answers, set its variables, then start the server. The app picks the provider from whichever key you set.

**A. Free: Google Gemini** (no card needed, limited requests per day)
1. Sign in at https://aistudio.google.com , choose "Get API key", create a key.
2. `$env:GEMINI_API_KEY = "<your key>"`

**B. Free: Groq** (no card needed, limited requests per day)
1. Sign up at https://console.groq.com , open "API Keys", create a key.
2. `$env:GROQ_API_KEY = "<your key>"`
   If it says the model was not found, pick a current tool-calling model from Groq's model list: `$env:CHAT_MODEL = "<model name>"`

**C. Free and private: Ollama** (runs on your PC, no key, no limits; needs a decent computer)
1. Install Ollama from https://ollama.com , then `ollama pull llama3.1`
2. `$env:CHAT_PROVIDER = "ollama"`

**D. Paid: Claude**  (console.anthropic.com, add credit, create a key)
`$env:ANTHROPIC_API_KEY = "sk-ant-..."`

Then: `python chatbot\server.py` and open http://127.0.0.1:8001 . The page header shows which provider and model are answering.

Notes: free models differ in quality and in how well they use tools, and free limits change, so check the provider's page.
Change the model with `$env:CHAT_MODEL = "..."`. Any other OpenAI-compatible service works with
`CHAT_PROVIDER=custom`, `CHAT_BASE_URL`, `CHAT_API_KEY`, `CHAT_MODEL`. Add `--port 8002` to change the port.

## Make web search reliable (recommended, free)
Out of the box search uses DuckDuckGo's page, which can block automated use. For dependable results get a free key from
https://tavily.com (or Brave Search API) and set it before starting: `$env:TAVILY_API_KEY = "<key>"` (or `$env:BRAVE_API_KEY`).

## Which model to expect what from
- Claude or Gemini: best at tools, files, images and deep research.
- Groq / OpenRouter free models: fine for chat, uneven with tools.
- Ollama small models (llama3.1 8B): chat works; web search, deep research and images are weak or unavailable.
  Download the model first or you get "did not find the model":  `ollama pull llama3.1`

## Watch it with Agent Auditor
Start the Auditor server first (port 8000), then in the chat terminal, before step 2:
```
$env:AUDITOR_URL = "http://127.0.0.1:8000"
$env:AUDITOR_API_KEY = "<the Auditor server's key>"
```
The startup line says `Tracing ON`. Chat for a while, then open http://127.0.0.1:8000/dashboard and pick agent `chat-agent`.
Change the model or system prompt and a new version label is registered automatically, so you can canary it like any other agent.

## Limits
- Claude is the model; this app is the chat window, memory and tools around it. Answers can be wrong.
- Web pages are fetched from public addresses only (private and local addresses are blocked). Scanned PDFs (pictures of text) cannot be read.
- One person, one computer. It has no sign-in, so do not expose the port to a network.
