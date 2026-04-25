# EmulAItor

A local AI agent with web browsing, code execution, image generation, and persistent memory.

## Stack

- **Backend** — FastAPI + WebSockets
- **LLM** — OpenRouter (free model fallback chain)
- **Browser** — Playwright (+ browser-use for AI-driven navigation)
- **Code sandbox** — Docker (subprocess fallback)
- **Search** — Brave Search API
- **Images** — Pollinations.ai (free, no key)
- **Memory** — SQLite

---

## Setup

### 1. Clone / enter directory

```bash
cd bud.app
```

### 2. Create a virtual environment

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
```

### 5. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Key | Where to get it |
|-----|----------------|
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys (free account) |
| `BRAVE_API_KEY` | https://api.search.brave.com (free tier, 2k req/month) |

Both keys have free tiers — no credit card required.

### 6. Run

```bash
python main.py
```

Open **http://127.0.0.1:8000** in your browser.

---

## Tools available to the agent

| Tool | What it does |
|------|-------------|
| `web_search(query)` | Brave Search — current events, facts |
| `browse(url, task)` | Navigate, screenshot, read page content |
| `run_code(language, code)` | Execute Python / JS / Bash in sandbox |
| `image_generate(prompt)` | AI image via Pollinations (free) |
| `html_preview(html)` | Render HTML → screenshot |
| `remember(fact)` | Store to SQLite long-term memory |
| `recall(query)` | Search stored memories |
| `read_file(path)` | Read from `./workspace/` |
| `write_file(path, content)` | Write to `./workspace/` |

---

## LLM fallback chain

Configured in `llm_router.py` — edit `MODELS` list to change order:

1. `nousresearch/hermes-3-llama-3.1-405b:free`
2. `meta-llama/llama-3.2-3b-instruct:free`
3. `google/gemma-3-12b-it:free`
4. `google/gemma-3-4b-it:free`
5. `google/gemma-3n-e4b-it:free`
6. `google/gemma-3n-e2b-it:free`
7. `openrouter/auto`

On rate-limit (429), a model is cooled down for 60 seconds before retrying.
Models without native function-calling get prompt-injected tool instructions automatically.

---

## Optional: Docker sandbox

If Docker Desktop is installed and running, code execution uses an isolated container with no network and 128 MB RAM limit. Without Docker, it falls back to a local subprocess with a timeout.

---

## Project structure

```
bud.app/
├── main.py          FastAPI app + WebSocket handler
├── agent.py         Agent loop (no LangChain)
├── llm_router.py    Free model fallback chain
├── tools.py         All tool implementations
├── db.py            SQLite (sessions, messages, facts)
├── static/
│   ├── index.html   Frontend SPA
│   └── screenshots/ Auto-generated screenshots
├── workspace/       Sandboxed file read/write
├── .env             Your API keys (git-ignored)
├── .env.example     Key template
└── requirements.txt
```

---

## Tips

- **Shift+Enter** for newlines in the input box; **Enter** to send.
- Click any screenshot in the Activity pane to open a full-size lightbox.
- Tool cards in the Activity pane are collapsible — click the header.
- The `workspace/` folder persists between sessions; ask the agent to save files there.
- Memory (`remember` / `recall`) is global across all sessions.
