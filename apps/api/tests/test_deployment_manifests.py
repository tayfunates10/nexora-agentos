"""Deployment manifests are platform contracts, so the rules they encode are tested.

These assertions are the deployment equivalents of the boundaries the runtime already
enforces: least privilege, no credential in source, probes that fail the right way, and
an egress policy a compromised tool cannot use to reach internal services.
"""

from pathlib import Path

import pytest
import yaml

MANIFEST_ROOT = Path(__file__).resolve().parents[3] / "infra" / "k8s"
PRIVATE_RANGES = {
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "127.0.0.0/8",
}
CREDENTIAL_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "DATABASE_URL", "REDIS_URL")


def documents():
    for path in sorted(MANIFEST_ROOT.rglob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if document:
                yield path, document


def by_kind(kind):
    return [document for _, document in documents() if document.get("kind") == kind]


def pod_specs():
    """Every pod template in the repository, whatever object carries it."""
    for document in by_kind("Deployment"):
        yield document["metadata"]["name"], document["spec"]["template"]["spec"]
    for document in by_kind("Job"):
        yield document["metadata"]["name"], document["spec"]["template"]["spec"]


def test_manifests_are_present_and_parse():
    names = {document["metadata"]["name"] for document in by_kind("Deployment")}

    assert names == {"nexora-api", "nexora-worker", "nexora-web"}
    assert [document["metadata"]["name"] for document in by_kind("Job")] == ["nexora-migrate"]


@pytest.mark.parametrize("name,spec", list(pod_specs()), ids=lambda value: str(value)[:40])
def test_workloads_run_unprivileged_without_a_cluster_token(name, spec):
    security = spec["securityContext"]

    assert security["runAsNonRoot"] is True
    assert security["runAsUser"] > 0
    assert security["seccompProfile"]["type"] == "RuntimeDefault"
    # No workload talks to the Kubernetes API, so none receives a token for it.
    assert spec["automountServiceAccountToken"] is False


@pytest.mark.parametrize("name,spec", list(pod_specs()), ids=lambda value: str(value)[:40])
def test_containers_are_bounded_and_immutable(name, spec):
    for container in spec["containers"]:
        security = container["securityContext"]
        resources = container["resources"]

        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]
        # An unbounded workload can starve its neighbours out of the node.
        assert set(resources["requests"]) == set(resources["limits"]) == {"cpu", "memory"}


@pytest.mark.parametrize(
    "document", by_kind("Deployment"), ids=lambda value: value["metadata"]["name"]
)
def test_liveness_never_depends_on_a_datastore(document):
    for container in document["spec"]["template"]["spec"]["containers"]:
        liveness = container["livenessProbe"]["httpGet"]["path"]
        readiness = container["readinessProbe"]["httpGet"]["path"]

        # Readiness withdraws traffic during an outage; liveness restarting on the
        # same signal would turn a dependency blip into a cluster-wide restart loop.
        assert not liveness.endswith("/ready")
        if readiness.startswith("/api/v1/health"):
            assert readiness.endswith("/ready")
            assert liveness.endswith("/live")
            assert liveness != readiness


def test_no_secret_object_or_literal_credential_is_committed():
    assert by_kind("Secret") == []

    for path, document in documents():
        for container in _containers(document):
            for variable in container.get("env", []):
                if any(marker in variable["name"] for marker in CREDENTIAL_MARKERS):
                    assert "value" not in variable, f"{path}: {variable['name']} is inline"
                    assert variable["valueFrom"]["secretKeyRef"]["name"] == "nexora-secrets"


def _containers(document):
    template = document.get("spec", {}).get("template")
    if isinstance(template, dict):
        return template.get("spec", {}).get("containers", [])
    return []


def test_selectors_and_service_targets_agree():
    workloads = {
        document["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"]: document
        for document in by_kind("Deployment")
    }

    for document in by_kind("Deployment"):
        selector = document["spec"]["selector"]["matchLabels"]
        labels = document["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items()

    for service in by_kind("Service"):
        assert service["spec"]["selector"]["app.kubernetes.io/name"] in workloads

    for budget in by_kind("PodDisruptionBudget"):
        assert budget["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] in workloads


def test_namespace_enforces_restricted_pod_security():
    namespace = by_kind("Namespace")[0]

    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_network_is_default_deny():
    policies = {policy["metadata"]["name"]: policy for policy in by_kind("NetworkPolicy")}
    default = policies["default-deny"]

    assert default["spec"]["podSelector"] == {}
    assert set(default["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in default["spec"] and "egress" not in default["spec"]


@pytest.mark.parametrize(
    "policy_name,workload",
    [
        ("worker-provider-egress", "nexora-worker"),
        ("web-identity-egress", "nexora-web"),
    ],
)
def test_external_egress_is_tls_only_and_never_reaches_private_space(policy_name, workload):
    policies = {policy["metadata"]["name"]: policy for policy in by_kind("NetworkPolicy")}
    policy = policies[policy_name]
    rule = policy["spec"]["egress"][0]

    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": workload}
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    # Cluster services, cloud metadata and internal networks stay unreachable.
    assert PRIVATE_RANGES <= set(rule["to"][0]["ipBlock"]["except"])
    assert rule["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"


def test_the_api_has_no_route_to_the_public_internet():
    # Only the worker (model providers) and the web (identity provider) may leave
    # the cluster; the API's egress stays limited to datastores and telemetry.
    external = {
        policy["metadata"]["name"]
        for policy in by_kind("NetworkPolicy")
        if any(
            entry.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
            for rule in policy["spec"].get("egress", [])
            for entry in rule.get("to", [])
        )
    }

    assert external == {"worker-provider-egress", "web-identity-egress"}


def test_production_overlay_pins_images_by_digest():
    overlay = yaml.safe_load(
        (MANIFEST_ROOT / "overlays" / "production" / "kustomization.yaml").read_text()
    )

    assert overlay["images"], "production must pin the artifact it promotes"
    for image in overlay["images"]:
        # A moving tag cannot be the thing CI verified.
        assert image["digest"].startswith("sha256:")
        assert "newTag" not in image


def test_worker_shutdown_window_covers_its_configured_grace():
    worker = next(
        document
        for document in by_kind("Deployment")
        if document["metadata"]["name"] == "nexora-worker"
    )
    config = next(
        document
        for document in by_kind("ConfigMap")
        if document["metadata"]["name"] == "nexora-worker-config"
    )

    grace = float(config["data"]["NEXORA_WORKER_SHUTDOWN_GRACE_SECONDS"])
    assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] > grace
