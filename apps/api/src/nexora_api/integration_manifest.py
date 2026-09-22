"""The connector contract — Nexora's integration SDK.

A connector declares who it is, how it authenticates, what it can do and which fields a
tenant must supply. Adding Mikro, an ERP or a custom REST endpoint is a new definition
published into the registry, not a change to the platform's core code.

Credential *fields* are declared here; credential *values* never are. A field marked
secret is written once into the vault and afterwards exists only as a masked hint.
"""

import ipaddress
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexora_api.tool_contracts import ToolContractError, validate_registration_schema

SEMVER = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
SLUG = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
LABEL = r"^[a-z][a-z0-9_-]{1,39}$"
FIELD_KEY = r"^[a-z][a-z0-9_]{0,63}$"
CAPABILITY = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*(\.[a-z][a-z0-9]*([_-][a-z0-9]+)*)+$"
SCOPE = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"

AuthType = Literal["api_key", "oauth2", "basic", "bearer_token", "webhook"]
ConnectorStatus = Literal["available", "beta", "deprecated", "disabled"]
EndpointSideEffect = Literal["read", "write", "destructive", "external_communication"]

# Which fields each authentication style expects a tenant to provide. A definition that
# contradicts its own auth type is rejected at publish time rather than at connect time.
REQUIRED_SECRET_FIELDS: dict[str, frozenset[str]] = {
    "api_key": frozenset({"api_key"}),
    "bearer_token": frozenset({"access_token"}),
    "basic": frozenset({"password"}),
    "webhook": frozenset({"signing_secret"}),
    "oauth2": frozenset(),
}


def secure_https_url(value: str, label: str) -> str:
    """Reject anything that is not a plain HTTPS endpoint on the public internet.

    Tenants supply base URLs for their own systems, so this runs on tenant input as well
    as on published definitions: no credentials in the URL, no query or fragment, no
    loopback or private literal address.
    """
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or len(value) > 500
    ):
        raise ValueError(f"{label} must be an HTTPS URL on port 443 without credentials or query")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost":
        raise ValueError(f"{label} hostname is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return value
    if not address.is_global:
        raise ValueError(f"{label} literal IP must be globally routable")
    return value


class CredentialPlacement(BaseModel):
    """How the connector runtime attaches a stored secret to an outbound request.

    The agent never sees any of this. It names a capability; the runtime reads the
    placement, opens the credential and builds the request.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["bearer_header", "header", "query", "basic"]
    value_field: str = Field(default="access_token", pattern=FIELD_KEY)
    username_field: str | None = Field(default=None, pattern=FIELD_KEY)
    header: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    query_parameter: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.kind == "header" and not self.header:
            raise ValueError("A header placement must name its header")
        if self.kind == "query" and not self.query_parameter:
            raise ValueError("A query placement must name its parameter")
        if self.kind == "basic" and not self.username_field:
            raise ValueError("A basic placement must name the field holding the user name")
        return self


class ConnectorEndpoint(BaseModel):
    """One executable connector capability and its governed tool contract.

    Path placeholders are resolved from non-secret integration configuration first and
    then from validated tool arguments. Consumed placeholders are removed before the
    remaining arguments are sent as query parameters or a JSON body.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    capability: str = Field(max_length=100)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(min_length=1, max_length=300)
    description: str = Field(default="Connector capability", min_length=1, max_length=1000)
    side_effect: EndpointSideEffect = "read"
    input_schema: dict[str, object] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )
    output_schema: dict[str, object] | None = None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if not re.fullmatch(CAPABILITY, self.capability):
            raise ValueError(f"Invalid capability: {self.capability}")
        if not self.path.startswith("/") or "://" in self.path or ".." in self.path:
            raise ValueError("An endpoint path must be an absolute path under the base URL")
        placeholders = re.findall(r"{([^{}]+)}", self.path)
        if any(not re.fullmatch(FIELD_KEY, item) for item in placeholders):
            raise ValueError("Endpoint path placeholders must be connector field identifiers")
        try:
            validate_registration_schema(self.input_schema, side_effect=self.side_effect)
            if self.output_schema is not None:
                validate_registration_schema(self.output_schema, output=True)
        except ToolContractError as exc:
            raise ValueError(f"Invalid connector tool contract: {exc.code}") from None
        return self

class CredentialField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=FIELD_KEY)
    label: str = Field(min_length=1, max_length=100)
    secret: bool = True
    required: bool = True
    help: str = Field(default="", max_length=300)
    # A connector may constrain the shape of a value, but never its meaning: the platform
    # does not interpret tenant credentials.
    pattern: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _usable_pattern(self) -> Self:
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error:
                raise ValueError(f"Credential field {self.key} has an invalid pattern") from None
        return self


