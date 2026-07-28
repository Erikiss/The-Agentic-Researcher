"""Deterministic, offline-first mathematical evidence discovery.

The adapters in this module intentionally do not perform network requests.  A
caller supplies records (or a zero-argument loader), which makes the discovery
pipeline reproducible in tests, a checked-out data snapshot, or a cache.

The ranking contract accepts cosine similarity or full embeddings from the
original high-dimensional representation.  Fields such as ``map_x``,
``map_y``, and ``map_distance_2d`` may be retained as provenance metadata, but
they are never ranking signals: a t-SNE projection is a visualization, not a
metric-preserving nearest-neighbour index.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class EvidenceSource(str, Enum):
    """Supported mathematical evidence sources."""

    ARXIV = "arxiv"
    OPENALEX = "openalex"
    ERDOS_PROBLEMS = "erdos_problems"
    OEIS = "oeis"
    JOURNAL_OF_INTEGER_SEQUENCES = "journal_of_integer_sequences"


class LearningCategory(str, Enum):
    """Pedagogical distance from the learner's current level."""

    FOUNDATION = "foundation"
    BRIDGE = "bridge"
    EXTENSION = "extension"
    RESEARCH_ONLY = "research_only"


class Recommendation(str, Enum):
    """How an evidence item may enter generated learning material."""

    AUTO_RECOMMEND = "auto_recommend"
    CANDIDATE = "candidate"
    OUTLOOK = "outlook"
    REJECT = "reject"


@dataclass(frozen=True)
class ResearchTopic:
    """A curated mathematical topic used as the evidence query."""

    title: str
    description: str = ""
    subject_tags: tuple[str, ...] = ()
    learner_level: int = 1
    embedding: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("A research topic requires a non-empty title")
        if self.learner_level < 0:
            raise ValueError("learner_level must be non-negative")


@dataclass(frozen=True)
class EvidenceCandidate:
    """A normalized record emitted by one of the offline adapters."""

    source: EvidenceSource
    identifier: str
    title: str
    summary: str = ""
    url: str = ""
    subject_tags: tuple[str, ...] = ()
    embedding: tuple[float, ...] | None = None
    embedding_similarity: float | None = None
    citation_count: int | None = None
    publication_year: int | None = None
    prerequisite_level: int = 1
    resource_type: str = ""
    status: str | None = None
    oeis_ids: tuple[str, ...] = ()
    formalized: bool = False
    source_quality: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("An evidence candidate requires an identifier")
        if not self.title.strip():
            raise ValueError("An evidence candidate requires a title")
        if self.embedding_similarity is not None and not math.isfinite(
            self.embedding_similarity
        ):
            raise ValueError("embedding_similarity must be finite")
        if self.citation_count is not None and self.citation_count < 0:
            raise ValueError("citation_count must be non-negative")
        if self.prerequisite_level < 0:
            raise ValueError("prerequisite_level must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "source": self.source.value,
            "identifier": self.identifier,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "subject_tags": list(self.subject_tags),
            "embedding": list(self.embedding) if self.embedding is not None else None,
            "embedding_similarity": self.embedding_similarity,
            "citation_count": self.citation_count,
            "publication_year": self.publication_year,
            "prerequisite_level": self.prerequisite_level,
            "resource_type": self.resource_type,
            "status": self.status,
            "oeis_ids": list(self.oeis_ids),
            "formalized": self.formalized,
            "source_quality": self.source_quality,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvidenceResult:
    """A scored candidate with a pedagogical and publication decision."""

    candidate: EvidenceCandidate
    score: float
    category: LearningCategory
    recommendation: Recommendation
    signals: Mapping[str, float]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "candidate": self.candidate.to_dict(),
            "score": self.score,
            "category": self.category.value,
            "recommendation": self.recommendation.value,
            "signals": dict(self.signals),
            "reasons": list(self.reasons),
        }


class EvidenceAdapter(Protocol):
    """Structural interface shared by all record adapters."""

    def load(self) -> tuple[EvidenceCandidate, ...]:
        """Load normalized candidates without performing implicit network I/O."""


