# Discord Mathematics Research Pipeline

This workflow connects
[Discord Mathematics Early University](https://github.com/Erikiss/Discord-Mathematics-Early-University)
to The Agentic Researcher. The Agentic Researcher is the top-level
orchestrator: the Discord repository supplies source material, commercial
agents perform a small high-quality interpretation pass, and a local
open-weight model performs the high-volume follow-up research.

## Pipeline and trust boundary

1. The Discord adapter exports the four configured channels as an
   `agentic-researcher/ingest-bundle/v1` document. It groups related messages
   while retaining source IDs, formulas, attachment metadata, and provenance.
   Its optional `materialize_media.py` step resolves short-lived Discord image
   URLs into opaque local files and a URL-free manifest.
2. Claude Code, OpenAI Codex, and Google Antigravity independently interpret
   the same bundle. Discord text, attachment text, and formulas are untrusted
   source data, never agent instructions.
3. The deterministic merge requires a 2-of-3 consensus. Critical disagreement,
   including unresolved formula disagreement, leaves a topic as
   `needs_review`.
4. Accepted topics are expanded into synthetic foundation, worked-example,
   literature, and counterexample tasks.
5. OpenCode sends the resulting queue to a local open-weight model. Persistent
   state makes the batch safe to resume after an interrupted local or Colab
   session.

The consensus stage improves extraction quality; it is not a mathematical
proof. Every generated project still requires source verification and an
explicit verification status.

Keep the raw/private ingest bundle, materialized images, and curator run
directory out of public repositories and public CI artifacts. They can contain
Discord messages, author data, attachment text, or other sensitive context.
The provider adapter sends only the public discussion blocks and a recursively
redacted projection; it never serializes the bundle's `private` partition.
With `--media-root`, it stages only attachment-ID-matched image files under
opaque names and verifies a supplied SHA-256 before the provider can inspect
them. Publish only deliberately reviewed, provenance-preserving results.

## 1. Curate the ingest bundle

Install and authenticate all three commercial command-line tools, then run
them independently:

```bash
./agentic-researcher curate discord_exports/ingest_bundle.json \
  --provider claude \
  --provider codex \
  --provider antigravity \
  --media-root discord_exports/curation_media \
  --items-per-prompt 12 \
  --max-input-chars 24000 \
  --threshold 2 \
  --runs-dir private-runs/curation \
  --output curated_topics.json
```

The default provider commands are conservative adapters for `claude`,
`codex`, and the experimental `agy` command name used by Antigravity
installations. Inspect the normalized adapter contract with:

```bash
./agentic-researcher provider-contract
```

Override a local command spelling without changing pipeline code:

```bash
./agentic-researcher curate discord_exports/ingest_bundle.json \
  --provider claude --provider codex --provider antigravity \
  --command 'antigravity=agy -p {prompt}' \
  --threshold 2 --output curated_topics.json
```

The equivalent environment override is
`AR_PROVIDER_ANTIGRAVITY_COMMAND`. Commands may use `{prompt}`,
`{prompt_file}`, `{workspace}`, and `{output_file}` placeholders.

Large bundles are deterministically split at atomic discussion-block
boundaries. Each provider must return a valid response for every chunk before
the default quality gate passes; the per-provider responses are then combined
before the 2-of-3 topic merge. `--items-per-prompt` limits each invocation to
12 complete blocks by default, while `--max-input-chars` applies an approximate
24,000-character JSON budget. The first reached limit closes the current
chunk; a single oversized atomic block remains intact. Lower either limit for
smaller model contexts or command-line limits without changing source IDs.

If the three tools were run elsewhere, merge their checked JSON responses
without invoking a provider:

```bash
./agentic-researcher curate discord_exports/ingest_bundle.json \
  --response claude=responses/claude.json \
  --response codex=responses/codex.json \
  --response antigravity=responses/antigravity.json \
  --threshold 2 --output curated_topics.json
```

Review every `needs_review` entry in `curated_topics.json`. In particular, do
not silently choose between conflicting LaTeX formulas. The next stage skips
these entries by default.

The CLI requires one valid response from each of Claude, Codex, and Antigravity
before it merges. If one service is temporarily unavailable,
`--allow-degraded-consensus` permits an explicit fallback, but that run should
be labeled and manually reviewed; it is no longer the intended three-system
quality gate.

## 2. Expand accepted topics

```bash
./agentic-researcher expand curated_topics.json \
  --output research_queue.json
```

Each queue item is an `agentic-researcher/research-spec/v1` contract marked as
synthetic and linked back to its Discord bundle, curated topic, and source item
IDs. The expansion always creates four auditable baseline task types; agreed
curator proposals are added instead of replacing them:

- foundations and prerequisite reconstruction;
- a verified worked example;
- an accessible literature and code survey;
- assumption and counterexample audit.

`--include-needs-review` exists for controlled experiments, but unresolved
topics should normally be corrected and re-curated first.

## 3. Discover and rank mathematical evidence

The `research_pipeline` package contains offline-first adapters for cached or
exported records:

- the explorable
  [Math arXiv Data Map](https://lmcinnes.github.io/datamapplot_examples/arXiv_math/)
  for candidate discovery;
- OpenAlex for bibliographic, citation, topic, and retraction metadata;
- [Erdős Problems](https://www.erdosproblems.com/) for related problems;
- [OEIS](https://oeis.org/) for relevant integer sequences;
- the
  [Journal of Integer Sequences](https://cs.uwaterloo.ca/journals/JIS/)
  for related articles.

The published map embeds titles and abstracts with `nomic-embed` through
Sentence Transformers, reduces them with t-SNE, clusters with HDBSCAN, and
renders them with DataMapPlot. Model2Vec was a later discussion suggestion, not
the embedding used for this map. The map is a discovery interface, not a
ranking oracle: its t-SNE coordinates, apparent density, point size, and 2D
distance are visualization metadata only. It also does not supply citation
counts. Ranking therefore uses the original high-dimensional embedding or a
precomputed semantic similarity when available, then enriches candidates with
OpenAlex and considers subject overlap, verified citations, source quality,
retraction status, and prerequisite distance.

Adapters never make implicit network requests. Supply reproducible snapshots
or cached records, normalize them, and retain every scoring signal:

```python
from research_pipeline import (
    ArxivCandidateAdapter,
    ErdosProblemsAdapter,
    EvidenceRanker,
    JournalOfIntegerSequencesAdapter,
    OEISAdapter,
    OpenAlexMetadataAdapter,
    ResearchTopic,
    collect_candidates,
)

topic = ResearchTopic(
    "Chain rule",
    "Derivation and applications of the derivative of a composition",
    ("calculus", "differentiation"),
    learner_level=1,
)
papers = ArxivCandidateAdapter(arxiv_snapshot).load()
papers = OpenAlexMetadataAdapter(openalex_snapshot).enrich(papers)
related = collect_candidates(
    ErdosProblemsAdapter(erdos_snapshot),
    OEISAdapter(oeis_snapshot),
    JournalOfIntegerSequencesAdapter(jis_snapshot),
)
ranked = EvidenceRanker().rank(topic, (*papers, *related))
auditable_results = [result.to_dict() for result in ranked]
```

Open, unknown, or otherwise unsolved Erdős problems are always classified as
`research_only` with recommendation `outlook`, even if their semantic score is
high. Only `proved`, `disproved`, or `solved` problems can be automatically
recommended as learning material. A retracted work is rejected.

## 4. Run the low-cost batch

Configure OpenCode to use a local OpenAI-compatible endpoint such as vLLM, then
run:

```bash
./agentic-researcher run-batch research_queue.json \
  --work-root private-runs/projects \
  --state private-runs/batch_state.json \
  --provider opencode \
  --max-attempts 2 \
  --timeout 7200
```

Rerun the same command with the same queue and state path to resume. Successful
tasks are not repeated; incomplete and failed tasks follow the configured retry
limit. Before spending compute, initialize one project without invoking the
provider:

```bash
./agentic-researcher run-batch research_queue.json \
  --work-root private-runs/projects \
  --state private-runs/dry-run-state.json \
  --provider opencode --max-tasks 1 --dry-run
```

For Google Colab, use
[`notebooks/open_weight_bulk_research.ipynb`](../notebooks/open_weight_bulk_research.ipynb).
It mounts Google Drive for the queue, generated projects, and resumable state;
starts a local vLLM endpoint; and uses `openai/gpt-oss-20b` by default or
`openai/gpt-oss-120b` when at least 75 GiB of GPU memory is available. The
larger branch is intended for an 80 GiB A100-class runtime.

## Outputs

The principal machine-readable artifacts are:

| Artifact | Schema or role |
| --- | --- |
| `ingest_bundle.json` | `agentic-researcher/ingest-bundle/v1`; untrusted Discord source data |
| `curated_topics.json` | `agentic-researcher/curated-topics/v1`; consensus and disagreements |
| `research_queue.json` | `agentic-researcher/research-queue/v1`; synthetic research tasks |
| `batch_state.json` | `agentic-researcher/batch-state/v1`; resumable task state |
| `run-result.json` | `agentic-researcher/run-result/v1`; per-task outcome and verification |

Keep each queue together with its matching state file. The runner rejects a
state file whose queue ID or provider does not match, preventing accidental
cross-run reuse.
