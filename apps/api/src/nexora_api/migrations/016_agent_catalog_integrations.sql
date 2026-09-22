-- Standard agent catalog, tenant agent instances and the integration vault.
--
-- Three concerns are deliberately kept in separate tables so they can move at their own
-- pace: what Nexora publishes (catalog_agents / catalog_agent_versions), what a tenant
-- installed from it (tenant_agents), and what a tenant connected (tenant_integrations).
-- Nothing here changes how existing workspace agents or runs behave.

-- ------------------------------------------------------------------ platform audit
-- Workspace actions are audited in security_events, which requires a workspace. Catalog
-- publishing happens above every workspace, so it gets its own append-only journal.
CREATE TABLE platform_events (
    id uuid PRIMARY KEY,
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 100),
    request_id text NOT NULL,
    target text CHECK (target IS NULL OR length(target) <= 200),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX platform_events_time ON platform_events(created_at, id);
CREATE FUNCTION reject_platform_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'platform_events is append-only';
END;
$$;
CREATE TRIGGER platform_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON platform_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_platform_event_mutation();

-- ----------------------------------------------------------------- agent catalog
-- Categories and icons are free-form identifiers on purpose: a platform admin adds an
-- Accounting or HR agent without a schema change and without touching the console.
CREATE TABLE catalog_agents (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE
        CHECK (slug ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(slug) BETWEEN 3 AND 64),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    description text NOT NULL CHECK (length(btrim(description)) BETWEEN 1 AND 1000),
    category text NOT NULL CHECK (category ~ '^[a-z][a-z0-9_-]{1,39}$'),
    icon text NOT NULL CHECK (icon ~ '^[a-z][a-z0-9_-]{1,39}$'),
    status text NOT NULL
        CHECK (status IN ('draft','beta','stable','deprecated','disabled')),
    -- Restricted is the default: a new catalog entry reaches no tenant until it is
    -- either published publicly or granted to named workspaces.
    visibility text NOT NULL DEFAULT 'restricted'
        CHECK (visibility IN ('restricted','public')),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The sortable components are stored next to the string so version ordering is an index
-- scan and never a lexical comparison ('1.10.0' must outrank '1.9.0').
CREATE TABLE catalog_agent_versions (
    id uuid PRIMARY KEY,
    catalog_agent_id uuid NOT NULL REFERENCES catalog_agents(id),
    version text NOT NULL CHECK (version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    major integer NOT NULL CHECK (major >= 0),
    minor integer NOT NULL CHECK (minor >= 0),
    patch integer NOT NULL CHECK (patch >= 0),
    status text NOT NULL
        CHECK (status IN ('draft','beta','stable','deprecated','disabled')),
    channel text NOT NULL CHECK (channel IN ('stable','beta','canary')),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    min_runtime_version text NOT NULL
        CHECK (min_runtime_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    changelog text NOT NULL DEFAULT '' CHECK (length(changelog) <= 5000),
    published_by_issuer text NOT NULL,
    published_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (catalog_agent_id, version),
    UNIQUE (id, catalog_agent_id)
);
CREATE INDEX catalog_agent_versions_order
    ON catalog_agent_versions(catalog_agent_id, major DESC, minor DESC, patch DESC);

CREATE TABLE catalog_agent_entitlements (
    catalog_agent_id uuid NOT NULL REFERENCES catalog_agents(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    granted_by_issuer text NOT NULL,
    granted_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_agent_id, workspace_id)
);
CREATE INDEX catalog_agent_entitlements_workspace
    ON catalog_agent_entitlements(workspace_id, catalog_agent_id);

-- A rollout is the staged answer to "which version does this channel currently serve".
-- Exactly one rollout per agent and channel may be active at a time.
CREATE TABLE agent_rollouts (
    id uuid PRIMARY KEY,
    catalog_agent_id uuid NOT NULL REFERENCES catalog_agents(id),
    version_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('stable','beta','canary')),
    percentage integer NOT NULL CHECK (percentage BETWEEN 0 AND 100),
    state text NOT NULL CHECK (state IN ('active','paused','completed','rolled_back')),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (version_id, catalog_agent_id)
        REFERENCES catalog_agent_versions(id, catalog_agent_id)
);
CREATE UNIQUE INDEX agent_rollouts_one_active
    ON agent_rollouts(catalog_agent_id, channel) WHERE state = 'active';
CREATE INDEX agent_rollouts_agent_time ON agent_rollouts(catalog_agent_id, created_at, id);

-- ----------------------------------------------------------- integration registry
CREATE TABLE integration_definitions (
    id text PRIMARY KEY
        CHECK (id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(id) BETWEEN 2 AND 64),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    description text NOT NULL CHECK (length(btrim(description)) BETWEEN 1 AND 1000),
    category text NOT NULL CHECK (category ~ '^[a-z][a-z0-9_-]{1,39}$'),
    icon text NOT NULL CHECK (icon ~ '^[a-z][a-z0-9_-]{1,39}$'),
    auth_type text NOT NULL
        CHECK (auth_type IN ('api_key','oauth2','basic','bearer_token','webhook')),
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(capabilities) = 'array'),
    scopes jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(scopes) = 'array'),
    credential_fields jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(credential_fields) = 'array'),
    status text NOT NULL CHECK (status IN ('available','beta','deprecated','disabled')),
    version text NOT NULL CHECK (version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    -- The published definition in full, including the request shapes the connector
    -- runtime builds calls from. Operator-published, never tenant-supplied, and the
    -- reason a new service needs no change to platform code.
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- integration vault
-- account_identifier is what the tenant recognises (an @handle, an account number). It is
-- part of the uniqueness key so one workspace can connect the same service several times.
CREATE TABLE tenant_integrations (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    integration_definition_id text NOT NULL REFERENCES integration_definitions(id),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
    account_identifier text NOT NULL CHECK (length(btrim(account_identifier)) BETWEEN 1 AND 200),
    auth_type text NOT NULL
        CHECK (auth_type IN ('api_key','oauth2','basic','bearer_token','webhook')),
    credential_reference uuid,
    status text NOT NULL
        CHECK (status IN ('pending','connected','expired','revoked','error','disabled')),
    scopes jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(scopes) = 'array'),
    granted_scopes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(granted_scopes) = 'array'),
    -- Declared connector fields that are not secrets, such as a tenant-owned base URL or
    -- an account number. Secret fields never appear here; they only exist as ciphertext.
    config jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_tested_at timestamptz,
    last_success_at timestamptz,
    last_error text CHECK (last_error IS NULL OR length(last_error) <= 500),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, integration_definition_id, account_identifier)
);
CREATE INDEX tenant_integrations_workspace ON tenant_integrations(workspace_id, id);