RecordLoader = Callable[[], Any]

_ARXIV_ID = re.compile(r"(?i)(?:arxiv:|/abs/|/pdf/)?(\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?")
_OEIS_ID = re.compile(r"(?i)\bA\d{6}\b")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "auf",
        "das",
        "der",
        "die",
        "ein",
        "eine",
        "for",
        "für",
        "in",
        "is",
        "mit",
        "of",
        "on",
        "oder",
        "the",
        "to",
        "und",
        "von",
        "was",
        "with",
        "zu",
    }
)


def _resolve(loader: RecordLoader | Any) -> Any:
    return loader() if callable(loader) else loader


def _decode_document(
    value: Any,
    *,
    yaml_loader: Callable[[str], Any] | None = None,
) -> Any:
    if isinstance(value, Path):
        value = value.read_text(encoding="utf-8")
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if yaml_loader is None:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised without PyYAML
            raise ValueError(
                "YAML input requires PyYAML or an injected yaml_loader"
            ) from exc
        yaml_loader = yaml.safe_load
    return yaml_loader(text)


def _record_list(value: Any, *, container_keys: Sequence[str] = ()) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in container_keys:
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(
                nested, (str, bytes, bytearray)
            ):
                return [item for item in nested if isinstance(item, Mapping)]
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    raise TypeError(f"Expected records, received {type(value).__name__}")


def _first(record: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return default


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, Mapping):
        value = value.get("state")
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "lean"}
    return bool(value)


def _coerce_embedding(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        embedding = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not embedding or not all(math.isfinite(item) for item in embedding):
        return None
    return embedding


def _coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[,;]", value)
    elif isinstance(value, Mapping):
        values = value.keys()
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)

    tags: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            item = _first(item, "display_name", "name", "id")
        if item is None:
            continue
        tag = str(item).strip()
        if tag:
            tags.add(tag)
    return tuple(sorted(tags, key=str.casefold))


def _coerce_oeis(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)
    ids: set[str] = set()
    for item in values:
        text = str(item).strip()
        ids.update(match.upper() for match in _OEIS_ID.findall(text))
        # The public OEIS JSON export uses an integer ``number`` field rather
        # than the display identifier (for example 45 means A000045).
        if text.isdigit():
            ids.add(f"A{int(text):06d}")
        elif re.fullmatch(r"(?i)A\d{1,6}", text):
            ids.add(f"A{int(text[1:]):06d}")
    return tuple(sorted(ids))


def _similarity(record: Mapping[str, Any]) -> float | None:
    # Deliberately excludes map_distance_2d, x/y, and all t-SNE coordinates.
    return _coerce_float(
        _first(
            record,
            "embedding_similarity",
            "high_dimensional_similarity",
            "cosine_similarity",
        )
    )


def _prerequisite_level(record: Mapping[str, Any], default: int) -> int:
    raw = _first(record, "prerequisite_level", "difficulty_level", "learner_level")
    parsed = _coerce_int(raw)
    if parsed is not None:
        return max(0, parsed)
    named = str(_first(record, "difficulty", "audience", default="")).casefold()
    levels = {
        "introductory": 1,
        "undergraduate": 2,
        "advanced undergraduate": 2,
        "graduate": 3,
        "research": 4,
    }
    return levels.get(named, default)


def _arxiv_id(value: Any) -> str:
    text = str(value or "").strip()
    match = _ARXIV_ID.search(text)
    return match.group(1) if match else text.removeprefix("arXiv:")


def _doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("state")
    text = str(value or "").strip().casefold().replace("_", " ")
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    return " ".join(text.split())


