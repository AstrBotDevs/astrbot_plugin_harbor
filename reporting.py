"""Optional, agent-neutral Harbor Studio telemetry contract."""

import json


def studio_report(result):
    """Translate native usage into versioned, agent-neutral telemetry.

    Args:
        result: Native evaluation result.

    Returns:
        Version-one telemetry document, with unknown usage preserved as null.
    """
    usage = result.get("stats", {}).get("token_usage")
    return {
        "schema_version": 1,
        "trace": result.get("trace"),
        "tokens": {
            "input": usage["input_other"] + usage["input_cached"],
            "output": usage["output"],
            "cached": usage["input_cached"],
        }
        if usage is not None
        else None,
    }


def write_result(result, path):
    """Atomically persist native output and optional Studio telemetry.

    Args:
        result: Current native evaluation result.
        path: Destination of the native result JSON.
    """
    for target, data in (
        (path, result),
        (path.with_name("studio-result.json"), studio_report(result)),
    ):
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
