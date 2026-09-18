"""Check span lifetimes for failed and interrupted native agent operations."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("phoenix.otel")

from astrbot_plugin_harbor.tracing import trace_agent
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["exception", "cancelled", "tool_error", "tool_closed"]
)
async def test_traces_close_on_errors_and_cross_task_generator_yields(
    monkeypatch, failure
):
    """Keep spans attached to their trial and restore the native implementations."""
    import astrbot_plugin_harbor.tracing as tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "register", lambda **kwargs: provider)
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://unused/v1/traces")

    async def chat(**kwargs):
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise ValueError("model unavailable")

    closed = []

    class Executor:
        async def execute(self, tool, run_context, **kwargs):
            try:
                yield SimpleNamespace(isError=failure == "tool_error")
                yield "second result"
            finally:
                closed.append(True)

    original_executor = Executor()
    runner = SimpleNamespace(
        provider=SimpleNamespace(text_chat=chat, get_model=lambda: "test-model"),
        tool_executor=original_executor,
        get_final_llm_resp=lambda: None,
    )
    result = {
        "model": "test-model",
        "astrbot_version": "test",
        "workspace": "/task",
        "status": "running",
    }
    try:
        with trace_agent(runner, result, "test instruction", "trial-test"):
            if failure in ("exception", "cancelled"):
                await runner.provider.text_chat(contexts=[])
            else:
                executor = runner.tool_executor.execute(
                    SimpleNamespace(name="test-tool"), None, value=1
                )
                await asyncio.create_task(anext(executor))
                if failure == "tool_closed":
                    await asyncio.create_task(executor.aclose())
                else:
                    assert await asyncio.create_task(anext(executor)) == "second result"
                    with pytest.raises(StopAsyncIteration):
                        await asyncio.create_task(anext(executor))
                result["status"] = "completed"
    except (ValueError, asyncio.CancelledError):
        pass
    assert runner.provider.text_chat is chat
    assert runner.tool_executor is original_executor
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    child, root = spans
    assert child.parent.span_id == root.context.span_id
    assert child.status.status_code == StatusCode.ERROR
    assert child.end_time <= root.end_time
    if failure in ("exception", "cancelled"):
        assert root.status.status_code == StatusCode.ERROR
    else:
        assert closed == [True]
