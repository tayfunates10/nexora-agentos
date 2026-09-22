# ADR 0044: Standard agent catalog, tenant instances and the integration vault

Status: implemented.

## Context

Nexora had one kind of agent: a row in `agent_definitions`, owned by a workspace, created
by that workspace, and described by a name, an instruction text and a model profile. That
is a good model for an agent a customer writes for themselves, and the wrong model for the
agents Nexora sells.

Three concerns were tangled together in it.

*What Nexora publishes* — a Social Media Management agent, a Reporting agent, an SEO agent
— has to be developed, versioned and released by Nexora, on its own clock, for many
customers at once. That is a product, not a workspace record.

*What a customer configured* — this agency's Instagram account, this brand voice, this
prompt addition, this pinned version — has to survive every release of that product, and
has to stay invisible to every other customer.

*What a customer connected* — an Instagram login, an ERP key, a WordPress application
password — has to persist across sessions, has to be usable by an agent, and must never be
readable by anyone, including the agent that uses it.

Collapsing these into one table forces the wrong trade-off at every turn: publishing an
agent update either rewrites customer configuration or cannot reach customers at all;
adding a new standard agent means changing console code; and a credential has nowhere to
live except next to the data that describes it.

## Decision

### Three objects, not one

| Object | Table | Owner | Lifecycle |
| --- | --- | --- | --- |
| Standard agent | `catalog_agents` | Nexora | Published, versioned, rolled out |
| Published version | `catalog_agent_versions` | Nexora | **Immutable** once published |
| Tenant instance | `tenant_agents` | One workspace | Pinned to a version, configured freely |
| Custom agent | `agent_definitions` | One workspace | Unchanged; now records fork lineage |

`agent_definitions` keeps working exactly as it did. It gained three nullable columns
(`origin_catalog_agent_id`, `origin_version_id`, `manifest`) that are set only when a
workspace forks a standard agent, so every existing agent, run and console screen behaves
as before.

### An agent is a manifest, not console code

A standard agent version is a validated document: identity, category and icon; system
instructions; a model policy expressed in operator profile names rather than vendor model
ids; reasoning, memory, retrieval, budget and rate limits; the tools and integrations it
requires and the ones it can use; the capabilities it exposes; and the actions that may
never happen without a human decision.

The console reads that document. No screen names a particular agent, so a platform
administrator can publish an Accounting agent, an HR agent or a WhatsApp agent and it
appears — with its required connections, its capabilities and its approval gates — without
a console release. `agents/<slug>/manifest.json` holds the packages this repository ships;
`AgentManifest` in `apps/api` is the contract they are validated against, and the same
contract is applied at publish time by the platform API.

Connectors work the same way. `ConnectorManifest` describes how a service authenticates,
what it can do, which fields a tenant must fill in and — for the runtime — which request
each capability corresponds to. `connectors/<id>/connector.json` holds the shipped
definitions. Adding a service is a new definition published into the registry, not a change
to platform code.

### Versions only move forward, and never change

A published version is immutable: publishing the same number again is a conflict, and a
new number must sort above every existing one. Ordering is numeric, on stored
`major`/`minor`/`patch` columns, because `1.10.0` has to outrank `1.9.0`.

What *does* move is release state. A version carries a status (`draft`, `beta`, `stable`,
`deprecated`, `disabled`) and a channel (`stable`, `beta`, `canary`). A workspace
subscribes to a channel and sees that channel and everything more stable than it, so a
canary workspace receives canary, beta and stable releases while a production workspace
receives only stable ones.

### Rollouts stage a release; rollbacks restore one

`agent_rollouts` records which version currently serves a channel and to what percentage
of the estate. A workspace's inclusion is a hash of the agent and the workspace, so
widening a rollout from 5% to 25% adds tenants and never moves one that was already
included, and every process computes the same answer without coordination. A tenant
outside an active rollout keeps the version that served the channel before it, rather than
jumping ahead.

Rolling back retires the rollout and reinstates the version it replaced at 100%. The failed
release stays in history as `rolled_back`; nothing is deleted.

### A tenant's version is a pin, not a consequence

Installing records a version on the instance. A later release changes what is *offered*,
not what is *running*. Moving between versions — forward or back — writes an append-only
row in `tenant_agent_version_events` and touches nothing else: display name, prompt
override, settings and integration bindings live on the instance, so an update and a
rollback both leave them exactly as they were. That is the property the upgrade tests
assert directly.

### An agent holds a binding, never a credential

```
Agent instance -> Integration binding -> Tenant integration -> Credential reference -> Vault
```

