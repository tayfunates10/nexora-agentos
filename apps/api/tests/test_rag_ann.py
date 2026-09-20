import pytest

from nexora_api.rag_ann import hnsw_index_name, validate_ann_target


def test_hnsw_index_name_is_stable_and_model_scoped():
    first = hnsw_index_name("text-embedding-3-small", 1536)
    second = hnsw_index_name("text-embedding-3-small", 1536)
    other = hnsw_index_name("another-model", 1536)

    assert first == second
    assert first.startswith("rag_chunks_hnsw_1536_")
    assert first != other
    assert len(first) <= 63


@pytest.mark.parametrize("dimensions", [0, 2001, 4096])
def test_hnsw_rejects_unindexable_vector_dimensions(dimensions):
    with pytest.raises(ValueError, match="HNSW vector dimensions"):
        validate_ann_target("embed-model", dimensions)
