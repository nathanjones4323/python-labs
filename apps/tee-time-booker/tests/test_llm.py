"""The OpenAI-compatible model: Messages API shapes in, chat completions out,
and the response back as Anthropic blocks, without a server."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")

import httpx  # noqa: E402
from tee_time_booker.bot.llm import ModelError, OpenAICompatibleModel  # noqa: E402

TOOLS = [
    {
        "name": "list_plans",
        "description": "Plans",
        "input_schema": {"type": "object", "properties": {}},
    }
]


class FakeOpenAI:
    """Answers each `chat.completions.create` with the next scripted choice."""

    def __init__(self, *choices):
        self.choices = list(choices)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request):
        self.requests.append(request)
        choice = self.choices.pop(0)
        if isinstance(choice, Exception):
            raise choice
        return SimpleNamespace(choices=[choice])


def _choice(content=None, calls=(), finish_reason="stop"):
    tool_calls = [
        SimpleNamespace(
            id=id_, function=SimpleNamespace(name=name, arguments=arguments)
        )
        for id_, name, arguments in calls
    ]
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None),
        finish_reason=finish_reason,
    )


def _create(model, messages):
    return asyncio.run(
        model.messages.create(
            model="m",
            max_tokens=100,
            system="Be brief.",
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
        )
    )


def test_a_tool_conversation_round_trips():
    client = FakeOpenAI(
        _choice(
            "Looking.",
            [("c1", "list_plans", "{}"), ("c2", "get_plan", '{"plan_id": "p"}')],
            "tool_calls",
        ),
        _choice("Two plans.", finish_reason="stop"),
    )
    model = OpenAICompatibleModel(client)
    first = _create(model, [{"role": "user", "content": "my plans?"}])

    assert first.stop_reason == "tool_use"
    text, *calls = first.content
    assert text.text == "Looking."
    assert [(c.id, c.name, c.input) for c in calls] == [
        ("c1", "list_plans", {}),
        ("c2", "get_plan", {"plan_id": "p"}),
    ]
    request = client.requests[0]
    assert request["messages"][0] == {"role": "system", "content": "Be brief."}
    assert request["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert "output_config" not in request and "cache_control" not in request

    # The agent appends the response's own blocks and its tool results.
    history = [
        {"role": "user", "content": "my plans?"},
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "[]"},
                {
                    "type": "tool_result",
                    "tool_use_id": "c2",
                    "content": "not found",
                    "is_error": True,
                },
            ],
        },
    ]
    second = _create(model, history)
    assert second.stop_reason == "end_turn"
    assert second.content[0].text == "Two plans."

    sent = client.requests[1]["messages"][1:]
    assert sent[0] == {"role": "user", "content": "my plans?"}
    assert sent[1]["role"] == "assistant" and sent[1]["content"] == "Looking."
    assert [json.loads(c["function"]["arguments"]) for c in sent[1]["tool_calls"]] == [
        {},
        {"plan_id": "p"},
    ]
    assert sent[2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": "[]"},
        {"role": "tool", "tool_call_id": "c2", "content": "Error: not found"},
    ]


def test_unreadable_arguments_still_reach_the_tool_as_empty():
    client = FakeOpenAI(_choice(calls=[("c1", "list_plans", "{not json")]))
    response = _create(
        OpenAICompatibleModel(client), [{"role": "user", "content": "x"}]
    )
    # "stop" with tool calls in it is still a tool round.
    assert response.stop_reason == "tool_use"
    assert response.content[0].input == {}


def test_a_cut_off_answer_says_so():
    client = FakeOpenAI(_choice("Sat Oct", finish_reason="length"))
    response = _create(
        OpenAICompatibleModel(client), [{"role": "user", "content": "x"}]
    )
    assert response.stop_reason == "max_tokens"


def test_an_unreachable_server_is_a_model_error():
    error = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    model = OpenAICompatibleModel(FakeOpenAI(error))
    with pytest.raises(ModelError, match="APIConnectionError"):
        _create(model, [{"role": "user", "content": "x"}])


def test_a_refusal_carries_the_providers_own_words():
    # As OpenRouter sends an upstream provider's error.
    body = {
        "error": {
            "message": "Provider returned error",
            "code": 429,
            "metadata": {"raw": "gemma is temporarily rate-limited upstream."},
        }
    }
    request = httpx.Request("POST", "http://x")
    error = openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=request, json=body),
        body=body,
    )
    model = OpenAICompatibleModel(FakeOpenAI(error))
    with pytest.raises(ModelError) as raised:
        _create(model, [{"role": "user", "content": "x"}])
    # Short for the chat, specific for setup.
    assert str(raised.value) == "RateLimitError"
    assert raised.value.detail == "gemma is temporarily rate-limited upstream."
