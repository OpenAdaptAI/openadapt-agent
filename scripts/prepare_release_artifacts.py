"""Build and verify the exact artifact handoff for an admitted Agent release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INVENTORY_SCHEMA = "openadapt.production-release-artifact-inventory/v1"
TAG_BINDING_SCHEMA = "openadapt.production-release-tag-binding/v1"
STAGING_SCHEMA = "openadapt.production-release-staging-evidence/v1"
TAG_RULESET_SCHEMA = "openadapt.production-release-tag-ruleset/v1"
REFERENCE_SCHEMA = "openadapt.production-evidence-object-reference/v2"
TARGET = "agent"
CLAIM_SCOPE = "production_agent"
REFERENCE_DOMAIN = b"OpenAdapt production release tag admission reference v1\0"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

INVENTORY_KEYS = {"schema_version", "target", "claim_scope", "artifacts"}
ARTIFACT_KEYS = {
    "name",
    "kind",
    "sha256",
    "size_bytes",
    "media_type",
    "publish_destinations",
}
REFERENCE_KEYS = {
    "schema_version",
    "repository",
    "repository_id",
    "repository_owner_id",
    "registry_source_commit",
    "registry_revision",
    "registry_head_sha256",
    "registry_entry_sha256",
    "kind",
    "object_schema_version",
    "object_path",
    "object_sha256",
    "size_bytes",
    "object_media_type",
    "semantic_identity_sha256",
    "subject_sha256",
}
TAG_BINDING_KEYS = {
    "schema_version",
    "admission_reference",
    "admission_reference_sha256",
    "artifact_inventory_sha256",
}
STAGING_KEYS = {
    "schema_version",
    "repository",
    "repository_id",
    "draft_release_id",
    "tag",
    "target_commitish",
    "draft",
    "prerelease",
    "release_app_id",
    "release_app_installation_id",
    "release_app_bot_user_id",
    "release_author_login",
    "assets",
    "immutable_releases",
    "immutable_releases_sha256",
    "tag_ref_state",
    "tag_ref_state_sha256",
    "tag_rulesets",
    "tag_rulesets_sha256",
    "observed_at",
}
STAGED_ASSET_KEYS = {
    "asset_id",
    "name",
    "kind",
    "sha256",
    "size_bytes",
    "media_type",
    "publish_destinations",
    "uploader_id",
    "uploader_login",
}
TAG_RULESET_KEYS = {
    "schema_version",
    "role",
    "repository",
    "repository_id",
    "ruleset_id",
    "name",
    "target",
    "enforcement",
    "bypass_actors",
    "conditions",
    "rules",
}
STAGING_DOMAIN = b"OpenAdapt production release staging evidence v1\0"
TAG_RULESETS_DOMAIN = b"OpenAdapt production release tag rulesets v1\0"
IMMUTABLE_RELEASES_DOMAIN = b"OpenAdapt production immutable releases response v1\0"
TAG_REF_STATE_DOMAIN = b"OpenAdapt production release tag ref state v1\0"
CREATION_RULESET_NAME = "OpenAdapt policy: release tag creation"
IMMUTABILITY_RULESET_NAME = "OpenAdapt policy: immutable release tags"
REPOSITORY = "OpenAdaptAI/openadapt-agent"
REPOSITORY_ID = "1136136670"
RELEASE_APP_ID = "4730708"
RELEASE_APP_INSTALLATION_ID = "156835568"
RELEASE_APP_ACTOR_ID = "321543906"
RELEASE_APP_LOGIN = "openadapt-release[bot]"
DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class ArtifactHandoffError(RuntimeError):
    """The candidate artifact handoff is invalid."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the shared compact canonical JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, *, kind: str, media_type: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ArtifactHandoffError(f"release artifact must be a regular file: {path.name}")
    if SAFE_NAME.fullmatch(path.name) is None:
        raise ArtifactHandoffError(f"release artifact has an unsafe name: {path.name}")
    size = path.stat().st_size
    if size < 1:
        raise ArtifactHandoffError(f"release artifact is empty: {path.name}")
    return {
        "name": path.name,
        "kind": kind,
        "sha256": "sha256:" + _sha256_file(path),
        "size_bytes": size,
        "media_type": media_type,
        "publish_destinations": ["github-release", "pypi"],
    }


