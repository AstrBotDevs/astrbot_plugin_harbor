"""Exercise the headless evaluation entrypoint against a local model endpoint."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "custom_config,tracing,tool_rounds",
    [(False, False, 4), (True, False, 4), (False, True, 4), (False, False, 35)],
)
def test_local_evaluation_uses_real_tools_and_workspace(
    tmp_path, custom_config, tracing, tool_rounds
):
    """Run real AstrBot initialization, model requests, and local file tools."""
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("Write a marker file and read it back.", encoding="utf-8")
    output = tmp_path / "logs"
    shadow = tmp_path / "host packages"
    (shadow / "data").mkdir(parents=True)
    (shadow / "data/__init__.py").write_text(
        'raise RuntimeError("Host data package must not be imported")\n'
    )
    skills = tmp_path / "skills"
    (skills / "marker-skill").mkdir(parents=True)
    (skills / "marker-skill/SKILL.md").write_text(
        "---\nname: marker-skill\ndescription: Evaluate marker-file workflows.\n---\n"
        "Read a marker back after writing it.\n"
    )
    requests = []
    trace_requests = []
    if tracing:
        pytest.importorskip("phoenix.otel")
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/traces":
                trace_requests.append(ExportTraceServiceRequest.FromString(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.loads(body)
            requests.append(payload)
            turn = len(requests)
            message = {"role": "assistant", "content": "Verified the marker."}
            if turn <= tool_rounds:
                name = (
                    "astrbot_file_write_tool" if turn == 1 else "astrbot_file_read_tool"
                )
                arguments = {"path": "marker.txt"}
                if turn == 1:
                    arguments["content"] = "harbor-local-marker"
                elif turn == 3:
                    name = "astrbot_execute_shell"
                    arguments = {"command": "pwd"}
                elif turn == 4:
                    name = "astrbot_execute_python"
                    arguments = {
                        "code": (
                            "from pathlib import Path\n"
                            "print(Path.cwd())\n"
                            f"Path({str(tmp_path / 'outside.txt')!r}).write_text('full-access')"
                        )
                    }
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{turn}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            response = {
                "id": f"chatcmpl-{turn}",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls"
                        if turn <= tool_rounds
                        else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    extra_args = []
    original_config = None
    if custom_config:
        config_path = tmp_path / "config.json"
        original_config = json.dumps(
            {
                "provider": [
                    {
                        "id": "custom",
                        "type": "openai_chat_completion",
                        "provider_type": "chat_completion",
                        "enable": True,
                        "key": ["$ASTRBOT_EVAL_API_KEY"],
                        "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                        "model": "overridden-model",
                        "modalities": ["text", "tool_use"],
                    }
                ],
                "agent_runner": {
                    "runner_type": "local",
                    "config": {
                        "model": {"provider_id": "custom"},
                        "misc": {"max_steps": 1},
                    },
                },
                "provider_settings": {"computer_use_runtime": "none"},
            }
        )
        config_path.write_text(original_config)
        extra_args = ["--config", str(config_path)]
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "run_agent.py"),
                "--instruction-file",
                str(instruction),
                "--workspace",
                str(workspace),
                "--output-dir",
                str(output),
                "--model",
                "test-model",
                "--skills-dir",
                str(skills),
                *extra_args,
            ],
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("PHOENIX_")
                },
                "ASTRBOT_EVAL_API_KEY": "test-only-key",
                "PYTHONPATH": str(shadow),
                "ASTRBOT_EVAL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                **(
                    {
                        "PHOENIX_COLLECTOR_ENDPOINT": f"http://127.0.0.1:{server.server_port}/v1/traces",
                        "PHOENIX_PROJECT_NAME": "astrbot-test",
                    }
                    if tracing
                    else {}
                ),
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(requests) == tool_rounds + 1
    assert requests[0]["model"] == "test-model"
    assert (workspace / "marker.txt").read_text() == "harbor-local-marker"
    assert "harbor-local-marker" in json.dumps(requests[-1]["messages"])
    tools = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert "astrbot_execute_shell" in tools
    assert "astrbot_upload_file" not in tools
    assert "astrbot_download_file" not in tools
    assert json.dumps(str(workspace))[1:-1] in json.dumps(requests[0]["messages"])
    assert "marker-skill" in json.dumps(requests[0]["messages"])
    assert (tmp_path / "outside.txt").read_text() == "full-access"
    tool_results = [
        message for message in requests[-1]["messages"] if message["role"] == "tool"
    ]
    assert json.dumps(str(workspace))[1:-1] in json.dumps(tool_results)
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "completed"
    assert result["plugin_version"] == "v0.1.0"
    assert result["max_steps"] is None
    assert result["permissions"] == {
        "allow_execution": True,
        "allow_network": True,
        "filesystem_scope": "host",
    }
    assert result["stats"]["token_usage"] == {
        "input_other": 10 * (tool_rounds + 1),
        "input_cached": 0,
        "output": 5 * (tool_rounds + 1),
    }
    assert (output / "final.txt").read_text() == "Verified the marker."
    assert not (workspace / "data").exists()
    assert "test-only-key" not in (output / "result.json").read_text()
    if custom_config:
        assert config_path.read_text() == original_config
    if tracing:
        spans = [
            span
            for request in trace_requests
            for resource in request.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        assert len(spans) == 10  # One trial, five model calls, four real tools.
        root = next(span for span in spans if span.name == "astrbot.eval")
        assert root.trace_id.hex() == result["trace"]["trace_id"]
        assert root.span_id.hex() == result["trace"]["span_id"]
        assert all(span.trace_id == root.trace_id for span in spans)
        assert all(
            span.parent_span_id == root.span_id for span in spans if span != root
        )
        assert all(
            span.end_time_unix_nano >= span.start_time_unix_nano for span in spans
        )
        model_spans = [span for span in spans if span.name == "astrbot.llm"]
        assert len(model_spans) == 5
        for span in model_spans:
            attrs = {attr.key: attr.value for attr in span.attributes}
            assert attrs["llm.token_count.prompt"].int_value == 10
            assert attrs["llm.token_count.completion"].int_value == 5
        assert "harbor-local-marker" in str(trace_requests)
        assert "test-only-key" not in str(trace_requests)
        assert "Failed to detach context" not in completed.stderr
    else:
        assert "trace" not in result
