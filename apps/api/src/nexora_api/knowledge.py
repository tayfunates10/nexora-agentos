import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexora_api.auth import Principal, authenticated
from nexora_api.rag import AccessScope


class KnowledgeAclIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=255)


class KnowledgeSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:-]+$")
    version: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=500000)
    access_scope: AccessScope = AccessScope.WORKSPACE
    acl: list[KnowledgeAclIdentity] = Field(default_factory=list, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_chars: int = Field(default=1200, ge=200, le=4000)
    overlap_chars: int = Field(default=120, ge=0, le=3999)

    @model_validator(mode="after")
    def validate_acl_and_chunking(self):
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        if self.access_scope == AccessScope.RESTRICTED and not self.acl:
            raise ValueError("restricted sources require at least one ACL identity")
        if self.access_scope == AccessScope.WORKSPACE and self.acl:
            raise ValueError("workspace sources must not include ACL identities")
        encoded = json.dumps(
            self.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if len(encoded) > 32768:
            raise ValueError("metadata must be at most 32768 bytes")
        return self


class KnowledgeIngestion(BaseModel):
    id: UUID
    workspace_id: UUID
    source_key: str
    version: str
    title: str
    access_scope: AccessScope
    status: str
    attempt_count: int
    source_id: UUID | None = None
    chunk_count: int | None = None
    embedding_input_tokens: int | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class KnowledgeSource(BaseModel):
    id: UUID
    source_key: str
    version: str
    title: str
    access_scope: AccessScope
    content_hash: str
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class KnowledgeSourcePage(BaseModel):
    items: list[KnowledgeSource]
    next_cursor: UUID | None = None


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/knowledge", tags=["knowledge"])
Identity = Annotated[Principal, Depends(authenticated)]


@router.post("/sources", response_model=KnowledgeIngestion, status_code=202)
async def ingest_source(
    workspace_id: UUID,
    body: KnowledgeSourceInput,
    principal: Identity,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
):
    job, created = await request.app.state.rag.enqueue_ingestion(
        principal,
        workspace_id,
        body=body,
        idempotency_key=idempotency_key,
        request_id=request.state.request_id,
    )
    response.status_code = 202 if created else 200
    return job


@router.get("/ingestions/{job_id}", response_model=KnowledgeIngestion)
async def get_ingestion(
    workspace_id: UUID,
    job_id: UUID,
    principal: Identity,
    request: Request,
):
    return await request.app.state.rag.get_ingestion(principal, workspace_id, job_id)


@router.get("/sources", response_model=KnowledgeSourcePage)
async def list_sources(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await request.app.state.rag.list_sources(
        principal, workspace_id, limit + 1, cursor
    )
    return KnowledgeSourcePage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.delete("/sources/{source_key}", status_code=204)
async def delete_source(
    workspace_id: UUID,
    source_key: str,
    principal: Identity,
    request: Request,
):
    if not source_key or len(source_key) > 255:
        raise HTTPException(404)
    await request.app.state.rag.delete_source(
        principal,
        workspace_id,
        source_key=source_key,
        request_id=request.state.request_id,
    )
    return Response(status_code=204)
