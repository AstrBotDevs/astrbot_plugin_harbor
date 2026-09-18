"""Harbor installed-agent adapter for AstrBot's unrestricted local runtime."""

import asyncio
import hashlib
import json
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from . import PLUGIN_FILES
from .reporting import studio_report


class AstrBotAgent(BaseInstalledAgent):
    """Install a source checkout and run one headless AstrBot per Harbor trial."""

    capabilities = AgentCapabilities(native_config=True)

    def __init__(
        self,
        *args,
        source_dir: str | None = None,
        workspace: str | None = None,
        provider_id: str | None = None,
        **kwargs,
    ) -> None:
        """Select the checkout and optional task workspace.

        Args:
            source_dir: Local AstrBot Git checkout, including uncommitted edits.
            workspace: Task-container cwd; defaults to the image's working directory.
            provider_id: Provider ID in the optional AstrBot config.
            args: Positional Harbor agent arguments.
            kwargs: Keyword Harbor agent arguments, including model and config.
        """
        super().__init__(*args, **kwargs)
        self.source_dir = (
            Path(source_dir).expanduser().resolve()
            if source_dir
            else Path(__file__).resolve().parents[3]
        )
        self.workspace = workspace
        self.provider_id = provider_id
        self.snapshot_sha256: str | None = None

    @staticmethod
    def name() -> str:
        """Return the agent's Harbor identifier.

        Returns:
            Agent name recorded in trial results.
        """
        return "astrbot"

    def get_version_command(self) -> str:
        """Return the installed AstrBot version command.

        Returns:
            Command executed inside the task environment.
        """
        return "/installed-agent/astrbot/.venv/bin/python -c " + shlex.quote(
            "from astrbot import __version__; print(__version__)"
        )

    async def install(self, environment: BaseEnvironment) -> None:
        """Upload the current checkout and install its locked dependencies.

        Args:
            environment: Harbor-owned task environment.

        Raises:
            RuntimeError: If the checkout or installation is invalid.
        """
        if not (self.source_dir / "pyproject.toml").is_file():
            raise RuntimeError("source_dir must point to an AstrBot checkout")
        frozen = "--frozen " if (self.source_dir / "uv.lock").is_file() else ""
        tracked_files = await asyncio.to_thread(
            subprocess.check_output,
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "astrbot",
                "pyproject.toml",
                "uv.lock",
                "README.md",
                "LICENSE",
                "scripts/hatch_build.py",
            ],
            cwd=self.source_dir,
        )
        files = tracked_files.decode().split("\0")
        files.append("uv.lock")  # AstrBot may keep its local lockfile untracked.
        # Package tracked source with current edits, never the user's data directory.
        with tempfile.TemporaryDirectory(prefix="astrbot-harbor-source-") as tmp:
            archive = Path(tmp) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name in sorted(set(files)):
                    if name and (self.source_dir / name).is_file():
                        source = self.source_dir / name
                        if source.is_symlink():
                            raise RuntimeError(f"Refusing source symlink: {name}")
                        bundle.add(source, arcname=name, recursive=False)
                for name in PLUGIN_FILES:
                    bundle.add(
                        Path(__file__).parent / name,
                        arcname=f"harbor_plugin/{name}",
                        recursive=False,
                    )
            self.snapshot_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            await environment.upload_file(archive, "/installed-agent/astrbot.tar.gz")
        await self.ensure_system_dependencies(
            environment, ("curl", "git", "build_tools")
        )
        await self.exec_as_root(
            environment,
            command=(
                "mkdir -p /installed-agent/astrbot && "
                "tar -xzf /installed-agent/astrbot.tar.gz -C /installed-agent/astrbot && "
                "curl -LsSf https://astral.sh/uv/install.sh | "
                "env UV_INSTALL_DIR=/installed-agent/bin sh && "
                "UV_PYTHON_INSTALL_DIR=/installed-agent/python "
                f"/installed-agent/bin/uv sync {frozen}--no-dev --python 3.12 "
                "--project /installed-agent/astrbot"
            ),
        )
        if self._get_env("PHOENIX_COLLECTOR_ENDPOINT"):
            await self.exec_as_root(
                environment,
                command=(
                    "/installed-agent/bin/uv pip install "
                    "--python /installed-agent/astrbot/.venv/bin/python "
                    "-r /installed-agent/astrbot/harbor_plugin/requirements-tracing.txt"
                ),
            )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Execute AstrBot locally inside the task's environment.

        Args:
            instruction: Task text supplied by Harbor.
            environment: Environment containing the task's files and services.
            context: Harbor result context, populated from the native usage log.

        Raises:
            ValueError: If no model is supplied or the task needs unsupported MCP.
        """
        if not self.model_name:
            raise ValueError("Specify --model provider/model-id")
        if self.mcp_servers:
            raise ValueError(
                "This initial local adapter does not yet connect task MCP servers"
            )
        model = self.model_name.split("/", 1)[-1]
        logs = str(self.environment_logs_dir)
        command = [
            "/installed-agent/astrbot/.venv/bin/python",
            "/installed-agent/astrbot/harbor_plugin/run_agent.py",
            "--instruction-file",
            "/installed-agent/instruction.txt",
            "--output-dir",
            logs,
            "--model",
            model,
            "--trial-id",
            self.logs_dir.parent.name,
        ]
        if self.workspace:
            command.extend(["--workspace", self.workspace])
        if self.provider_id:
            command.extend(["--provider-id", self.provider_id])
        if self.skills_dir:
            command.extend(["--skills-dir", self.skills_dir])
        with tempfile.TemporaryDirectory(prefix="astrbot-harbor-task-") as tmp:
            instruction_path = Path(tmp) / "instruction.txt"
            instruction_path.write_text(instruction, encoding="utf-8")
            await environment.upload_file(
                instruction_path, "/installed-agent/instruction.txt"
            )
            if self._config is not None:
                config_path = Path(tmp) / "config.json"
                if isinstance(self._config, Path):
                    config_path.write_bytes(self._config.read_bytes())
                else:
                    config_path.write_text(json.dumps(self._config), encoding="utf-8")
                await environment.upload_file(
                    config_path, "/installed-agent/config.json"
                )
                command.extend(["--config", "/installed-agent/config.json"])
        env = {}
        for key in (
            "ASTRBOT_EVAL_API_KEY",
            "ASTRBOT_EVAL_BASE_URL",
            "PHOENIX_COLLECTOR_ENDPOINT",
            "PHOENIX_PROJECT_NAME",
            "PHOENIX_API_KEY",
        ):
            if value := self._get_env(key):
                env[key] = value
        context.metadata = {
            **(context.metadata or {}),
            "source_archive_sha256": self.snapshot_sha256,
        }
        await self.exec_as_agent(
            environment,
            command=shlex.join(command)
            + f" > {shlex.quote(logs + '/console.log')} 2>&1",
            env=env,
            cwd=self.workspace,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Load usage after Harbor synchronizes logs, including timed-out trials.

        Args:
            context: Trial context to populate with AstrBot's native statistics.
        """
        result_path = self.logs_dir / "result.json"
        if not result_path.is_file():
            return
        result = json.loads(result_path.read_text(encoding="utf-8"))
        usage = result.get("stats", {}).get("token_usage")
        if usage is not None:
            context.n_input_tokens = usage["input_other"] + usage["input_cached"]
            context.n_cache_tokens = usage["input_cached"]
            context.n_output_tokens = usage["output"]
        context.metadata = {
            **(context.metadata or {}),
            "astrbot": result,
            "studio": studio_report(result),
            "source_archive_sha256": self.snapshot_sha256,
        }
