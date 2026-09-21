"""The release scripts decide what ships, so their rules are tested like any other.

Both are loaded by path: they are build-time tools and deliberately not part of the
runtime package, so nothing in the published image depends on them.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrations = load("check_migrations")
promote = load("promote_release")

OVERLAY = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: nexora
resources:
  - ../../base
# Promote the exact artifact that passed CI.
images:
  - name: nexora/api
    newName: REPLACE_WITH_REGISTRY/nexora-api
    digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
  - name: nexora/web
    newName: REPLACE_WITH_REGISTRY/nexora-web
    digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
replicas:
  - name: nexora-api
    count: 3
"""
DIGEST = "sha256:" + "ab" * 32


def test_adding_a_migration_is_allowed():
    base = {"001_a.sql": "aa", "002_b.sql": "bb"}
    head = dict(base, **{"003_c.sql": "cc"})

    assert migrations.violations(base, head) == []


def test_rewriting_or_deleting_released_history_is_refused():
    base = {"001_a.sql": "aa", "002_b.sql": "bb"}

    assert migrations.violations(base, {"001_a.sql": "changed", "002_b.sql": "bb"}) == [
        "001_a.sql: released migration was modified"
    ]
    assert migrations.violations(base, {"001_a.sql": "aa"}) == [
        "002_b.sql: released migration was deleted"
    ]


def test_two_branches_claiming_one_number_are_refused():
    head = {"001_a.sql": "aa", "002_b.sql": "bb", "002_c.sql": "cc"}

    problems = migrations.violations({}, head)

    assert problems == ["002: duplicate migration number across 002_b.sql, 002_c.sql"]


@pytest.mark.parametrize("name", ["7_late.sql", "007-Late.sql", "007_Late.sql", "notes.sql"])
def test_unordered_names_are_refused(name):
    assert migrations.violations({}, {name: "aa"}) == [f"{name}: expected NNN_lower_snake_case.sql"]


def test_real_repository_history_is_append_only():
    # The checker must agree with the runtime's own checksum rule on real files.
    current = migrations.current(
        Path(__file__).resolve().parents[1] / "src" / "nexora_api" / "migrations"
    )

    assert len(current) >= 6
    assert migrations.violations(current, current) == []


def test_promotion_pins_only_the_named_image():
    promoted = promote.pin_digest(OVERLAY, "nexora/api", DIGEST, "ghcr.io/acme/nexora-api")
    parsed = yaml.safe_load(promoted)
    images = {image["name"]: image for image in parsed["images"]}

    assert images["nexora/api"]["digest"] == DIGEST
    assert images["nexora/api"]["newName"] == "ghcr.io/acme/nexora-api"
    assert images["nexora/web"]["digest"].endswith("0000")
    assert parsed["replicas"][0]["count"] == 3


def test_promotion_preserves_comments_and_layout():
    promoted = promote.pin_digest(OVERLAY, "nexora/web", DIGEST)

    assert "# Promote the exact artifact that passed CI." in promoted
    assert len(promoted.splitlines()) == len(OVERLAY.splitlines())


@pytest.mark.parametrize(
    "digest",
    ["latest", "sha256:short", "sha512:" + "ab" * 32, "sha256:" + "AB" * 32, ""],
)
def test_only_a_well_formed_digest_can_be_promoted(digest):
    with pytest.raises(promote.PromotionError, match="digest"):
        promote.pin_digest(OVERLAY, "nexora/api", digest)


def test_an_image_the_overlay_does_not_declare_is_refused():
    with pytest.raises(promote.PromotionError, match="no digest field"):
        promote.pin_digest(OVERLAY, "nexora/unknown", DIGEST)


def test_a_tagged_entry_must_lose_its_tag_before_promotion():
    tagged = OVERLAY.replace(
        "    digest: sha256:0000000000000000000000000000000000000000000000000000000000000000\n",
        "    newTag: v1\n",
        1,
    )

    # A tag beside a digest leaves it ambiguous which artifact actually ships.
    with pytest.raises(promote.PromotionError, match="newTag"):
        promote.pin_digest(tagged, "nexora/api", DIGEST)


def test_both_staging_overlays_are_promotable_as_shipped():
    root = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "overlays" / "staging"

    for overlay in (root / "kustomization.yaml", root / "migrate" / "kustomization.yaml"):
        promoted = promote.pin_digest(overlay.read_text(), "nexora/api", DIGEST)

        pinned = {image["name"]: image["digest"] for image in yaml.safe_load(promoted)["images"]}
        assert pinned["nexora/api"] == DIGEST


def test_the_production_overlay_is_promotable_as_shipped():
    overlay = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "k8s"
        / "overlays"
        / "production"
        / "kustomization.yaml"
    )

    promoted = promote.pin_digest(overlay.read_text(), "nexora/api", DIGEST)

    assert yaml.safe_load(promoted)["images"][0]["digest"] == DIGEST
