import json
import logging

import httpx

from tools import call_tool, get_tool_schemas

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5


class OllamaClient:
    """Thin wrapper around Ollama's /api/chat, reused by every agent in the
    orchestrator with its own system prompt and its own scoped tool subset."""

    def __init__(self, host: str, model: str, api_key: str | None = None):
        self._host = host.rstrip("/")
        self._model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=120.0, headers=headers)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, messages: list[dict], *, tool_names=None, json_mode: bool = False) -> dict:
        payload = {"model": self._model, "messages": messages, "stream": False}
        if tool_names:
            payload["tools"] = get_tool_schemas(tool_names)
        if json_mode:
            payload["format"] = "json"
        response = await self._client.post(f"{self._host}/api/chat", json=payload)
        response.raise_for_status()
        return response.json()["message"]

    async def chat(
        self,
        history: list[dict],
        *,
        system_prompt: str,
        tool_names: list[str] | None = None,
        memory=None,
        chat_id=None,
        json_mode: bool = False,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> str:
        """Run one agent turn. If tool_names is given, loops tool calls until
        the model returns plain content or max_tool_rounds is hit. json_mode
        only applies when the agent has no tools (Ollama's format=json mode
        is unreliable in combination with tool calling)."""

        messages = [{"role": "system", "content": system_prompt}, *history]

        if not tool_names:
            message = await self._request(messages, json_mode=json_mode)
            return message.get("content", "")

        allowed = set(tool_names)
        for _ in range(max_tool_rounds):
            message = await self._request(messages, tool_names=tool_names)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return message.get("content", "")

            messages.append(message)
            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                logger.info("Calling tool %s with args %s", name, arguments)
                result = await call_tool(name, arguments, memory=memory, chat_id=chat_id, allowed=allowed)
                messages.append({"role": "tool", "name": name, "content": result})

        # Exceeded tool-call rounds; force a final answer without offering tools again.
        final_message = await self._request(messages)
        return final_message.get("content", "")
