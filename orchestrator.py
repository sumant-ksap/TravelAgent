import asyncio
import datetime
import json
import logging
import re

from prompts import (
    AGENT_PROMPTS,
    AGENT_STATE_KEY,
    AGENT_TOOLS,
    PHASE_ONE_AGENTS,
    PHASE_TWO_AGENTS,
    PLANNER_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def default_trip_state() -> dict:
    return {
        "origin": None,
        "destinations": [],
        "departure_date": None,
        "return_date": None,
        "travelers": {"adults": 1, "children": 0, "infants": 0},
        "budget": {"amount": None, "currency": None},
        "preferences": {
            "travel_style": None,
            "hotel_level": None,
            "cabin": None,
            "interests": [],
            "dietary": [],
            "accessibility": [],
        },
        "destination_research": {},
        "flights": {},
        "hotels": {},
        "transport": {},
        "activities": {},
        "food": {},
        "weather": {},
        "visa": {},
        "itinerary": {},
        "budget_analysis": {},
        "selected_options": {},
        "booking_status": {},
    }


def _merge_lists(base_list: list, new_list: list) -> list:
    """Union rather than overwrite, so e.g. adding a second destination or
    interest can't silently wipe out ones already recorded this trip -
    regardless of whether the planner sends just the new items or the full
    list back (both are safe to merge this way)."""
    merged = list(base_list)
    for item in new_list:
        if item not in merged:
            merged.append(item)
    return merged


def _deep_merge(base: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if value is None or value == "":
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        elif isinstance(value, list) and isinstance(base.get(key), list):
            base[key] = _merge_lists(base[key], value)
        else:
            base[key] = value
    return base


def _extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _format_history(history: list[dict]) -> str:
    return "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in history)


class TravelOrchestrator:
    """Runs the agentic travel workflow: extract requirements -> update
    TripState -> fan out to only the specialist agents needed this turn
    (phase 1 concurrently, phase 2 after) -> synthesize one final reply."""

    def __init__(self, ollama, trip_store):
        self._ollama = ollama
        self._trip_store = trip_store

    async def handle_turn(self, chat_id: int, history: list[dict], user_message: str) -> str:
        trip_state = await self._trip_store.get_trip_state(chat_id)
        if not trip_state:
            trip_state = default_trip_state()

        plan = await self._plan(history, trip_state, user_message)
        logger.info(
            "chat %s: intent=%s agents_needed=%s",
            chat_id,
            plan.get("intent"),
            plan.get("agents_needed"),
        )

        updates = plan.get("trip_state_updates")
        if isinstance(updates, dict):
            _deep_merge(trip_state, updates)

        agents_needed = [a for a in (plan.get("agents_needed") or []) if a in AGENT_PROMPTS]
        agent_briefs = plan.get("agent_briefs") or {}

        if not agents_needed:
            reply = plan.get("direct_reply") or await self._fallback_reply(history, trip_state, user_message)
            await self._trip_store.save_trip_state(chat_id, trip_state)
            return reply

        results: dict = {}

        phase_one = [a for a in agents_needed if a in PHASE_ONE_AGENTS]
        if phase_one:
            phase_one_results = await asyncio.gather(
                *[
                    self._run_specialist(name, agent_briefs.get(name, ""), trip_state, user_message)
                    for name in phase_one
                ]
            )
            for name, result in zip(phase_one, phase_one_results):
                results[name] = result
                self._apply_result(trip_state, name, result)

        phase_two = [a for a in agents_needed if a in PHASE_TWO_AGENTS]
        if phase_two:
            phase_two_results = await asyncio.gather(
                *[
                    self._run_specialist(
                        name, agent_briefs.get(name, ""), trip_state, user_message, prior_results=results
                    )
                    for name in phase_two
                ]
            )
            for name, result in zip(phase_two, phase_two_results):
                results[name] = result
                self._apply_result(trip_state, name, result)

        reply = await self._synthesize(history, trip_state, user_message, results)
        await self._trip_store.save_trip_state(chat_id, trip_state)
        return reply

    async def _plan(self, history: list[dict], trip_state: dict, user_message: str) -> dict:
        today = datetime.date.today().isoformat()
        content = (
            f"Today's date: {today}\n\n"
            f"Current TripState (JSON):\n{json.dumps(trip_state, default=str)}\n\n"
            f"Recent conversation:\n{_format_history(history[-8:])}\n\n"
            f"Latest traveler message: {user_message}\n\n"
            "Respond with the JSON object described in your instructions."
        )
        try:
            raw = await self._ollama.chat(
                [{"role": "user", "content": content}],
                system_prompt=PLANNER_SYSTEM_PROMPT,
                json_mode=True,
            )
        except Exception:
            logger.exception("Planner call failed")
            return {"agents_needed": [], "direct_reply": None}

        plan = _extract_json(raw)
        if not isinstance(plan, dict):
            # Raw text here is the planner's broken JSON attempt, not a natural-language
            # reply - showing it to the traveler verbatim would look like a bug. Let
            # handle_turn fall back to a proper synthesis call instead.
            logger.warning("Planner returned non-JSON output: %r", raw[:300])
            return {"agents_needed": [], "direct_reply": None}
        return plan

    async def _run_specialist(
        self,
        name: str,
        brief: str,
        trip_state: dict,
        user_message: str,
        prior_results: dict | None = None,
    ) -> dict:
        context_parts = [f"TripState (JSON):\n{json.dumps(trip_state, default=str)}"]
        if prior_results:
            context_parts.append(
                "Specialist results gathered earlier this turn (JSON):\n"
                + json.dumps(prior_results, default=str)
            )
        context_parts.append(
            f"Task for you from the orchestrator: {brief or 'Assist with the traveler request below.'}"
        )
        context_parts.append(f"Traveler's latest message: {user_message}")

        try:
            raw = await self._ollama.chat(
                [{"role": "user", "content": "\n\n".join(context_parts)}],
                system_prompt=AGENT_PROMPTS[name],
                tool_names=AGENT_TOOLS.get(name, []),
            )
        except Exception:
            logger.exception("Specialist agent %s failed", name)
            return {
                "agent": name,
                "status": "failed",
                "data": {},
                "warnings": [f"The {name} agent failed to respond."],
                "assumptions": [],
                "sources": [],
                "requires_user_input": False,
            }

        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            parsed = {
                "agent": name,
                "status": "success",
                "data": {"summary": raw},
                "warnings": [],
                "assumptions": [],
                "sources": [],
                "requires_user_input": False,
            }
        parsed.setdefault("agent", name)
        return parsed

    def _apply_result(self, trip_state: dict, name: str, result: dict) -> None:
        key = AGENT_STATE_KEY.get(name)
        if not key:
            return
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            trip_state[key] = data

    async def _synthesize(
        self, history: list[dict], trip_state: dict, user_message: str, results: dict
    ) -> str:
        content = (
            f"TripState (JSON):\n{json.dumps(trip_state, default=str)}\n\n"
            f"Specialist agent results (JSON):\n{json.dumps(results, default=str)}\n\n"
            f"Recent conversation:\n{_format_history(history[-8:])}\n\n"
            f"Traveler's latest message: {user_message}\n\n"
            "Write the final traveler-facing reply now."
        )
        try:
            reply = await self._ollama.chat(
                [{"role": "user", "content": content}], system_prompt=SYNTHESIS_SYSTEM_PROMPT
            )
        except Exception:
            logger.exception("Synthesis call failed")
            return "I gathered some information but ran into trouble writing it up. Could you try again?"
        return reply or "I ran into trouble putting together a response. Could you rephrase your request?"

    async def _fallback_reply(self, history: list[dict], trip_state: dict, user_message: str) -> str:
        content = (
            f"TripState (JSON):\n{json.dumps(trip_state, default=str)}\n\n"
            f"Specialist agent results (JSON): {{}}\n\n"
            f"Recent conversation:\n{_format_history(history[-8:])}\n\n"
            f"Traveler's latest message: {user_message}\n\n"
            "Write the final traveler-facing reply now."
        )
        try:
            reply = await self._ollama.chat(
                [{"role": "user", "content": content}], system_prompt=SYNTHESIS_SYSTEM_PROMPT
            )
        except Exception:
            logger.exception("Fallback synthesis call failed")
            return "Could you tell me a bit more about your trip?"
        return reply or "Could you tell me a bit more about your trip?"