class OAuthConfig(BaseModel):
    """Per-connector OAuth shape. Client credentials stay with the operator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    authorize_url: str = Field(max_length=500)
    token_url: str = Field(max_length=500)
    default_scopes: list[str] = Field(default_factory=list, max_length=50)
    refreshable: bool = True
    # Refresh this long before expiry so a run never starts with a token about to die.
    # Providers that issue long-lived tokens are renewed days ahead, not minutes.
    refresh_margin_seconds: int = Field(default=300, ge=30, le=2_592_000)

    @model_validator(mode="after")
    def _https_endpoints(self) -> Self:
        for name, url in (("authorize_url", self.authorize_url), ("token_url", self.token_url)):
            if not url.startswith("https://"):
                raise ValueError(f"{name} must be an HTTPS URL")
        for scope in self.default_scopes:
            if not re.fullmatch(SCOPE, scope):
                raise ValueError(f"Invalid OAuth scope: {scope}")
        return self


class ConnectorManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=SLUG, min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    category: str = Field(pattern=LABEL)
    icon: str = Field(pattern=LABEL)
    version: str = Field(pattern=SEMVER)
    status: ConnectorStatus = "available"
    auth: AuthType
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    scopes: list[str] = Field(default_factory=list, max_length=100)
    credential_fields: list[CredentialField] = Field(default_factory=list, max_length=20)
    oauth: OAuthConfig | None = None
    # A cheap, read-only call used by "Test connection". Declared, never inferred.
    test_capability: str | None = Field(default=None, max_length=100)
    # Where the connector's API lives. A hosted provider states it here; a tenant-owned
    # system names the credential field the tenant fills in instead.
    base_url: str | None = Field(default=None, max_length=500)
    base_url_field: str | None = Field(default=None, pattern=FIELD_KEY)
    credential_placement: CredentialPlacement | None = None
    endpoints: list[ConnectorEndpoint] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for value in self.capabilities:
            if not re.fullmatch(CAPABILITY, value) or len(value) > 100:
                raise ValueError(f"Invalid capability: {value}")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities contains duplicates")
        for scope in self.scopes:
            if not re.fullmatch(SCOPE, scope):
                raise ValueError(f"Invalid scope: {scope}")
        keys = [field.key for field in self.credential_fields]
        if len(set(keys)) != len(keys):
            raise ValueError("credential_fields contains duplicate keys")
        if self.test_capability and self.test_capability not in self.capabilities:
            raise ValueError("test_capability must be one of the declared capabilities")

        if self.auth == "oauth2":
            if self.oauth is None:
                raise ValueError("An oauth2 connector must declare its oauth configuration")
            unknown = set(self.oauth.default_scopes) - set(self.scopes)
            if unknown:
                raise ValueError(f"OAuth default scopes are not declared: {sorted(unknown)}")
        elif self.oauth is not None:
            raise ValueError("Only an oauth2 connector may declare oauth configuration")

        expected = REQUIRED_SECRET_FIELDS[self.auth]
        declared = {field.key for field in self.credential_fields if field.required}
        missing = expected - declared
        if missing:
            raise ValueError(f"{self.auth} connector is missing fields: {sorted(missing)}")

        if self.base_url is not None:
            secure_https_url(self.base_url, "base_url")
        if self.base_url and self.base_url_field:
            raise ValueError("A connector states its base URL or names a field, never both")
        field_keys = {field.key for field in self.credential_fields}
        if self.base_url_field and self.base_url_field not in field_keys:
            raise ValueError("base_url_field must be a declared credential field")

        if self.endpoints:
            if not self.base_url and not self.base_url_field:
                raise ValueError("A connector with endpoints must have a base URL")
            if self.credential_placement is None:
                raise ValueError("A connector with endpoints must declare credential placement")
            seen = {endpoint.capability for endpoint in self.endpoints}
            if len(seen) != len(self.endpoints):
                raise ValueError("endpoints declare the same capability twice")
            undeclared = sorted(seen - set(self.capabilities))
            if undeclared:
                raise ValueError(f"endpoints reference undeclared capabilities: {undeclared}")
        placement = self.credential_placement
        if placement is not None and self.auth != "oauth2":
            # OAuth tokens are minted by the platform; every other style is filled in by
            # the tenant, so the placement can only use fields the tenant was asked for.
            for name in filter(None, (placement.value_field, placement.username_field)):
                if name not in field_keys:
                    raise ValueError(f"credential_placement references unknown field: {name}")
        return self

    def endpoint_for(self, capability: str) -> ConnectorEndpoint | None:
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.capability == capability), None
        )

    @property
    def secret_field_keys(self) -> frozenset[str]:
        return frozenset(field.key for field in self.credential_fields if field.secret)

    def validate_credential(self, values: dict[str, str]) -> None:
        """Check a tenant's submission against the declared fields, without storing it."""
        declared = {field.key: field for field in self.credential_fields}
        unknown = sorted(set(values) - set(declared))
        if unknown:
            raise ValueError(f"Unknown credential fields: {unknown}")
        for key, field in declared.items():
            value = values.get(key)
            if field.required and not value:
                raise ValueError(f"Missing credential field: {key}")
            if value is not None:
                if not isinstance(value, str) or len(value) > 8192:
                    raise ValueError(f"Credential field {key} must be text")
                if field.pattern and not re.fullmatch(field.pattern, value):
                    raise ValueError(f"Credential field {key} does not match its expected format")


def define_integration(**fields) -> ConnectorManifest:
    """Author a connector definition in code; identical validation to loading JSON."""
    return ConnectorManifest.model_validate(fields)


def load_connector(document: dict[str, object]) -> ConnectorManifest:
    return ConnectorManifest.model_validate(document)
