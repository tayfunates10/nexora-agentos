FROM python:3.12-slim AS builder
WORKDIR /build
COPY apps/api/requirements.lock ./requirements.lock
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock
COPY apps/api/pyproject.toml ./pyproject.toml
COPY apps/api/src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 nexora
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER nexora
EXPOSE 8000
CMD ["uvicorn", "nexora_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
