import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_auth import token

from nexora_api.embeddings import EmbeddingBatch
from nexora_api.knowledge_worker import KnowledgeIngestionWorker
from nexora_api.main import create_app
from nexora_api.migrate import migrate
from nexora_api.rag_pipeline import RagEmbeddingPipeline
from nexora_api.rag_repository import RagRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1",
        reason="Requires PostgreSQL",
    ),
]


class StubEmbeddingAdapter:
    name = "stub"

    def __init__(self):
        self.calls = 0

    async def embed(self, *, request_id, texts, model, dimensions, timeout_seconds):
        self.calls += 1
        vectors = tuple((1.0,) + (0.0,) * (dimensions - 1) for _ in texts)
        return EmbeddingBatch(
            vectors=vectors,
            model=model,
            dimensions=dimensions,
            input_tokens=len(texts),
        )

    async def cancel(self, request_id):
        return None


def setup_workspace(keys, auth_settings, suffix):
    prefix = suffix + "-" + str(uuid4())
    owner, admin, member, member2 = [
        prefix + role for role in ("owner", "admin", "member", "member2")
    ]

    def headers(subject, idempotency_key=None):
        result = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "knowledge-integration",
        }
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    client = TestClient(create_app(settings=auth_settings))
    client.__enter__()
    workspace_id = client.post(
        "/api/v1/workspaces",
        json={"name": "Knowledge workspace"},
        headers=headers(owner),
    ).json()["id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    for subject, role in ((admin, "admin"), (member, "member"), (member2, "member")):
        response = client.put(
            base + "/members",
            json={"subject": subject, "role": role},
            headers=headers(owner),
        )
        assert response.status_code == 200, response.text
    return client, headers, workspace_id, owner, admin, member, member2


def pipeline(auth_settings, adapter):
    return RagEmbeddingPipeline(
        RagRepository(auth_settings),
        adapter,
        embedding_model="embed-test",
        dimensions=3,
        batch_size=8,
        timeout_seconds=3,
    )


def test_public_ingestion_is_idempotent_and_indexes_with_acl(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, owner, admin, member, member2 = setup_workspace(
        keys, auth_settings, "knowledge-index"
    )
    base = f"/api/v1/workspaces/{workspace_id}/knowledge"
    body = {
        "source_key": "handbook",
        "version": "v1",
        "title": "Engineering Handbook",
        "text": "Approved engineering guidance. " * 60,
        "access_scope": "restricted",
        "acl": [{"issuer": auth_settings.auth_issuer, "subject": member}],
        "metadata": {"kind": "handbook"},
        "max_chars": 300,
        "overlap_chars": 20,
    }

    denied = client.post(
        base + "/sources",
        json=body,
        headers=headers(member, "knowledge-denied-1"),
    )
    assert denied.status_code == 403

    created = client.post(
        base + "/sources",
        json=body,
        headers=headers(admin, "knowledge-idempotency-1"),
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert "text" not in created.json()
    assert created.json()["status"] == "queued"

    replay = client.post(
        base + "/sources",
        json=body,
        headers=headers(admin, "knowledge-idempotency-1"),
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == job_id

    conflict = client.post(
        base + "/sources",
        json={**body, "text": "Different material."},
        headers=headers(admin, "knowledge-idempotency-1"),
    )
    assert conflict.status_code == 409

    assert (
        client.get(
            base + "/ingestions/" + job_id,
            headers=headers(member),
        ).status_code
        == 403
    )

    adapter = StubEmbeddingAdapter()
    worker = KnowledgeIngestionWorker(
        auth_settings,
        pipeline(auth_settings, adapter),
        worker_id="knowledge-worker",
        lease_seconds=6,
    )
    assert asyncio.run(worker.process_once()) is True
    assert adapter.calls > 0

    status = client.get(
        base + "/ingestions/" + job_id,
        headers=headers(admin),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["chunk_count"] > 0
    assert status.json()["embedding_input_tokens"] > 0

    allowed = client.get(base + "/sources", headers=headers(member))
    hidden = client.get(base + "/sources", headers=headers(member2))
    assert [item["source_key"] for item in allowed.json()["items"]] == ["handbook"]
    assert hidden.json()["items"] == []

    deleted = client.delete(base + "/sources/handbook", headers=headers(admin))
    assert deleted.status_code == 204
    assert client.get(base + "/sources", headers=headers(member)).json()["items"] == []
    client.__exit__(None, None, None)


def test_revoked_manager_cannot_trigger_embedding_provider(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, owner, admin, member, _member2 = setup_workspace(
        keys, auth_settings, "knowledge-revoked"
    )
    base = f"/api/v1/workspaces/{workspace_id}"
    knowledge = base + "/knowledge"
    body = {
        "source_key": "revoked-guide",
        "version": "v1",
        "title": "Revoked Guide",
        "text": "This should never reach the embedding provider.",
    }
    created = client.post(
        knowledge + "/sources",
        json=body,
        headers=headers(admin, "knowledge-revoked-1"),
    )
    assert created.status_code == 202
    job_id = created.json()["id"]

    demoted = client.put(
        base + "/members",
        json={"subject": admin, "role": "member"},
        headers=headers(owner),
    )
    assert demoted.status_code == 200

    adapter = StubEmbeddingAdapter()
    worker = KnowledgeIngestionWorker(
        auth_settings,
        pipeline(auth_settings, adapter),
        worker_id="knowledge-revoked-worker",
        lease_seconds=6,
    )
    assert asyncio.run(worker.process_once()) is True
    assert adapter.calls == 0

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            "SELECT status,error_code FROM knowledge_ingestion_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row == ("failed", "knowledge_permission_revoked")
    assert client.get(knowledge + "/sources", headers=headers(member)).json()["items"] == []
    client.__exit__(None, None, None)


def test_delete_cancels_queued_ingestion_and_rejects_running_delete(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, _owner, admin, _member, _member2 = setup_workspace(
        keys, auth_settings, "knowledge-delete"
    )
    base = f"/api/v1/workspaces/{workspace_id}/knowledge"

    queued = client.post(
        base + "/sources",
        json={
            "source_key": "queued-source",
            "version": "v1",
            "title": "Queued",
            "text": "Queued text.",
        },
        headers=headers(admin, "knowledge-delete-queued"),
    )
    assert queued.status_code == 202
    queued_id = queued.json()["id"]
    assert client.delete(base + "/sources/queued-source", headers=headers(admin)).status_code == 204
    assert (
        client.get(base + "/ingestions/" + queued_id, headers=headers(admin)).json()["status"]
        == "cancelled"
    )

    running = client.post(
        base + "/sources",
        json={
            "source_key": "running-source",
            "version": "v1",
            "title": "Running",
            "text": "Running text.",
        },
        headers=headers(admin, "knowledge-delete-running"),
    )
    running_id = running.json()["id"]
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            """UPDATE knowledge_ingestion_jobs
               SET status='running',attempt_count=1,lease_owner='busy-worker',
                   lease_expires_at=now()+interval '1 minute'
               WHERE id=%s""",
            (running_id,),
        )

    assert (
        client.delete(base + "/sources/running-source", headers=headers(admin)).status_code == 409
    )

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            """UPDATE knowledge_ingestion_jobs
               SET lease_expires_at=now()-interval '1 second'
               WHERE id=%s""",
            (running_id,),
        )

    assert (
        client.delete(base + "/sources/running-source", headers=headers(admin)).status_code == 204
    )
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            """SELECT status,lease_owner,lease_expires_at
               FROM knowledge_ingestion_jobs WHERE id=%s""",
            (running_id,),
        ).fetchone()
    assert row == ("cancelled", None, None)
    client.__exit__(None, None, None)

def test_lost_ingestion_lease_rolls_back_source_write(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, _owner, admin, _member, _member2 = setup_workspace(
        keys, auth_settings, "knowledge-fence"
    )
    base = f"/api/v1/workspaces/{workspace_id}/knowledge"
    created = client.post(
        base + "/sources",
        json={
            "source_key": "fenced-source",
            "version": "v1",
            "title": "Fenced source",
            "text": "Lease fencing must prevent stale workers from publishing chunks. " * 20,
            "max_chars": 300,
            "overlap_chars": 20,
        },
        headers=headers(admin, "knowledge-fence-1"),
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]

    class LeaseStealingEmbeddingAdapter(StubEmbeddingAdapter):
        async def embed(self, *, request_id, texts, model, dimensions, timeout_seconds):
            async with await psycopg.AsyncConnection.connect(
                auth_settings.database_url.get_secret_value()
            ) as connection:
                await connection.execute(
                    """UPDATE knowledge_ingestion_jobs
                       SET lease_owner='replacement-worker',
                           lease_expires_at=now()+interval '1 minute',
                           updated_at=now()
                       WHERE id=%s AND status='running'""",
                    (job_id,),
                )
            return await super().embed(
                request_id=request_id,
                texts=texts,
                model=model,
                dimensions=dimensions,
                timeout_seconds=timeout_seconds,
            )

    adapter = LeaseStealingEmbeddingAdapter()
    worker = KnowledgeIngestionWorker(
        auth_settings,
        pipeline(auth_settings, adapter),
        worker_id="stale-worker",
        lease_seconds=6,
    )
    assert asyncio.run(worker.process_once()) is True
    assert adapter.calls > 0

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        job = connection.execute(
            """SELECT status,lease_owner FROM knowledge_ingestion_jobs WHERE id=%s""",
            (job_id,),
        ).fetchone()
        source_count = connection.execute(
            """SELECT count(*) FROM rag_sources
               WHERE workspace_id=%s AND source_key='fenced-source'""",
            (workspace_id,),
        ).fetchone()[0]

    assert job == ("running", "replacement-worker")
    assert source_count == 0
    client.__exit__(None, None, None)

def test_knowledge_endpoints_reject_cross_tenant_access(keys, auth_settings):
    migrate(auth_settings)
    client, headers, _workspace_id, _owner, admin, _member, _member2 = setup_workspace(
        keys, auth_settings, "knowledge-tenant-a"
    )
    other_owner = "knowledge-tenant-b-owner-" + str(uuid4())
    other_workspace = client.post(
        "/api/v1/workspaces",
        json={"name": "Other knowledge workspace"},
        headers=headers(other_owner),
    )
    assert other_workspace.status_code in (200, 201), other_workspace.text
    other_workspace_id = other_workspace.json()["id"]
    other_base = f"/api/v1/workspaces/{other_workspace_id}/knowledge"

    created = client.post(
        other_base + "/sources",
        json={
            "source_key": "private-handbook",
            "version": "v1",
            "title": "Private handbook",
            "text": "Tenant B material.",
        },
        headers=headers(other_owner, "knowledge-tenant-b-ingest"),
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]

    assert client.get(other_base + "/sources", headers=headers(admin)).status_code == 403
    assert (
        client.get(other_base + "/ingestions/" + job_id, headers=headers(admin)).status_code
        == 403
    )
    assert (
        client.post(
            other_base + "/sources",
            json={
                "source_key": "cross-tenant-write",
                "version": "v1",
                "title": "Denied",
                "text": "Must not be queued.",
            },
            headers=headers(admin, "knowledge-cross-tenant-write"),
        ).status_code
        == 403
    )
    assert (
        client.delete(
            other_base + "/sources/private-handbook",
            headers=headers(admin),
        ).status_code
        == 403
    )
    client.__exit__(None, None, None)

