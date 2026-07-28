import json

import pytest

from research_pipeline.evidence import (
    ArxivCandidateAdapter,
    ErdosProblemsAdapter,
    EvidenceCandidate,
    EvidenceRanker,
    EvidenceSource,
    JournalOfIntegerSequencesAdapter,
    LearningCategory,
    OEISAdapter,
    OpenAlexMetadataAdapter,
    Recommendation,
    ResearchTopic,
    collect_candidates,
)


def test_arxiv_ranking_uses_embedding_not_2d_map_distance():
    topic = ResearchTopic(
        "Chain rule",
        "Differentiating compositions of functions",
        ("calculus",),
        embedding=(1.0, 0.0, 0.0),
    )
    candidates = ArxivCandidateAdapter(
        [
            {
                "id": "2401.00001v2",
                "title": "A close dot in the map",
                "categories": ["calculus"],
                "embedding": [0.0, 1.0, 0.0],
                "map_distance_2d": 0.0001,
                "prerequisite_level": 1,
            },
            {
                "id": "2401.00002",
                "title": "Compositions and the chain rule",
                "abstract": "A calculus treatment of derivatives.",
                "categories": ["calculus"],
                "embedding": [0.98, 0.02, 0.0],
                "map_distance_2d": 9999,
                "prerequisite_level": 1,
            },
        ]
    ).load()

    ranked = EvidenceRanker().rank(topic, candidates)

    assert ranked[0].candidate.identifier == "2401.00002"
    assert ranked[0].signals["semantic"] > ranked[1].signals["semantic"]
    assert "map_distance_2d" not in ranked[0].signals
    assert candidates[0].metadata["map_distance_2d"] == pytest.approx(0.0001)


def test_precomputed_high_dimensional_similarity_is_supported():
    topic = ResearchTopic("Prime gaps", subject_tags=("number theory",))
    candidate = ArxivCandidateAdapter(
        [
            {
                "arxiv_id": "arXiv:2501.01234v3",
                "title": "Prime gaps",
                "categories": ["number theory"],
                "high_dimensional_similarity": 0.91,
                "prerequisite_level": 1,
            }
        ]
    ).load()[0]

    result = EvidenceRanker().score(topic, candidate)

    assert candidate.identifier == "2501.01234"
    assert result.signals["semantic"] == pytest.approx(0.91)
    assert "high-dimensional" in result.reasons[0]


def test_openalex_enriches_arxiv_from_injected_snapshot():
    arxiv = ArxivCandidateAdapter(
        lambda: [
            {
                "id": "2401.12345",
                "title": "The Chain Rule",
                "categories": ["calculus"],
            }
        ]
    ).load()
    loader_calls = 0

    def openalex_loader():
        nonlocal loader_calls
        loader_calls += 1
        return {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "display_name": "The Chain Rule",
                    "ids": {"arxiv": "https://arxiv.org/abs/2401.12345"},
                    "cited_by_count": 81,
                    "publication_year": 2024,
                    "concepts": [{"display_name": "Mathematical analysis"}],
                    "abstract_inverted_index": {
                        "An": [0],
                        "introduction": [1],
                        "to": [2],
                        "derivatives": [3],
                    },
                }
            ]
        }

    enriched = OpenAlexMetadataAdapter(openalex_loader).enrich(arxiv)

    assert loader_calls == 1
    assert enriched[0].source is EvidenceSource.ARXIV
    assert enriched[0].citation_count == 81
    assert enriched[0].publication_year == 2024
    assert "Mathematical analysis" in enriched[0].subject_tags
    assert enriched[0].summary == "An introduction to derivatives"
    assert enriched[0].metadata["openalex_id"] == "https://openalex.org/W123"


def test_erdos_yaml_parses_upstream_shape_and_enforces_status_policy():
    yaml_snapshot = """
- number: "4"
  status:
    state: "proved"
  formalized:
    state: "yes"
  oeis: ["A002386"]
  tags: ["number theory", "primes"]
  comments: "prime gap example"
  high_dimensional_similarity: 0.95
  prerequisite_level: 1
- number: "5"
  status:
    state: "open"
  formalized:
    state: "yes"
  oeis: ["A001223"]
  tags: ["number theory", "primes"]
  high_dimensional_similarity: 0.99
  prerequisite_level: 1
"""
    def fixture_yaml_loader(text):
        # The adapter owns I/O and schema normalization; YAML decoding is
        # injectable so PyYAML is not a mandatory runtime/test dependency.
        assert text == yaml_snapshot.strip()
        return [
            {
                "number": "4",
                "status": {"state": "proved"},
                "formalized": {"state": "yes"},
                "oeis": ["A002386"],
                "tags": ["number theory", "primes"],
                "comments": "prime gap example",
                "high_dimensional_similarity": 0.95,
                "prerequisite_level": 1,
            },
            {
                "number": "5",
                "status": {"state": "open"},
                "formalized": {"state": "yes"},
                "oeis": ["A001223"],
                "tags": ["number theory", "primes"],
                "high_dimensional_similarity": 0.99,
                "prerequisite_level": 1,
            },
        ]

    candidates = ErdosProblemsAdapter(
        lambda: yaml_snapshot, yaml_loader=fixture_yaml_loader
    ).load()
    topic = ResearchTopic(
        "Prime gaps",
        "Gaps between consecutive prime numbers",
        ("number theory", "primes"),
    )

    results = {
        result.candidate.identifier: result
        for result in EvidenceRanker().rank(topic, candidates)
    }

    assert candidates[0].oeis_ids == ("A002386",)
    assert candidates[0].formalized is True
    assert results["4"].recommendation is Recommendation.AUTO_RECOMMEND
    assert results["5"].recommendation is Recommendation.OUTLOOK
    assert results["5"].category is LearningCategory.RESEARCH_ONLY
    assert "only be shown as an outlook" in results["5"].reasons[-1]


