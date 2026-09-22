import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from nexora_api.agent_catalog import CHANNEL_SCOPE, OFFERABLE_STATUSES, Channel
from nexora_api.agent_manifest import (
    AgentManifest,
    Version,
    compare_versions,
    define_agent,
    latest,
    load_manifest,
)
from nexora_api.integration_manifest import (
    ConnectorManifest,
    define_integration,
    load_connector,
    secure_https_url,
)
from nexora_api.platform_admin import ROLLOUT_BUCKETS, rollout_bucket

REPOSITORY = Path(__file__).resolve().parents[3]
AGENTS = sorted((REPOSITORY / "agents").glob("*/manifest.json"))
CONNECTORS = sorted((REPOSITORY / "connectors").glob("*/connector.json"))

BASE = {
    "id": "nexora.social-media",
    "slug": "social-media",
    "name": "Social Media Management",
    "description": "Manage connected social accounts.",
    "category": "marketing",
    "icon": "megaphone",
    "version": "1.0.0",
    "system_instructions": "Work only from tool output.",
}


# ------------------------------------------------------------------ semantic versions
@pytest.mark.parametrize("value", ["1", "1.0", "1.0.0.0", "v1.0.0", "1.0.0-beta", "01.0.0", ""])
def test_a_version_must_be_a_plain_semantic_version(value):
    with pytest.raises(ValueError):
        Version(value)


def test_versions_order_numerically_not_lexically():
    # The ordering that a text column would get wrong, and the reason the numeric parts
    # are stored alongside the string.
    assert Version("1.9.0") < Version("1.10.0")
    assert latest(["1.9.0", "1.10.0", "1.10.1", "2.0.0"]) == "2.0.0"
    assert compare_versions("1.10.0", "1.9.0") == 1
    assert compare_versions("1.2.3", "1.2.3") == 0


@pytest.mark.parametrize(
    ("previous", "released", "kind"),
    [("1.4.2", "1.4.3", "patch"), ("1.4.2", "1.5.0", "minor"), ("1.4.2", "2.0.0", "major")],
)
def test_bump_kind_follows_semantic_versioning(previous, released, kind):
    assert Version(released).bump_kind(Version(previous)) == kind


def test_a_runtime_serves_a_version_only_at_or_above_its_minimum():
    assert Version("2.7.0").satisfies_minimum(Version("1.0.0"))
    assert Version("2.7.0").satisfies_minimum(Version("2.7.0"))
    assert not Version("2.7.0").satisfies_minimum(Version("3.0.0"))


# ------------------------------------------------------------------- agent manifests
def test_a_manifest_states_its_integrations_and_approval_gates():
    manifest = define_agent(
        **BASE,
        required_integrations=["instagram"],
        optional_integrations=["facebook"],
        capabilities=["social.analytics.read"],
        approval_required=["social.post.publish"],
    )
    assert manifest.integrations() == [("instagram", True), ("facebook", False)]
    assert manifest.requires_approval("social.post.publish")
    assert not manifest.requires_approval("social.analytics.read")


@pytest.mark.parametrize(
    "override",
    [
        {"id": "social-media"},
        {"id": "nexora.something-else"},
        {"version": "1.0"},
        {"required_integrations": ["instagram"], "optional_integrations": ["instagram"]},
        {"required_tools": ["a.b"], "optional_tools": ["a.b"]},
        {"capabilities": ["Not A Capability"]},
        {"approval_required": ["social.post.publish", "social.post.publish"]},
        {"required_integrations": ["Instagram"]},
        {"settings_schema": {"type": "array"}},
        {"reasoning": {"max_steps": 3, "escalate_after_steps": 5}},
    ],
)
def test_an_incoherent_manifest_is_refused(override):
    with pytest.raises(ValueError):
        define_agent(**{**BASE, **override})


def test_a_manifest_carries_no_credentials():
    """An agent declares which integration it needs, never how to authenticate to it."""
    fields = set(AgentManifest.model_fields)
    assert not {
        field
        for field in fields
        if any(word in field for word in ("secret", "token", "password", "credential", "key"))
    }


@pytest.mark.parametrize("path", AGENTS, ids=lambda path: path.parent.name)
def test_every_published_agent_package_validates(path):
    manifest = load_manifest(json.loads(path.read_text()))
    assert manifest.slug == path.parent.name
    assert manifest.id.endswith("." + manifest.slug)
    # A published package carries a release state a tenant could actually be offered.
    assert manifest.status in ("stable", "beta")


