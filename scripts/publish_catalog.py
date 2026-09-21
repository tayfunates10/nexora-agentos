#!/usr/bin/env python3
"""Validate and publish standard agent and connector packages.

This is how an agent reaches customers without a console release. A package is a manifest
directory under `agents/` or `connectors/`; publishing sends it to the platform API as a
new immutable version. The API refuses to overwrite a published version, so a release
that forgets to raise the version number fails loudly instead of changing what a pinned
tenant is running.

    python scripts/publish_catalog.py --check
    python scripts/publish_catalog.py --publish --base-url https://api.example --token "$TOKEN"

The token is a platform administrator's bearer token. It is read from the environment or
the command line and is never written to output.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
AGENTS = REPOSITORY / "agents"
CONNECTORS = REPOSITORY / "connectors"
TIMEOUT_SECONDS = 15


class PublishError(RuntimeError):
    """A package the platform refused, or a release that would change history."""


def _contracts():
    """Import the published manifest contracts, which are owned by the API package."""
    sys.path.insert(0, str(REPOSITORY / "apps/api/src"))
    from nexora_api.agent_manifest import AgentManifest  # noqa: PLC0415
    from nexora_api.integration_manifest import ConnectorManifest  # noqa: PLC0415

    return AgentManifest, ConnectorManifest


def agent_packages(selected: list[str] | None = None) -> dict[str, dict]:
    return _packages(AGENTS, "manifest.json", selected)


def connector_packages(selected: list[str] | None = None) -> dict[str, dict]:
    return _packages(CONNECTORS, "connector.json", selected)


def _packages(root: Path, filename: str, selected: list[str] | None) -> dict[str, dict]:
    wanted = None if selected is None or "*" in selected else set(selected)
    found: dict[str, dict] = {}
    if not root.is_dir():
        return found
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if wanted is not None and directory.name not in wanted:
            continue
        document = directory / filename
        if not document.is_file():
            raise PublishError(f"{directory.name}: expected {filename}")
        found[directory.name] = json.loads(document.read_text(encoding="utf-8"))
    missing = (wanted or set()) - set(found)
    if missing:
        raise PublishError(f"No package for: {', '.join(sorted(missing))}")
    return found


def validate(agents: dict[str, dict], connectors: dict[str, dict]) -> list[str]:
    """Check every package against the contract the platform will apply to it."""
    AgentManifest, ConnectorManifest = _contracts()
    problems: list[str] = []
    published = {name for name in connector_packages()}

    for name, document in connectors.items():
        try:
            manifest = ConnectorManifest.model_validate(document)
        except Exception as error:  # noqa: BLE001 - reported, not handled
            problems.append(f"connectors/{name}: {error}")
            continue
        if manifest.id != name:
            problems.append(f"connectors/{name}: id is {manifest.id}")

    for name, document in agents.items():
        try:
            manifest = AgentManifest.model_validate(document)
        except Exception as error:  # noqa: BLE001 - reported, not handled
            problems.append(f"agents/{name}: {error}")
            continue
        if manifest.slug != name:
            problems.append(f"agents/{name}: slug is {manifest.slug}")
        # An agent that names an integration nobody publishes can never become ready.
        unknown = sorted({integration for integration, _ in manifest.integrations()} - published)
        if unknown:
            problems.append(f"agents/{name}: references unpublished connectors {unknown}")
    return problems


def _request(base_url: str, token: str, method: str, path: str, body: dict | None):
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {"body": error.read().decode(errors="replace")[:500]}
    except urllib.error.URLError as error:
        raise PublishError(f"{method} {path} could not reach the platform: {error.reason}") from None


def publish_connector(base_url: str, token: str, document: dict) -> str:
    status, payload = _request(
        base_url, token, "PUT", f"/api/v1/platform/integrations/{document['id']}", document
    )
    if status != 200:
        raise PublishError(f"connectors/{document['id']}: platform answered {status} {payload}")
    return f"connectors/{document['id']} {document['version']}"


def publish_agent(base_url: str, token: str, document: dict) -> str:
    slug = document["slug"]
    status, _ = _request(base_url, token, "GET", f"/api/v1/platform/agents/{slug}", None)
    if status == 404:
        created, payload = _request(
            base_url,
            token,
            "POST",
            "/api/v1/platform/agents",
            {
                "slug": slug,
                "name": document["name"],
                "description": document["description"],
                "category": document["category"],
                "icon": document["icon"],
                "status": document.get("status", "draft"),
            },
        )
        if created != 201:
            raise PublishError(f"agents/{slug}: catalog entry refused with {created} {payload}")
    elif status != 200:
        raise PublishError(f"agents/{slug}: catalog lookup answered {status}")

    version = document["version"]
    status, payload = _request(
        base_url, token, "POST", f"/api/v1/platform/agents/{slug}/versions",
        {"manifest": document},
    )
    if status == 409:
        # Already published, and a published version is immutable. This is only safe to
        # treat as a no-op; changing it would alter what pinned tenants already run.
        return f"agents/{slug} {version} already published"
    if status != 201:
        raise PublishError(f"agents/{slug}: version {version} refused with {status} {payload}")
    return f"agents/{slug} {version}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate packages and stop")
    parser.add_argument("--publish", action="store_true", help="publish to a running platform")
    parser.add_argument("--base-url", help="platform API base URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("NEXORA_PLATFORM_TOKEN", ""),
        help="platform administrator bearer token; prefer NEXORA_PLATFORM_TOKEN",
    )
    parser.add_argument("--agents", nargs="*", help="agent slugs to publish, or * for all")
    parser.add_argument("--connectors", nargs="*", help="connector ids to publish, or * for all")
    arguments = parser.parse_args()

    try:
        agents = agent_packages(arguments.agents)
        connectors = connector_packages(arguments.connectors)
    except (PublishError, json.JSONDecodeError) as error:
        print(f"Package layout is invalid: {error}", file=sys.stderr)
        return 2

    problems = validate(agents, connectors)
    if problems:
        print("Packages did not validate:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"Validated {len(agents)} agent and {len(connectors)} connector packages.")
    if not arguments.publish:
        return 0

    if not arguments.base_url or not arguments.token:
        print("Publishing needs --base-url and a platform token.", file=sys.stderr)
        return 2
    try:
        # Connectors first: an agent that requires one must find it already registered.
        for document in connectors.values():
            print(publish_connector(arguments.base_url, arguments.token, document))
        for document in agents.values():
            print(publish_agent(arguments.base_url, arguments.token, document))
    except PublishError as error:
        print(f"Publishing failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
