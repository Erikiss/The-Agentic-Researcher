"""Reusable building blocks for automated research pipelines."""

from .evidence import (
    ArxivCandidateAdapter,
    ErdosProblemsAdapter,
    EvidenceCandidate,
    EvidenceRanker,
    EvidenceResult,
    EvidenceSource,
    JournalOfIntegerSequencesAdapter,
    LearningCategory,
    OEISAdapter,
    OpenAlexMetadataAdapter,
    Recommendation,
    ResearchTopic,
    collect_candidates,
)

__all__ = [
    "ArxivCandidateAdapter",
    "ErdosProblemsAdapter",
    "EvidenceCandidate",
    "EvidenceRanker",
    "EvidenceResult",
    "EvidenceSource",
    "JournalOfIntegerSequencesAdapter",
    "LearningCategory",
    "OEISAdapter",
    "OpenAlexMetadataAdapter",
    "Recommendation",
    "ResearchTopic",
    "collect_candidates",
]