def test_the_reference_social_media_agent_matches_its_specification():
    manifest = load_manifest(
        json.loads((REPOSITORY / "agents/social-media/manifest.json").read_text())
    )
    assert manifest.required_integrations == ["instagram"]
    assert set(manifest.optional_integrations) == {"facebook", "google-analytics"}
    assert {
        "social.analytics.read",
        "social.content.generate",
        "social.content.plan",
        "social.comments.read",
        "social.comments.classify",
        "social.messages.read",
    } <= set(manifest.capabilities)
    # Everything that speaks in public goes through a person first.
    assert set(manifest.approval_required) == {
        "social.post.publish",
        "social.comment.reply",
        "social.message.send",
        "social.post.delete",
    }


def test_the_reporting_agent_requires_no_integration():
    manifest = load_manifest(
        json.loads((REPOSITORY / "agents/reporting/manifest.json").read_text())
    )
    assert manifest.required_integrations == []
    assert len(manifest.optional_integrations) >= 4


def test_the_advertising_agent_gates_every_spend_change():
    manifest = load_manifest(
        json.loads((REPOSITORY / "agents/advertising/manifest.json").read_text())
    )
    assert all(capability.endswith((".read", ".propose")) for capability in manifest.capabilities)
    assert {"ads.budget.update", "ads.campaign.update"} <= set(manifest.approval_required)


# --------------------------------------------------------------- connector manifests
def test_a_connector_declares_fields_but_never_values():
    connector = define_integration(
        id="mikro",
        name="Mikro ERP",
        description="Stock and invoice records.",
        category="erp",
        icon="database",
        version="1.0.0",
        auth="api_key",
        capabilities=["stock.read"],
        credential_fields=[
            {"key": "api_key", "label": "Service API key", "secret": True, "required": True},
            {"key": "base_url", "label": "Service URL", "secret": False, "required": True},
        ],
    )
    assert connector.secret_field_keys == frozenset({"api_key"})
    connector.validate_credential({"api_key": "k", "base_url": "https://erp.example.com"})
    with pytest.raises(ValueError):
        connector.validate_credential({"base_url": "https://erp.example.com"})
    with pytest.raises(ValueError):
        connector.validate_credential({"api_key": "k", "unexpected": "value"})


def test_an_oauth_connector_must_declare_its_flow():
    with pytest.raises(ValueError):
        define_integration(
            id="instagram",
            name="Instagram",
            description="Instagram Graph API.",
            category="social",
            icon="instagram",
            version="1.0.0",
            auth="oauth2",
            capabilities=["media.read"],
        )


def test_an_api_key_connector_must_ask_for_a_key():
    with pytest.raises(ValueError):
        define_integration(
            id="erp",
            name="ERP",
            description="Generic ERP.",
            category="erp",
            icon="database",
            version="1.0.0",
            auth="api_key",
            capabilities=["records.read"],
        )


def test_a_connector_endpoint_cannot_escape_its_base_url():
    for path in ("../admin", "https://elsewhere.example/x", "relative"):
        with pytest.raises(ValueError):
            define_integration(
                id="custom-rest",
                name="Custom",
                description="Tenant API.",
                category="custom",
                icon="plug",
                version="1.0.0",
                auth="bearer_token",
                capabilities=["request.read"],
                base_url="https://api.example.com",
                credential_fields=[
                    {"key": "access_token", "label": "Token", "secret": True, "required": True}
                ],
                credential_placement={"kind": "bearer_header", "value_field": "access_token"},
                endpoints=[{"capability": "request.read", "method": "GET", "path": path}],
            )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "https://localhost/api",
        "https://127.0.0.1/api",
        "https://10.0.0.5/api",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@api.example.com",
        "https://api.example.com?token=x",
        "https://api.example.com:8443",
    ],
)
def test_a_base_url_that_could_reach_inside_is_refused(url):
    with pytest.raises(ValueError):
        secure_https_url(url, "base URL")


def test_a_connector_carries_no_client_secret():
    """Operator OAuth client credentials are process configuration, not registry data."""
    assert not {
        field for field in ConnectorManifest.model_fields if "client" in field or "secret" in field
    }


@pytest.mark.parametrize("path", CONNECTORS, ids=lambda path: path.parent.name)
def test_every_published_connector_package_validates(path):
    connector = load_connector(json.loads(path.read_text()))
    assert connector.id == path.parent.name
    if connector.test_capability:
        # A declared probe has to be a request the runtime can actually make.
        assert connector.endpoint_for(connector.test_capability) is not None


def test_agent_packages_only_reference_published_connectors():
    published = {path.parent.name for path in CONNECTORS}
    for path in AGENTS:
        manifest = load_manifest(json.loads(path.read_text()))
        referenced = {name for name, _ in manifest.integrations()}
        assert referenced <= published, f"{manifest.slug} references {referenced - published}"