def test_erdos_json_supports_lean_suffix_and_disproved_as_solved():
    snapshot = json.dumps(
        {
            "problems": [
                {
                    "number": 16,
                    "status": {"state": "disproved (Lean)"},
                    "tags": ["number theory"],
                    "embedding_similarity": 1,
                    "prerequisite_level": 1,
                }
            ]
        }
    )
    candidate = ErdosProblemsAdapter(snapshot).load()[0]
    topic = ResearchTopic("Number theory", subject_tags=("number theory",))

    result = EvidenceRanker(auto_recommend_threshold=0.5).score(topic, candidate)

    assert candidate.status == "disproved"
    assert result.recommendation is Recommendation.AUTO_RECOMMEND


def test_oeis_and_journal_records_are_normalized_without_network():
    oeis = OEISAdapter(
        {
            "results": [
                {
                    # OEIS JSON represents A000045 as integer 45.
                    "number": 45,
                    "name": "Fibonacci numbers",
                    "formula": "a(n) = a(n-1) + a(n-2)",
                    "keywords": ["nonn", "easy"],
                    "embedding_similarity": 0.8,
                },
                {"number": "not-an-oeis-id", "name": "discard me"},
            ]
        }
    ).load()
    journal = JournalOfIntegerSequencesAdapter(
        [
            {
                "doi": "10.1234/jis.1",
                "title": "Identities for Fibonacci numbers",
                "abstract": "Integer sequence identities.",
                "keywords": ["Fibonacci", "recurrences"],
                "oeis": ["A000045", "possible"],
                "year": 2020,
            }
        ]
    ).load()

    assert len(oeis) == 1
    assert oeis[0].identifier == "A000045"
    assert oeis[0].prerequisite_level == 1
    assert journal[0].source is EvidenceSource.JOURNAL_OF_INTEGER_SEQUENCES
    assert journal[0].oeis_ids == ("A000045",)
    assert journal[0].publication_year == 2020


@pytest.mark.parametrize(
    ("prerequisite_level", "expected"),
    [
        (1, LearningCategory.FOUNDATION),
        (2, LearningCategory.BRIDGE),
        (3, LearningCategory.EXTENSION),
        (4, LearningCategory.RESEARCH_ONLY),
    ],
)
def test_pedagogical_categories_are_relative_to_learner(
    prerequisite_level, expected
):
    topic = ResearchTopic("Calculus", learner_level=1)
    candidate = EvidenceCandidate(
        source=EvidenceSource.ARXIV,
        identifier=str(prerequisite_level),
        title="Calculus evidence",
        prerequisite_level=prerequisite_level,
    )

    assert EvidenceRanker.categorize(topic, candidate) is expected


def test_retracted_openalex_enrichment_is_rejected():
    candidate = ArxivCandidateAdapter(
        [{"id": "2401.12345", "title": "A paper", "embedding_similarity": 1}]
    ).load()[0]
    enriched = OpenAlexMetadataAdapter(
        [
            {
                "display_name": "A paper",
                "is_retracted": True,
                "cited_by_count": 1000,
            }
        ]
    ).enrich([candidate])[0]

    result = EvidenceRanker().score(ResearchTopic("A paper"), enriched)

    assert result.recommendation is Recommendation.REJECT
    assert "retracted" in result.reasons[-1]


def test_collection_is_stable_and_deduplicates_only_exact_source_ids():
    first = ArxivCandidateAdapter(
        [
            {"id": "2401.00001", "title": "First"},
            {"id": "2401.00001v2", "title": "Duplicate version"},
        ]
    )
    second = OEISAdapter(
        [{"id": "A000045", "name": "Fibonacci numbers"}]
    )

    candidates = collect_candidates(first, second)

    assert [(item.source.value, item.identifier) for item in candidates] == [
        ("arxiv", "2401.00001"),
        ("oeis", "A000045"),
    ]


def test_ranking_is_deterministic_for_tied_scores():
    topic = ResearchTopic("Topology")
    candidates = [
        EvidenceCandidate(EvidenceSource.ARXIV, "b", "Topology"),
        EvidenceCandidate(EvidenceSource.ARXIV, "A", "Topology"),
    ]
    ranker = EvidenceRanker()

    first = ranker.rank(topic, candidates)
    second = ranker.rank(topic, reversed(candidates))

    assert [result.candidate.identifier for result in first] == ["A", "b"]
    assert [result.to_dict() for result in first] == [
        result.to_dict() for result in second
    ]
