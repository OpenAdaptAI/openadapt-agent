"""Exact artifact-inventory and immutable tag-binding tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_release_artifacts", ROOT / "scripts" / "prepare_release_artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
HANDOFF = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HANDOFF
SPEC.loader.exec_module(HANDOFF)

ArtifactHandoffError = HANDOFF.ArtifactHandoffError
build_artifact_inventory = HANDOFF.build_artifact_inventory
build_publication_staging = HANDOFF.build_publication_staging
build_tag_binding = HANDOFF.build_tag_binding
canonical_json_bytes = HANDOFF.canonical_json_bytes
verify_artifact_inventory = HANDOFF.verify_artifact_inventory
verify_artifact_subset = HANDOFF.verify_artifact_subset
verify_tag_object = HANDOFF.verify_tag_object


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "openadapt_agent-2.0.2-py3-none-any.whl").write_bytes(b"exact wheel")
    (dist / "openadapt_agent-2.0.2.tar.gz").write_bytes(b"exact sdist")
    return dist


def _reference() -> dict[str, object]:
    return {
        "schema_version": "openadapt.production-evidence-object-reference/v2",
        "repository": "OpenAdaptAI/.github",
        "repository_id": 858454062,
        "repository_owner_id": 132681217,
        "registry_source_commit": "a" * 40,
        "registry_revision": 19,
        "registry_head_sha256": "sha256:" + "b" * 64,
        "registry_entry_sha256": "sha256:" + "c" * 64,
        "kind": "qualification-release",
        "object_schema_version": "openadapt.qualification-release/v1",
        "object_path": (
            "production-evidence/objects/sha256/dd/"
            + "d" * 64
            + ".qualification-release.json"
        ),
        "object_sha256": "sha256:" + "d" * 64,
        "size_bytes": 4123,
        "object_media_type": (
            "application/vnd.openadapt.qualification-release+json;version=1"
        ),
        "semantic_identity_sha256": "sha256:" + "e" * 64,
        "subject_sha256": None,
    }


def test_inventory_binds_exact_wheel_and_sdist_bytes(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)

    assert inventory["schema_version"] == "openadapt.production-release-artifact-inventory/v1"
    assert inventory["target"] == "agent"
    assert inventory["claim_scope"] == "production_agent"
    assert inventory["artifacts"] == [
        {
            "name": "openadapt_agent-2.0.2.tar.gz",
            "kind": "python-sdist",
            "sha256": "sha256:" + hashlib.sha256(b"exact sdist").hexdigest(),
            "size_bytes": len(b"exact sdist"),
            "media_type": "application/gzip",
            "publish_destinations": ["github-release", "pypi"],
        },
        {
            "name": "openadapt_agent-2.0.2-py3-none-any.whl",
            "kind": "python-wheel",
            "sha256": "sha256:" + hashlib.sha256(b"exact wheel").hexdigest(),
            "size_bytes": len(b"exact wheel"),
            "media_type": "application/zip",
            "publish_destinations": ["github-release", "pypi"],
        },
    ]
    verify_artifact_inventory(inventory, dist)


def test_inventory_refuses_extra_missing_or_changed_files(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)

    (dist / "unexpected.txt").write_text("not admitted", encoding="utf-8")
    with pytest.raises(ArtifactHandoffError, match="exactly one wheel"):
        build_artifact_inventory(dist)
    (dist / "unexpected.txt").unlink()

    (dist / "openadapt_agent-2.0.2-py3-none-any.whl").write_bytes(b"changed")
    with pytest.raises(ArtifactHandoffError, match="differs from the exact"):
        verify_artifact_inventory(inventory, dist)


def test_subset_accepts_exact_staging_and_refuses_conflicts(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)
    (dist / "openadapt_agent-2.0.2.tar.gz").unlink()
    verify_artifact_subset(inventory, dist)

    (dist / "openadapt_agent-2.0.2-py3-none-any.whl").write_bytes(b"conflict")
    with pytest.raises(ArtifactHandoffError, match="size differs"):
        verify_artifact_subset(inventory, dist)


def test_tag_binding_uses_the_domain_separated_reference_digest() -> None:
    reference = _reference()
    reference_json = json.dumps(reference, indent=2)
    inventory_digest = "sha256:" + "f" * 64

    binding = build_tag_binding(reference_json, inventory_digest)
    expected_reference_digest = hashlib.sha256(
        b"OpenAdapt production release tag admission reference v1\0"
        + canonical_json_bytes(reference)
    ).hexdigest()

    assert set(binding) == {
        "schema_version",
        "admission_reference",
        "admission_reference_sha256",
        "artifact_inventory_sha256",
    }
    assert binding["schema_version"] == "openadapt.production-release-tag-binding/v1"
    assert binding["admission_reference"] == reference
    assert binding["admission_reference_sha256"] == "sha256:" + expected_reference_digest
    assert binding["artifact_inventory_sha256"] == inventory_digest


def test_tag_binding_refuses_an_open_or_bundle_reference() -> None:
    reference = _reference()
    reference["extra"] = "not closed"
    with pytest.raises(ArtifactHandoffError, match="closed v2 contract"):
        build_tag_binding(json.dumps(reference), "sha256:" + "f" * 64)

    reference.pop("extra")
    reference["subject_sha256"] = "sha256:" + "1" * 64
    with pytest.raises(ArtifactHandoffError, match="must not have a subject"):
        build_tag_binding(json.dumps(reference), "sha256:" + "f" * 64)


def test_raw_tag_object_requires_the_exact_canonical_message(tmp_path: Path) -> None:
    reference = _reference()
    reference_json = json.dumps(reference)
    binding = build_tag_binding(reference_json, "sha256:" + "f" * 64)
    body = canonical_json_bytes(binding) + b"\n"
    tag_object = tmp_path / "tag-object"
    tag_object.write_bytes(
        b"object "
        + b"1" * 40
        + b"\ntype commit\ntag v2.0.2\n"
        + b"tagger OpenAdapt Release App <bot@example.com> 0 +0000\n\n"
        + body
    )
    verify_tag_object(tag_object, reference_json, "sha256:" + "f" * 64)

    tag_object.write_bytes(tag_object.read_bytes() + b"\n")
    with pytest.raises(ArtifactHandoffError, match="canonical binding"):
        verify_tag_object(tag_object, reference_json, "sha256:" + "f" * 64)


def test_git_creates_the_exact_canonical_tag_message(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "OpenAdapt Release App"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "bot@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--allow-empty", "-q", "-m", "candidate"],
        check=True,
    )
    reference_json = json.dumps(_reference())
    binding = build_tag_binding(reference_json, "sha256:" + "f" * 64)
    binding_path = repository / "tag-binding.json"
    binding_path.write_bytes(canonical_json_bytes(binding) + b"\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "tag",
            "-a",
            "--cleanup=verbatim",
            "v2.0.2",
            "-F",
            str(binding_path),
        ],
        check=True,
    )
    raw = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "tag", "refs/tags/v2.0.2"],
        check=True,
        capture_output=True,
    ).stdout
    tag_object = repository / "tag-object"
    tag_object.write_bytes(raw)
    verify_tag_object(tag_object, reference_json, "sha256:" + "f" * 64)


def _release_and_assets(dist: Path, inventory: dict[str, object]) -> tuple[dict, list[dict]]:
    release = {
        "id": 9901,
        "tag_name": "v2.0.2",
        "target_commitish": "a" * 40,
        "draft": True,
        "prerelease": False,
        "immutable": False,
        "author": {"id": 321543906, "login": "openadapt-release[bot]"},
    }
    artifacts = inventory["artifacts"]
    assert isinstance(artifacts, list)
    assets = []
    for index, artifact in enumerate(artifacts, start=1):
        assert isinstance(artifact, dict)
        assets.append(
            {
                "id": 8800 + index,
                "name": artifact["name"],
                "state": "uploaded",
                "size": artifact["size_bytes"],
                "digest": artifact["sha256"],
                "content_type": artifact["media_type"],
                "uploader": {"id": 321543906, "login": "openadapt-release[bot]"},
            }
        )
    return release, assets


def _rulesets() -> tuple[dict, dict]:
    conditions = {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}}
    creation = {
        "id": 7001,
        "name": "OpenAdapt policy: release tag creation",
        "target": "tag",
        "enforcement": "active",
        "conditions": conditions,
        "bypass_actors": [
            {"actor_id": 4730708, "actor_type": "Integration", "bypass_mode": "always"}
        ],
        "rules": [{"type": "creation"}],
    }
    immutable = {
        "id": 7002,
        "name": "OpenAdapt policy: immutable release tags",
        "target": "tag",
        "enforcement": "active",
        "conditions": conditions,
        "bypass_actors": [],
        "rules": [
            {"type": "non_fast_forward"},
            {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}},
            {"type": "deletion"},
        ],
    }
    return creation, immutable


def test_publication_staging_binds_app_assets_and_live_tag_policy(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)
    release, assets = _release_and_assets(dist, inventory)
    creation, immutable = _rulesets()

    staging = build_publication_staging(
        inventory,
        release,
        assets,
        creation,
        immutable,
        {"enabled": True, "enforced_by_owner": False},
        {"ref": "refs/tags/v2.0.2", "exists": False},
        dist,
        expected_tag="v2.0.2",
        expected_source_commit="a" * 40,
        observed_at="2026-08-27T12:30:45Z",
    )

    assert staging["schema_version"] == "openadapt.production-release-staging-evidence/v1"
    assert staging["draft_release_id"] == "9901"
    assert staging["release_app_id"] == "4730708"
    assert staging["release_app_installation_id"] == "156835568"
    assert staging["release_app_bot_user_id"] == "321543906"
    assert staging["immutable_releases"] == {
        "enabled": True,
        "enforced_by_owner": False,
    }
    assert staging["immutable_releases_sha256"].startswith("sha256:")
    assert staging["tag_ref_state"] == {
        "ref": "refs/tags/v2.0.2",
        "exists": False,
    }
    assert staging["tag_ref_state_sha256"].startswith("sha256:")
    assert [item["role"] for item in staging["tag_rulesets"]] == [
        "creation_authority",
        "immutability",
    ]
    assert staging["tag_rulesets_sha256"].startswith("sha256:")
    assert [item["name"] for item in staging["assets"]] == sorted(
        item["name"] for item in staging["assets"]
    )


def test_publication_staging_refuses_policy_or_asset_drift(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)
    release, assets = _release_and_assets(dist, inventory)
    creation, immutable = _rulesets()
    immutable["conditions"]["ref_name"]["include"] = ["refs/tags/*"]

    with pytest.raises(ArtifactHandoffError, match="ref pattern differs"):
        build_publication_staging(
            inventory,
            release,
            assets,
            creation,
            immutable,
            {"enabled": True, "enforced_by_owner": True},
            {"ref": "refs/tags/v2.0.2", "exists": False},
            dist,
            expected_tag="v2.0.2",
            expected_source_commit="a" * 40,
            observed_at="2026-08-27T12:30:45Z",
        )

    creation, immutable = _rulesets()
    assets[0]["uploader"]["id"] = 123
    with pytest.raises(ArtifactHandoffError, match="uploader differs"):
        build_publication_staging(
            inventory,
            release,
            assets,
            creation,
            immutable,
            {"enabled": True, "enforced_by_owner": False},
            {"ref": "refs/tags/v2.0.2", "exists": False},
            dist,
            expected_tag="v2.0.2",
            expected_source_commit="a" * 40,
            observed_at="2026-08-27T12:30:45Z",
        )


def test_publication_staging_refuses_open_immutable_release_state(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)
    release, assets = _release_and_assets(dist, inventory)
    creation, immutable = _rulesets()

    states = (
        {"enabled": True},
        {"enabled": True, "enforced_by_owner": False, "extra": 1},
    )
    for state in states:
        with pytest.raises(ArtifactHandoffError, match="must be enabled"):
            build_publication_staging(
                inventory,
                release,
                assets,
                creation,
                immutable,
                state,
                {"ref": "refs/tags/v2.0.2", "exists": False},
                dist,
                expected_tag="v2.0.2",
                expected_source_commit="a" * 40,
                observed_at="2026-08-27T12:30:45Z",
            )


def test_publication_staging_refuses_an_existing_release_tag(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    inventory = build_artifact_inventory(dist)
    release, assets = _release_and_assets(dist, inventory)
    creation, immutable = _rulesets()

    with pytest.raises(ArtifactHandoffError, match="prospective release tag"):
        build_publication_staging(
            inventory,
            release,
            assets,
            creation,
            immutable,
            {"enabled": True, "enforced_by_owner": False},
            {"ref": "refs/tags/v2.0.2", "exists": True},
            dist,
            expected_tag="v2.0.2",
            expected_source_commit="a" * 40,
            observed_at="2026-08-27T12:30:45Z",
        )
