import pytest

from nexora_api.retrieval_eval import RetrievalEvalCase, score_retrieval


def cases():
    return (
        RetrievalEvalCase(
            case_id="exact-code",
            query_embedding=(1.0, 0.0, 0.0),
            expected_source_keys=frozenset({"incident"}),
        ),
        RetrievalEvalCase(
            case_id="two-relevant",
            query_embedding=(0.0, 1.0, 0.0),
            expected_source_keys=frozenset({"handbook", "runbook"}),
        ),
    )


def test_retrieval_eval_reports_hit_recall_and_mrr():
    report = score_retrieval(
        cases(),
        {
            "exact-code": ("noise", "incident"),
            "two-relevant": ("handbook", "noise", "runbook"),
        },
        k=3,
    )

    assert report.case_count == 2
    assert report.hit_rate == 1.0
    assert report.mean_recall == 1.0
    assert report.mean_reciprocal_rank == pytest.approx(0.75)
    assert report.cases[0].reciprocal_rank == 0.5


def test_retrieval_eval_deduplicates_ranked_sources_before_scoring():
    report = score_retrieval(
        (
            RetrievalEvalCase(
                case_id="dedupe",
                query_embedding=(1.0,),
                expected_source_keys=frozenset({"source-a", "source-b"}),
            ),
        ),
        {"dedupe": ("source-a", "source-a", "source-b")},
        k=2,
    )

    assert report.mean_recall == 1.0
    assert report.cases[0].ranked_source_keys == ("source-a", "source-b")


@pytest.mark.parametrize(
    "rankings",
    [{}, {"exact-code": ("incident",)}],
)
def test_retrieval_eval_requires_exact_case_coverage(rankings):
    with pytest.raises(ValueError, match="exactly one result"):
        score_retrieval(cases(), rankings, k=3)