-- Ciphertext only. The data key is itself wrapped by an operator-held master key, so a
-- database dump on its own decrypts nothing. `hint` is the masked tail shown in the
-- console; it is derived from the secret but never sufficient to reconstruct it.
CREATE TABLE integration_credentials (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    tenant_integration_id uuid NOT NULL,
    key_id text NOT NULL CHECK (key_id ~ '^[A-Za-z0-9._-]{1,64}$'),
    wrapped_key bytea NOT NULL CHECK (octet_length(wrapped_key) BETWEEN 1 AND 4096),
    wrap_nonce bytea NOT NULL CHECK (octet_length(wrap_nonce) = 12),
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 1 AND 65536),
    hint text NOT NULL CHECK (length(hint) BETWEEN 1 AND 64),
    expires_at timestamptz,
    rotated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (tenant_integration_id, workspace_id)
        REFERENCES tenant_integrations(id, workspace_id)
);
CREATE INDEX integration_credentials_integration
    ON integration_credentials(tenant_integration_id, created_at DESC);

-- The composite reference makes a credential from another workspace unrepresentable
-- rather than merely rejected in application code.
ALTER TABLE tenant_integrations
    ADD CONSTRAINT tenant_integrations_credential_fk
    FOREIGN KEY (credential_reference, workspace_id)
        REFERENCES integration_credentials(id, workspace_id);

-- PKCE verifiers and the OAuth state are short-lived secrets, so they are sealed with the
-- same vault envelope and the state is stored only as a digest.
CREATE TABLE integration_oauth_states (
    state_digest text PRIMARY KEY CHECK (length(state_digest) = 64),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    integration_definition_id text NOT NULL REFERENCES integration_definitions(id),
    tenant_integration_id uuid NOT NULL,
    key_id text NOT NULL CHECK (key_id ~ '^[A-Za-z0-9._-]{1,64}$'),
    wrapped_key bytea NOT NULL CHECK (octet_length(wrapped_key) BETWEEN 1 AND 4096),
    wrap_nonce bytea NOT NULL CHECK (octet_length(wrap_nonce) = 12),
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 1 AND 65536),
    redirect_uri text NOT NULL CHECK (length(redirect_uri) BETWEEN 1 AND 500),
    requested_scopes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(requested_scopes) = 'array'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_integration_id, workspace_id)
        REFERENCES tenant_integrations(id, workspace_id)
);
CREATE INDEX integration_oauth_states_expiry ON integration_oauth_states(expires_at);

