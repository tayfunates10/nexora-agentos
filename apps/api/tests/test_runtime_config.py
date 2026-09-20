import asyncio
import json

import pytest

from nexora_api.config import Settings
from nexora_api.model_routing import ModelCapability
from nexora_api.runtime_config import RuntimeConfigError, load_runtime_config
from nexora_api.worker_service import (
    build_mcp_adapters,
    build_provider_adapters,
    build_retriever,
    close_mcp_adapters,
    close_retriever,
    load_worker_config,
)

WORKSPACE = "11111111-1111-1111-1111-111111111111"


def document(**overrides):
    base = {
        "model_candidates": [
            {
                "provider": "openai",
                "model": "operator-model",
                "capabilities": ["text", "tools"],
                "quality_tier": 2,
                "estimated_cost_per_million_tokens": 5,
            }
        ],
        "profiles": {
            "default": {
                "allowed_workspaces": [WORKSPACE],
                "allowed_providers": ["openai"],
                "allowed_tools": ["lookup"],
                "max_steps": 4,
            }
        },
    }
    base.update(overrides)
    return base


def write(tmp_path, content):
    path = tmp_path / "runtime.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_operator_configuration_builds_routing_and_profiles(tmp_path):
    config = load_runtime_config(write(tmp_path, document()))

    candidate = config.candidates()[0]
    profile = config.execution_profiles()["default"]

    assert candidate.provider == "openai"
    assert candidate.capabilities == frozenset({ModelCapability.TEXT, ModelCapability.TOOLS})
    assert profile.allowed_tools == frozenset({"lookup"})
    assert profile.max_steps == 4
    assert config.providers == frozenset({"openai"})
    assert config.model_capabilities("openai")["operator-model"] == candidate.capabilities


@pytest.mark.parametrize(
    "mutation",
    [
        {"profiles": {}},
        {"model_candidates": []},
        {"profiles": {"default": {"allowed_workspaces": [], "allowed_providers": ["openai"]}}},
        {
            "profiles": {
                "default": {"allowed_workspaces": [WORKSPACE], "allowed_providers": ["anthropic"]}
            }
        },
        {
            "profiles": {
                "default": {
                    "allowed_workspaces": [WORKSPACE],
                    "allowed_providers": ["openai"],
                    "allowed_tools": ["Not A Tool"],
                }
            }
        },
        {
            "profiles": {
                "default": {
                    "allowed_workspaces": ["not-a-uuid"],
                    "allowed_providers": ["openai"],
                }
            }
        },
        {
            "profiles": {
                "default": {
                    "allowed_workspaces": [WORKSPACE],
                    "allowed_providers": ["openai"],
                    "max_steps": 99,
                }
            }
        },
        {"model_candidates": [{"provider": "openai", "model": "m", "capabilities": []}]},
    ],
)
def test_unusable_configuration_is_refused(tmp_path, mutation):
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, document(**mutation)))


def test_credentials_cannot_be_declared_in_the_config_file(tmp_path):
    # Secrets belong to the process environment; an unknown key must not be ignored.
    invalid = document()
    invalid["api_key"] = "sk-should-never-be-here"

    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, invalid))


def test_duplicate_candidates_are_refused(tmp_path):
    invalid = document()
    invalid["model_candidates"] = invalid["model_candidates"] * 2

    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, invalid))


@pytest.mark.parametrize("content", ["", "[]", "{", "not json"])
def test_malformed_files_are_refused(tmp_path, content):
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, content))


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(tmp_path / "absent.json")


def test_worker_without_configured_profiles_refuses_to_start():
    with pytest.raises(RuntimeConfigError, match="NEXORA_WORKER_RUNTIME_CONFIG"):
        load_worker_config(Settings(worker_runtime_config=None))


def test_provider_adapter_requires_its_credential(tmp_path):
    config = load_runtime_config(write(tmp_path, document()))

    with pytest.raises(RuntimeConfigError, match="NEXORA_OPENAI_API_KEY"):
        build_provider_adapters(Settings(openai_api_key=None), config)

    adapters = build_provider_adapters(Settings(openai_api_key="sk-operator-test"), config)

    assert adapters["openai"].name == "openai"
    assert adapters["openai"].capabilities("operator-model")


def test_unsupported_provider_has_no_adapter(tmp_path):
    config = load_runtime_config(
        write(
            tmp_path,
            document(
                model_candidates=[
                    {"provider": "acme", "model": "m-1", "capabilities": ["text"]},
                ],
                profiles={
                    "default": {
                        "allowed_workspaces": [WORKSPACE],
                        "allowed_providers": ["acme"],
                    }
                },
            ),
        )
    )

    with pytest.raises(RuntimeConfigError, match="no adapter is available"):
        build_provider_adapters(Settings(), config)


def test_retrieval_is_opt_in_and_uses_operator_configuration(tmp_path):
    disabled = load_runtime_config(write(tmp_path, document()))
    assert build_retriever(Settings(openai_api_key=None), disabled) is None

    enabled_doc = document(
        retrieval={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 768,
            "limit": 6,
            "batch_size": 64,
            "timeout_seconds": 9,
        }
    )
    enabled = load_runtime_config(write(tmp_path, enabled_doc))

    with pytest.raises(RuntimeConfigError, match="NEXORA_OPENAI_API_KEY"):
        build_retriever(Settings(openai_api_key=None), enabled)

    retriever = build_retriever(Settings(openai_api_key="sk-operator-test"), enabled)
    assert retriever is not None
    assert retriever.embedding_model == "text-embedding-3-small"
    assert retriever.dimensions == 768
    assert retriever.retrieval_limit == 6
    assert retriever.batch_size == 64
    assert retriever.timeout_seconds == 9
    asyncio.run(close_retriever(retriever))


