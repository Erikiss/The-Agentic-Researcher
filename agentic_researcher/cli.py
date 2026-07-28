"""CLI for the machine-readable Agentic Researcher pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .pipeline import (
    CURATION_RESPONSE_SCHEMA,
    ValidationError,
    atomic_write_json,
    chunk_ingest_bundle,
    expand_topics,
    init_project,
    merge_curation_responses,
    read_json,
    run_batch,
    run_curators,
)
from .providers import ProviderError, SUPPORTED_PROVIDERS, normalize_provider


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTRUCTIONS = REPO_ROOT / "INSTRUCTIONS.md"


def _named_values(values: Sequence[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValidationError(f"{label} must use NAME=VALUE syntax: {value!r}")
        name, item = value.split("=", 1)
        name = name.strip().lower()
        if not name or not item:
            raise ValidationError(f"{label} must use non-empty NAME=VALUE syntax")
        if name in parsed:
            raise ValidationError(f"duplicate {label} name: {name}")
        parsed[name] = item
    return parsed


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-researcher",
        description="Machine-readable curation and low-cost research batch orchestration.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    initialize = subparsers.add_parser(
        "init-project", help="Initialize a project from research-spec.json."
    )
    initialize.add_argument("spec", type=Path)
    initialize.add_argument("destination", type=Path)
    initialize.add_argument("--instructions", type=Path, default=DEFAULT_INSTRUCTIONS)
    initialize.add_argument("--force", action="store_true")

    curate = subparsers.add_parser(
        "curate", help="Run independent commercial curators and deterministically merge their responses."
    )
    curate.add_argument("bundle", type=Path)
    curate.add_argument("--output", "-o", type=Path, required=True)
    curate.add_argument(
        "--provider",
        action="append",
        choices=SUPPORTED_PROVIDERS,
        default=[],
        help="Invoke this provider. Repeat for Claude, Codex, and Antigravity.",
    )
    curate.add_argument(
        "--response",
        action="append",
        default=[],
        metavar="PROVIDER=FILE",
        help="Use a precomputed curation response instead of invoking that provider.",
    )
    curate.add_argument(
        "--command",
        dest="provider_commands",
        action="append",
        default=[],
        metavar="PROVIDER=COMMAND",
        help="Override a provider command (also configurable via AR_PROVIDER_*_COMMAND).",
    )
    curate.add_argument("--runs-dir", type=Path)
    curate.add_argument(
        "--media-root",
        type=Path,
        help="Local image directory/manifest; matched media are copied into each private provider workspace.",
    )
    curate.add_argument("--threshold", type=int, default=2)
    curate.add_argument("--timeout", type=int, default=1800)
    curate.add_argument(
        "--items-per-prompt",
        type=int,
        default=12,
        help="Maximum atomic discussion blocks sent in one provider invocation.",
    )
    curate.add_argument(
        "--max-input-chars",
        type=int,
        default=24_000,
        help="Approximate JSON character budget per provider invocation.",
    )
    curate.add_argument(
        "--allow-degraded-consensus",
        action="store_true",
        help=(
            "Allow a non-standard or incomplete curator set. By default Claude, "
            "Codex, and Antigravity must all return valid responses."
        ),
    )

    expand = subparsers.add_parser(
        "expand", help="Expand curated topics into synthetic, low-cost research specifications."
    )
    expand.add_argument("curated", type=Path)
    expand.add_argument("--output", "-o", type=Path, required=True)
    expand.add_argument("--include-needs-review", action="store_true")

    batch = subparsers.add_parser(
        "run-batch", help="Run an idempotent, resumable queue through an agent provider."
    )
    batch.add_argument("queue", type=Path)
    batch.add_argument("--work-root", type=Path, required=True)
    batch.add_argument("--state", type=Path, required=True)
    batch.add_argument(
        "--provider",
        choices=SUPPORTED_PROVIDERS,
        default="opencode",
        help="OpenCode is the default adapter for a local open-weight model.",
    )
    batch.add_argument("--command", dest="provider_command")
    batch.add_argument("--instructions", type=Path, default=DEFAULT_INSTRUCTIONS)
    batch.add_argument("--max-tasks", type=int)
    batch.add_argument("--max-attempts", type=int, default=2)
    batch.add_argument("--timeout", type=int, default=7200)
    batch.add_argument("--no-retry-failed", action="store_true")
    batch.add_argument("--dry-run", action="store_true")

    contract = subparsers.add_parser(
        "provider-contract", help="Print the normalized provider adapter contract."
    )
    contract.add_argument("--provider", choices=SUPPORTED_PROVIDERS)
    return parser


def _curate(args: argparse.Namespace) -> dict[str, Any]:
    bundle = read_json(args.bundle)
    response_paths = _named_values(args.response, "--response")
    commands: dict[str, str] = {}
    for provider, command in _named_values(
        args.provider_commands, "--command"
    ).items():
        normalized_provider = normalize_provider(provider)
        if normalized_provider in commands:
            raise ValidationError(
                f"duplicate normalized --command provider: {normalized_provider}"
            )
        commands[normalized_provider] = command
    normalized_response_names = {
        normalize_provider(provider) for provider in response_paths
    }
    if len(normalized_response_names) != len(response_paths):
        raise ValidationError("duplicate normalized --response provider")
    normalized_invoked = {normalize_provider(provider) for provider in args.provider}
    overlap = normalized_response_names & normalized_invoked
    if overlap:
        raise ValidationError(
            f"providers cannot be both invoked and precomputed: {', '.join(sorted(overlap))}"
        )
    if not response_paths and not args.provider:
        raise ValidationError(
            "curate requires --provider and/or --response; use all three commercial providers for 2-of-3 consensus"
        )
    responses_by_provider: dict[str, dict[str, Any]] = {}
    for provider, path in sorted(response_paths.items()):
        response = read_json(Path(path))
        expected_provider = normalize_provider(provider)
        actual_provider = (
            normalize_provider(str(response.get("provider", "")))
            if isinstance(response, dict)
            else None
        )
        if isinstance(response, dict) and actual_provider != expected_provider:
            raise ValidationError(
                f"--response name {provider!r} does not match response provider "
                f"{response.get('provider')!r}"
            )
        if not isinstance(response, dict):
            raise ValidationError(f"precomputed response must be a JSON object: {path}")
        responses_by_provider[expected_provider] = response
    run_records: list[dict[str, Any]] = []
    chunk_count = 0
    invoked_coverage: dict[str, set[int]] = {}
    if args.provider:
        runs_dir = args.runs_dir or args.output.with_suffix(args.output.suffix + ".runs")
        chunks = chunk_ingest_bundle(
            bundle,
            max_items=args.items_per_prompt,
            max_item_chars=args.max_input_chars,
        )
        chunk_count = len(chunks)
        invoked_topics: dict[str, list[dict[str, Any]]] = {
            normalize_provider(provider): [] for provider in args.provider
        }
        invoked_coverage = {provider: set() for provider in invoked_topics}
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_dir = runs_dir / f"chunk-{chunk_index:04d}-of-{chunk_count:04d}"
            invoked, runs = run_curators(
                chunk,
                args.provider,
                chunk_dir,
                commands=commands,
                timeout_seconds=args.timeout,
                media_root=args.media_root,
            )
            item_ids = [item["id"] for item in chunk["items"]]
            for run in runs:
                record = run.as_dict()
                record.update(
                    {
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "item_ids": item_ids,
                    }
                )
                run_records.append(record)
                if (
                    isinstance(run.parsed, dict)
                    and run.parsed.get("schema_version") == CURATION_RESPONSE_SCHEMA
                ):
                    atomic_write_json(
                        chunk_dir / f"{run.provider}.response.json",
                        run.parsed,
                    )
            for response in invoked:
                provider = normalize_provider(str(response["provider"]))
                invoked_topics[provider].extend(response["topics"])
                invoked_coverage[provider].add(chunk_index)

        for provider, topics in sorted(invoked_topics.items()):
            if not invoked_coverage[provider]:
                continue
            combined = {
                "schema_version": CURATION_RESPONSE_SCHEMA,
                "provider": provider,
                "bundle_id": bundle.get("bundle_id"),
                "topics": topics,
                "chunk_coverage": sorted(invoked_coverage[provider]),
                "chunk_count": chunk_count,
            }
            responses_by_provider[provider] = combined
            atomic_write_json(runs_dir / f"{provider}.response.json", combined)
        atomic_write_json(runs_dir / "runs.json", run_records)
    responses = [
        responses_by_provider[provider] for provider in sorted(responses_by_provider)
    ]
    response_providers = {
        normalize_provider(str(response.get("provider", "")))
        for response in responses
        if isinstance(response, dict)
    }
    required_curators = {"claude", "codex", "antigravity"}
    incomplete = {
        provider: sorted(set(range(1, chunk_count + 1)) - covered)
        for provider, covered in invoked_coverage.items()
        if len(covered) != chunk_count
    }
    if (
        not args.allow_degraded_consensus
        and (response_providers != required_curators or incomplete)
    ):
        missing = sorted(required_curators - response_providers)
        unexpected = sorted(response_providers - required_curators)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if incomplete:
            details.append(
                "incomplete chunks: "
                + ", ".join(
                    f"{provider}={indices}" for provider, indices in sorted(incomplete.items())
                )
            )
        raise ValidationError(
            "curation requires valid responses from Claude, Codex, and Antigravity "
            f"({'; '.join(details)}); use --allow-degraded-consensus only for an "
            "explicitly reviewed fallback"
        )
    result = merge_curation_responses(bundle, responses, threshold=args.threshold)
    result["quality_gate"] = {
        "required_curators": sorted(required_curators),
        "all_required_curators_present": response_providers == required_curators,
        "all_invoked_chunks_complete": not incomplete,
        "degraded_consensus_allowed": bool(args.allow_degraded_consensus),
    }
    if run_records:
        result["curation_runs"] = {
            "path": str((args.runs_dir or args.output.with_suffix(args.output.suffix + ".runs")).resolve()),
            "chunks": chunk_count,
            "successful": sum(record["status"] == "success" for record in run_records),
            "failed": sum(record["status"] != "success" for record in run_records),
        }
    atomic_write_json(args.output, result)
    return result


def _provider_contract(provider: str | None) -> dict[str, Any]:
    providers = [provider] if provider else list(SUPPORTED_PROVIDERS)
    return {
        "input": "UTF-8 prompt on stdin unless command uses {prompt} or {prompt_file}",
        "working_directory": "the curation run or initialized research project",
        "output": "JSON on stdout; fenced JSON and common wrapper objects are accepted",
        "command_placeholders": ["{prompt}", "{prompt_file}", "{workspace}", "{output_file}"],
        "environment_override": "AR_PROVIDER_<UPPERCASE_NAME>_COMMAND",
        "providers": providers,
        "statuses": ["success", "partial", "failed", "timeout", "needs_review", "skipped"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Retain the plan-era spelling as a compatibility alias without asking
    # argparse to interpret a leading-dash token as a positional subcommand.
    if arguments[:1] == ["--init-project"]:
        arguments[0] = "init-project"
    args = parser.parse_args(arguments)
    try:
        if args.action == "init-project":
            result = init_project(
                read_json(args.spec),
                args.destination,
                instruction_template=args.instructions,
                force=args.force,
            )
        elif args.action == "curate":
            result = _curate(args)
        elif args.action == "expand":
            result = expand_topics(
                read_json(args.curated), include_needs_review=args.include_needs_review
            )
            atomic_write_json(args.output, result)
        elif args.action == "run-batch":
            result = run_batch(
                read_json(args.queue),
                args.work_root,
                args.state,
                instruction_template=args.instructions,
                provider=args.provider,
                command_override=args.provider_command,
                max_tasks=args.max_tasks,
                max_attempts=args.max_attempts,
                timeout_seconds=args.timeout,
                retry_failed=not args.no_retry_failed,
                dry_run=args.dry_run,
            )
        elif args.action == "provider-contract":
            result = _provider_contract(args.provider)
        else:
            parser.error(f"unknown command: {args.action}")
            return 2
    except (ValidationError, ProviderError, OSError) as exc:
        print(f"agentic-researcher: error: {exc}", file=sys.stderr)
        return 2
    _print_json(result)
    return 0