def build_artifact_inventory(dist: Path) -> dict[str, object]:
    """Inventory exactly one wheel and one source archive."""

    try:
        children = sorted(dist.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ArtifactHandoffError(f"could not read the distribution directory: {exc}") from exc
    wheel = [path for path in children if path.name.endswith(".whl")]
    sdist = [path for path in children if path.name.endswith(".tar.gz")]
    expected = {*wheel, *sdist}
    unexpected = [path.name for path in children if path not in expected]
    if len(wheel) != 1 or len(sdist) != 1 or unexpected:
        raise ArtifactHandoffError(
            "distribution must contain exactly one wheel and one source archive"
        )
    artifacts = [
        _artifact(sdist[0], kind="python-sdist", media_type="application/gzip"),
        _artifact(wheel[0], kind="python-wheel", media_type="application/zip"),
    ]
    artifacts.sort(key=lambda item: (str(item["kind"]), str(item["name"]), str(item["sha256"])))
    return {
        "schema_version": INVENTORY_SCHEMA,
        "target": TARGET,
        "claim_scope": CLAIM_SCOPE,
        "artifacts": artifacts,
    }


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactHandoffError(f"{label} is missing or invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactHandoffError(f"{label} must be a JSON object")
    return value


def load_json_array(path: Path, label: str) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactHandoffError(f"{label} is missing or invalid: {exc}") from exc
    if not isinstance(value, list):
        raise ArtifactHandoffError(f"{label} must be a JSON array")
    return value


def verify_artifact_inventory(inventory: dict[str, Any], dist: Path) -> None:
    """Verify the closed metadata object against every local artifact byte."""

    if set(inventory) != INVENTORY_KEYS:
        raise ArtifactHandoffError("artifact inventory fields differ from the closed contract")
    expected = build_artifact_inventory(dist)
    if inventory != expected:
        raise ArtifactHandoffError("artifact inventory differs from the exact distribution bytes")
    if canonical_json_bytes(inventory) != canonical_json_bytes(expected):
        raise ArtifactHandoffError("artifact inventory is not canonically equivalent")


def verify_artifact_subset(inventory: dict[str, Any], dist: Path) -> None:
    """Verify zero or more staged files without accepting an unexpected file."""

    if set(inventory) != INVENTORY_KEYS:
        raise ArtifactHandoffError("artifact inventory fields differ from the closed contract")
    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ArtifactHandoffError("artifact inventory must contain the two Agent archives")
    expected: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
            raise ArtifactHandoffError("artifact fields differ from the closed contract")
        name = artifact.get("name")
        if not isinstance(name, str) or name in expected:
            raise ArtifactHandoffError("artifact names must be unique strings")
        expected[name] = artifact
    try:
        children = sorted(dist.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ArtifactHandoffError(f"could not read the staged distribution: {exc}") from exc
    for path in children:
        artifact = expected.get(path.name)
        if artifact is None:
            raise ArtifactHandoffError(f"staged release has an unexpected asset: {path.name}")
        if not path.is_file() or path.is_symlink():
            raise ArtifactHandoffError(f"staged release asset is not a regular file: {path.name}")
        if path.stat().st_size != artifact.get("size_bytes"):
            raise ArtifactHandoffError(f"staged release asset size differs: {path.name}")
        if "sha256:" + _sha256_file(path) != artifact.get("sha256"):
            raise ArtifactHandoffError(f"staged release asset digest differs: {path.name}")


def _decimal_id(value: Any, label: str) -> str:
    normalized = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(normalized, str) or DECIMAL_ID.fullmatch(normalized) is None:
        raise ArtifactHandoffError(f"{label} must be a positive decimal ID")
    return normalized


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ArtifactHandoffError(f"{label} must be an exact UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ArtifactHandoffError(f"{label} is not a calendar timestamp") from exc
    return value


def _artifact_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if set(inventory) != INVENTORY_KEYS:
        raise ArtifactHandoffError("artifact inventory fields differ from the closed contract")
    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ArtifactHandoffError("artifact inventory must contain the two Agent archives")
    result: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
            raise ArtifactHandoffError("artifact fields differ from the closed contract")
        name = artifact.get("name")
        if (
            not isinstance(name, str)
            or SAFE_NAME.fullmatch(name) is None
            or name in result
            or name.casefold() in folded
        ):
            raise ArtifactHandoffError("artifact names must be safe and unique")
        if not isinstance(artifact.get("sha256"), str) or DIGEST.fullmatch(artifact["sha256"]) is None:
            raise ArtifactHandoffError(f"artifact digest is invalid: {name}")
        destinations = artifact.get("publish_destinations")
        if destinations != ["github-release", "pypi"]:
            raise ArtifactHandoffError(f"artifact destinations differ: {name}")
        result[name] = artifact
        folded.add(name.casefold())
    if artifacts != sorted(
        artifacts, key=lambda item: (str(item["kind"]), str(item["name"]), str(item["sha256"]))
    ):
        raise ArtifactHandoffError("artifacts must be sorted by kind, name, and digest")
    return result


def verify_release_assets(
    inventory: dict[str, Any],
    release: dict[str, Any],
    assets: list[Any],
    asset_root: Path,
    *,
    expected_tag: str,
    expected_source_commit: str,
    required_state: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Verify the App-authored GitHub Release and every downloaded asset byte."""

    if required_state not in {"draft", "published", "either"}:
        raise ArtifactHandoffError("release state selector is invalid")
    artifact_by_name = _artifact_map(inventory)
    verify_artifact_inventory(inventory, asset_root)
    if not isinstance(release, dict):
        raise ArtifactHandoffError("GitHub Release response must be an object")
    release_id = _decimal_id(release.get("id"), "GitHub Release ID")
    if release.get("tag_name") != expected_tag:
        raise ArtifactHandoffError("GitHub Release tag differs")
    if release.get("target_commitish") != expected_source_commit or HEX40.fullmatch(
        expected_source_commit
    ) is None:
        raise ArtifactHandoffError("GitHub Release target commit differs")
    if release.get("prerelease") is not False:
        raise ArtifactHandoffError("GitHub Release must not be a prerelease")
    author = release.get("author")
    if (
        not isinstance(author, dict)
        or _decimal_id(author.get("id"), "GitHub Release author ID") != RELEASE_APP_ACTOR_ID
        or author.get("login") != RELEASE_APP_LOGIN
    ):
        raise ArtifactHandoffError("GitHub Release author is not the release App")
    is_draft = release.get("draft")
    if not isinstance(is_draft, bool):
        raise ArtifactHandoffError("GitHub Release draft state is invalid")
    state = "draft" if is_draft else "published"
    if required_state != "either" and state != required_state:
        raise ArtifactHandoffError(f"GitHub Release state differs; expected {required_state}")
    if state == "published" and release.get("immutable") is not True:
        raise ArtifactHandoffError("published GitHub Release is not immutable")
    if not isinstance(assets, list) or len(assets) != len(artifact_by_name):
        raise ArtifactHandoffError("GitHub Release must contain the exact Agent asset set")
    normalized_assets: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ArtifactHandoffError("GitHub Release asset must be an object")
        name = asset.get("name")
        artifact = artifact_by_name.get(name) if isinstance(name, str) else None
        if artifact is None or name in seen_names:
            raise ArtifactHandoffError("GitHub Release assets contain an unexpected name")
        asset_id = _decimal_id(asset.get("id"), f"GitHub Release asset ID for {name}")
        if asset_id in seen_ids:
            raise ArtifactHandoffError("GitHub Release asset IDs must be unique")
        uploader = asset.get("uploader")
        if (
            not isinstance(uploader, dict)
            or _decimal_id(uploader.get("id"), f"GitHub Release uploader ID for {name}")
            != RELEASE_APP_ACTOR_ID
            or uploader.get("login") != RELEASE_APP_LOGIN
        ):
            raise ArtifactHandoffError(f"GitHub Release asset uploader differs: {name}")
        if (
            asset.get("state") != "uploaded"
            or asset.get("size") != artifact["size_bytes"]
            or asset.get("digest") != artifact["sha256"]
            or asset.get("content_type") != artifact["media_type"]
        ):
            raise ArtifactHandoffError(f"GitHub Release asset metadata differs: {name}")
        normalized_assets.append(
            {
                "asset_id": asset_id,
                "name": name,
                "kind": artifact["kind"],
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "media_type": artifact["media_type"],
                "publish_destinations": artifact["publish_destinations"],
                "uploader_id": RELEASE_APP_ACTOR_ID,
                "uploader_login": RELEASE_APP_LOGIN,
            }
        )
        seen_names.add(name)
        seen_ids.add(asset_id)
    normalized_assets.sort(key=lambda item: (str(item["name"]), str(item["asset_id"])))
    return release_id, state, normalized_assets


def _normalize_ruleset(
    value: dict[str, Any], *, role: str
) -> dict[str, Any]:
    if role not in {"creation_authority", "immutability"}:
        raise ArtifactHandoffError("tag ruleset role is invalid")
    if not isinstance(value, dict):
        raise ArtifactHandoffError("tag ruleset response must be an object")
    expected_name = CREATION_RULESET_NAME if role == "creation_authority" else IMMUTABILITY_RULESET_NAME
    if (
        value.get("name") != expected_name
        or value.get("target") != "tag"
        or value.get("enforcement") != "active"
    ):
        raise ArtifactHandoffError(f"{role} tag ruleset identity differs")
    conditions = value.get("conditions")
    if not isinstance(conditions, dict) or set(conditions) != {"ref_name"}:
        raise ArtifactHandoffError(f"{role} tag ruleset conditions differ")
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict) or ref_name != {
        "include": ["refs/tags/v*"],
        "exclude": [],
    }:
        raise ArtifactHandoffError(f"{role} tag ruleset ref pattern differs")
    rules = value.get("rules")
    if not isinstance(rules, list):
        raise ArtifactHandoffError(f"{role} tag ruleset rules are invalid")
    if role == "creation_authority":
        expected_actors = [
            {
                "actor_id": int(RELEASE_APP_ID),
                "actor_type": "Integration",
                "bypass_mode": "always",
            }
        ]
        if value.get("bypass_actors") != expected_actors or rules != [{"type": "creation"}]:
            raise ArtifactHandoffError("creation tag authority differs")
        normalized_rules = [{"type": "creation"}]
    else:
        if value.get("bypass_actors") != [] or len(rules) != 3:
            raise ArtifactHandoffError("immutable tag authority differs")
        by_type = {
            rule.get("type"): rule
            for rule in rules
            if isinstance(rule, dict) and isinstance(rule.get("type"), str)
        }
        if set(by_type) != {"deletion", "non_fast_forward", "update"}:
            raise ArtifactHandoffError("immutable tag rules differ")
        if by_type["deletion"] != {"type": "deletion"} or by_type["non_fast_forward"] != {
            "type": "non_fast_forward"
        }:
            raise ArtifactHandoffError("immutable tag rules contain unexpected parameters")
        if by_type["update"] != {
            "type": "update",
            "parameters": {"update_allows_fetch_and_merge": False},
        }:
            raise ArtifactHandoffError("immutable tag update rule differs")
        normalized_rules = [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "update",
                "parameters": {"update_allows_fetch_and_merge": False},
            },
        ]
    normalized = {
        "schema_version": TAG_RULESET_SCHEMA,
        "role": role,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "ruleset_id": _decimal_id(value.get("id"), f"{role} tag ruleset ID"),
        "name": expected_name,
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": (
            []
            if role == "immutability"
            else [
                {
                    "actor_id": RELEASE_APP_ID,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ]
        ),
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "rules": normalized_rules,
    }
    if set(normalized) != TAG_RULESET_KEYS:
        raise ArtifactHandoffError("normalized tag ruleset fields differ")
    return normalized


def build_publication_staging(
    inventory: dict[str, Any],
    release: dict[str, Any],
    assets: list[Any],
    creation_ruleset: dict[str, Any],
    immutability_ruleset: dict[str, Any],
    immutable_releases: dict[str, Any],
    tag_ref_state: dict[str, Any],
    asset_root: Path,
    *,
    expected_tag: str,
    expected_source_commit: str,
    observed_at: str,
) -> dict[str, Any]:
    """Build the closed evidence object used to qualify a staged release."""

    release_id, state, normalized_assets = verify_release_assets(
        inventory,
        release,
        assets,
        asset_root,
        expected_tag=expected_tag,
        expected_source_commit=expected_source_commit,
        required_state="draft",
    )
    if state != "draft":
        raise ArtifactHandoffError("publication staging requires a draft GitHub Release")
    if (
        set(immutable_releases) != {"enabled", "enforced_by_owner"}
        or immutable_releases.get("enabled") is not True
        or not isinstance(immutable_releases.get("enforced_by_owner"), bool)
    ):
        raise ArtifactHandoffError("immutable GitHub Releases must be enabled")
    expected_ref_state = {"ref": f"refs/tags/{expected_tag}", "exists": False}
    if tag_ref_state != expected_ref_state:
        raise ArtifactHandoffError("the prospective release tag must not exist during staging")
    rulesets = [
        _normalize_ruleset(creation_ruleset, role="creation_authority"),
        _normalize_ruleset(immutability_ruleset, role="immutability"),
    ]
    staging = {
        "schema_version": STAGING_SCHEMA,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "draft_release_id": release_id,
        "tag": expected_tag,
        "target_commitish": expected_source_commit,
        "draft": True,
        "prerelease": False,
        "release_app_id": RELEASE_APP_ID,
        "release_app_installation_id": RELEASE_APP_INSTALLATION_ID,
        "release_app_bot_user_id": RELEASE_APP_ACTOR_ID,
        "release_author_login": RELEASE_APP_LOGIN,
        "assets": normalized_assets,
        "immutable_releases": immutable_releases,
        "immutable_releases_sha256": _sha256_bytes(
            IMMUTABLE_RELEASES_DOMAIN + canonical_json_bytes(immutable_releases)
        ),
        "tag_ref_state": tag_ref_state,
        "tag_ref_state_sha256": _sha256_bytes(
            TAG_REF_STATE_DOMAIN + canonical_json_bytes(tag_ref_state)
        ),
        "tag_rulesets": rulesets,
        "tag_rulesets_sha256": _sha256_bytes(
            TAG_RULESETS_DOMAIN + canonical_json_bytes(rulesets)
        ),
        "observed_at": _timestamp(observed_at, "publication staging observed_at"),
    }
    if set(staging) != STAGING_KEYS:
        raise ArtifactHandoffError("publication staging fields differ from the closed contract")
    return staging


def parse_admission_reference(value: str) -> dict[str, Any]:
    """Parse the closed v2 central-registry reference carried by the tag."""

    try:
        reference = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ArtifactHandoffError(f"admission reference is invalid JSON: {exc}") from exc
    if not isinstance(reference, dict) or set(reference) != REFERENCE_KEYS:
        raise ArtifactHandoffError("admission reference fields differ from the closed v2 contract")
    if reference.get("schema_version") != REFERENCE_SCHEMA:
        raise ArtifactHandoffError("admission reference schema is invalid")
    if reference.get("kind") != "qualification-release":
        raise ArtifactHandoffError("admission reference kind is invalid")
    if reference.get("subject_sha256") is not None:
        raise ArtifactHandoffError("the regular admission object must not have a subject")
    for key in (
        "registry_head_sha256",
        "registry_entry_sha256",
        "object_sha256",
        "semantic_identity_sha256",
    ):
        if not isinstance(reference.get(key), str) or DIGEST.fullmatch(reference[key]) is None:
            raise ArtifactHandoffError(f"admission reference {key} is invalid")
    if (
        not isinstance(reference.get("registry_source_commit"), str)
        or HEX40.fullmatch(reference["registry_source_commit"]) is None
    ):
        raise ArtifactHandoffError("admission reference registry source commit is invalid")
    return reference


def build_tag_binding(
    admission_reference_json: str, artifact_inventory_sha256: str
) -> dict[str, object]:
    """Build the canonical annotation that binds an immutable tag to admission."""

    reference = parse_admission_reference(admission_reference_json)
    if DIGEST.fullmatch(artifact_inventory_sha256) is None:
        raise ArtifactHandoffError("artifact inventory digest must be a lowercase sha256 digest")
    reference_sha256 = _sha256_bytes(REFERENCE_DOMAIN + canonical_json_bytes(reference))
    return {
        "schema_version": TAG_BINDING_SCHEMA,
        "admission_reference": reference,
        "admission_reference_sha256": reference_sha256,
        "artifact_inventory_sha256": artifact_inventory_sha256,
    }


def verify_tag_binding(
    binding: dict[str, Any], admission_reference_json: str, artifact_inventory_sha256: str
) -> None:
    """Verify an existing tag message before a release recovery continues."""

    if set(binding) != TAG_BINDING_KEYS:
        raise ArtifactHandoffError("tag binding fields differ from the closed contract")
    expected = build_tag_binding(admission_reference_json, artifact_inventory_sha256)
    if binding != expected:
        raise ArtifactHandoffError("tag binding differs from the admitted release")


def verify_tag_object(
    tag_object: Path, admission_reference_json: str, artifact_inventory_sha256: str
) -> None:
    """Verify the raw annotated-tag message, including its one trailing LF."""

    try:
        raw = tag_object.read_bytes()
    except OSError as exc:
        raise ArtifactHandoffError(f"could not read the annotated tag object: {exc}") from exc
    try:
        headers, message = raw.split(b"\n\n", 1)
    except ValueError as exc:
        raise ArtifactHandoffError("annotated tag object has no message boundary") from exc
    header_lines = headers.splitlines()
    required_prefixes = (b"object ", b"type commit", b"tag ", b"tagger ")
    if len(header_lines) != 4 or not all(
        line.startswith(prefix) for line, prefix in zip(header_lines, required_prefixes, strict=True)
    ):
        raise ArtifactHandoffError("annotated tag object headers differ from the closed shape")
    expected = build_tag_binding(admission_reference_json, artifact_inventory_sha256)
    if message != canonical_json_bytes(expected) + b"\n":
        raise ArtifactHandoffError("annotated tag message differs from the canonical binding")


def _write_exact(path: Path, value: object, *, trailing_lf: bool = False) -> None:
    body = canonical_json_bytes(value) + (b"\n" if trailing_lf else b"")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    except OSError as exc:
        raise ArtifactHandoffError(f"could not write {path}: {exc}") from exc


def _write_github_output(path: Path, name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ArtifactHandoffError(f"GitHub output {name} must be one line")
    try:
        with path.open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    except OSError as exc:
        raise ArtifactHandoffError(f"could not write GitHub output: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--dist", type=Path, required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)
    inventory_parser.add_argument("--github-output", type=Path)

    verify_parser = subparsers.add_parser("verify-inventory")
    verify_parser.add_argument("--dist", type=Path, required=True)
    verify_parser.add_argument("--inventory", type=Path, required=True)

    subset_parser = subparsers.add_parser("verify-subset")
    subset_parser.add_argument("--dist", type=Path, required=True)
    subset_parser.add_argument("--inventory", type=Path, required=True)

    release_assets_parser = subparsers.add_parser("verify-release-assets")
    release_assets_parser.add_argument("--inventory", type=Path, required=True)
    release_assets_parser.add_argument("--release", type=Path, required=True)
    release_assets_parser.add_argument("--assets", type=Path, required=True)
    release_assets_parser.add_argument("--asset-root", type=Path, required=True)
    release_assets_parser.add_argument("--expected-tag", required=True)
    release_assets_parser.add_argument("--expected-source-commit", required=True)
    release_assets_parser.add_argument(
        "--require-state", choices=("draft", "published", "either"), required=True
    )
    release_assets_parser.add_argument("--github-output", type=Path)

    staging_parser = subparsers.add_parser("staging-evidence")
    staging_parser.add_argument("--inventory", type=Path, required=True)
    staging_parser.add_argument("--release", type=Path, required=True)
    staging_parser.add_argument("--assets", type=Path, required=True)
    staging_parser.add_argument("--creation-ruleset", type=Path, required=True)
    staging_parser.add_argument("--immutability-ruleset", type=Path, required=True)
    staging_parser.add_argument("--immutable-releases", type=Path, required=True)
    staging_parser.add_argument("--tag-ref-state", type=Path, required=True)
    staging_parser.add_argument("--asset-root", type=Path, required=True)
    staging_parser.add_argument("--expected-tag", required=True)
    staging_parser.add_argument("--expected-source-commit", required=True)
    staging_parser.add_argument("--observed-at", required=True)
    staging_parser.add_argument("--output", type=Path, required=True)
    staging_parser.add_argument("--github-output", type=Path)

    binding_parser = subparsers.add_parser("tag-binding")
    binding_parser.add_argument("--admission-reference-json", required=True)
    binding_parser.add_argument("--artifact-inventory-sha256", required=True)
    binding_parser.add_argument("--output", type=Path, required=True)

    verify_binding_parser = subparsers.add_parser("verify-tag-binding")
    verify_binding_parser.add_argument("--admission-reference-json", required=True)
    verify_binding_parser.add_argument("--artifact-inventory-sha256", required=True)
    verify_binding_parser.add_argument("--binding", type=Path, required=True)

    verify_tag_parser = subparsers.add_parser("verify-tag-object")
    verify_tag_parser.add_argument("--admission-reference-json", required=True)
    verify_tag_parser.add_argument("--artifact-inventory-sha256", required=True)
    verify_tag_parser.add_argument("--tag-object", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "inventory":
            inventory = build_artifact_inventory(args.dist)
            _write_exact(args.output, inventory)
            if args.github_output is not None:
                _write_github_output(
                    args.github_output,
                    "artifact_inventory_json",
                    canonical_json_bytes(inventory).decode("utf-8"),
                )
        elif args.command == "verify-inventory":
            verify_artifact_inventory(load_json_object(args.inventory, "artifact inventory"), args.dist)
        elif args.command == "verify-subset":
            verify_artifact_subset(load_json_object(args.inventory, "artifact inventory"), args.dist)
        elif args.command == "verify-release-assets":
            assets_value = load_json_array(args.assets, "GitHub Release assets")
            release_id, state, _ = verify_release_assets(
                load_json_object(args.inventory, "artifact inventory"),
                load_json_object(args.release, "GitHub Release"),
                assets_value,
                args.asset_root,
                expected_tag=args.expected_tag,
                expected_source_commit=args.expected_source_commit,
                required_state=args.require_state,
            )
            if args.github_output is not None:
                _write_github_output(args.github_output, "draft_release_id", release_id)
                _write_github_output(args.github_output, "release_state", state)
        elif args.command == "staging-evidence":
            assets_value = load_json_array(args.assets, "GitHub Release assets")
            staging = build_publication_staging(
                load_json_object(args.inventory, "artifact inventory"),
                load_json_object(args.release, "GitHub Release"),
                assets_value,
                load_json_object(args.creation_ruleset, "creation tag ruleset"),
                load_json_object(args.immutability_ruleset, "immutability tag ruleset"),
                load_json_object(args.immutable_releases, "immutable Releases state"),
                load_json_object(args.tag_ref_state, "prospective tag ref state"),
                args.asset_root,
                expected_tag=args.expected_tag,
                expected_source_commit=args.expected_source_commit,
                observed_at=args.observed_at,
            )
            _write_exact(args.output, staging)
            if args.github_output is not None:
                _write_github_output(
                    args.github_output,
                    "publication_staging_json",
                    canonical_json_bytes(staging).decode("utf-8"),
                )
                _write_github_output(
                    args.github_output,
                    "publication_staging_sha256",
                    _sha256_bytes(STAGING_DOMAIN + canonical_json_bytes(staging)),
                )
                _write_github_output(
                    args.github_output,
                    "draft_release_id",
                    str(staging["draft_release_id"]),
                )
        elif args.command == "tag-binding":
            binding = build_tag_binding(
                args.admission_reference_json, args.artifact_inventory_sha256
            )
            _write_exact(args.output, binding, trailing_lf=True)
        elif args.command == "verify-tag-binding":
            verify_tag_binding(
                load_json_object(args.binding, "tag binding"),
                args.admission_reference_json,
                args.artifact_inventory_sha256,
            )
        elif args.command == "verify-tag-object":
            verify_tag_object(
                args.tag_object,
                args.admission_reference_json,
                args.artifact_inventory_sha256,
            )
    except ArtifactHandoffError as exc:
        print(f"RELEASE ARTIFACT HANDOFF REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
