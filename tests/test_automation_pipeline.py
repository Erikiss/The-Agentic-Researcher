from __future__ import annotations

import hashlib
import json
import subprocess
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
    commit_curation_delta,
    content_hash,
    expand_topics,
    init_project,
    merge_curation_responses,
    prepare_curation_delta,
    provider_safe_bundle,
    run_batch,
    run_curators,
    stage_media_files,
)
from agentic_researcher.providers import (
    DEFAULT_COMMANDS,
    ProviderRun,
    extract_json_document,
    invoke_provider,
    probe_provider,
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


def chunk_response(chunk: dict, provider: str) -> dict:
    return {
        "schema_version": CURATION_RESPONSE_SCHEMA,
        "provider": provider,
        "bundle_id": chunk["bundle_id"],
        "topics": [
            {
                "title": f"Topic {item['id']}",
                "summary": "Synthetic curator response",
                "source_item_ids": [item["id"]],
                "area": "Calculus",
                "formulas": [r"f'(x)"],
                "questions": [],
                "prerequisites": [],
                "uncertainties": [],
                "confidence": 0.9,
            }
            for item in chunk["items"]
        ],
    }


def delta_bundle() -> dict:
    items = [
        {"id": "block-calculus-1", "text": "How does the chain rule follow?"},
        {"id": "block-algebra-1", "text": "Why is every field an integral domain?"},
    ]
    return {
        "schema_version": INGEST_BUNDLE_SCHEMA,
        "bundle_id": "discord-math-delta",
        "items": [
            {**item, "fingerprint": content_hash(item, 64)}
            for item in items
        ],
    }


def provider_run(provider: str, parsed: dict | None) -> ProviderRun:
    return ProviderRun(
        provider=provider,
        status="success" if parsed is not None else "failed",
        command=["fake-provider"],
        exit_code=0 if parsed is not None else 1,
        duration_seconds=0.01,
        stdout=json.dumps(parsed) if parsed is not None else "",
        stderr="" if parsed is not None else "simulated failure",
        parsed=parsed,
        error=None if parsed is not None else "simulated failure",
    )


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


def test_expand_can_route_needs_review_topics_to_a_guarded_research_queue() -> None:
    curated = merge_curation_responses(
        bundle(),
        [
            response("claude", formula="formula-a"),
            response("codex", formula="formula-b"),
            response("antigravity", formula="formula-c"),
        ],
    )

    assert expand_topics(curated)["tasks"] == []
    queue = expand_topics(curated, include_needs_review=True)

    assert len(queue["tasks"]) == 4
    for task in queue["tasks"]:
        assert task["context"]["curation_status"] == "needs_review"
        assert task["context"]["critical_disagreements"] == ["formulas"]
        assert task["context"]["provider_support"]["count"] == 3
        assert "Resolve the listed disputed fields" in " ".join(
            task["constraints"]
        )


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


def test_curation_delta_selects_all_then_only_content_changes() -> None:
    source = delta_bundle()

    first_delta, first_report = prepare_curation_delta(source)
    seen = commit_curation_delta(first_delta)
    repeated_delta, repeated_report = prepare_curation_delta(source, seen)

    changed = json.loads(json.dumps(source))
    changed_item = changed["items"][1]
    changed_item["text"] = "Why is every field necessarily an integral domain?"
    changed_item["fingerprint"] = content_hash(
        {"id": changed_item["id"], "text": changed_item["text"]},
        64,
    )
    changed_delta, changed_report = prepare_curation_delta(changed, seen)

    assert [item["id"] for item in first_delta["items"]] == [
        "block-calculus-1",
        "block-algebra-1",
    ]
    assert first_report["status"] == "ready"
    assert first_report["selected_item_count"] == 2
    assert len(seen["fingerprints"]) == 2
    assert repeated_delta["items"] == []
    assert repeated_report["status"] == "no_work"
    assert repeated_report["already_seen_item_count"] == 2
    assert [item["id"] for item in changed_delta["items"]] == [
        "block-algebra-1"
    ]
    assert changed_report["status"] == "ready"
    assert changed_report["selected_item_count"] == 1
    assert changed_report["already_seen_item_count"] == 1


def test_curation_delta_rejects_a_stale_declared_fingerprint() -> None:
    source = delta_bundle()
    source["items"][0]["text"] = "Content changed without refreshing the hash."

    with pytest.raises(
        ValidationError,
        match="fingerprint does not match its canonical content",
    ):
        prepare_curation_delta(source)


def test_cli_curation_delta_file_roundtrip_tracks_content_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = delta_bundle()
    bundle_path = tmp_path / "bundle.json"
    seen_path = tmp_path / "seen.json"
    delta_path = tmp_path / "delta.json"
    report_path = tmp_path / "report.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    prepare_args = [
        "prepare-curation-delta",
        str(bundle_path),
        "--seen",
        str(seen_path),
        "--output",
        str(delta_path),
        "--report",
        str(report_path),
    ]

    assert main(prepare_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert [item["id"] for item in json.loads(delta_path.read_text())["items"]] == [
        "block-calculus-1",
        "block-algebra-1",
    ]
    assert json.loads(report_path.read_text())["selected_item_count"] == 2

    assert (
        main(
            [
                "commit-curation-delta",
                str(delta_path),
                "--seen",
                str(seen_path),
            ]
        )
        == 0
    )
    committed = json.loads(capsys.readouterr().out)
    assert committed["seen_fingerprint_count"] == 2
    assert json.loads(seen_path.read_text())["fingerprints"]

    assert main(prepare_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_work"
    assert json.loads(delta_path.read_text())["items"] == []
    assert json.loads(report_path.read_text())["already_seen_item_count"] == 2

    changed_item = source["items"][0]
    changed_item["text"] = "How exactly does the chain rule follow?"
    changed_item["fingerprint"] = content_hash(
        {"id": changed_item["id"], "text": changed_item["text"]},
        64,
    )
    bundle_path.write_text(json.dumps(source), encoding="utf-8")

    assert main(prepare_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    changed_delta = json.loads(delta_path.read_text())
    assert [item["id"] for item in changed_delta["items"]] == [
        "block-calculus-1"
    ]
    assert json.loads(report_path.read_text())["selected_item_count"] == 1


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
    assert "{prompt}" in DEFAULT_COMMANDS["antigravity"]
    assert DEFAULT_COMMANDS["opencode"] == ["opencode", "run", "{prompt}"]
    assert "{prompt}" not in DEFAULT_COMMANDS["claude"]
    assert "{prompt}" not in DEFAULT_COMMANDS["codex"]
    assert ["--sandbox", "read-only"] == DEFAULT_COMMANDS["codex"][3:5]
    assert "--permission-mode" in DEFAULT_COMMANDS["claude"]
    assert "--sandbox" in DEFAULT_COMMANDS["antigravity"]


def test_codex_default_attaches_staged_images_read_only_and_ephemerally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    media = workspace / "media"
    media.mkdir(parents=True)
    image = media / "formula.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    observed: list[str] = []

    def fake_run(command, **kwargs):
        del kwargs
        observed.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"success"}',
            stderr="",
        )

    monkeypatch.setattr("agentic_researcher.providers.subprocess.run", fake_run)
    run = invoke_provider(
        "codex",
        "Inspect the staged formula image.",
        workspace,
        image_paths=[Path("media") / image.name],
    )

    assert run.status == "success"
    assert "--sandbox" in observed
    assert observed[observed.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in observed
    assert observed[observed.index("--image") + 1] == str(
        Path("media") / image.name
    )


def test_provider_auth_probe_reuses_the_resolved_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        observed.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("agentic_researcher.providers.subprocess.run", fake_run)
    result = probe_provider(
        "claude",
        [r"C:\Tools\claude.exe", "-p"],
        require_auth=True,
    )

    assert result["ready"] is True
    assert observed == [
        [r"C:\Tools\claude.exe", "--version"],
        [r"C:\Tools\claude.exe", "auth", "status"],
    ]


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
    assert extract_json_document(
        '{"status":"SUCCESS","response":"{\\"ok\\":true}"}'
    ) == {"ok": True}


def test_provider_adapter_uses_utf8_for_mathematical_prompts(tmp_path: Path) -> None:
    script = tmp_path / "unicode_provider.py"
    script.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
        "print(json.dumps({'status': 'success', 'summary': prompt, "
        "'artifacts': [], 'verification': {}}))\n",
        encoding="utf-8",
    )
    prompt = "Kettenregel: ∫ α ↦ β und 𝔼[X]"

    run = invoke_provider(
        "opencode",
        prompt,
        tmp_path / "workspace",
        command_override=[sys.executable, str(script)],
    )

    assert run.status == "success"
    assert run.parsed["summary"] == prompt


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


def test_run_curators_reads_staged_manifest_for_image_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_bundle = bundle()
    image_bytes = b"\x89PNG\r\n\x1a\nstable-fixture"
    source_bundle["items"][0]["attachments"] = [
        {
            "id": "attachment-1",
            "media_type": "image",
            "content_sha256": hashlib.sha256(image_bytes).hexdigest(),
        }
    ]
    source_media = tmp_path / "source-media"
    source_media.mkdir()
    (source_media / "attachment-1.png").write_bytes(image_bytes)
    observed_paths: list[Path] = []

    def fake_invoke(provider, prompt, workspace, **kwargs):
        del prompt
        image_paths = kwargs["image_paths"]
        observed_paths.extend(image_paths)
        assert all((workspace / path).is_file() for path in image_paths)
        parsed = response(provider)
        return provider_run(provider, parsed)

    monkeypatch.setattr("agentic_researcher.pipeline.invoke_provider", fake_invoke)
    responses, runs = run_curators(
        source_bundle,
        ["codex"],
        tmp_path / "curation",
        media_root=source_media,
    )

    assert len(responses) == 1
    assert runs[0].status == "success"
    assert len(observed_paths) == 1
    assert observed_paths[0].parent == Path("media")
    assert observed_paths[0].suffix == ".png"


def test_run_curators_rejects_provider_impersonation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_invoke(provider, prompt, workspace, **kwargs):
        del prompt, workspace, kwargs
        return provider_run(provider, response("codex"))

    monkeypatch.setattr("agentic_researcher.pipeline.invoke_provider", fake_invoke)
    responses, runs = run_curators(bundle(), ["claude"], tmp_path / "curation")

    assert responses == []
    assert runs[0].status == "failed"
    assert "does not match invoked provider" in str(runs[0].error)


def test_run_curators_rejects_incomplete_chunk_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = bundle()
    source["items"].append({"id": "block-algebra-2", "text": "A second topic"})

    def fake_invoke(provider, prompt, workspace, **kwargs):
        del prompt, workspace, kwargs
        return provider_run(provider, response(provider))

    monkeypatch.setattr("agentic_researcher.pipeline.invoke_provider", fake_invoke)
    responses, runs = run_curators(source, ["claude"], tmp_path / "curation")

    assert responses == []
    assert runs[0].status == "failed"
    assert "does not cover source item ids: block-algebra-2" in str(runs[0].error)


def test_run_curators_persists_each_valid_response_before_next_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def fake_invoke(provider, prompt, workspace, **kwargs):
        del prompt, workspace, kwargs
        events.append(f"invoke:{provider}")
        return provider_run(provider, response(provider))

    def persist(provider: str, parsed: dict) -> None:
        assert parsed["provider"] == provider
        events.append(f"persist:{provider}")

    monkeypatch.setattr("agentic_researcher.pipeline.invoke_provider", fake_invoke)
    responses, runs = run_curators(
        bundle(),
        ["claude", "codex"],
        tmp_path / "curation",
        on_valid_response=persist,
    )

    assert len(responses) == 2
    assert all(run.status == "success" for run in runs)
    assert events == [
        "invoke:claude",
        "persist:claude",
        "invoke:codex",
        "persist:codex",
    ]


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


def test_cli_two_of_three_mode_requires_quorum_for_every_chunk(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = bundle()
    source["items"] = [
        {"id": f"block-{index:02d}", "text": f"Discussion {index}"}
        for index in range(4)
    ]
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    call_index = 0

    def fake_run_curators(chunk, providers, workspace, **kwargs):
        nonlocal call_index
        del workspace, kwargs
        failed_provider = "antigravity" if call_index == 0 else "claude"
        call_index += 1
        responses = []
        runs = []
        for provider in providers:
            parsed = (
                None
                if provider == failed_provider
                else chunk_response(chunk, provider)
            )
            if parsed is not None:
                responses.append(parsed)
            runs.append(provider_run(provider, parsed))
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
                "--threshold",
                "2",
                "--allow-degraded-consensus",
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
    gate = result["quality_gate"]
    assert len(result["topics"]) == 4
    assert gate["all_required_curators_attempted"] is True
    assert gate["all_invoked_chunks_complete"] is False
    assert gate["chunk_quorum_met"] is True
    assert gate["complete_curators"] == ["codex"]
    assert [entry["count"] for entry in gate["chunk_quorum"]] == [2, 2]


def test_cli_resume_reuses_successful_provider_response_without_reinvoking(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle()), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run_curators(chunk, providers, workspace, **kwargs):
        del workspace, kwargs
        calls.append(list(providers))
        successful = {"codex"} if len(calls) == 1 else {"claude"}
        responses = []
        runs = []
        for provider in providers:
            parsed = (
                chunk_response(chunk, provider)
                if provider in successful
                else None
            )
            if parsed is not None:
                responses.append(parsed)
            runs.append(provider_run(provider, parsed))
        return responses, runs

    monkeypatch.setattr("agentic_researcher.cli.run_curators", fake_run_curators)
    output = tmp_path / "curated.json"
    runs_dir = tmp_path / "runs"
    arguments = [
        "curate",
        str(bundle_path),
        "--output",
        str(output),
        "--runs-dir",
        str(runs_dir),
        "--threshold",
        "2",
        "--allow-degraded-consensus",
        "--resume",
        "--provider",
        "claude",
        "--provider",
        "codex",
        "--provider",
        "antigravity",
    ]

    assert main(arguments) == 2
    capsys.readouterr()
    assert not output.exists()
    assert (runs_dir / "chunk-0001-of-0001" / "codex.response.json").exists()

    assert main(arguments) == 0
    capsys.readouterr()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert calls == [
        ["claude", "codex", "antigravity"],
        ["claude", "antigravity"],
    ]
    assert result["quality_gate"]["chunk_quorum_met"] is True
    assert result["curation_runs"]["reused_responses"] == 1


def test_cli_resume_reinvokes_when_chunk_content_hash_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = bundle()
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run_curators(chunk, providers, workspace, **kwargs):
        del workspace, kwargs
        calls.append(list(providers))
        parsed = chunk_response(chunk, "claude")
        return [parsed], [provider_run("claude", parsed)]

    monkeypatch.setattr("agentic_researcher.cli.run_curators", fake_run_curators)
    output = tmp_path / "curated.json"
    runs_dir = tmp_path / "runs"
    arguments = [
        "curate",
        str(bundle_path),
        "--output",
        str(output),
        "--runs-dir",
        str(runs_dir),
        "--threshold",
        "1",
        "--allow-degraded-consensus",
        "--resume",
        "--provider",
        "claude",
    ]

    assert main(arguments) == 0
    capsys.readouterr()
    cache_path = runs_dir / "chunk-0001-of-0001" / "claude.response.json"
    first_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(first_cache["curation_chunk_input_hash"]) == 64

    source["items"][0]["text"] = "The same id now has different mathematics."
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    assert main(arguments) == 0
    capsys.readouterr()

    second_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert calls == [["claude"], ["claude"]]
    assert (
        second_cache["curation_chunk_input_hash"]
        != first_cache["curation_chunk_input_hash"]
    )


@pytest.mark.parametrize("invalid_field", ["provider", "bundle", "source"])
def test_cli_resume_reinvokes_when_cached_response_fails_validation(
    invalid_field: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = bundle()
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    chunk = chunk_ingest_bundle(source)[0]
    cached = chunk_response(chunk, "claude")
    cached["curation_chunk_input_hash"] = content_hash(chunk, 64)
    if invalid_field == "provider":
        cached["provider"] = "codex"
    elif invalid_field == "bundle":
        cached["bundle_id"] = "wrong-bundle"
    else:
        cached["topics"][0]["source_item_ids"] = ["unknown-source-item"]

    runs_dir = tmp_path / "runs"
    cache_path = runs_dir / "chunk-0001-of-0001" / "claude.response.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(cached), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run_curators(current_chunk, providers, workspace, **kwargs):
        del workspace, kwargs
        calls.append(list(providers))
        parsed = chunk_response(current_chunk, "claude")
        return [parsed], [provider_run("claude", parsed)]

    monkeypatch.setattr("agentic_researcher.cli.run_curators", fake_run_curators)
    assert (
        main(
            [
                "curate",
                str(bundle_path),
                "--output",
                str(tmp_path / "curated.json"),
                "--runs-dir",
                str(runs_dir),
                "--threshold",
                "1",
                "--allow-degraded-consensus",
                "--resume",
                "--provider",
                "claude",
            ]
        )
        == 0
    )
    capsys.readouterr()

    repaired = json.loads(cache_path.read_text(encoding="utf-8"))
    assert calls == [["claude"]]
    assert repaired["provider"] == "claude"
    assert repaired["bundle_id"] == source["bundle_id"]
    assert repaired["topics"][0]["source_item_ids"] == ["block-calculus-1"]


def test_cli_never_caches_a_failed_parsed_provider_response(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle()), encoding="utf-8")

    def fake_run_curators(chunk, providers, workspace, **kwargs):
        del providers, workspace, kwargs
        invalid = chunk_response(chunk, "claude")
        invalid["bundle_id"] = "wrong-bundle"
        run = ProviderRun(
            provider="claude",
            status="failed",
            command=["fake-provider"],
            exit_code=0,
            duration_seconds=0.01,
            stdout=json.dumps(invalid),
            stderr="",
            parsed=invalid,
            error="curation response bundle mismatch",
        )
        return [], [run]

    monkeypatch.setattr("agentic_researcher.cli.run_curators", fake_run_curators)
    runs_dir = tmp_path / "runs"
    result = main(
        [
            "curate",
            str(bundle_path),
            "--output",
            str(tmp_path / "curated.json"),
            "--runs-dir",
            str(runs_dir),
            "--threshold",
            "1",
            "--allow-degraded-consensus",
            "--resume",
            "--provider",
            "claude",
        ]
    )

    assert result == 2
    capsys.readouterr()
    assert not (
        runs_dir / "chunk-0001-of-0001" / "claude.response.json"
    ).exists()


def test_cli_rejects_mixed_precomputed_and_live_curators(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = bundle()
    bundle_path = tmp_path / "bundle.json"
    response_path = tmp_path / "claude.json"
    bundle_path.write_text(json.dumps(source), encoding="utf-8")
    response_path.write_text(
        json.dumps(chunk_response(source, "claude")), encoding="utf-8"
    )

    result = main(
        [
            "curate",
            str(bundle_path),
            "--output",
            str(tmp_path / "curated.json"),
            "--provider",
            "codex",
            "--response",
            f"claude={response_path}",
        ]
    )

    assert result == 2
    assert "cannot mix --response with live --provider runs" in capsys.readouterr().err


def test_provider_preflight_requires_two_spawnable_curators(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = {"claude", "codex"}
    require_auth_values: list[bool] = []

    def fake_probe(provider, override=None, **kwargs):
        del override
        require_auth_values.append(bool(kwargs["require_auth"]))
        return {
            "provider": provider,
            "ready": provider in ready,
            "executable": provider,
            "exit_code": 0 if provider in ready else None,
            "duration_seconds": 0.01,
            "error": None if provider in ready else "missing",
        }

    monkeypatch.setattr("agentic_researcher.cli.probe_provider", fake_probe)
    arguments = [
        "provider-preflight",
        "--provider",
        "claude",
        "--provider",
        "codex",
        "--provider",
        "antigravity",
        "--minimum",
        "2",
        "--require-auth",
    ]
    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] == ["claude", "codex"]
    assert result["require_auth"] is True
    assert require_auth_values == [True, True, True]

    ready.remove("claude")
    assert main(arguments) == 2
    assert "requires 2 ready" in capsys.readouterr().err


def test_shell_launcher_dispatches_machine_readable_commands() -> None:
    launcher = (REPO_ROOT / "agentic-researcher").read_text(encoding="utf-8")
    assert (
        "init-project|--init-project|curate|expand|run-batch|provider-contract|"
        "provider-preflight|prepare-curation-delta|commit-curation-delta"
        in launcher
    )
    assert "python3 -m agentic_researcher" in launcher


def test_windows_import_wires_delta_state_and_no_work_short_circuit() -> None:
    script = (
        REPO_ROOT / "automation" / "Invoke-DiscordMathImport.ps1"
    ).read_text(encoding="utf-8")

    assert 'Join-Path $script:StateRoot "curation-seen.json"' in script
    assert 'Join-Path $RunRoot "curation_input.json"' in script
    assert 'Join-Path $RunRoot "curation_delta_report.json"' in script
    assert '"prepare-curation-delta"' in script
    assert '"commit-curation-delta"' in script
    assert script.index("$seenState = Commit-CurationSeenState") > script.index(
        "Expanded research queue does not contain a tasks array."
    )
    assert '$curationStatus -ceq "no_work"' in script
    assert "Complete-NoWorkBatch" in script
    assert "providers were not invoked." in script
    assert "provider was not invoked." in script


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 parser test")
def test_windows_import_script_has_valid_powershell_syntax() -> None:
    script_path = REPO_ROOT / "automation" / "Invoke-DiscordMathImport.ps1"
    escaped = str(script_path).replace("'", "''")
    command = (
        "$parseErrors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{escaped}', [ref]$null, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -gt 0) { "
        "$parseErrors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
