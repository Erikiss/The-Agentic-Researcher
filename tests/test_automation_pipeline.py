from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agentic_researcher.cli import main
from agentic_researcher.pipeline import (
    CURATED_TOPICS_SCHEMA,
    CURATION_RESPONSE_SCHEMA,
    INGEST_BUNDLE_SCHEMA,
    RESEARCH_QUEUE_SCHEMA,
    RESEARCH_SPEC_SCHEMA,
    ValidationError,
    build_curation_prompt,
    chunk_ingest_bundle,
    content_hash,
    expand_topics,
    init_project,
    merge_curation_responses,
    provider_safe_bundle,
    run_batch,
    stage_media_files,
)
from agentic_researcher.providers import (
    DEFAULT_COMMANDS,
    ProviderRun,
    extract_json_document,
    invoke_provider,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = REPO_ROOT / "INSTRUCTIONS.md"


def bundle() -> dict:
    return {
        "schema_version": INGEST_BUNDLE_SCHEMA,
        "bundle_id": "discord-math-2026-07-28",
        "items": [
            {
                "id": "block-calculus-1",
                "text": "How exactly does the chain rule follow?",
                "latex": [r"(f\circ g)'=(f'\circ g)g'"],
                "safety": {"untrusted": True},
            }
        ],
    }


def response(
    provider: str,
    *,
    formula: str = r"(f\circ g)'=(f'\circ g)g'",
    area: str = "Calculus",
) -> dict:
    return {
        "schema_version": CURATION_RESPONSE_SCHEMA,
        "provider": provider,
        "bundle_id": bundle()["bundle_id"],
        "topics": [
            {
                "title": f"Chain rule ({provider})",
                "summary": f"{provider} summary",
                "source_item_ids": ["block-calculus-1"],
                "area": area,
                "subarea": "Differential calculus",
                "formulas": [formula],
                "questions": ["How is the chain rule derived?"],
                "prerequisites": ["limits", "derivatives"],
                "uncertainties": [],
                "confidence": 0.9,
            }
        ],
    }


def test_two_of_three_merge_is_deterministic_and_accepts_majority() -> None:
    responses = [
        response("antigravity", formula="incorrect"),
        response("codex"),
        response("claude"),
    ]

    first = merge_curation_responses(bundle(), responses)
    second = merge_curation_responses(bundle(), list(reversed(responses)))

    assert first == second
    assert first["schema_version"] == CURATED_TOPICS_SCHEMA
    assert first["consensus"]["threshold"] == 2
    assert first["topics"][0]["status"] == "accepted"
    assert first["topics"][0]["topic"]["formulas"] == [
        r"(f\circ g)'=(f'\circ g)g'"
    ]
    assert first["topics"][0]["providers"] == ["antigravity", "claude", "codex"]


def test_formula_without_two_of_three_consensus_needs_review() -> None:
    result = merge_curation_responses(
        bundle(),
        [
            response("claude", formula="formula-a"),
            response("codex", formula="formula-b"),
            response("antigravity", formula="formula-c"),
        ],
    )

    topic = result["topics"][0]
    assert topic["status"] == "needs_review"
    assert "formulas" in topic["critical_disagreements"]
    assert set(topic["alternatives"]["formulas"]) == {
        "claude",
        "codex",
        "antigravity",
    }


def test_consensus_rejects_unknown_source_item_ids() -> None:
    responses = [
        response(provider) for provider in ("claude", "codex", "antigravity")
    ]
    for item in responses:
        item["topics"][0]["source_item_ids"] = ["unknown-item"]

    with pytest.raises(ValidationError, match="unknown source item ids"):
        merge_curation_responses(bundle(), responses)


def test_single_provider_only_counts_when_threshold_is_explicitly_one() -> None:
    result = merge_curation_responses(bundle(), [response("claude")], threshold=1)

    topic = result["topics"][0]
    assert topic["status"] == "accepted"
    assert topic["support_count"] == 1
    assert topic["consensus"]["area"] is True

    with pytest.raises(ValidationError, match="exceeds 1 available"):
        merge_curation_responses(bundle(), [response("claude")], threshold=2)


def test_expand_marks_synthetic_tasks_and_is_stable() -> None:
    curated = merge_curation_responses(
        bundle(), [response("claude"), response("codex"), response("antigravity")]
    )

    first = expand_topics(curated)
    second = expand_topics(curated)

    assert first == second
    assert first["schema_version"] == RESEARCH_QUEUE_SCHEMA
    assert len(first["tasks"]) == 4
    assert all(task["schema_version"] == RESEARCH_SPEC_SCHEMA for task in first["tasks"])
    assert all(task["synthetic"] is True for task in first["tasks"])
    assert all(
        task["source"]["source_item_ids"] == ["block-calculus-1"]
        for task in first["tasks"]
    )
    assert all(
        "never rank by 2D distance" in " ".join(task["evidence_policy"]["rules"])
        for task in first["tasks"]
    )
    assert all(
        "Erdős Problems" in task["evidence_policy"]["sources"]
        for task in first["tasks"]
    )


def test_curator_tasks_enrich_instead_of_replacing_required_baseline() -> None:
    curated = merge_curation_responses(
        bundle(), [response("claude"), response("codex"), response("antigravity")]
    )
    curated["topics"][0]["topic"]["proposed_tasks"] = [
        {
            "task_type": "extension",
            "title": "Explore a higher-order extension",
            "problem": "Determine which higher-order chain-rule form is accessible.",
        }
    ]

    queue = expand_topics(curated)

    assert len(queue["tasks"]) == 5
    assert {task["task_type"] for task in queue["tasks"]} == {
        "foundation",
        "worked_example",
        "literature",
        "counterexample",
        "extension",
    }
    assert all(
        any(
            "evidence-ranking.json" in output
            for output in task["expected_outputs"]
        )
        for task in queue["tasks"]
    )


def test_ingest_chunking_preserves_every_atomic_item_once() -> None:
    source = bundle()
    source["items"] = [
        {"id": f"block-{index:02d}", "text": "x" * (40 + index)}
        for index in range(7)
    ]

    chunks = chunk_ingest_bundle(source, max_items=3, max_item_chars=500)

    assert [len(chunk["items"]) for chunk in chunks] == [3, 3, 1]
    assert [
        item["id"] for chunk in chunks for item in chunk["items"]
    ] == [item["id"] for item in source["items"]]
    assert [chunk["curation_chunk"]["index"] for chunk in chunks] == [1, 2, 3]
    assert all(chunk["curation_chunk"]["count"] == 3 for chunk in chunks)


def test_init_project_is_idempotent_and_preserves_existing_progress(tmp_path: Path) -> None:
    curated = merge_curation_responses(
        bundle(), [response("claude"), response("codex"), response("antigravity")]
    )
    spec = expand_topics(curated)["tasks"][0]
    project = tmp_path / spec["task_id"]

    created = init_project(spec, project, instruction_template=INSTRUCTIONS)
    (project / "TODO.md").write_text("- [x] progress\n", encoding="utf-8")
    unchanged = init_project(spec, project, instruction_template=INSTRUCTIONS)

    assert created["status"] == "created"
    assert unchanged["status"] == "unchanged"
    assert (project / "research-spec.json").exists()
    assert (project / "AGENTS.md").exists()
    assert (project / "CLAUDE.md").exists()
    assert (project / "GEMINI.md").exists()
    assert json.loads((project / "evidence-ranking.json").read_text())["status"] == "pending"
    assert (project / "TODO.md").read_text(encoding="utf-8") == "- [x] progress\n"
    assert spec["problem"] in (project / "PROBLEM.md").read_text(encoding="utf-8")


def test_init_project_rejects_different_contract_without_force(tmp_path: Path) -> None:
    spec = {
        "schema_version": RESEARCH_SPEC_SCHEMA,
        "task_id": "task-one",
        "title": "One",
        "problem": "Do one",
        "task_type": "foundation",
    }
    project = tmp_path / "project"
    init_project(spec, project, instruction_template=INSTRUCTIONS)
    changed = {**spec, "problem": "Changed"}

    with pytest.raises(ValidationError, match="different research-spec"):
        init_project(changed, project, instruction_template=INSTRUCTIONS)


def test_research_task_id_cannot_escape_batch_work_root(tmp_path: Path) -> None:
    queue = {
        "schema_version": RESEARCH_QUEUE_SCHEMA,
        "queue_id": "unsafe-queue",
        "bundle_id": "bundle-one",
        "tasks": [
            {
                "schema_version": RESEARCH_SPEC_SCHEMA,
                "task_id": "../outside",
                "title": "Unsafe",
                "problem": "Must never escape.",
                "task_type": "foundation",
            }
        ],
    }

    with pytest.raises(ValidationError, match="path-safe"):
        run_batch(
            queue,
            tmp_path / "work",
            tmp_path / "state.json",
            instruction_template=INSTRUCTIONS,
            dry_run=True,
        )
    assert not (tmp_path / "outside").exists()


def test_provider_defaults_put_prompt_in_argument_for_antigravity_and_opencode() -> None:
    assert DEFAULT_COMMANDS["antigravity"] == ["agy", "-p", "{prompt}"]
    assert DEFAULT_COMMANDS["opencode"] == ["opencode", "run", "{prompt}"]
    assert "{prompt}" not in DEFAULT_COMMANDS["claude"]
    assert "{prompt}" not in DEFAULT_COMMANDS["codex"]


def test_provider_adapter_supports_prompt_argument_and_wrapper_json(tmp_path: Path) -> None:
    script = tmp_path / "provider.py"
    script.write_text(
        "import json, sys\n"
        "payload = {'status': 'success', 'summary': sys.argv[1], "
        "'artifacts': [], 'verification': {}}\n"
        "print(json.dumps({'result': json.dumps(payload)}))\n",
        encoding="utf-8",
    )

    run = invoke_provider(
        "opencode",
        "prompt-through-argument",
        tmp_path / "workspace",
        command_override=[sys.executable, str(script), "{prompt}"],
    )

    assert run.status == "success"
    assert run.parsed["summary"] == "prompt-through-argument"
    assert extract_json_document('```json\n{"ok": true}\n```') == {"ok": True}


def test_curator_prompt_never_leaks_private_bundle_and_uses_staged_media(
    tmp_path: Path,
) -> None:
    source_bundle = bundle()
    image_bytes = b"\x89PNG\r\n\x1a\nstable-fixture"
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    source_bundle["items"][0]["attachments"] = [
        {
            "id": "attachment-1",
            "media_type": "image",
            "content_sha256": image_hash,
        }
    ]
    source_bundle["private"] = {
        "username": "PRIVATE_USERNAME",
        "cdn_url": "https://cdn.example/PRIVATE_TOKEN",
    }
    source_bundle["public"] = {
        "messages": [{"id": "safe", "username": "PRIVATE_USERNAME"}]
    }
    source_media = tmp_path / "source-media"
    source_media.mkdir()
    (source_media / "attachment-1.png").write_bytes(image_bytes)
    staged = stage_media_files(
        source_bundle, source_media, tmp_path / "provider-workspace" / "media"
    )

    prompt = build_curation_prompt(source_bundle, "claude", staged)
    projection = provider_safe_bundle(source_bundle, staged)

    assert "PRIVATE_USERNAME" not in prompt
    assert "PRIVATE_TOKEN" not in prompt
    assert '"private"' not in prompt
    assert '"manifest_path": "media/manifest.json"' in prompt
    assert str(staged.resolve()) not in prompt
    assert "attachment-1" in prompt
    assert str(source_media.resolve()) not in prompt
    assert "messages" not in projection["public"]
    assert projection["public"]["message_count"] == 1


def test_provider_projection_removes_raw_discord_identifiers() -> None:
    source_bundle = bundle()
    source_bundle["items"][0]["metadata"] = {
        "raw_content": "private raw copy",
        "discord": {"message_id": "123", "guild_id": "456"},
        "global_name": "private name",
        "link": "https://discord.com/channels/456/789/123",
    }

    rendered = json.dumps(provider_safe_bundle(source_bundle), ensure_ascii=False)

    assert "private raw copy" not in rendered
    assert "private name" not in rendered
    assert "discord.com/channels" not in rendered
    assert '"message_id"' not in rendered


def test_batch_is_resumable_and_does_not_repeat_success(tmp_path: Path) -> None:
    script = tmp_path / "provider.py"
    counter = tmp_path / "counter.txt"
    script.write_text(
        "import json, pathlib, sys\n"
        "counter = pathlib.Path(sys.argv[1])\n"
        "value = int(counter.read_text() if counter.exists() else '0') + 1\n"
        "counter.write_text(str(value))\n"
        "print(json.dumps({'status': 'success', 'summary': 'done', "
        "'artifacts': ['report.tex'], 'verification': {'status': 'verified'}}))\n",
        encoding="utf-8",
    )
    spec = {
        "schema_version": RESEARCH_SPEC_SCHEMA,
        "task_id": "chain-rule-foundations",
        "title": "Chain rule foundations",
        "problem": "Explain and verify the chain rule.",
        "task_type": "foundation",
        "synthetic": True,
    }
    queue = {
        "schema_version": RESEARCH_QUEUE_SCHEMA,
        "queue_id": "queue-one",
        "bundle_id": "bundle-one",
        "tasks": [spec],
    }
    state_path = tmp_path / "state.json"
    command = [sys.executable, str(script), str(counter), "{prompt}"]

    first = run_batch(
        queue,
        tmp_path / "work",
        state_path,
        instruction_template=INSTRUCTIONS,
        command_override=command,
    )
    second = run_batch(
        queue,
        tmp_path / "work",
        state_path,
        instruction_template=INSTRUCTIONS,
        command_override=command,
    )

    task_state = second["tasks"][spec["task_id"]]
    assert first["summary"] == {"success": 1}
    assert task_state["status"] == "success"
    assert task_state["attempts"] == 1
    assert counter.read_text() == "1"
    result = json.loads(
        (tmp_path / "work" / spec["task_id"] / "run-result.json").read_text()
    )
    assert result["status"] == "success"
    assert result["verification"]["status"] == "verified"


def test_batch_recovers_interrupted_attempt_once_then_stops(tmp_path: Path) -> None:
    spec = {
        "schema_version": RESEARCH_SPEC_SCHEMA,
        "task_id": "interrupted-task",
        "title": "Interrupted",
        "problem": "Resume safely.",
        "task_type": "foundation",
    }
    queue = {
        "schema_version": RESEARCH_QUEUE_SCHEMA,
        "queue_id": "interrupted-queue",
        "bundle_id": "bundle-one",
        "tasks": [spec],
    }
    state_path = tmp_path / "state.json"
    project = tmp_path / "work" / spec["task_id"]
    init_project(spec, project, instruction_template=INSTRUCTIONS)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "agentic-researcher/batch-state/v1",
                "queue_id": queue["queue_id"],
                "provider": "opencode",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "tasks": {
                    spec["task_id"]: {
                        "task_id": spec["task_id"],
                        "input_hash": content_hash(spec, 64),
                        "status": "running",
                        "attempts": 2,
                        "project": str(project),
                        "started_at": "2026-01-01T00:00:00Z",
                        "finished_at": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = run_batch(
        queue,
        tmp_path / "work",
        state_path,
        instruction_template=INSTRUCTIONS,
        command_override=[sys.executable, "-c", "raise SystemExit(99)", "{prompt}"],
        max_attempts=2,
    )

    assert state["tasks"][spec["task_id"]]["status"] == "failed"
    assert state["tasks"][spec["task_id"]]["attempts"] == 2
    assert "previous provider process" in state["tasks"][spec["task_id"]]["error"]


def test_cli_provider_contract_and_schema_documents_are_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["provider-contract", "--provider", "opencode"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["providers"] == ["opencode"]
    assert "{prompt}" in output["command_placeholders"]

    schema_files = list((REPO_ROOT / "schemas").glob("*.schema.json"))
    assert len(schema_files) >= 7
    for schema_file in schema_files:
        parsed = json.loads(schema_file.read_text(encoding="utf-8"))
        assert parsed["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_cli_accepts_leading_dash_init_project_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = {
        "schema_version": RESEARCH_SPEC_SCHEMA,
        "task_id": "alias-test",
        "title": "Alias test",
        "problem": "Verify the compatibility spelling.",
        "task_type": "foundation",
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    assert (
        main(
            [
                "--init-project",
                str(spec_path),
                str(tmp_path / "project"),
                "--instructions",
                str(INSTRUCTIONS),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "created"


def test_cli_offline_curate_expand_and_dry_run_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle()), encoding="utf-8")
    response_args: list[str] = []
    for provider in ("claude", "codex", "antigravity"):
        path = tmp_path / f"{provider}.json"
        path.write_text(json.dumps(response(provider)), encoding="utf-8")
        response_args.extend(["--response", f"{provider}={path}"])
    curated_path = tmp_path / "curated.json"
    queue_path = tmp_path / "queue.json"

    assert (
        main(
            [
                "curate",
                str(bundle_path),
                "--output",
                str(curated_path),
                *response_args,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["expand", str(curated_path), "--output", str(queue_path)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "run-batch",
                str(queue_path),
                "--work-root",
                str(tmp_path / "work"),
                "--state",
                str(tmp_path / "state.json"),
                "--instructions",
                str(INSTRUCTIONS),
                "--max-tasks",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )
    state = json.loads(capsys.readouterr().out)

    assert json.loads(curated_path.read_text())["topics"][0]["status"] == "accepted"
    assert len(json.loads(queue_path.read_text())["tasks"]) == 4
    assert state["summary"] == {"pending": 3, "skipped": 1}


def test_cli_requires_the_full_commercial_curator_triad_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle()), encoding="utf-8")
    response_args: list[str] = []
    for provider in ("claude", "codex"):
        path = tmp_path / f"{provider}.json"
        path.write_text(json.dumps(response(provider)), encoding="utf-8")
        response_args.extend(["--response", f"{provider}={path}"])

    output = tmp_path / "curated.json"
    assert (
        main(
            [
                "curate",
                str(bundle_path),
                "--output",
                str(output),
                *response_args,
            ]
        )
        == 2
    )
    assert "requires valid responses" in capsys.readouterr().err
    assert not output.exists()

    assert (
        main(
            [
                "curate",
                str(bundle_path),
                "--output",
                str(output),
                "--allow-degraded-consensus",
                *response_args,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert output.exists()


def test_cli_chunks_live_curators_and_requires_complete_coverage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = bundle()
    source["items"] = [
        {
            "id": f"block-{index:02d}",
            "text": f"Discussion {index}",
            "latex": [r"f'(x)"],
        }
        for index in range(5)
    ]
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    seen_chunks: list[list[str]] = []

    def fake_run_curators(chunk, providers, workspace, **kwargs):
        del workspace, kwargs
        item_ids = [item["id"] for item in chunk["items"]]
        seen_chunks.append(item_ids)
        responses = []
        runs = []
        for provider in providers:
            topics = [
                {
                    "title": f"Topic {item_id}",
                    "summary": "Synthetic curator response",
                    "source_item_ids": [item_id],
                    "area": "Calculus",
                    "formulas": [r"f'(x)"],
                    "questions": [],
                    "prerequisites": [],
                    "uncertainties": [],
                    "confidence": 0.9,
                }
                for item_id in item_ids
            ]
            parsed = {
                "schema_version": CURATION_RESPONSE_SCHEMA,
                "provider": provider,
                "bundle_id": chunk["bundle_id"],
                "topics": topics,
            }
            responses.append(parsed)
            runs.append(
                ProviderRun(
                    provider=provider,
                    status="success",
                    command=["fake-provider"],
                    exit_code=0,
                    duration_seconds=0.01,
                    stdout=json.dumps(parsed),
                    stderr="",
                    parsed=parsed,
                )
            )
        return responses, runs

    monkeypatch.setattr("agentic_researcher.cli.run_curators", fake_run_curators)
    output = tmp_path / "curated.json"
    assert (
        main(
            [
                "curate",
                str(bundle_path),
                "--output",
                str(output),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--items-per-prompt",
                "2",
                "--provider",
                "claude",
                "--provider",
                "codex",
                "--provider",
                "antigravity",
            ]
        )
        == 0
    )
    capsys.readouterr()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert seen_chunks == [
        ["block-00", "block-01"],
        ["block-02", "block-03"],
        ["block-04"],
    ]
    assert len(result["topics"]) == 5
    assert result["curation_runs"]["chunks"] == 3
    assert result["curation_runs"]["successful"] == 9
    assert result["quality_gate"]["all_invoked_chunks_complete"] is True


def test_shell_launcher_dispatches_machine_readable_commands() -> None:
    launcher = (REPO_ROOT / "agentic-researcher").read_text(encoding="utf-8")
    assert "init-project|--init-project|curate|expand|run-batch|provider-contract" in launcher
    assert "python3 -m agentic_researcher" in launcher