class ArxivCandidateAdapter:
    """Normalize cached arXiv/map candidate records.

    Map coordinates are retained in ``metadata`` for provenance only.
    """

    def __init__(self, loader: RecordLoader | Any):
        self._loader = loader

    def load(self) -> tuple[EvidenceCandidate, ...]:
        value = _decode_document(_resolve(self._loader))
        records = _record_list(value, container_keys=("results", "papers", "records"))
        candidates: list[EvidenceCandidate] = []
        for record in records:
            identifier = _arxiv_id(_first(record, "arxiv_id", "id", "identifier"))
            title = str(_first(record, "title", default=f"arXiv {identifier}")).strip()
            tags = _coerce_tags(_first(record, "categories", "subject_tags", "tags"))
            url = str(
                _first(record, "url", "entry_id", default=f"https://arxiv.org/abs/{identifier}")
            )
            candidates.append(
                EvidenceCandidate(
                    source=EvidenceSource.ARXIV,
                    identifier=identifier,
                    title=title,
                    summary=str(_first(record, "abstract", "summary", default="")),
                    url=url,
                    subject_tags=tags,
                    embedding=_coerce_embedding(
                        _first(record, "embedding", "sentence_embedding", "vector")
                    ),
                    embedding_similarity=_similarity(record),
                    citation_count=_coerce_int(
                        _first(record, "citation_count", "cited_by_count")
                    ),
                    publication_year=_coerce_int(
                        _first(record, "publication_year", "year")
                    ),
                    prerequisite_level=_prerequisite_level(record, default=3),
                    resource_type=str(_first(record, "resource_type", "type", default="paper")),
                    source_quality=_coerce_float(record.get("source_quality")),
                    metadata=dict(record),
                )
            )
        return tuple(candidates)


