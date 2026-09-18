"""Optional Phoenix tracing for the headless evaluation process only."""

import json
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanLimits
from opentelemetry.trace import StatusCode
from phoenix.otel import register


@contextmanager
def trace_agent(runner, result: dict, instruction: str, trial_id: str):
    """Trace one trial and temporarily wrap its provider and tool executor.

    Args:
        runner: Initialized non-streaming AstrBot tool-loop runner.
        result: Mutable trial result that receives the trace identifiers.
        instruction: Original task instruction.
        trial_id: Harbor trial name, or a unique ID for a direct run.

    Yields:
        The root agent span. Export failures do not change the task result.
    """
    project = os.environ.get("PHOENIX_PROJECT_NAME", "astrbot-eval")
    provider = register(
        endpoint=os.environ["PHOENIX_COLLECTOR_ENDPOINT"],
        protocol="http/protobuf",
        project_name=project,
        batch=True,
        set_global_tracer_provider=False,
        verbose=False,
        # Long agent conversations exceed the SDK's default 128 attributes.
        span_limits=SpanLimits(max_attributes=10000),
    )
    tracer = provider.get_tracer("astrbot.harbor")
    chat_provider = runner.provider
    original_chat = chat_provider.text_chat
    original_executor = runner.tool_executor
    active_tools = {}

    async def traced_chat(*args, **kwargs):
        """Record a native provider call without changing its request or response.

        Args:
            args: Positional provider arguments.
            kwargs: Native provider request, including contexts and tool schemas.

        Returns:
            The original AstrBot LLMResponse.
        """
        messages = [
            message.model_dump(mode="json")
            if hasattr(message, "model_dump")
            else message
            for message in kwargs.get("contexts", [])
        ]
        # Include legacy prompt arguments used by auxiliary calls on this provider.
        inputs = {"messages": messages}
        for key in ("prompt", "system_prompt"):
            if kwargs.get(key):
                inputs[key] = kwargs[key]
        attributes = {
            "openinference.span.kind": "LLM",
            "llm.model_name": kwargs.get("model") or chat_provider.get_model(),
            "input.value": json.dumps(inputs, ensure_ascii=False, default=str),
            "input.mime_type": "application/json",
            "session.id": trial_id,
        }
        for index, message in enumerate(messages):
            prefix = f"llm.input_messages.{index}.message"
            attributes[f"{prefix}.role"] = message.get("role", "user")
            content = message.get("content")
            attributes[f"{prefix}.content"] = (
                content if isinstance(content, str) else json.dumps(content)
            )
        if tools := kwargs.get("func_tool"):
            for index, tool in enumerate(tools.openai_schema()):
                attributes[f"llm.tools.{index}.tool.json_schema"] = json.dumps(tool)
        with tracer.start_as_current_span("astrbot.llm", attributes=attributes) as span:
            try:
                response = await original_chat(*args, **kwargs)
            except BaseException as exc:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                raise
            output = {
                "role": "assistant",
                "content": response.completion_text,
                "reasoning_content": response.reasoning_content,
                "tool_calls": [
                    {"id": call_id, "name": name, "arguments": arguments}
                    for call_id, name, arguments in zip(
                        response.tools_call_ids,
                        response.tools_call_name,
                        response.tools_call_args,
                    )
                ],
            }
            span.set_attribute("output.value", json.dumps(output, ensure_ascii=False))
            span.set_attribute("output.mime_type", "application/json")
            span.set_attribute("llm.output_messages.0.message.role", "assistant")
            span.set_attribute(
                "llm.output_messages.0.message.content", response.completion_text or ""
            )
            for index, call in enumerate(output["tool_calls"]):
                prefix = f"llm.output_messages.0.message.tool_calls.{index}.tool_call"
                span.set_attribute(f"{prefix}.id", call["id"])
                span.set_attribute(f"{prefix}.function.name", call["name"])
                span.set_attribute(
                    f"{prefix}.function.arguments", json.dumps(call["arguments"])
                )
            if response.usage is not None:
                span.set_attributes(
                    {
                        "llm.token_count.prompt": response.usage.input,
                        "llm.token_count.completion": response.usage.output,
                        "llm.token_count.total": response.usage.total,
                        "llm.token_count.prompt_details.cache_read": response.usage.input_cached,
                    }
                )
            if response.role == "err":
                span.set_status(StatusCode.ERROR, response.completion_text)
            return response

    class TracedExecutor:
        async def execute(self, tool, run_context, **tool_args):
            """Preserve executor yields while timing execution across asyncio tasks.

            Args:
                tool: The selected native AstrBot tool.
                run_context: Existing agent context.
                tool_args: Validated tool arguments.

            Yields:
                Unmodified results from the original executor.
            """
            encoded_args = json.dumps(tool_args, ensure_ascii=False, default=str)
            span = tracer.start_span(
                tool.name,
                attributes={
                    "openinference.span.kind": "TOOL",
                    "tool.name": tool.name,
                    "tool.parameters": encoded_args,
                    "input.value": encoded_args,
                    "input.mime_type": "application/json",
                    "session.id": trial_id,
                },
            )
            span_id = span.get_span_context().span_id
            active_tools[span_id] = span
            executor = original_executor.execute(tool, run_context, **tool_args)
            outputs = []
            try:
                while True:
                    # AstrBot awaits each anext() in a separate task. Never keep
                    # a ContextVar token attached across an async-generator yield.
                    with trace.use_span(span, end_on_exit=False):
                        try:
                            item = await anext(executor)
                        except StopAsyncIteration:
                            break
                    outputs.append(
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item
                    )
                    span.set_attribute(
                        "output.value",
                        json.dumps(outputs, ensure_ascii=False, default=str),
                    )
                    span.set_attribute("output.mime_type", "application/json")
                    if getattr(item, "isError", False):
                        span.set_status(StatusCode.ERROR, "Tool returned an error")
                    yield item
            except BaseException as exc:
                if span.is_recording():
                    span.set_status(StatusCode.ERROR, type(exc).__name__)
                    if isinstance(exc, Exception):
                        span.record_exception(exc)
                raise
            finally:
                try:
                    await executor.aclose()
                finally:
                    if active_tools.pop(span_id, None) is not None:
                        span.end()

    try:
        with tracer.start_as_current_span(
            "astrbot.eval",
            attributes={
                "openinference.span.kind": "AGENT",
                "input.value": instruction,
                "session.id": trial_id,
                "metadata": json.dumps(
                    {
                        "trial_id": trial_id,
                        "model": result["model"],
                        "astrbot_version": result["astrbot_version"],
                        "workspace": result["workspace"],
                    }
                ),
            },
        ) as span:
            span_context = span.get_span_context()
            result["trace"] = {
                "project": project,
                "trace_id": format(span_context.trace_id, "032x"),
                "span_id": format(span_context.span_id, "016x"),
                "trial_id": trial_id,
            }
            chat_provider.text_chat = traced_chat
            runner.tool_executor = TracedExecutor()
            try:
                yield span
            except BaseException as exc:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                raise
            finally:
                chat_provider.text_chat = original_chat
                runner.tool_executor = original_executor
                for unfinished in active_tools.values():
                    unfinished.set_status(
                        StatusCode.ERROR, "Trial ended during tool execution"
                    )
                    unfinished.end()
                active_tools.clear()
                span.set_attribute("astrbot.status", result["status"])
                if result["status"] != "completed":
                    span.set_status(StatusCode.ERROR, result["status"])
                final = runner.get_final_llm_resp()
                if final:
                    span.set_attribute("output.value", final.completion_text or "")
    finally:
        provider.force_flush(timeout_millis=5000)
        provider.shutdown()
