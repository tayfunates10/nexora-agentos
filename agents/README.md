# Standard agent packages

Each directory is one standard agent Nexora publishes, and each holds a single
`manifest.json`: the whole contract for that agent. There is no code here, because a
standard agent is a document the platform validates, stores and serves — not a module the
console imports.

## Adding or changing an agent

1. Edit `<slug>/manifest.json`, or create a new directory whose name is the agent's slug.
2. Raise `version` following semantic versioning:
   - **patch** for a fix that changes no contract,
   - **minor** for a backward-compatible addition,
   - **major** for a breaking change to inputs, outputs, settings or approval behaviour.
3. Describe the change in `changelog`. Customers read it on the update screen.
4. Validate locally: `python scripts/publish_catalog.py --check`.

A published version is immutable. Publishing the same number twice is refused, so a
customer pinned to `1.4.2` always runs the `1.4.2` that was reviewed. Correcting a mistake
means publishing a higher version, never editing a released one.

## What the fields mean

`status` and `channel` decide who is offered the release: a workspace on the stable channel
sees only `stable` releases, a beta workspace sees stable and beta, a canary workspace sees
everything. `min_runtime_version` is refused rather than downgraded — an agent that needs a
newer platform than a deployment runs simply will not install there.

`required_integrations` are what the agent cannot work without. The console names them
before a customer adds the agent, and the instance stays paused until each one is bound to
a connected account. `optional_integrations` widen what the agent can do when they are
present.

`approval_required` lists actions that must never happen without a recorded human decision.
Anything that speaks in public, spends money or deletes something belongs here.

An agent never declares a credential. It declares the integration it needs; the connector
runtime resolves that to a secret the agent never sees.

## Releasing

`agents/**` has its own pipeline. Changing a manifest runs the agent release workflow and
nothing else: the console is not rebuilt and no other agent is republished.
