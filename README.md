# TelegramChat

A Telegram bot backed by an AI agent: a local Ollama LLM that can call tools before answering,
with PostgreSQL storing per-chat conversation history so it has memory across messages.

Flow: Telegram message in → recent history loaded from Postgres → sent to Ollama chat model along
with available tools → if the model requests a tool call, it's executed and the result is fed back
(repeats up to 5 rounds) → final reply sent back via Telegram → user message and reply saved to
Postgres.

Agent tools (defined in [tools.py](tools.py), the loop lives in
[ollama_client.py](ollama_client.py)):
- `calculator` — safe arithmetic evaluation
- `current_datetime` — current UTC date/time
- `web_search` — DuckDuckGo instant-answer lookup (no API key needed)
- `get_weather` — current conditions for a named location via Open-Meteo (no API key needed)
- `search_memory` — searches this chat's own Postgres history for a keyword; scoped to the
  current `chat_id` only, so the model can never query another chat's history

**Model caveat:** tool calling only works if the Ollama model you configure actually supports it
(check `capabilities` in `ollama list`/`ollama show <model>` includes `tools`). The default
`gemma4:31b-cloud` here has been verified to support it; if you switch models and tool calls stop
happening, that's the likely reason.

## Setup

1. Create a virtualenv and install dependencies:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN` — get this from @BotFather. **Do not commit this file.**
   - `OLLAMA_API_HOST` — defaults to `http://localhost:11434`. Deliberately not named
     `OLLAMA_HOST` — Ollama itself often sets that system-wide for its own server bind address
     (e.g. `0.0.0.0:11434`, no scheme), and since `python-dotenv` doesn't override existing
     environment variables by default, a name collision there silently breaks the client request.
   - `OLLAMA_MODEL` — the model tag to use (e.g. `ollama pull <model>` first, then confirm the
     exact tag with `ollama list`; check its `capabilities` include `tools` if you want tool
     calling to work).
   - `POSTGRES_DSN` — connection string for a reachable Postgres instance. The bot creates its
     own `messages` table on startup.
   - `HISTORY_LIMIT` — how many past messages to feed back to the model as memory (default 20).
3. Run the bot:
   ```
   python bot.py
   ```

## Security note

An earlier version of this README contained a live bot token in plaintext. If that token was ever
committed or shared, regenerate it via @BotFather (`/revoke` then `/token`) and put the new one
only in your local `.env` file, which is git-ignored.
