# Connector definitions

Each directory is one service a workspace can connect, and each holds a single
`connector.json`. Adding a service is a new definition published into the registry — not a
change to platform code and not a console release.

## What a definition contains

- **Identity**: `id`, `name`, `description`, `category`, `icon`, `version`, `status`.
- **Authentication**: `auth` is one of `api_key`, `oauth2`, `basic`, `bearer_token` or
  `webhook`. An `oauth2` connector also declares its `authorize_url`, `token_url`, default
  scopes and how far ahead of expiry its token should be renewed.
- **Capabilities**: the dotted actions this service can perform, and `test_capability`, the
  cheap read-only one used by "Test connection".
- **Credential fields**: what a tenant is asked for. A field marked `secret` is written once
  into the vault and afterwards exists only as a masked hint; a field that is not secret,
  such as a base URL or an account number, stays readable so a person can recognise the
  connection.
- **Request shapes**: `base_url` (or `base_url_field` for a system the tenant hosts),
  `credential_placement`, and an `endpoints` entry per capability the runtime can call.

Values never appear here. A definition describes the fields a tenant fills in; the values
they fill in go to the vault.

## Safety rules the contract enforces

An endpoint path is absolute and cannot escape the base URL. A base URL must be HTTPS on
port 443 with no credentials and no query. A tenant-supplied host is resolved before each
request and refused if it points at private address space.

Validate a change with `python scripts/publish_catalog.py --check`. Connectors are
published before agents, so an agent that requires one always finds it registered.
