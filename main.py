"""AstrBot plugin providing isolated Harbor execution and optional Phoenix tracing."""

import os
from contextlib import nullcontext

from astrbot.api.star import Star


class HarborPlugin(Star):
    """Provide evaluation services without changing normal conversations."""

    def trace_evaluation(self, runner, result, instruction, trial_id):
        """Create tracing only when explicitly enabled for a disposable trial.

        Args:
            runner: Native AstrBot runner owned by this evaluation process.
            result: Mutable result receiving trace identifiers.
            instruction: Harbor task instruction.
            trial_id: Correlation identifier for this execution.

        Returns:
            A context manager for Phoenix tracing, or an inert context manager.
        """
        if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
            return nullcontext()
        from .tracing import trace_agent

        return trace_agent(runner, result, instruction, trial_id)

    async def responses(self, runner):
        """Drive the native runner without requiring a patched step limit API.

        Harbor owns the wall-clock timeout and cancellation of this process.

        Args:
            runner: Initialized native AstrBot runner.

        Yields:
            Native responses, with tools and runner state preserved.
        """
        while not runner.done():
            async for response in runner.step():
                yield response
