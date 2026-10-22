"""The model behind the bot: Claude, or any OpenAI-compatible endpoint (a
local Ollama, or a free hosted API).

The agent speaks the Messages API's shapes throughout: its history, its
tool definitions and the blocks it reads back. `AnthropicModel` passes those
straight through; `OpenAICompatibleModel` translates them to chat completions
on the way out and the response back into Anthropic blocks, so history never
mixes the two. Either one answers `messages.create(...)` with an object that
has `.content` and `.stop_reason`, and raises `ModelError` when the model
couldn't be reached.
"""

import json
from typing import Any, Protocol

import anthropic
from anthropic.types import Message, TextBlock, ToolUseBlock
from loguru import logger

from tee_time_booker.config import BotSettings

# Calls retry with backoff on connection errors, timeouts and 5xx.
MAX_RETRIES = 6

STOP_REASONS = {"tool_calls": "tool_use", "length": "max_tokens"}


class ModelError(Exception):
    """The model couldn't be reached or refused the request. `str()` is the
    error's kind, short enough for a chat; `detail` is the API's own words."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.detail = detail or kind


def _model_error(e: Exception) -> ModelError:
    return ModelError(type(e).__name__, _detail(e))


def _detail(e: Exception) -> str:
    """The most specific message in an API error: OpenRouter puts the
    upstream provider's in `error.metadata.raw`."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            raw = (error.get("metadata") or {}).get("raw")
            message = raw if isinstance(raw, str) else error.get("message")
            if message:
                return str(message)
    return str(e)


class Model(Protocol):
    @property
    def messages(self) -> "Model": ...

    async def create(self, **request: Any) -> Any: ...


class AnthropicModel:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

    @property
    def messages(self) -> "AnthropicModel":
        return self

    async def create(self, **request: Any) -> Message:
        try:
            return await self._client.messages.create(**request)
        except anthropic.APIError as e:
            raise _model_error(e) from e


class OpenAICompatibleModel:
    def __init__(self, client: Any) -> None:
        # An `openai.AsyncOpenAI`, imported lazily by `make_model`.
        self._client = client

    @property
    def messages(self) -> "OpenAICompatibleModel":
        return self

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        **_anthropic_only: Any,  # output_config, cache_control
    ) -> Any:
        import openai

        try:
            response = await self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    *to_chat_messages(messages),
                ],
                tools=[to_chat_tool(t) for t in tools],
            )
        except openai.APIError as e:
            raise _model_error(e) from e
        if not response.choices:
            raise ModelError("empty response")
        return from_chat_choice(response.choices[0])


def make_model(settings: BotSettings) -> Model:
    return _connect(settings.provider, settings.base_url, settings.api_key)


def _connect(
    provider: str,
    base_url: str | None,
    api_key: str | None,
    max_retries: int = MAX_RETRIES,
) -> Model:
    if provider == "anthropic":
        # Without a key, the SDK reads ANTHROPIC_API_KEY.
        return AnthropicModel(
            anthropic.AsyncAnthropic(api_key=api_key, max_retries=max_retries)
        )
    import openai

    return OpenAICompatibleModel(
        openai.AsyncOpenAI(
            base_url=base_url,
            # Ollama ignores the key, but the SDK insists on one.
            api_key=api_key or "unused",
            max_retries=max_retries,
        )
    )


# Enough to show the model answers and takes tools: some free endpoints
# refuse tools, or refuse callers that aren't on their list.
CHECK_TOOL = {
    "name": "ping",
    "description": "Not used; answer in text.",
    "input_schema": {"type": "object", "properties": {}},
}


async def check_model(
    provider: str,
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str | None:
    """Why `model` can't run the bot, or None when one request with a tool
    in it gets an answer. Not retried, so a failing endpoint says so at once."""
    try:
        await _connect(provider, base_url, api_key, max_retries=0).messages.create(
            model=model,
            max_tokens=256,
            system="Answer in one word.",
            tools=[CHECK_TOOL],
            messages=[{"role": "user", "content": "Say OK."}],
        )
    except ModelError as e:
        return e.detail
    return None


# --- Messages API -> chat completions ----------------------------------------


def to_chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or tool["name"],
            "parameters": tool.get("input_schema") or {"type": "object"},
        },
    }


def to_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            chat.append({"role": message["role"], "content": content})
        elif message["role"] == "assistant":
            chat.append(_assistant(content))
        else:
            chat.extend(_user(content))
    return chat


def _assistant(blocks: list[Any]) -> dict[str, Any]:
    text = "\n".join(
        _get(b, "text") for b in blocks if _get(b, "type") == "text"
    ).strip()
    calls = [
        {
            "id": _get(b, "id"),
            "type": "function",
            "function": {
                "name": _get(b, "name"),
                "arguments": json.dumps(_get(b, "input")),
            },
        }
        for b in blocks
        if _get(b, "type") == "tool_use"
    ]
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return message


def _user(blocks: list[Any]) -> list[dict[str, Any]]:
    """Tool results become one `tool` message each; any text stays a user message."""
    chat: list[dict[str, Any]] = []
    text: list[str] = []
    for block in blocks:
        if _get(block, "type") == "tool_result":
            result = _get(block, "content")
            if not isinstance(result, str):
                result = "\n".join(_get(part, "text") for part in result or [])
            if _get(block, "is_error"):
                result = f"Error: {result}"
            chat.append(
                {
                    "role": "tool",
                    "tool_call_id": _get(block, "tool_use_id"),
                    "content": result,
                }
            )
        elif _get(block, "type") == "text":
            text.append(_get(block, "text"))
    if text:
        chat.append({"role": "user", "content": "\n".join(text)})
    return chat


def _get(block: Any, key: str) -> Any:
    """A field of a block, whether it's a dict we built or an SDK object."""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


# --- chat completions -> Messages API ----------------------------------------


def from_chat_choice(choice: Any) -> Message:
    message = choice.message
    content: list[TextBlock | ToolUseBlock] = []
    if message.content:
        content.append(TextBlock(type="text", text=message.content))
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            # The tool's own validation tells the model what was wrong.
            logger.warning(f"Unreadable arguments for {call.function.name}")
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        content.append(
            ToolUseBlock(
                type="tool_use", id=call.id, name=call.function.name, input=arguments
            )
        )
    # Some servers say "stop" even when they called tools.
    has_calls = any(b.type == "tool_use" for b in content)
    stop_reason = (
        "tool_use" if has_calls else STOP_REASONS.get(choice.finish_reason, "end_turn")
    )
    return Message.model_construct(
        id="",
        type="message",
        role="assistant",
        model="",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
    )