# ------------------------------------------------------------- channels and rollouts
def test_a_channel_serves_itself_and_everything_more_stable():
    assert CHANNEL_SCOPE[Channel.STABLE] == ("stable",)
    assert CHANNEL_SCOPE[Channel.BETA] == ("stable", "beta")
    assert CHANNEL_SCOPE[Channel.CANARY] == ("stable", "beta", "canary")
    # Draft and disabled versions are never handed to a tenant.
    assert set(OFFERABLE_STATUSES) == {"stable", "beta"}


def test_a_rollout_bucket_is_stable_and_evenly_spread():
    agent = uuid4()
    workspace = uuid4()
    assert rollout_bucket(agent, workspace) == rollout_bucket(agent, workspace)
    # Two agents stage independently, so one tenant is not always first or always last.
    assert any(
        rollout_bucket(uuid4(), workspace) != rollout_bucket(agent, workspace) for _ in range(20)
    )

    buckets = [rollout_bucket(agent, UUID(int=index)) for index in range(2000)]
    assert 0 <= min(buckets) and max(buckets) < ROLLOUT_BUCKETS
    included = sum(bucket < 5 for bucket in buckets)
    assert 50 <= included <= 150, f"a 5 percent rollout reached {included} of 2000 workspaces"


def test_widening_a_rollout_never_drops_a_tenant_already_inside():
    agent = uuid4()
    workspaces = [UUID(int=index) for index in range(500)]
    reached = {
        percentage: {w for w in workspaces if rollout_bucket(agent, w) < percentage}
        for percentage in (5, 25, 50, 100)
    }
    assert reached[5] <= reached[25] <= reached[50] <= reached[100] == set(workspaces)


def test_mutating_connector_endpoint_must_declare_its_side_effect():
    with pytest.raises(ValueError, match="side_effect"):
        define_integration(
            id="writer",
            name="Writer",
            description="Writes records.",
            category="custom",
            icon="plug",
            version="1.0.0",
            auth="bearer_token",
            capabilities=["records.create"],
            credential_fields=[
                {"key": "access_token", "label": "Token", "secret": True, "required": True}
            ],
            base_url="https://api.example.com",
            credential_placement={"kind": "bearer_header", "value_field": "access_token"},
            endpoints=[{"capability": "records.create", "method": "POST", "path": "/records"}],
        )


def test_side_effecting_connector_endpoint_requires_an_idempotency_argument():
    with pytest.raises(ValueError, match="mutation_requires_idempotency_key"):
        define_integration(
            id="writer",
            name="Writer",
            description="Writes records.",
            category="custom",
            icon="plug",
            version="1.0.0",
            auth="bearer_token",
            capabilities=["records.create"],
            credential_fields=[
                {"key": "access_token", "label": "Token", "secret": True, "required": True}
            ],
            base_url="https://api.example.com",
            credential_placement={"kind": "bearer_header", "value_field": "access_token"},
            endpoints=[
                {
                    "capability": "records.create",
                    "method": "POST",
                    "path": "/records",
                    "side_effect": "write",
                    "retry": "never",
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                    "body_map": {"name": "name"},
                }
            ],
        )


def test_connector_request_mapping_can_only_use_declared_agent_inputs():
    with pytest.raises(ValueError, match="unknown input field"):
        define_integration(
            id="reader",
            name="Reader",
            description="Reads records.",
            category="custom",
            icon="plug",
            version="1.0.0",
            auth="bearer_token",
            capabilities=["records.read"],
            credential_fields=[
                {"key": "access_token", "label": "Token", "secret": True, "required": True}
            ],
            base_url="https://api.example.com",
            credential_placement={"kind": "bearer_header", "value_field": "access_token"},
            endpoints=[
                {
                    "capability": "records.read",
                    "method": "GET",
                    "path": "/records",
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer", "minimum": 1}},
                    },
                    "query_map": {"pageSize": "missing"},
                }
            ],
        )


def test_wordpress_create_is_an_explicit_governed_external_action():
    connector = load_connector(
        json.loads((REPOSITORY / "connectors/wordpress/connector.json").read_text())
    )
    endpoint = connector.endpoint_for("posts.create")
    assert endpoint is not None
    assert endpoint.method == "POST"
    assert endpoint.side_effect == "external_communication"
    assert endpoint.retry == "never"
    assert "idempotency_key" in endpoint.input_schema["required"]
    assert endpoint.body_map == {"title": "title", "content": "content", "status": "status"}
