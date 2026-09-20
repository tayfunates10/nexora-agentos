-- Run once as a PostgreSQL administrator in the dedicated Nexora database.
-- Login identities and passwords/IAM bindings are environment-specific and intentionally
-- not created here. Grant these capability roles to the separate API and worker logins.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexora_api_runtime') THEN
        CREATE ROLE nexora_api_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexora_worker_runtime') THEN
        CREATE ROLE nexora_worker_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE nexora_api_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
ALTER ROLE nexora_worker_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;

-- Example only; create environment-specific login identities separately, then:
-- GRANT nexora_api_runtime TO nexora_api_login;
-- GRANT nexora_worker_runtime TO nexora_worker_login;
--
-- Those login identities must use INHERIT (the PostgreSQL default). The migration
-- identity is separate, owns the Nexora database/schema/tables, and is never used by
-- API or worker pods.