`agent_integration_bindings` maps a name the manifest declared ("instagram") to one of the
workspace's own connections. Both sides of the binding carry the workspace in a composite
foreign key, so a binding across tenants cannot be written at all — it is unrepresentable,
not merely rejected in application code. The same shape lets one workspace connect the same
service many times (an agency running one Instagram connection per customer) and point two
instances of the same agent at different accounts.

An instance is only active when every integration its manifest requires is bound to a
connected account. Installing without them succeeds and leaves the agent paused, naming
what is missing; activating it is refused until they are in place. Switching a connection
off pauses the agents that depend on it rather than letting them run against nothing.

### Secrets are sealed, bound to a tenant, and never returned

`SecretVault` uses envelope encryption. Each credential gets a fresh 256-bit data key; the
secret is sealed with AES-256-GCM under that key; the data key is sealed under an
operator-held master key that lives outside PostgreSQL. A database dump decrypts nothing on
its own.

Associated data binds every ciphertext to its workspace, its integration and the purpose it
was sealed for, so a row copied into another tenant's context fails authentication instead
of decrypting. Several master keys may be loaded at once, which is what makes a key
rotation possible without an outage.

The master key is reached through a provider interface. The bundled provider reads keys the
operator supplied to the process; a KMS- or Vault-backed provider implements the same two
methods and changes nothing above that module.

Plaintext exists only inside a single function call. Afterwards the only representation
anyone can obtain — API response, console screen, audit record, log line, agent prompt — is
a masked hint that keeps at most the last four characters, and only when the secret is long
enough for those characters not to matter. The console contract encodes this: a payload
carrying something that does not look like a mask is rejected rather than rendered.

The vault fails closed. Without configured master keys the process starts, and every
attempt to store a credential answers 503, so an unconfigured deployment cannot quietly
keep secrets in the clear.

### The connector runtime is what holds the secret

An agent asks for a capability. `ConnectorRuntime` resolves the binding, opens the
credential inside the process, builds the outbound request from the connector definition
and returns the response. The secret is never an argument, a return value or a log field.

Outbound requests are constrained by the definition: a fixed base URL, or one the tenant
declared for its own system; an absolute path that cannot escape it; HTTPS on port 443 with
no credentials or query in the URL; no redirects; a bounded response; a hard timeout. A
tenant-supplied host is resolved before the request and refused if it points at private
address space, because a name pointing inside the platform is an attempt to reach in, not a
misconfiguration to follow.

### Platform administration is deployment configuration

Workspace roles answer what a member may do inside their tenant. They deliberately say
nothing about publishing a standard agent, because that acts on every tenant at once.
Platform administrators are an operator-listed set of verified token subjects. No API call,
no workspace membership and no agent action can add one, and the Agent Studio surface
answers 404 to everyone else rather than confirming it exists.

Catalog actions are audited to `platform_events`, which is append-only and workspace-free.
Tenant actions — connections added, credentials rotated, agents installed, updated, rolled
back and disabled, bindings changed — continue to go to `security_events`. Neither records
a secret.

### Three release clocks

`scripts/release_scope.py` decides, from the paths a change touched, which release units
must run: the console image, the platform image, the agent packages, the connector
definitions. A change under `agents/social-media/` publishes that agent and rebuilds
nothing; a console change rebuilds the console and republishes no agent. `Agent Release`
runs only on package paths; `Release` builds only the images a change affects.

`scripts/publish_catalog.py` validates every package against the same contract the API
applies and publishes the changed ones. Because a published version is immutable, a release
that forgets to raise a version number is reported rather than silently changing what a
pinned tenant runs.

## Consequences

Nexora can ship an agent fix to a subset of customers, watch it, widen it, and take it back
without redeploying anything a customer can see. A customer can stay on an old version on
purpose. A customer's credential survives sign-out, browser restart and the next day's
sign-in, and when a provider really does revoke it, the connection reports expired with a
reconnect path rather than failing silently.

The costs are real and accepted. The catalog is a second agent model to maintain alongside
custom agents. Master key material is now a deployment prerequisite for the Integrations
feature, and losing it loses every stored credential — deliberately, since the alternative
is a recoverable secret. A connector definition can describe a request shape that does not
match the provider's actual API; that surfaces as a failed connection test rather than as a
silent wrong call, which is why "Test connection" is part of the feature rather than an
afterthought.

Two things are deliberately not done yet. Agent runs do not call connectors during
execution: the runtime exists and is exercised by connection testing, but wiring it into
the worker's tool-execution path is a separate change, so the worker holds no write
privilege on the vault tables. And automatic update mode is recorded per workspace but not
yet acted on by a scheduler; every update today is a decision someone makes.
