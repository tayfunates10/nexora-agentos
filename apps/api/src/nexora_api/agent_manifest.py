"""The standard agent contract — Nexora's agent SDK.

A standard agent is a manifest, not code in the console. Everything a screen needs to
render an agent, and everything the runtime needs to execute one safely, is declared
here: which integrations it must have, which capabilities it exposes, which actions
require a human decision, how it uses models, memory, retrieval, budget and rate limits.

Two consequences are deliberate. Adding an Accounting or HR agent is a new manifest, not
a console release. And an agent never declares a credential: it declares the integration
it needs, and the connector runtime resolves that to a secret the agent never sees.
"""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MANIFEST_ID = r"^[a-z][a-z0-9]*(\.[a-z0-9-]+)+$"
SLUG = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
LABEL = r"^[a-z][a-z0-9_-]{1,39}$"
# Capabilities, permissions and approval-gated actions are dotted action identifiers,
# for example social.content.publish or instagram.comments.read.
ACTION = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*(\.[a-z][a-z0-9]*([_-][a-z0-9]+)*)+$"
TOOL = r"^[a-z][a-z0-9]*([._-][a-z0-9]+)*$"

AgentStatus = Literal["draft", "beta", "stable", "deprecated", "disabled"]
UpdateChannel = Literal["stable", "beta", "canary"]


class Version:
    """A semantic version that orders correctly and states its own compatibility."""

    __slots__ = ("major", "minor", "patch")

    def __init__(self, text: str):
        if not SEMVER.fullmatch(text):
            raise ValueError(f"Not a semantic version: {text}")
        self.major, self.minor, self.patch = (int(part) for part in text.split("."))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def parts(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.parts == other.parts

    def __hash__(self) -> int:
        return hash(self.parts)

    def __lt__(self, other: "Version") -> bool:
        return self.parts < other.parts

    def __le__(self, other: "Version") -> bool:
        return self.parts <= other.parts

    def satisfies_minimum(self, minimum: "Version") -> bool:
        """A runtime serves an agent when it is at least the declared minimum."""
        return minimum <= self

    def bump_kind(self, previous: "Version") -> Literal["major", "minor", "patch"]:
        if self.major != previous.major:
            return "major"
        return "minor" if self.minor != previous.minor else "patch"


class ReasoningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: Literal["low", "medium", "high"] = "medium"
    max_steps: int = Field(default=8, ge=1, le=100)
    # Escalating to the advanced model is a policy the manifest states, not something a
    # run negotiates at execution time.
    escalate_after_steps: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _escalation_within_budget(self) -> Self:
        if self.escalate_after_steps is not None and self.escalate_after_steps > self.max_steps:
            raise ValueError("escalate_after_steps must not exceed max_steps")
        return self


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Profile names, never vendor model identifiers: the operator's runtime configuration
    # decides which provider and model a profile resolves to.
    primary: str = Field(default="default", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    fallback: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    escalation: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Memory never spans tenants. The widest scope available is one workspace.
    scope: Literal["run", "agent", "workspace"] = "agent"
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_items: int = Field(default=200, ge=1, le=10_000)


class RagConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    source_tags: list[str] = Field(default_factory=list, max_length=20)


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_run_micros: int | None = Field(default=None, ge=0, le=10**12)
    daily_micros: int | None = Field(default=None, ge=0, le=10**12)
    max_tokens_per_run: int | None = Field(default=None, ge=1, le=10_000_000)


class RateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs_per_hour: int | None = Field(default=None, ge=1, le=100_000)
    tool_calls_per_run: int | None = Field(default=None, ge=1, le=1000)


class AgentManifest(BaseModel):
    """The published contract of one standard agent version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=MANIFEST_ID, max_length=100)
    slug: str = Field(pattern=SLUG, min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    category: str = Field(pattern=LABEL)
    icon: str = Field(pattern=LABEL)
    version: str = Field(pattern=SEMVER.pattern)
    min_runtime_version: str = Field(default="1.0.0", pattern=SEMVER.pattern)
    status: AgentStatus = "draft"
    channel: UpdateChannel = "stable"
    changelog: str = Field(default="", max_length=5000)

    system_instructions: str = Field(min_length=1, max_length=20_000)
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)

    required_tools: list[str] = Field(default_factory=list, max_length=100)
    optional_tools: list[str] = Field(default_factory=list, max_length=100)
    required_integrations: list[str] = Field(default_factory=list, max_length=20)
    optional_integrations: list[str] = Field(default_factory=list, max_length=20)

    capabilities: list[str] = Field(default_factory=list, max_length=100)
    permissions: list[str] = Field(default_factory=list, max_length=100)
    # Actions the runtime must never perform without a recorded human decision.
    approval_required: list[str] = Field(default_factory=list, max_length=100)

    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    rate_limits: RateLimits = Field(default_factory=RateLimits)

    input_schema: dict[str, object] = Field(default_factory=dict)
    output_schema: dict[str, object] = Field(default_factory=dict)
    settings_schema: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for field, pattern in (
            ("required_tools", TOOL),
            ("optional_tools", TOOL),
            ("required_integrations", SLUG),
            ("optional_integrations", SLUG),
            ("capabilities", ACTION),
            ("permissions", ACTION),
            ("approval_required", ACTION),
        ):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"{field} contains duplicates")
            for value in values:
                if not re.fullmatch(pattern, value) or len(value) > 100:
                    raise ValueError(f"{field} entry is not a valid identifier: {value}")

        if set(self.required_tools) & set(self.optional_tools):
            raise ValueError("A tool is either required or optional, never both")
        if set(self.required_integrations) & set(self.optional_integrations):
            raise ValueError("An integration is either required or optional, never both")
        if not self.id.endswith("." + self.slug):
            raise ValueError("Manifest id must end with the agent slug")
        for name, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
            ("settings_schema", self.settings_schema),
        ):
            if schema and schema.get("type") != "object":
                raise ValueError(f"{name} must describe an object")
        return self

    @property
    def semver(self) -> Version:
        return Version(self.version)

    @property
    def runtime_minimum(self) -> Version:
        return Version(self.min_runtime_version)

    def integrations(self) -> list[tuple[str, bool]]:
        """Every integration the agent references, with whether it is required."""
        return [(name, True) for name in self.required_integrations] + [
            (name, False) for name in self.optional_integrations
        ]

    def requires_approval(self, action: str) -> bool:
        return action in set(self.approval_required)


def define_agent(**fields) -> AgentManifest:
    """Author a manifest in code. Validation is identical to loading one from JSON."""
    return AgentManifest.model_validate(fields)


def load_manifest(document: dict[str, object]) -> AgentManifest:
    return AgentManifest.model_validate(document)


def compare_versions(left: str, right: str) -> int:
    first, second = Version(left), Version(right)
    return (first.parts > second.parts) - (first.parts < second.parts)


def latest(versions: list[str]) -> str | None:
    """The highest semantic version in a list, or None when the list is empty."""
    return max(versions, key=lambda value: Version(value).parts, default=None)