def test_evaluation_judge_requires_pinned_structured_output_model(tmp_path):
    invalid = document(
        evaluation_judge={
            "provider": "openai",
            "model": "operator-model",
            "allowed_workspaces": [WORKSPACE],
        }
    )
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, invalid))

    valid = document(
        model_candidates=[
            {
                "provider": "openai",
                "model": "operator-model",
                "capabilities": ["text", "tools"],
            },
            {
                "provider": "openai",
                "model": "judge-model-v1",
                "capabilities": ["text", "structured_output"],
            },
        ],
        evaluation_judge={
            "provider": "openai",
            "model": "judge-model-v1",
            "allowed_workspaces": [WORKSPACE],
            "prompt_version": "nexora-eval-judge-v1",
            "timeout_seconds": 20,
            "max_output_tokens": 256,
            "max_input_chars": 50000,
        },
    )
    config = load_runtime_config(write(tmp_path, valid))
    assert config.evaluation_judge is not None
    assert config.evaluation_judge.model == "judge-model-v1"
    assert config.evaluation_judge.prompt_version == "nexora-eval-judge-v1"


def test_evaluation_judge_rejects_unknown_prompt_or_duplicate_workspace(tmp_path):
    candidate = {
        "provider": "openai",
        "model": "judge-model-v1",
        "capabilities": ["text", "structured_output"],
    }
    for judge in (
        {
            "provider": "openai",
            "model": "judge-model-v1",
            "allowed_workspaces": [WORKSPACE],
            "prompt_version": "unreviewed-v2",
        },
        {
            "provider": "openai",
            "model": "judge-model-v1",
            "allowed_workspaces": [WORKSPACE, WORKSPACE],
        },
    ):
        with pytest.raises(RuntimeConfigError):
            load_runtime_config(
                write(
                    tmp_path,
                    document(
                        model_candidates=[candidate],
                        profiles={
                            "default": {
                                "allowed_workspaces": [WORKSPACE],
                                "allowed_providers": ["openai"],
                            }
                        },
                        evaluation_judge=judge,
                    ),
                )
            )


@pytest.mark.parametrize(
    "retrieval",
    [
        {"provider": "other", "model": "embed", "dimensions": 768},
        {"provider": "openai", "model": "embed", "dimensions": 0},
        {"provider": "openai", "model": "embed", "limit": 51},
        {"provider": "openai", "model": "embed", "batch_size": 0},
    ],
)
def test_invalid_retrieval_configuration_is_refused(tmp_path, retrieval):
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, document(retrieval=retrieval)))


def test_mcp_servers_are_operator_allowlisted_and_credentials_stay_in_env(tmp_path, monkeypatch):
    disabled = load_runtime_config(write(tmp_path, document()))
    assert build_mcp_adapters(disabled) == {}

    configured = load_runtime_config(
        write(
            tmp_path,
            document(
                mcp_servers={
                    "ops": {
                        "transport": "streamable_http",
                        "url": "https://mcp.example.test/mcp",
                        "bearer_token_env": "NEXORA_MCP_OPS_TOKEN",
                        "timeout_seconds": 9,
                        "max_response_bytes": 65536,
                    }
                }
            ),
        )
    )

    with pytest.raises(RuntimeConfigError, match="NEXORA_MCP_OPS_TOKEN"):
        build_mcp_adapters(configured)

    monkeypatch.setenv("NEXORA_MCP_OPS_TOKEN", "operator-only-secret")
    adapters = build_mcp_adapters(configured)
    adapter = adapters["ops"]
    assert adapter.endpoint.url == "https://mcp.example.test/mcp"
    assert adapter.endpoint.bearer_token == "operator-only-secret"
    assert adapter.endpoint.timeout_seconds == 9
    assert adapter.endpoint.max_response_bytes == 65536
    asyncio.run(close_mcp_adapters(adapters))


@pytest.mark.parametrize(
    "mcp_servers",
    [
        {"Bad Key": {"url": "https://mcp.example.test/mcp"}},
        {"ops": {"transport": "stdio", "url": "https://mcp.example.test/mcp"}},
        {"ops": {"url": "http://mcp.example.test/mcp"}},
        {"ops": {"url": "https://localhost/mcp"}},
        {"ops": {"url": "https://10.0.0.1/mcp"}},
        {"ops": {"url": "https://mcp.example.test:8443/mcp"}},
        {"ops": {"url": "https://mcp.example.test/mcp?token=secret"}},
        {
            "ops": {
                "url": "https://mcp.example.test/mcp",
                "bearer_token_env": "UNSCOPED_TOKEN",
            }
        },
    ],
)
def test_invalid_mcp_server_configuration_is_refused(tmp_path, mcp_servers):
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(write(tmp_path, document(mcp_servers=mcp_servers)))
