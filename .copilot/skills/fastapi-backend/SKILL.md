---
name: fastapi-backend
description: Use when creating FastAPI services, routers, dependency injection, Pydantic schemas, async endpoints, background APIs, middleware, or Python API structure.
---

# FastAPI Backend

Build typed, async-safe APIs with explicit layers.

## Structure
API routers -> application services -> domain -> repositories/adapters.

## Rules
- Pydantic request/response models at boundaries.
- SQL/network I/O must not block the event loop.
- Dependency injection for auth, DB sessions and service composition.
- Map domain errors to stable API error codes centrally.
- Use request IDs and trace context.
- Do not return ORM objects directly.
- Validate pagination and payload limits.

## Testing
Unit-test domain/service logic and integration-test API + database boundaries.
