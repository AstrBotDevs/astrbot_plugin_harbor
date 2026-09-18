"""Run the AstrBot local agent once, without a dashboard or IM connection."""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path
from time import time
from uuid import uuid4

from reporting import write_result


async def run(args: argparse.Namespace) -> None:
    """Initialize an isolated AstrBot instance and execute one evaluation task.

    Args:
        args: Parsed CLI options, with absolute input and output paths.

    Raises:
        RuntimeError: If initialization or agent execution fails.
    """
    task = asyncio.current_task()
    with suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)

    # Import only after main() has selected the per-trial ASTRBOT_ROOT.
    from astrbot import __version__
    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.agent.message import dump_messages_with_checkpoints
    from astrbot.core.agent.runners.base import AgentState
    from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
    from astrbot.core.config.agent_runner import (
        normalize_agent_runner,
        resolve_context_compression_config,
    )
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.utils.astrbot_path import (
        get_astrbot_plugin_path,
        get_astrbot_skills_path,
    )

    if args.config:
        # AstrBotConfig fills missing fields and performs normal config migration.
        from astrbot.core.config import AstrBotConfig

        astrbot_config.clear()
        astrbot_config.update(AstrBotConfig(str(args.config)))
    else:
        if not os.environ.get("ASTRBOT_EVAL_API_KEY"):
            raise ValueError("ASTRBOT_EVAL_API_KEY is required without --config")
        astrbot_config["provider"] = [
            {
                "id": "eval",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "enable": True,
                "key": ["$ASTRBOT_EVAL_API_KEY"],
                "api_base": os.environ.get(
                    "ASTRBOT_EVAL_BASE_URL", "https://api.openai.com/v1"
                ),
                "model": args.model,
                "modalities": ["text", "image", "tool_use"],
            }
        ]
        astrbot_config["agent_runner"]["config"]["model"]["provider_id"] = "eval"

    if astrbot_config["agent_runner"]["runner_type"] != "local":
        raise ValueError("Evaluation requires an AstrBot local agent_runner config")
    astrbot_config["agent_runner"] = normalize_agent_runner(
        astrbot_config["agent_runner"]
    )
    if args.skills_dir:
        shutil.copytree(args.skills_dir, get_astrbot_skills_path(), dirs_exist_ok=True)

    # These overrides apply only to this disposable evaluation instance.
    astrbot_config["platform"] = []
    astrbot_config["admins_id"] = ["harbor"]
    astrbot_config["agent_runner"]["runner_type"] = "local"
    settings = astrbot_config["provider_settings"]
    settings["computer_use_runtime"] = "local"
    settings["computer_use_local_permissions"] = {
        role: {
            "allow_execution": True,
            "allow_network": True,
            "filesystem_scope": "host",
        }
        for role in ("admin", "member")
    }
    runner_config = astrbot_config["agent_runner"]["config"]
    provider_id = args.provider_id or runner_config["model"]["provider_id"]
    if not provider_id:
        raise ValueError("Set --provider-id or agent_runner.config.model.provider_id")
    runner_config["model"]["fallback_provider_ids"] = []
    runner_config["misc"]["max_steps"] = None

    class EvaluationEvent(AstrMessageEvent):
        async def send(self, message) -> None:
            """Record a platform reply in the trial's native event log.

            Args:
                message: Outgoing AstrBot message chain.
            """
            event_log.write(
                json.dumps(
                    {
                        "timestamp": time(),
                        "type": "send",
                        "chain_type": message.type,
                        "content": [part.toDict() for part in message.chain],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            event_log.flush()

    lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
    initialized = False
    runner = None
    result = {
        "astrbot_version": __version__,
        "model": args.model,
        "workspace": str(args.workspace),
        "runtime": "local",
        "permissions": settings["computer_use_local_permissions"]["admin"],
        "max_steps": None,
        "status": "initializing",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock = Path(__import__("astrbot").__file__).resolve().parents[1] / "uv.lock"
    if lock.is_file():
        shutil.copy2(lock, args.output_dir / "dependencies.lock")
    result_path = args.output_dir / "result.json"
    with (
        (args.output_dir / "events.jsonl").open("w", encoding="utf-8") as event_log,
        ExitStack() as tracing,
    ):
        try:
            await lifecycle.initialize()
            initialized = True
            context = lifecycle.star_context
            registered = context.get_registered_star("astrbot_plugin_harbor")
            if registered is None or registered.star_cls is None:
                raise RuntimeError(
                    "Harbor plugin failed to load in the evaluation instance"
                )
            plugin = registered.star_cls
            loaded_from = Path(sys.modules[type(plugin).__module__].__file__).resolve()
            if not loaded_from.is_relative_to(
                Path(get_astrbot_plugin_path()).resolve()
            ):
                raise RuntimeError(
                    "Harbor plugin was imported outside the isolated runtime"
                )
            result["plugin_version"] = registered.version
            provider = context.get_provider_by_id(provider_id)
            if provider is None:
                raise RuntimeError(f"Evaluation provider {provider_id!r} did not load")
            provider.set_model(args.model)

            # Reuse the existing ChatUI custom-workspace mapping for a headless
            # session. A fixed display name prevents an extra title-generation call.
            session = await db_helper.create_platform_session(
                creator="harbor", display_name="Harbor evaluation"
            )
            project = await db_helper.create_chatui_project(
                creator="harbor",
                title="Harbor evaluation",
                workspace_type="custom",
                workspace_path=str(args.workspace),
            )
            await db_helper.add_session_to_project(
                session.session_id, project.project_id
            )
            instruction = args.instruction_file.read_text(encoding="utf-8")
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id = "astrbot"
            message.message_id = session.session_id
            message.sender = MessageMember(user_id="harbor", nickname="Harbor")
            message.message = [Plain(text=instruction)]
            message.message_str = instruction
            event = EvaluationEvent(
                instruction,
                message,
                PlatformMetadata(
                    name="webchat",
                    id="webchat",
                    description="Headless Harbor evaluation",
                    support_proactive_message=False,
                ),
                f"webchat!harbor!{session.session_id}",
            )
            event.role = "admin"
            event.plugins_name = []
            conversation_id = await context.conversation_manager.new_conversation(
                event.unified_msg_origin,
                persona_id=runner_config["persona"]["persona_id"],
            )
            conversation = await context.conversation_manager.get_conversation(
                event.unified_msg_origin, conversation_id
            )
            misc = runner_config["misc"]
            built = await build_main_agent(
                event=event,
                plugin_context=context,
                provider=provider,
                req=ProviderRequest(prompt=instruction, conversation=conversation),
                config=MainAgentBuildConfig(
                    tool_call_timeout=misc["tool_call_timeout"],
                    tool_schema_mode=misc["tool_schema_mode"],
                    streaming_response=False,
                    computer_use_runtime="local",
                    provider_settings=settings,
                    add_cron_tools=False,
                    llm_safety_mode=runner_config["persona"]["safety_mode"],
                    safety_mode_strategy=runner_config["persona"][
                        "safety_mode_strategy"
                    ],
                    request_max_retries=runner_config["model"]["request_max_retries"],
                    **resolve_context_compression_config(runner_config["compression"]),
                ),
            )
            if built is None:
                raise RuntimeError("AstrBot did not create an agent for this task")
            runner = built.agent_runner
            result["status"] = "running"
            result["tools"] = built.provider_request.func_tool.names()
            result["agent_config"] = runner_config
            result["max_context_tokens"] = provider.provider_config.get(
                "max_context_tokens"
            )
            (args.output_dir / "system_prompt.txt").write_text(
                built.provider_request.system_prompt or "", encoding="utf-8"
            )
            tracing.enter_context(
                plugin.trace_evaluation(runner, result, instruction, args.trial_id)
            )
            write_result(result, result_path)
            async for response in plugin.responses(runner):
                event_log.write(
                    json.dumps(
                        {
                            "timestamp": time(),
                            "type": response.type,
                            "chain_type": response.data["chain"].type,
                            "content": [
                                part.toDict() for part in response.data["chain"].chain
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                event_log.flush()
                # Persist usage while running so a hard timeout leaves useful data.
                result["stats"] = runner.stats.to_dict()
                write_result(result, result_path)
            if runner.state != AgentState.DONE:
                raise RuntimeError(f"AstrBot ended in state {runner.state.name}")
            final_response = runner.get_final_llm_resp()
            (args.output_dir / "final.txt").write_text(
                final_response.completion_text if final_response else "",
                encoding="utf-8",
            )
            result["status"] = "completed"
        except BaseException as exc:
            result["status"] = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            )
            result["error_type"] = type(exc).__name__
            raise
        finally:
            if runner is not None:
                result["stats"] = runner.stats.to_dict()
                (args.output_dir / "messages.json").write_text(
                    json.dumps(
                        dump_messages_with_checkpoints(runner.run_context.messages),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            write_result(result, result_path)
            if initialized:
                await lifecycle.stop()
            await db_helper.engine.dispose()


def main() -> None:
    """Parse the one-shot CLI and select runtime storage before importing AstrBot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruction-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--model", required=True, help="Model ID accepted by the provider"
    )
    parser.add_argument(
        "--config", type=Path, help="Optional AstrBot configuration JSON"
    )
    parser.add_argument("--provider-id", help="Provider ID selected from --config")
    parser.add_argument("--trial-id", default=uuid4().hex, help="Trace correlation ID")
    parser.add_argument(
        "--skills-dir", type=Path, help="Task-provided skills directory"
    )
    args = parser.parse_args()
    for name in ("instruction_file", "workspace", "output_dir", "config", "skills_dir"):
        if value := getattr(args, name):
            setattr(args, name, value.expanduser().resolve())
    if not args.workspace.is_dir():
        parser.error("--workspace must be an existing directory")
    # The adapter places this package immediately under the AstrBot source root.
    # For direct local runs, explicitly select the unmodified checkout.
    source = Path(
        os.environ.get("ASTRBOT_SOURCE_DIR", Path(__file__).resolve().parents[1])
    )
    if not (source / "astrbot").is_dir():
        source = Path(__file__).resolve().parents[3]
    if not (source / "astrbot").is_dir():
        parser.error("Set ASTRBOT_SOURCE_DIR to the AstrBot checkout")
    sys.path.insert(0, str(source))
    with tempfile.TemporaryDirectory(prefix="astrbot-eval-") as runtime_dir:
        os.environ["ASTRBOT_ROOT"] = runtime_dir
        # Load only this plugin into the isolated instance through AstrBot's loader.
        from_file = Path(__file__).resolve().parent
        plugin_dir = Path(runtime_dir) / "data/plugins/astrbot_plugin_harbor"
        plugin_dir.mkdir(parents=True)
        # AstrBot imports data.plugins by module name, independently of ASTRBOT_ROOT.
        # Explicit packages prevent a host data package from shadowing this instance.
        (plugin_dir.parent / "__init__.py").touch()
        (plugin_dir.parent.parent / "__init__.py").touch()
        sys.path.insert(0, runtime_dir)
        # The manifest has no AstrBot imports, so root selection remains early.
        from manifest import PLUGIN_FILES

        for name in PLUGIN_FILES:
            shutil.copy2(from_file / name, plugin_dir / name)
        # Copy a supplied config before AstrBotConfig performs migrations or saves.
        if args.config:
            config_copy = Path(runtime_dir) / "eval_config.json"
            config_copy.write_bytes(args.config.read_bytes())
            args.config = config_copy
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
