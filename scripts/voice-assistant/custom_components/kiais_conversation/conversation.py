"""Conversation agent that streams from an OpenAI-compatible chat completions API.

Modelled on the core `ollama` and `open_router` integrations of Home Assistant 2026.10:
the chat log, the LLM API (tools) and the streaming deltas are Home Assistant's own; only
the transport (Server-Sent Events from `/v1/chat/completions`) is ours.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import json
from typing import Any, Literal

import aiohttp
import probatio

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    CONF_LLM_HASS_API,
    CONF_MODEL,
    CONF_PROMPT,
    MATCH_ALL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, intent, llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.json import json_dumps
from homeassistant.util import ulid

from .const import (
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_TEMPERATURE,
    CONF_TIMEOUT,
    DEFAULT_MAX_HISTORY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    LOGGER,
    MAX_TOOL_ITERATIONS,
)

# Spoken when the gateway fails; the pipeline is Spanish-only.
ERROR_SPEECH = "Lo siento, ahora no puedo responder."
RETRY_STATUSES = {429, 500, 502, 503, 504}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    async_add_entities([KiaisConversationEntity(entry)])


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Format a Home Assistant LLM tool as an OpenAI function tool (as core ollama)."""
    spec: dict[str, Any] = {
        "name": tool.name,
        "parameters": probatio.to_openapi(
            tool.parameters,
            custom_serializer=custom_serializer,
            openapi_version="3.1.0",
        ),
    }
    if tool.description:
        spec["description"] = tool.description
    return {"type": "function", "function": spec}


def _tool_result_payload(content: conversation.ToolResultContent) -> str:
    """Serialise a tool result (2026.10 `result`, older `tool_result`)."""
    result = getattr(content, "result", None)
    if result is not None:
        return json_dumps({"data": result.data, "error": result.error})
    return json_dumps(content.tool_result)


def _to_message(content: conversation.Content) -> dict[str, Any] | None:
    """Convert chat log content to a chat completions message (as core open_router)."""
    if isinstance(content, conversation.ToolResultContent):
        return {
            "role": "tool",
            "tool_call_id": content.tool_call_id,
            "content": _tool_result_payload(content),
        }
    if content.role in ("system", "user"):
        return {"role": content.role, "content": content.content or ""}
    if isinstance(content, conversation.AssistantContent):
        message: dict[str, Any] = {"role": "assistant", "content": content.content}
        if content.tool_calls:
            message["tool_calls"] = [
                {
                    "type": "function",
                    "id": call.id,
                    "function": {
                        "name": call.tool_name,
                        "arguments": json_dumps(call.tool_args),
                    },
                }
                for call in content.tool_calls
            ]
        return message
    return None


def _trimmed(content: list[conversation.Content], max_user_turns: int) -> list:
    """Keep the system prompt and the last `max_user_turns` user turns (0 keeps all).

    Cuts only at user messages so tool calls and their results stay together.
    """
    if max_user_turns <= 0:
        return content
    user_idx = [i for i, c in enumerate(content) if c.role == "user"]
    if len(user_idx) <= max_user_turns:
        return content
    start = user_idx[-max_user_turns]
    return [c for c in content[:start] if c.role == "system"] + content[start:]


class KiaisConversationEntity(conversation.ConversationEntity):
    """Kiais conversation agent."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise the agent."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Kiais",
            model=entry.data[CONF_MODEL],
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        if entry.options.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return the supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as an agent."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister the agent."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer one user turn."""
        options = self.entry.options
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        try:
            await self._async_handle_chat_log(chat_log)
        except HomeAssistantError as err:
            LOGGER.error("Kiais request failed: %s", err)
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, ERROR_SPEECH)
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )

        # continue_conversation follows Home Assistant's rule (the answer ends in "?").
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_handle_chat_log(self, chat_log: conversation.ChatLog) -> None:
        """Run the tool loop, streaming each round into the chat log."""
        options = self.entry.options
        tools = None
        if chat_log.llm_api:
            tools = [
                _format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            ]
        max_history = int(options.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY))

        for _ in range(MAX_TOOL_ITERATIONS):
            payload: dict[str, Any] = {
                "model": self.entry.data[CONF_MODEL],
                "stream": True,
                "temperature": float(options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)),
                "max_tokens": int(options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
                "messages": [
                    m
                    for c in _trimmed(chat_log.content, max_history)
                    if (m := _to_message(c)) is not None
                ],
            }
            if tools:
                payload["tools"] = tools
            async for _content in chat_log.async_add_delta_content_stream(
                self.entity_id, self._stream(payload)
            ):
                pass
            if not chat_log.unresponded_tool_results:
                break

    async def _stream(
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Yield chat log deltas from one streamed chat completion."""
        session = async_get_clientsession(self.hass)
        url = f"{self.entry.data[CONF_BASE_URL].rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key := self.entry.data.get(CONF_API_KEY):
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=10,
            sock_read=float(self.entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        )

        yield {"role": "assistant"}
        calls: dict[int, dict[str, str]] = {}
        # One retry, and only before anything was received: the gateway already
        # retries and fails over between its backends.
        for attempt in (1, 2):
            try:
                async with session.post(
                    url, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status in RETRY_STATUSES and attempt == 1:
                        LOGGER.warning("Kiais HTTP %s, retrying once", resp.status)
                        await asyncio.sleep(0.5)
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        raise HomeAssistantError(f"HTTP {resp.status}: {body}")
                    async for delta in self._parse_sse(resp, calls):
                        yield delta
                break
            except (aiohttp.ClientConnectionError, TimeoutError) as err:
                if attempt == 1 and not calls:
                    LOGGER.warning("Kiais connection error, retrying once: %s", err)
                    await asyncio.sleep(0.5)
                    continue
                raise HomeAssistantError(f"gateway error: {err}") from err

        if calls:
            tool_calls = []
            for slot in calls.values():
                try:
                    args = json.loads(slot["arguments"] or "{}")
                except json.JSONDecodeError as err:
                    raise HomeAssistantError(f"bad tool arguments: {err}") from err
                tool_calls.append(
                    llm.ToolInput(
                        id=slot["id"] or ulid.ulid_now(),
                        tool_name=slot["name"],
                        tool_args=args,
                    )
                )
            yield {"tool_calls": tool_calls}

    @staticmethod
    async def _parse_sse(
        resp: aiohttp.ClientResponse, calls: dict[int, dict[str, str]]
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Parse `data:` lines; accumulate tool call fragments into `calls`."""
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            chunk = json.loads(data)
            if "error" in chunk:
                raise HomeAssistantError(f"gateway error: {chunk['error']}")
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if thinking := delta.get("reasoning_content") or delta.get("reasoning"):
                    yield {"thinking_content": thinking}
                if text := delta.get("content"):
                    yield {"content": text}
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        tc.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    # OpenAI sends the name once; some backends behind Kiais repeat the
                    # whole name in every chunk, so only append a different piece.
                    if (name := fn.get("name")) and name != slot["name"]:
                        slot["name"] += name
                    slot["arguments"] += fn.get("arguments") or ""
