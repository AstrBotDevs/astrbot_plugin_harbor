"""Validate the optional Harbor integration against its actual public API."""

import json
import shlex
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor")

from astrbot_plugin_harbor.agent import AstrBotAgent
from harbor.models.agent.context import AgentContext


@pytest.mark.asyncio
@pytest.mark.parametrize("tracing", [False, True])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("local_runtime", [False, True])
@pytest.mark.parametrize("distro", ["ubuntu", "debian"])
async def test_source_snapshot_includes_edits_but_not_runtime_data(
    tmp_path, monkeypatch, tracing, locked, local_runtime, distro
):
    """Package current tracked source without the user's private runtime files."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    for name in ("astrbot/agent.py", "uv.lock", "pyproject.toml"):
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    (checkout / "astrbot/agent.py").write_text("uncommitted change", encoding="utf-8")
    # The original checkout has neither an evals directory nor a tracked lockfile.
    subprocess.run(
        ["git", "rm", "--cached", "uv.lock"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    (checkout / "data").mkdir()
    (checkout / "data/secret.json").write_text("private", encoding="utf-8")
    if not locked:
        (checkout / "uv.lock").unlink()
    contents = {}
    runtime = tmp_path / "runtime.tar.gz"
    runtime.write_bytes(b"operator-managed-toolchain")
    for name in ("UBUNTU", "DEBIAN"):
        monkeypatch.delenv(f"ASTRBOT_HARBOR_{name}_MIRROR", raising=False)
    if local_runtime:
        monkeypatch.setenv("ASTRBOT_HARBOR_RUNTIME_ARCHIVE", str(runtime))
        monkeypatch.setenv(
            f"ASTRBOT_HARBOR_{distro.upper()}_MIRROR", f"https://example.org/{distro}"
        )
    else:
        monkeypatch.delenv("ASTRBOT_HARBOR_RUNTIME_ARCHIVE", raising=False)
        monkeypatch.delenv("ASTRBOT_HARBOR_UBUNTU_MIRROR", raising=False)
    monkeypatch.setenv("ASTRBOT_HARBOR_PYPI_INDEX", "https://example.org/simple")
    if tracing:
        monkeypatch.setenv(
            "PHOENIX_COLLECTOR_ENDPOINT", "http://collector:6006/v1/traces"
        )
    else:
        monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)

    async def upload(source_path, target_path):
        if target_path == "/installed-agent/runtime.tar.gz":
            assert Path(source_path).read_bytes() == b"operator-managed-toolchain"
            contents["runtime_uploaded"] = True
            return
        with tarfile.open(source_path) as archive:
            for member in archive.getmembers():
                contents[member.name] = archive.extractfile(member).read().decode()

    environment = AsyncMock()
    environment.upload_file.side_effect = upload
    agent = AstrBotAgent(logs_dir=tmp_path / "logs", source_dir=str(checkout))
    agent.ensure_system_dependencies = AsyncMock()
    agent.exec_as_root = AsyncMock()
    await agent.install(environment)
    assert contents["astrbot/agent.py"] == "uncommitted change"
    assert "Harbor plugin failed to load" in contents["harbor_plugin/run_agent.py"]
    assert "class HarborPlugin" in contents["harbor_plugin/main.py"]
    assert "def trace_agent" in contents["harbor_plugin/tracing.py"]
    assert "arize-phoenix-otel" in contents["harbor_plugin/requirements-tracing.txt"]
    assert ("uv.lock" in contents) == locked
    calls = agent.exec_as_root.call_args_list
    if local_runtime:
        command = calls[0].kwargs["command"]
        assert f"https://example.org/{distro}" in command
        replacement = shlex.split(command.split("sed -E -i ", 1)[1])[0]
        source = (
            "URIs: http://deb.debian.org/debian https://security.debian.org/debian-security\n"
            if distro == "debian"
            else "deb http://archive.ubuntu.com/ubuntu noble main\n"
        )
        result = subprocess.run(
            ["sed", "-E", replacement],
            input=source,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert f"https://example.org/{distro}" in result
        if distro == "debian":
            assert "https://example.org/debian-security" in result
        calls = calls[1:]
    assert ("--frozen" in calls[2].kwargs["command"]) == locked
    assert not any(name.startswith("data/") for name in contents)
    assert agent.snapshot_sha256
    assert agent.exec_as_root.await_count == (4 if tracing else 3) + local_runtime
    assert contents.get("runtime_uploaded", False) == local_runtime
    bootstrap = calls[1].kwargs["command"]
    assert ("curl" not in bootstrap) == local_runtime
    sync = calls[2].kwargs
    assert sync["env"]["UV_DEFAULT_INDEX"] == "https://example.org/simple"
    assert "setup.log" in sync["command"]
    if local_runtime:
        assert sync["env"]["UV_PYTHON_DOWNLOADS"] == "never"
        assert "--python /installed-agent/python/bin/python3.12" in sync["command"]
    if tracing:
        assert (
            "requirements-tracing.txt" in agent.exec_as_root.call_args.kwargs["command"]
        )


@pytest.mark.asyncio
async def test_run_preserves_instruction_and_quotes_paths(tmp_path, monkeypatch):
    """Pass task text through a file and quote every shell argument."""
    monkeypatch.setenv("ASTRBOT_EVAL_API_KEY", "test-only-key")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://collector:6006/v1/traces")
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "astrbot-test")
    monkeypatch.setenv("PHOENIX_API_KEY", "trace-test-key")
    agent = AstrBotAgent(
        logs_dir=tmp_path / "logs",
        model_name="openai/vendor/model; echo wrong",
        workspace="/task with spaces/$(echo wrong)",
    )
    agent.exec_as_agent = AsyncMock()
    environment = AsyncMock()
    uploads = {}

    async def upload(source_path, target_path):
        uploads[target_path] = Path(source_path).read_text()

    environment.upload_file.side_effect = upload
    instruction = "Handle 'quotes', `backticks`, $(substitution) and\nnewlines."
    await agent.run(instruction, environment, AgentContext())
    assert uploads["/installed-agent/instruction.txt"] == instruction
    call = agent.exec_as_agent.call_args.kwargs
    argv = shlex.split(call["command"].split(" > ", 1)[0])
    assert argv[argv.index("--workspace") + 1] == agent.workspace
    assert argv[argv.index("--model") + 1] == "vendor/model; echo wrong"
    assert call["env"]["ASTRBOT_EVAL_API_KEY"] == "test-only-key"
    assert "test-only-key" not in call["command"]
    assert (
        call["env"]["PHOENIX_COLLECTOR_ENDPOINT"] == "http://collector:6006/v1/traces"
    )
    assert call["env"]["PHOENIX_PROJECT_NAME"] == "astrbot-test"
    assert call["env"]["PHOENIX_API_KEY"] == "trace-test-key"
    assert "trace-test-key" not in call["command"]
    assert argv[argv.index("--trial-id") + 1] == tmp_path.name


def test_usage_survives_timeout_and_missing_usage_stays_unknown(tmp_path):
    """Recover partial counters without reporting missing counters as zero."""
    agent = AstrBotAgent(logs_dir=tmp_path)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"status": "error"}))
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.n_input_tokens is None
    result_path.write_text(
        json.dumps(
            {
                "status": "running",
                "stats": {
                    "token_usage": {"input_other": 20, "input_cached": 10, "output": 5}
                },
            }
        )
    )
    agent.populate_context_post_run(context)
    assert context.n_input_tokens == 30
    assert context.n_cache_tokens == 10
    assert context.n_output_tokens == 5
    assert context.metadata["astrbot"]["status"] == "running"
