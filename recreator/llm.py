"""
llm.py - The small engine every agent runs on.

An "agent" here is just a loop:
  1. Send Claude the instructions, the task and a list of tools.
  2. Claude replies, usually asking to use one or more tools.
  3. We run those tools and send the results back.
  4. Repeat until Claude calls the special "submit" tool with its final
     answer, or we hit the turn limit (a cost and runaway guard).
"""
import json
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
MAX_TOOL_OUTPUT = 20_000  # characters; long outputs are cut so they don't flood the context


@dataclass
class Tool:
    """A function the agent may call, plus the JSON schema describing its inputs."""
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cut {len(text) - limit} characters]"


def _to_tool_content(result: Any) -> str | list:
    """
    Tools may return plain text, a dict/list (sent as JSON), or a list of
    content blocks (used for screenshots, which go to Claude as images).
    """
    if isinstance(result, list) and result and isinstance(result[0], dict) and "type" in result[0]:
        return result
    if isinstance(result, (dict, list)):
        return truncate(json.dumps(result, indent=1, default=str))
    return truncate(str(result))


def run_agent(
    *,
    client: anthropic.Anthropic,
    model: str,
    system: str,
    task: str,
    tools: list[Tool],
    submit_name: str,
    submit_description: str,
    result_model: type[T],
    max_turns: int,
    log: Callable[[str], None] = print,
) -> T:
    submit_tool = {
        "name": submit_name,
        "description": submit_description,
        "input_schema": result_model.model_json_schema(),
    }
    api_tools = [t.spec() for t in tools] + [submit_tool]
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": task}]

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=model, max_tokens=8000, system=system, tools=api_tools, messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        # Claude stopped without using a tool: nudge it to keep going or submit.
        if response.stop_reason != "tool_use":
            messages.append({"role": "user", "content": f"Continue. When finished, call {submit_name}."})
            continue

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # The final answer: validate it. If valid we're done; if not, explain and let it retry.
            if block.name == submit_name:
                try:
                    final = result_model.model_validate(block.input)
                    log(f"  [turn {turn}] submitted {submit_name}")
                    return final
                except ValidationError as err:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": f"That report doesn't match the schema:\n{err}\nFix it and submit again."})
                    continue

            # A normal tool call. Errors go back to Claude as text so it can adapt.
            tool = by_name.get(block.name)
            log(f"  [turn {turn}] {block.name} {json.dumps(block.input, default=str)[:160]}")
            try:
                if tool is None:
                    raise ValueError(f"No tool named {block.name}")
                content, is_error = _to_tool_content(tool.fn(**block.input)), False
            except Exception as err:  # noqa: BLE001 - we want every failure reported back
                content, is_error = f"Tool error: {type(err).__name__}: {err}", True
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})

        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Agent did not submit within {max_turns} turns")


def structured_call(*, client, model, system, task, submit_name, result_model: type[T]) -> T:
    """One-shot version for steps that need no tools (like triage): force the submit tool."""
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=system,
        tools=[{"name": submit_name, "description": "Submit your result.",
                "input_schema": result_model.model_json_schema()}],
        tool_choice={"type": "tool", "name": submit_name},
        messages=[{"role": "user", "content": task}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    return result_model.model_validate(block.input)