-- ------------------------------------------------------------- tenant agent instances
CREATE TABLE agent_update_policies (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    channel text NOT NULL DEFAULT 'stable' CHECK (channel IN ('stable','beta','canary')),
    mode text NOT NULL DEFAULT 'manual' CHECK (mode IN ('manual','automatic')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_agents (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    catalog_agent_id uuid NOT NULL REFERENCES catalog_agents(id),
    version_id uuid NOT NULL,
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
    status text NOT NULL CHECK (status IN ('active','paused','disabled')),
    -- Why an instance is paused. The platform pauses one whose required connections are
    -- missing and resumes it when they arrive; a pause a person asked for is theirs to
    -- undo, and no binding change may quietly resume it.
    paused_reason text CHECK (paused_reason IN ('not_ready','operator')),
    update_channel text NOT NULL CHECK (update_channel IN ('stable','beta','canary')),
    update_mode text NOT NULL CHECK (update_mode IN ('manual','automatic')),
    instructions_override text
        CHECK (instructions_override IS NULL OR length(instructions_override) <= 20000),
    settings jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (version_id, catalog_agent_id)
        REFERENCES catalog_agent_versions(id, catalog_agent_id),
    CHECK ((status = 'paused') = (paused_reason IS NOT NULL))
);
CREATE INDEX tenant_agents_workspace ON tenant_agents(workspace_id, id);
-- A workspace may run several instances of one agent, one per customer, so the name is
-- what distinguishes them. A removed instance keeps its history but stops reserving its
-- name, so the same agent can be added again under it.
CREATE UNIQUE INDEX tenant_agents_named
    ON tenant_agents(workspace_id, catalog_agent_id, display_name)
    WHERE status <> 'disabled';

-- Install, update and rollback are recorded so a tenant can see exactly which version ran
-- when, and so a rollback is a normal recorded transition rather than a silent rewrite.
CREATE TABLE tenant_agent_version_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    tenant_agent_id uuid NOT NULL,
    from_version_id uuid,
    to_version_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('installed','updated','rolled_back')),
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    request_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_agent_id, workspace_id) REFERENCES tenant_agents(id, workspace_id)
);
CREATE INDEX tenant_agent_version_events_agent
    ON tenant_agent_version_events(tenant_agent_id, created_at DESC, id);
CREATE FUNCTION reject_tenant_agent_version_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tenant_agent_version_events is append-only';
END;
$$;
CREATE TRIGGER tenant_agent_version_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON tenant_agent_version_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_tenant_agent_version_event_mutation();

-- An agent never holds a credential. It holds a binding, and the connector runtime
-- resolves the binding to a secret it alone can open. Both composite references carry the
-- workspace, so a binding across tenants cannot be written at all.
CREATE TABLE agent_integration_bindings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    tenant_agent_id uuid NOT NULL,
    binding_key text NOT NULL
        CHECK (binding_key ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(binding_key) BETWEEN 2 AND 64),
    tenant_integration_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_agent_id, binding_key),
    FOREIGN KEY (tenant_agent_id, workspace_id) REFERENCES tenant_agents(id, workspace_id),
    FOREIGN KEY (tenant_integration_id, workspace_id)
        REFERENCES tenant_integrations(id, workspace_id)
);
CREATE INDEX agent_integration_bindings_workspace
    ON agent_integration_bindings(workspace_id, tenant_agent_id);

-- ---------------------------------------------------------------- custom agents
-- Existing workspace agents are the custom-agent table and keep working untouched. These
-- columns only record where a forked agent came from, so a later catalog release never
-- changes a fork's behaviour.
ALTER TABLE agent_definitions
    ADD COLUMN origin_catalog_agent_id uuid REFERENCES catalog_agents(id),
    ADD COLUMN origin_version_id uuid REFERENCES catalog_agent_versions(id),
    ADD COLUMN manifest jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(manifest) = 'object'),
    ADD CONSTRAINT agent_definitions_fork_complete
        CHECK ((origin_catalog_agent_id IS NULL) = (origin_version_id IS NULL));