def _openalex_abstract(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in value.items():
        if not isinstance(offsets, Iterable) or isinstance(offsets, (str, bytes)):
            continue
        for offset in offsets:
            parsed = _coerce_int(offset)
            if parsed is not None:
                positions.append((parsed, str(word)))
    return " ".join(word for _, word in sorted(positions))


class OpenAlexMetadataAdapter:
    """Normalize OpenAlex snapshots and enrich arXiv candidates."""

    def __init__(self, loader: RecordLoader | Any):
        self._loader = loader

    def _records(self) -> list[Mapping[str, Any]]:
        value = _decode_document(_resolve(self._loader))
        return _record_list(value, container_keys=("results", "works", "records"))

    def load(self) -> tuple[EvidenceCandidate, ...]:
        candidates: list[EvidenceCandidate] = []
        for record in self._records():
            identifier = str(_first(record, "id", "openalex_id", "doi", "title"))
            ids = record.get("ids") if isinstance(record.get("ids"), Mapping) else {}
            concepts = _coerce_tags(
                _first(record, "concepts", "topics", "keywords", default=())
            )
            candidates.append(
                EvidenceCandidate(
                    source=EvidenceSource.OPENALEX,
                    identifier=identifier,
                    title=str(_first(record, "display_name", "title", default=identifier)),
                    summary=_openalex_abstract(
                        _first(record, "abstract", "abstract_inverted_index")
                    ),
                    url=str(
                        _first(
                            record,
                            "landing_page_url",
                            "doi",
                            default=identifier if identifier.startswith("http") else "",
                        )
                    ),
                    subject_tags=concepts,
                    embedding=_coerce_embedding(
                        _first(record, "embedding", "sentence_embedding", "vector")
                    ),
                    embedding_similarity=_similarity(record),
                    citation_count=_coerce_int(record.get("cited_by_count")),
                    publication_year=_coerce_int(record.get("publication_year")),
                    prerequisite_level=_prerequisite_level(record, default=3),
                    resource_type=str(_first(record, "type", default="work")),
                    source_quality=_coerce_float(record.get("source_quality")),
                    metadata={**dict(record), "ids": dict(ids)},
                )
            )
        return tuple(candidates)

    def enrich(
        self, candidates: Iterable[EvidenceCandidate]
    ) -> tuple[EvidenceCandidate, ...]:
        """Attach OpenAlex bibliometrics to matching cached candidates.

        Matching uses arXiv ID first, DOI second, then normalized exact title.
        It never performs a remote lookup.
        """

        by_arxiv: dict[str, Mapping[str, Any]] = {}
        by_doi: dict[str, Mapping[str, Any]] = {}
        by_title: dict[str, Mapping[str, Any]] = {}
        for record in self._records():
            ids = record.get("ids") if isinstance(record.get("ids"), Mapping) else {}
            arxiv = _arxiv_id(
                _first(
                    record,
                    "arxiv_id",
                    default=_first(ids, "arxiv", default=""),
                )
            )
            doi = _doi(_first(record, "doi", default=_first(ids, "doi", default="")))
            title = str(_first(record, "display_name", "title", default="")).strip().casefold()
            if arxiv:
                by_arxiv[arxiv] = record
            if doi:
                by_doi[doi] = record
            if title:
                by_title[title] = record

        enriched: list[EvidenceCandidate] = []
        for candidate in candidates:
            candidate_doi = _doi(candidate.metadata.get("doi"))
            record = (
                by_arxiv.get(_arxiv_id(candidate.identifier))
                or (by_doi.get(candidate_doi) if candidate_doi else None)
                or by_title.get(candidate.title.strip().casefold())
            )
            if record is None:
                enriched.append(candidate)
                continue

            tags = set(candidate.subject_tags)
            tags.update(
                _coerce_tags(_first(record, "concepts", "topics", "keywords", default=()))
            )
            metadata = dict(candidate.metadata)
            metadata["openalex_id"] = _first(record, "id", "openalex_id")
            metadata["openalex_is_retracted"] = bool(record.get("is_retracted", False))
            metadata["openalex"] = dict(record)
            source_quality = candidate.source_quality
            if source_quality is None:
                source_quality = 0.85
            enriched.append(
                replace(
                    candidate,
                    summary=candidate.summary
                    or _openalex_abstract(
                        _first(record, "abstract", "abstract_inverted_index")
                    ),
                    subject_tags=tuple(sorted(tags, key=str.casefold)),
                    citation_count=_coerce_int(
                        record.get("cited_by_count"), candidate.citation_count
                    ),
                    publication_year=_coerce_int(
                        record.get("publication_year"), candidate.publication_year
                    ),
                    source_quality=max(source_quality, 0.85),
                    metadata=metadata,
                )
            )
        return tuple(enriched)


class ErdosProblemsAdapter:
    """Normalize the teorth/erdosproblems YAML or equivalent JSON snapshot."""

    def __init__(
        self,
        loader: RecordLoader | Any,
        *,
        yaml_loader: Callable[[str], Any] | None = None,
    ):
        self._loader = loader
        self._yaml_loader = yaml_loader

    def load(self) -> tuple[EvidenceCandidate, ...]:
        value = _decode_document(
            _resolve(self._loader), yaml_loader=self._yaml_loader
        )
        records = _record_list(value, container_keys=("problems", "results", "records"))
        candidates: list[EvidenceCandidate] = []
        for record in records:
            number = str(_first(record, "number", "id", "problem_number")).strip()
            status = _status(
                _first(record, "status", "informal_status", default="unknown")
            )
            comments = str(_first(record, "comments", default="")).strip()
            title = str(
                _first(
                    record,
                    "title",
                    default=(
                        f"Erdős problem #{number}: {comments}"
                        if comments
                        else f"Erdős problem #{number}"
                    ),
                )
            )
            tags = _coerce_tags(_first(record, "tags", "subject_tags"))
            oeis_ids = _coerce_oeis(record.get("oeis"))
            formalized = _coerce_bool(record.get("formalized")) or _status(
                record.get("formal_status")
            ) == "lean"
            candidates.append(
                EvidenceCandidate(
                    source=EvidenceSource.ERDOS_PROBLEMS,
                    identifier=number,
                    title=title,
                    summary=str(
                        _first(record, "statement", "description", default=comments)
                    ),
                    url=str(
                        _first(
                            record,
                            "url",
                            default=f"https://www.erdosproblems.com/{number}",
                        )
                    ),
                    subject_tags=tags,
                    embedding=_coerce_embedding(
                        _first(record, "embedding", "sentence_embedding", "vector")
                    ),
                    embedding_similarity=_similarity(record),
                    prerequisite_level=_prerequisite_level(record, default=3),
                    resource_type="problem",
                    status=status,
                    oeis_ids=oeis_ids,
                    formalized=formalized,
                    source_quality=_coerce_float(record.get("source_quality")),
                    metadata=dict(record),
                )
            )
        return tuple(candidates)


class OEISAdapter:
    """Normalize cached OEIS search/export records."""

    def __init__(self, loader: RecordLoader | Any):
        self._loader = loader

    def load(self) -> tuple[EvidenceCandidate, ...]:
        value = _decode_document(_resolve(self._loader))
        records = _record_list(value, container_keys=("results", "sequences", "records"))
        candidates: list[EvidenceCandidate] = []
        for record in records:
            identifier_matches = _coerce_oeis(
                _first(record, "oeis_id", "number", "id")
            )
            if not identifier_matches:
                continue
            identifier = identifier_matches[0]
            title = str(_first(record, "name", "title", default=identifier))
            tags = _coerce_tags(_first(record, "keywords", "tags", "subject_tags"))
            candidates.append(
                EvidenceCandidate(
                    source=EvidenceSource.OEIS,
                    identifier=identifier,
                    title=title,
                    summary=str(
                        _first(record, "description", "formula", "comments", default="")
                    ),
                    url=str(
                        _first(
                            record,
                            "url",
                            default=f"https://oeis.org/{identifier}",
                        )
                    ),
                    subject_tags=tags,
                    embedding=_coerce_embedding(
                        _first(record, "embedding", "sentence_embedding", "vector")
                    ),
                    embedding_similarity=_similarity(record),
                    citation_count=_coerce_int(record.get("citation_count")),
                    publication_year=_coerce_int(
                        _first(record, "publication_year", "year")
                    ),
                    prerequisite_level=_prerequisite_level(record, default=1),
                    resource_type="integer_sequence",
                    oeis_ids=(identifier,),
                    source_quality=_coerce_float(record.get("source_quality")),
                    metadata=dict(record),
                )
            )
        return tuple(candidates)


class JournalOfIntegerSequencesAdapter:
    """Normalize cached Journal of Integer Sequences bibliographic records."""

    def __init__(self, loader: RecordLoader | Any):
        self._loader = loader

    def load(self) -> tuple[EvidenceCandidate, ...]:
        value = _decode_document(_resolve(self._loader))
        records = _record_list(value, container_keys=("results", "articles", "records"))
        candidates: list[EvidenceCandidate] = []
        for record in records:
            identifier = str(
                _first(record, "doi", "article_id", "id", "url", "title")
            ).strip()
            title = str(_first(record, "title", default=identifier)).strip()
            candidates.append(
                EvidenceCandidate(
                    source=EvidenceSource.JOURNAL_OF_INTEGER_SEQUENCES,
                    identifier=identifier,
                    title=title,
                    summary=str(_first(record, "abstract", "summary", default="")),
                    url=str(_first(record, "url", "link", default="")),
                    subject_tags=_coerce_tags(
                        _first(record, "keywords", "tags", "subject_tags")
                    ),
                    embedding=_coerce_embedding(
                        _first(record, "embedding", "sentence_embedding", "vector")
                    ),
                    embedding_similarity=_similarity(record),
                    citation_count=_coerce_int(
                        _first(record, "citation_count", "cited_by_count")
                    ),
                    publication_year=_coerce_int(
                        _first(record, "publication_year", "year")
                    ),
                    prerequisite_level=_prerequisite_level(record, default=2),
                    resource_type="journal_article",
                    oeis_ids=_coerce_oeis(
                        _first(record, "oeis", "oeis_ids", default=())
                    ),
                    source_quality=_coerce_float(record.get("source_quality")),
                    metadata=dict(record),
                )
            )
        return tuple(candidates)


def collect_candidates(*adapters: EvidenceAdapter) -> tuple[EvidenceCandidate, ...]:
    """Load candidates in adapter order and remove exact source/id duplicates."""

    seen: set[tuple[EvidenceSource, str]] = set()
    collected: list[EvidenceCandidate] = []
    for adapter in adapters:
        for candidate in adapter.load():
            key = (candidate.source, candidate.identifier.casefold())
            if key in seen:
                continue
            seen.add(key)
            collected.append(candidate)
    return tuple(collected)


_DEFAULT_SOURCE_QUALITY: Mapping[EvidenceSource, float] = {
    EvidenceSource.ARXIV: 0.80,
    EvidenceSource.OPENALEX: 0.85,
    EvidenceSource.ERDOS_PROBLEMS: 0.75,
    EvidenceSource.OEIS: 0.90,
    EvidenceSource.JOURNAL_OF_INTEGER_SEQUENCES: 0.85,
}
_SOLVED_ERDOS_STATES = frozenset({"proved", "disproved", "solved"})


def _tokens(*values: str) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(
            token
            for token in (part.casefold() for part in _TOKEN.findall(value))
            if len(token) > 1 and token not in _STOP_WORDS
        )
    return tokens


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    left_norm = math.sqrt(sum(item * item for item in left))
    right_norm = math.sqrt(sum(item * item for item in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _bounded(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, value))


class EvidenceRanker:
    """Rank evidence with auditable, non-2D signals."""

    DEFAULT_WEIGHTS: Mapping[str, float] = {
        "semantic": 0.45,
        "subject": 0.20,
        "lexical": 0.10,
        "citations": 0.10,
        "source_quality": 0.10,
        "pedagogy": 0.05,
    }

    def __init__(
        self,
        *,
        weights: Mapping[str, float] | None = None,
        auto_recommend_threshold: float = 0.55,
        minimum_candidate_score: float = 0.15,
    ):
        chosen = dict(weights or self.DEFAULT_WEIGHTS)
        if set(chosen) != set(self.DEFAULT_WEIGHTS):
            raise ValueError(
                f"weights must contain exactly {sorted(self.DEFAULT_WEIGHTS)}"
            )
        if any(value < 0 or not math.isfinite(value) for value in chosen.values()):
            raise ValueError("weights must be finite and non-negative")
        total = sum(chosen.values())
        if total <= 0:
            raise ValueError("at least one ranking weight must be positive")
        self.weights = {name: value / total for name, value in chosen.items()}
        self.auto_recommend_threshold = _bounded(auto_recommend_threshold)
        self.minimum_candidate_score = _bounded(minimum_candidate_score)

    @staticmethod
    def categorize(
        topic: ResearchTopic, candidate: EvidenceCandidate
    ) -> LearningCategory:
        """Classify prerequisite distance relative to the target learner."""

        if (
            candidate.source is EvidenceSource.ERDOS_PROBLEMS
            and _status(candidate.status) not in _SOLVED_ERDOS_STATES
        ):
            return LearningCategory.RESEARCH_ONLY
        gap = candidate.prerequisite_level - topic.learner_level
        if gap <= 0:
            return LearningCategory.FOUNDATION
        if gap == 1:
            return LearningCategory.BRIDGE
        if gap == 2:
            return LearningCategory.EXTENSION
        return LearningCategory.RESEARCH_ONLY

    @staticmethod
    def _semantic_signal(
        topic: ResearchTopic, candidate: EvidenceCandidate
    ) -> tuple[float, bool]:
        similarity = candidate.embedding_similarity
        if similarity is None and topic.embedding is not None and candidate.embedding is not None:
            similarity = _cosine(topic.embedding, candidate.embedding)
        return _bounded(similarity), similarity is not None

    @staticmethod
    def _subject_signal(
        topic: ResearchTopic, candidate: EvidenceCandidate
    ) -> float:
        topic_tags = {tag.strip().casefold() for tag in topic.subject_tags if tag.strip()}
        candidate_tags = {
            tag.strip().casefold() for tag in candidate.subject_tags if tag.strip()
        }
        if not topic_tags or not candidate_tags:
            return 0.0
        return len(topic_tags & candidate_tags) / len(topic_tags | candidate_tags)

    @staticmethod
    def _lexical_signal(
        topic: ResearchTopic, candidate: EvidenceCandidate
    ) -> float:
        topic_tokens = _tokens(topic.title, topic.description, *topic.subject_tags)
        candidate_tokens = _tokens(
            candidate.title, candidate.summary, *candidate.subject_tags
        )
        if not topic_tokens or not candidate_tokens:
            return 0.0
        return len(topic_tokens & candidate_tokens) / len(topic_tokens)

    @staticmethod
    def _citation_signal(candidate: EvidenceCandidate) -> float:
        if candidate.citation_count is None:
            return 0.0
        return min(1.0, math.log1p(candidate.citation_count) / math.log1p(1000))

    @staticmethod
    def _pedagogy_signal(category: LearningCategory) -> float:
        return {
            LearningCategory.FOUNDATION: 1.0,
            LearningCategory.BRIDGE: 0.75,
            LearningCategory.EXTENSION: 0.35,
            LearningCategory.RESEARCH_ONLY: 0.10,
        }[category]

    @staticmethod
    def _is_retracted(candidate: EvidenceCandidate) -> bool:
        return bool(
            candidate.metadata.get("is_retracted")
            or candidate.metadata.get("openalex_is_retracted")
        )

    def score(
        self, topic: ResearchTopic, candidate: EvidenceCandidate
    ) -> EvidenceResult:
        """Score one candidate and return all contributing signals."""

        category = self.categorize(topic, candidate)
        semantic, has_embedding_signal = self._semantic_signal(topic, candidate)
        source_quality = _bounded(
            candidate.source_quality
            if candidate.source_quality is not None
            else _DEFAULT_SOURCE_QUALITY[candidate.source]
        )
        if candidate.formalized:
            source_quality = min(1.0, source_quality + 0.10)
        signals = {
            "semantic": semantic,
            "subject": self._subject_signal(topic, candidate),
            "lexical": self._lexical_signal(topic, candidate),
            "citations": self._citation_signal(candidate),
            "source_quality": source_quality,
            "pedagogy": self._pedagogy_signal(category),
        }
        score = round(
            sum(self.weights[name] * value for name, value in signals.items()), 6
        )

        reasons: list[str] = []
        if has_embedding_signal:
            reasons.append("uses original high-dimensional semantic similarity")
        else:
            reasons.append("no high-dimensional similarity supplied")
        if candidate.citation_count is not None:
            reasons.append(f"citation count: {candidate.citation_count}")
        reasons.append(f"pedagogical category: {category.value}")

        normalized_status = _status(candidate.status)
        if self._is_retracted(candidate):
            recommendation = Recommendation.REJECT
            reasons.append("source metadata marks this work as retracted")
        elif candidate.source is EvidenceSource.ERDOS_PROBLEMS and (
            normalized_status not in _SOLVED_ERDOS_STATES
        ):
            recommendation = Recommendation.OUTLOOK
            reasons.append(
                f"Erdős status '{normalized_status or 'unknown'}' is not solved; "
                "it may only be shown as an outlook"
            )
        elif category is LearningCategory.RESEARCH_ONLY:
            recommendation = Recommendation.OUTLOOK
            reasons.append("prerequisites exceed the configured learner range")
        elif score >= self.auto_recommend_threshold:
            recommendation = Recommendation.AUTO_RECOMMEND
            if candidate.source is EvidenceSource.ERDOS_PROBLEMS:
                reasons.append(
                    f"Erdős status '{normalized_status}' is eligible for learning use"
                )
        elif score >= self.minimum_candidate_score:
            recommendation = Recommendation.CANDIDATE
        else:
            recommendation = Recommendation.REJECT
            reasons.append("insufficient evidence score")

        return EvidenceResult(
            candidate=candidate,
            score=score,
            category=category,
            recommendation=recommendation,
            signals=signals,
            reasons=tuple(reasons),
        )

    def rank(
        self,
        topic: ResearchTopic,
        candidates: Iterable[EvidenceCandidate],
    ) -> tuple[EvidenceResult, ...]:
        """Rank candidates deterministically by score, source, and identifier."""

        results = [self.score(topic, candidate) for candidate in candidates]
        results.sort(
            key=lambda result: (
                -result.score,
                result.candidate.source.value,
                result.candidate.identifier.casefold(),
            )
        )
        return tuple(results)
