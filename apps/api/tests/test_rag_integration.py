import asyncio
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_auth import token

from nexora_api.auth import Principal
from nexora_api.main import create_app
from nexora_api.migrate import migrate
from nexora_api.rag import AccessScope, AclIdentity
from nexora_api.rag_repository import RagRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1",
        reason="Requires PostgreSQL and pgvector",
    ),
]


def test_rag_retrieval_filters_acl_before_context_and_hides_cross_tenant(keys, auth_settings):
    migrate(auth_settings)
    prefix = "rag-acl-" + str(uuid4())
    owner = prefix + "-owner"
    allowed = prefix + "-allowed"
    denied = prefix + "-denied"
    issuer = auth_settings.auth_issuer or ""

    def headers(subject):
        return {"Authorization": "Bearer " + token(keys, subject)}

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "RAG ACL workspace"},
            headers=headers(owner),
        ).json()["id"]
        base = "/api/v1/workspaces/" + workspace_id
        for subject in (allowed, denied):
            response = client.put(
                base + "/members",
                json={"subject": subject, "role": "member"},
                headers=headers(owner),
            )
            assert response.status_code == 200, response.text

        other_workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "Other RAG workspace"},
            headers=headers(owner),
        ).json()["id"]

    repository = RagRepository(auth_settings)
    owner_principal = Principal(issuer, owner)
    allowed_principal = Principal(issuer, allowed)
    denied_principal = Principal(issuer, denied)

    async def exercise():
        await repository.index_source(
            owner_principal,
            UUID(workspace_id),
            source_key="workspace-handbook",
            version="v1",
            title="Workspace handbook",
            text="General workspace information available to every current member.",
            embedding_model="test-embed-v1",
            embeddings=((0.0, 1.0, 0.0),),
            request_id="rag-workspace-index",
        )
        restricted_id = await repository.index_source(
            owner_principal,
            UUID(workspace_id),
            source_key="restricted-plan",
            version="v1",
            title="Restricted plan",
            text="Confidential launch plan available only to the explicitly allowed member.",
            embedding_model="test-embed-v1",
            embeddings=((1.0, 0.0, 0.0),),
            request_id="rag-restricted-index",
            access_scope=AccessScope.RESTRICTED,
            acl=(AclIdentity(issuer=issuer, subject=allowed),),
        )
        allowed_results = await repository.retrieve(
            allowed_principal,
            UUID(workspace_id),
            embedding_model="test-embed-v1",
            query_embedding=(1.0, 0.0, 0.0),
            limit=5,
        )
        denied_results = await repository.retrieve(
            denied_principal,
            UUID(workspace_id),
            embedding_model="test-embed-v1",
            query_embedding=(1.0, 0.0, 0.0),
            limit=5,
        )

        with pytest.raises(HTTPException) as manage_error:
            await repository.index_source(
                denied_principal,
                UUID(workspace_id),
                source_key="member-write",
                version="v1",
                title="Denied write",
                text="Members cannot directly manage the knowledge index.",
                embedding_model="test-embed-v1",
                embeddings=((1.0, 0.0, 0.0),),
                request_id="rag-member-write",
            )

        with pytest.raises(HTTPException) as tenant_error:
            await repository.retrieve(
                denied_principal,
                UUID(other_workspace_id),
                embedding_model="test-embed-v1",
                query_embedding=(1.0, 0.0, 0.0),
            )

        return (
            restricted_id,
            allowed_results,
            denied_results,
            manage_error.value,
            tenant_error.value,
        )

    restricted_id, allowed_results, denied_results, manage_error, tenant_error = asyncio.run(
        exercise()
    )

    assert allowed_results[0].source_key == "restricted-plan"
    assert allowed_results[0].source_version == "v1"
    assert {item.source_key for item in allowed_results} == {
        "restricted-plan",
        "workspace-handbook",
    }
    assert {item.source_key for item in denied_results} == {"workspace-handbook"}
    assert manage_error.status_code == 403
    assert tenant_error.status_code == 404

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            """SELECT c.workspace_id,c.source_version,c.access_scope,a.subject
               FROM rag_chunks c
               JOIN rag_source_acl a
                 ON a.workspace_id=c.workspace_id AND a.source_id=c.source_id
               WHERE c.source_id=%s""",
            (restricted_id,),
        ).fetchone()
        assert str(row[0]) == workspace_id
        assert row[1] == "v1"
        assert row[2] == "restricted"
        assert row[3] == allowed


def test_rag_reindex_switches_current_version_and_deletion_is_idempotent(keys, auth_settings):
    migrate(auth_settings)
    owner = "rag-version-" + str(uuid4())
    issuer = auth_settings.auth_issuer or ""

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "RAG version workspace"},
            headers={"Authorization": "Bearer " + token(keys, owner)},
        ).json()["id"]

    repository = RagRepository(auth_settings)
    principal = Principal(issuer, owner)

    async def exercise():
        await repository.index_source(
            principal,
            UUID(workspace_id),
            source_key="policy",
            version="v1",
            title="Policy",
            text="Old policy content.",
            embedding_model="test-embed-v1",
            embeddings=((0.0, 1.0, 0.0),),
            request_id="rag-v1",
        )
        v2_id = await repository.index_source(
            principal,
            UUID(workspace_id),
            source_key="policy",
            version="v2",
            title="Policy",
            text="Current policy content.",
            embedding_model="test-embed-v1",
            embeddings=((1.0, 0.0, 0.0),),
            request_id="rag-v2",
        )
        first = await repository.retrieve(
            principal,
            UUID(workspace_id),
            embedding_model="test-embed-v1",
            query_embedding=(1.0, 0.0, 0.0),
        )

        same_v2_id = await repository.index_source(
            principal,
            UUID(workspace_id),
            source_key="policy",
            version="v2",
            title="Policy updated",
            text="Current policy content re-indexed deterministically.",
            embedding_model="test-embed-v1",
            embeddings=((1.0, 0.0, 0.0),),
            request_id="rag-v2-reindex",
        )
        second = await repository.retrieve(
            principal,
            UUID(workspace_id),
            embedding_model="test-embed-v1",
            query_embedding=(1.0, 0.0, 0.0),
        )
        deleted = await repository.delete_source(
            principal,
            UUID(workspace_id),
            source_key="policy",
            request_id="rag-delete",
        )
        deleted_again = await repository.delete_source(
            principal,
            UUID(workspace_id),
            source_key="policy",
            request_id="rag-delete-replay",
        )
        empty = await repository.retrieve(
            principal,
            UUID(workspace_id),
            embedding_model="test-embed-v1",
            query_embedding=(1.0, 0.0, 0.0),
        )
        return v2_id, same_v2_id, first, second, deleted, deleted_again, empty

    v2_id, same_v2_id, first, second, deleted, deleted_again, empty = asyncio.run(exercise())

    assert v2_id == same_v2_id
    assert [item.source_version for item in first] == ["v2"]
    assert first[0].content == "Current policy content."
    assert [item.source_version for item in second] == ["v2"]
    assert second[0].content == "Current policy content re-indexed deterministically."
    assert deleted == 2
    assert deleted_again == 0
    assert empty == ()

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM rag_sources WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()[0]
            == 0
        )
