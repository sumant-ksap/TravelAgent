# TravelAgent

A Telegram travel-planning assistant built as a multi-agent AI workflow on top of a local/cloud
Ollama LLM, with PostgreSQL persisting both conversation history and a structured per-chat trip
state.

## Architecture

One user message triggers a full agentic workflow instead of a single flat model call:

1. **Orchestrator — planning** ([orchestrator.py](orchestrator.py), prompt in
   [prompts.py](prompts.py)): extracts any new/changed trip details into a structured `TripState`
   (origin, dates, travelers, budget, preferences, ...) and decides which specialist agents (if
   any) are actually needed to answer this turn. Simple chit-chat/follow-ups skip specialists
   entirely and get a direct reply.
2. **Specialist agents** — each is a separate LLM call with its own focused system prompt and its
   own scoped subset of tools (principle of least privilege): `destination`, `flight`, `hotel`,
   `transport`, `activities`, `food`, `weather`, `visa`, `itinerary`, `budget`, `booking`,
   `trip_support`. Data-gathering agents (destination/flight/hotel/transport/activities/food/
   weather/visa) run concurrently via `asyncio.gather`; `itinerary`/`budget`/`booking` run in a
   second phase since they consume the first phase's results. Every specialist returns a
   structured `{agent, status, data, warnings, assumptions, sources, requires_user_input}` result.
3. **Orchestrator — synthesis**: combines the `TripState` and every specialist's structured result
   into one coherent, traveler-facing reply — never dumping raw JSON, always distinguishing
   verified tool data from estimates.

`TripState` is persisted per Telegram chat in Postgres ([db.py](db.py)) so it survives across
messages; `/newtrip` resets it while carrying forward saved preferences, `/trip` shows the current
state, `/reset` clears everything.

## Tools

Defined in [tools.py](tools.py), dispatched through the generic tool-loop in
[ollama_client.py](ollama_client.py). Each specialist agent only ever sees the tools listed for it
in `prompts.AGENT_TOOLS`.

| Tool | Backing service | API key needed |
|---|---|---|
| `web_search` | DuckDuckGo instant answers | no |
| `get_weather` | Open-Meteo | no |
| `currency_convert` | Frankfurter (ECB reference rates) | no |
| `search_places` | OpenStreetMap Overpass (retries transient overload) | no |
| `get_route` | OSRM public routing (driving-network estimate) | no |
| `search_flights` | Amadeus flight offers | **yes, optional** |
| `search_hotels` | Amadeus hotel offers | **yes, optional** |
| `calculator` | safe arithmetic evaluation | no |
| `current_datetime` | current UTC date/time | no |
| `search_memory` | this chat's own Postgres history, scoped to its `chat_id` | no |

`search_flights`/`search_hotels` activate automatically once `AMADEUS_API_KEY` /
`AMADEUS_API_SECRET` are set in `.env` (free self-service test keys at
[developers.amadeus.com](https://developers.amadeus.com)). Without them, the Flight/Hotel agents
are told plainly that no live fare/rate provider is configured and fall back to web-search-based
research — the system is instructed to never fabricate a price or invented availability.

**Model caveat:** tool calling only works if the Ollama model you configure actually supports it
(check `capabilities` in `ollama list`/`ollama show <model>` includes `tools`). The default
`gemma4:31b-cloud` here has been verified to support it; if you switch models and tool calls stop
happening, that's the likely reason. This project also relies on `format: "json"` support for the
planning step — most modern Ollama models support this.

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
     own `messages`, `trip_state`, and `traveler_profile` tables on startup.
   - `HISTORY_LIMIT` — how many past messages to feed back to the model as memory (default 20).
   - `AMADEUS_API_KEY` / `AMADEUS_API_SECRET` — optional; leave blank unless you want live flight
     and hotel search (see the Tools table above).
3. Run the bot:
   ```
   python bot.py
   ```

## Security note

An earlier version of this README contained a live bot token in plaintext. If that token was ever
committed or shared, regenerate it via @BotFather (`/revoke` then `/token`) and put the new one
only in your local `.env` file, which is git-ignored.
