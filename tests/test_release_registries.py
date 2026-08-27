"""Release ordering and registry-parity contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CANDIDATE_SCHEMA = ROOT / "schemas" / "production-lifecycle-admission-candidate.schema.json"
MCPB_IGNORE = ROOT / ".mcpbignore"
RELEASE_CANDIDATE = ROOT / "release-candidate.json"
SPEC = importlib.util.spec_from_file_location(
    "verify_release_registries", ROOT / "scripts" / "verify_release_registries.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)
SCHEMA_SPEC = importlib.util.spec_from_file_location(
    "validate_server_schema", ROOT / "scripts" / "validate_server_schema.py"
)
assert SCHEMA_SPEC is not None and SCHEMA_SPEC.loader is not None
SCHEMA_VALIDATOR = importlib.util.module_from_spec(SCHEMA_SPEC)
sys.modules[SCHEMA_SPEC.name] = SCHEMA_VALIDATOR
SCHEMA_SPEC.loader.exec_module(SCHEMA_VALIDATOR)
CANDIDATE_SPEC = importlib.util.spec_from_file_location(
    "verify_release_candidate", ROOT / "scripts" / "verify_release_candidate.py"
)
assert CANDIDATE_SPEC is not None and CANDIDATE_SPEC.loader is not None
CANDIDATE = importlib.util.module_from_spec(CANDIDATE_SPEC)
sys.modules[CANDIDATE_SPEC.name] = CANDIDATE
CANDIDATE_SPEC.loader.exec_module(CANDIDATE)
MCP_REGISTRY = VERIFY.MCP_REGISTRY
MCP_SERVER_NAME = VERIFY.MCP_SERVER_NAME
PYPI_PROJECT = VERIFY.PYPI_PROJECT
ReleaseVerificationError = VERIFY.ReleaseVerificationError
_policy_binding = VERIFY._policy_binding
build_candidate = VERIFY.build_candidate
verify_mcp_registry = VERIFY.verify_mcp_registry
verify_pypi = VERIFY.verify_pypi
inspect_mcp_publication = VERIFY.inspect_mcp_publication
inspect_publication = VERIFY.inspect_publication
inspect_pypi_publication = VERIFY.inspect_pypi_publication
RegistrySchemaError = SCHEMA_VALIDATOR.RegistrySchemaError
SCHEMA_URL = SCHEMA_VALIDATOR.SCHEMA_URL
validate_server_schema = SCHEMA_VALIDATOR.validate_server_schema
CandidateError = CANDIDATE.CandidateError
load_and_verify_candidate = CANDIDATE.load_and_verify_candidate
verify_event_binding = CANDIDATE.verify_event_binding
verify_repository_binding = CANDIDATE.verify_repository_binding
VERSION = "9.8.7"
SOURCE_COMMIT = "a" * 40
POLICY_COMMIT = "b" * 40


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _dist(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    dist = tmp_path / "dist"
    dist.mkdir()
    files = {
        f"openadapt_agent-{VERSION}-py3-none-any.whl": b"wheel bytes",
        f"openadapt_agent-{VERSION}.tar.gz": b"sdist bytes",
    }
    for name, body in files.items():
        (dist / name).write_bytes(body)
    return dist, files


def _pypi_fetch(
    files: dict[str, bytes],
    *,
    latest: str = VERSION,
    published_names: set[str] | None = None,
    exact_missing: bool = False,
):
    urls = []
    bodies: dict[str, bytes] = {}
    selected = set(files) if published_names is None else published_names
    for name, body in files.items():
        if name not in selected:
            continue
        url = f"https://files.pythonhosted.org/packages/test/{name}"
        bodies[url] = body
        urls.append(
            {
                "filename": name,
                "url": url,
                "size": len(body),
                "digests": {"sha256": hashlib.sha256(body).hexdigest()},
                "yanked": False,
            }
        )
    version_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{VERSION}/json"
    latest_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/json"
    if not exact_missing:
        bodies[version_url] = _json_bytes({"info": {"version": VERSION}, "urls": urls})
    bodies[latest_url] = _json_bytes({"info": {"version": latest}})

    def fetch(url: str) -> bytes:
        try:
            return bodies[url]
        except KeyError as exc:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None) from exc

    return fetch


def _server_json(tmp_path: Path) -> tuple[Path, dict]:
    value = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": MCP_SERVER_NAME,
        "version": VERSION,
        "packages": [
            {
                "registryType": "pypi",
                "identifier": PYPI_PROJECT,
                "version": VERSION,
                "transport": {"type": "stdio"},
                "environmentVariables": [{"name": "TOKEN", "isRequired": False, "isSecret": True}],
            }
        ],
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def _mcp_fetch(
    server: dict,
    *,
    latest_version: str = VERSION,
    exact_missing: bool = False,
    latest_missing: bool = False,
):
    normalized = json.loads(json.dumps(server))
    normalized["packages"][0]["environmentVariables"][0].pop("isRequired")
    version_url = (
        f"{MCP_REGISTRY}/v0.1/servers/io.github.OpenAdaptAI%2Fopenadapt-agent/versions/9.8.7"
    )
    latest_url = (
        f"{MCP_REGISTRY}/v0.1/servers/io.github.OpenAdaptAI%2Fopenadapt-agent/versions/latest"
    )
    latest_server = json.loads(json.dumps(normalized))
    latest_server["version"] = latest_version
    latest_server["packages"][0]["version"] = latest_version
    official = {
        "io.modelcontextprotocol.registry/official": {
            "status": "active",
            "isLatest": True,
        }
    }
    bodies = {}
    if not exact_missing:
        bodies[version_url] = _json_bytes({"server": normalized, "_meta": official})
    if not latest_missing:
        bodies[latest_url] = _json_bytes({"server": latest_server, "_meta": official})

    def fetch(url: str) -> bytes:
        try:
            return bodies[url]
        except KeyError as exc:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None) from exc

    return fetch


def _policy(tmp_path: Path) -> Path:
    path = tmp_path / "production-lifecycle-policy.json"
    path.write_text(
        json.dumps(
            {
                "revision": 4,
                "targets": [
                    {
                        "id": "agent",
                        "source_repository": "OpenAdaptAI/openadapt-agent",
                        "release_kind": "public_package",
                        "required_claim_scope": "qualified_agent_bridge_release",
                        "required_artifact_kinds": ["sdist", "wheel"],
                        "package_index_project": "openadapt-agent",
                        "artifact_authority_by_kind": {
                            "sdist": "pypi",
                            "wheel": "pypi",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_exact_registry_parity_builds_only_an_unadmitted_candidate(tmp_path: Path) -> None:
    dist, files = _dist(tmp_path)
    artifacts, pypi = verify_pypi(dist, VERSION, fetch=_pypi_fetch(files))
    server_path, server = _server_json(tmp_path)
    mcp = verify_mcp_registry(server_path, VERSION, fetch=_mcp_fetch(server))
    authority = _policy_binding(_policy(tmp_path), POLICY_COMMIT)

    candidate = build_candidate(
        version=VERSION,
        tag=f"v{VERSION}",
        source_commit=SOURCE_COMMIT,
        artifacts=artifacts,
        pypi=pypi,
        mcp_registry=mcp,
        lifecycle_authority=authority,
        verified_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert candidate["admission_status"] == "not_admitted"
    assert candidate["candidate_role"] == "production_admission_input"
    assert candidate["production_authority"]["source_commit"] == POLICY_COMMIT
    assert candidate["release"]["source_commit"] == SOURCE_COMMIT
    assert [item["kind"] for item in candidate["release"]["artifacts"]] == [
        "sdist",
        "wheel",
    ]
    # PyPI latest is distribution parity evidence. It is never a Production selector.
    all_keys: set[str] = set()

    def collect_keys(value: object) -> None:
        if isinstance(value, dict):
            all_keys.update(value)
            for item in value.values():
                collect_keys(item)
        elif isinstance(value, list):
            for item in value:
                collect_keys(item)

    collect_keys(candidate)
    assert {"admission_id", "release_identity", "channel"}.isdisjoint(all_keys)


def test_pypi_newer_default_refuses_the_candidate(tmp_path: Path) -> None:
    dist, files = _dist(tmp_path)
    with pytest.raises(ReleaseVerificationError, match="PyPI latest is not the exact release"):
        verify_pypi(dist, VERSION, fetch=_pypi_fetch(files, latest="9.8.8"))


def test_publication_inspection_accepts_missing_partial_and_complete_states(
    tmp_path: Path,
) -> None:
    dist, files = _dist(tmp_path)
    server_path, server = _server_json(tmp_path)
    previous = "9.8.6"

    assert (
        inspect_pypi_publication(
            dist,
            VERSION,
            previous,
            fetch=_pypi_fetch(files, latest=previous, exact_missing=True),
        )
        == "missing"
    )
    first_name = next(iter(files))
    assert (
        inspect_pypi_publication(
            dist,
            VERSION,
            previous,
            fetch=_pypi_fetch(files, published_names={first_name}),
        )
        == "partial"
    )
    assert (
        inspect_pypi_publication(
            dist, VERSION, previous, fetch=_pypi_fetch(files)
        )
        == "complete"
    )
    assert (
        inspect_mcp_publication(
            server_path,
            VERSION,
            previous,
            fetch=_mcp_fetch(server, latest_version=previous, exact_missing=True),
        )
        == "missing"
    )
    assert (
        inspect_mcp_publication(
            server_path, VERSION, previous, fetch=_mcp_fetch(server)
        )
        == "complete"
    )


def test_publication_inspection_rejects_mcp_before_complete_pypi(
    tmp_path: Path,
) -> None:
    dist, files = _dist(tmp_path)
    server_path, server = _server_json(tmp_path)
    first_name = next(iter(files))

    with pytest.raises(
        ReleaseVerificationError,
        match="MCP publication exists before the exact PyPI artifact set is complete",
    ):
        inspect_publication(
            dist,
            server_path,
            VERSION,
            "9.8.6",
            pypi_fetch=_pypi_fetch(files, published_names={first_name}),
            mcp_fetch=_mcp_fetch(server),
        )


def test_publication_inspection_rejects_unexpected_prior_defaults(
    tmp_path: Path,
) -> None:
    dist, files = _dist(tmp_path)
    server_path, server = _server_json(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="PyPI current version differs"):
        inspect_pypi_publication(
            dist,
            VERSION,
            "9.8.6",
            fetch=_pypi_fetch(files, latest="9.8.5", exact_missing=True),
        )
    with pytest.raises(ReleaseVerificationError, match="MCP current version differs"):
        inspect_mcp_publication(
            server_path,
            VERSION,
            "9.8.6",
            fetch=_mcp_fetch(server, latest_version="9.8.5", exact_missing=True),
        )


def test_publication_inspection_rejects_conflicting_public_bytes_and_identity(
    tmp_path: Path,
) -> None:
    dist, files = _dist(tmp_path)
    first_name = next(iter(files))
    pypi = _pypi_fetch(files)

    def conflicting_pypi(url: str) -> bytes:
        body = pypi(url)
        if url.endswith(first_name):
            return b"x" * len(body)
        return body

    with pytest.raises(ReleaseVerificationError, match="PyPI bytes differ"):
        inspect_pypi_publication(
            dist, VERSION, "9.8.6", fetch=conflicting_pypi
        )

    server_path, server = _server_json(tmp_path)
    mcp = _mcp_fetch(server, latest_version="9.8.6", exact_missing=True)

    def conflicting_mcp(url: str) -> bytes:
        body = mcp(url)
        if url.endswith("/latest"):
            value = json.loads(body)
            value["server"]["packages"][0]["identifier"] = "other-project"
            return _json_bytes(value)
        return body

    with pytest.raises(ReleaseVerificationError, match="MCP current package differs"):
        inspect_mcp_publication(
            server_path, VERSION, "9.8.6", fetch=conflicting_mcp
        )


def test_mcp_latest_metadata_must_equal_the_exact_server(tmp_path: Path) -> None:
    server_path, server = _server_json(tmp_path)
    with pytest.raises(ReleaseVerificationError, match="MCP registry latest metadata differs"):
        verify_mcp_registry(
            server_path,
            VERSION,
            fetch=_mcp_fetch(server, latest_version="9.8.8"),
        )


def test_candidate_schema_is_closed_and_marks_the_record_not_admitted() -> None:
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["admission_status"] == {"const": "not_admitted"}
    assert "admission_id" not in schema["properties"]
    assert "release_identity" not in schema["properties"]
    assert "schemas/" in MCPB_IGNORE.read_text(encoding="utf-8").splitlines()


def test_reviewed_tag_candidate_is_closed_versioned_and_not_admitted() -> None:
    candidate = load_and_verify_candidate()
    raw = json.loads(RELEASE_CANDIDATE.read_text(encoding="utf-8"))

    assert set(raw) == CANDIDATE.CANDIDATE_KEYS
    assert candidate["candidate_role"] == "release_tag_input"
    assert candidate["admission_status"] == "not_admitted"
    assert candidate["tag"] == f"v{candidate['version']}"
    assert candidate["changelog_heading"] == f"## [{candidate['version']}]"
    ignored = MCPB_IGNORE.read_text(encoding="utf-8").splitlines()
    assert "release-candidate.json" in ignored
    assert "CHANGELOG.md" in ignored


def test_manual_tag_event_requires_exact_main_version_and_commit() -> None:
    candidate = load_and_verify_candidate()
    source_commit = "a" * 40
    verify_event_binding(
        candidate,
        event_name="workflow_dispatch",
        ref="refs/heads/main",
        source_commit=source_commit,
        requested_version=candidate["version"],
        requested_source_commit=source_commit,
    )

    mismatches = [
        {"ref": "refs/heads/feature"},
        {"requested_version": "99.0.0"},
        {"requested_source_commit": "b" * 40},
        {"source_commit": "short"},
    ]
    base = {
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "source_commit": source_commit,
        "requested_version": candidate["version"],
        "requested_source_commit": source_commit,
    }
    for change in mismatches:
        with pytest.raises(CandidateError):
            verify_event_binding(candidate, **(base | change))


def test_tag_publication_requires_the_exact_candidate_tag() -> None:
    candidate = load_and_verify_candidate()
    verify_event_binding(
        candidate,
        event_name="push",
        ref=f"refs/tags/{candidate['tag']}",
        source_commit="a" * 40,
    )
    with pytest.raises(CandidateError, match="pushed tag differs"):
        verify_event_binding(
            candidate,
            event_name="push",
            ref="refs/tags/v99.0.0",
            source_commit="a" * 40,
        )


def test_candidate_binds_head_and_the_annotated_previous_tag() -> None:
    candidate = load_and_verify_candidate()
    source_commit = CANDIDATE._git(ROOT, "rev-parse", "HEAD").stdout.strip()

    verify_repository_binding(candidate, source_commit)
    with pytest.raises(CandidateError, match="differs from repository HEAD"):
        verify_repository_binding(candidate, "a" * 40)


def test_release_orders_publish_parity_then_candidate_and_pins_publisher() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    publish = workflow.index("./mcp-publisher publish")
    parity = workflow.index("Verify registry parity and write the unadmitted candidate")
    retain = workflow.index("Upload the bounded admission-candidate handoff")
    assert publish < parity < retain
    assert "registry-parity:" in workflow
    assert (
        "needs: [validate, authorize-release-app, bind-release-artifacts, pypi-publish, "
        "mcp-registry-publish]" in workflow
    )
    assert "releases/latest/download" not in workflow
    assert 'publisher_version="1.8.1"' in workflow
    assert "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc" in workflow
    assert "production-lifecycle-admissions.json" not in workflow


def test_release_app_can_create_only_the_reviewed_annotated_tag() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "environment: release-identity" in workflow
    assert "actions/create-github-app-token@" in workflow
    assert "vars.OPENADAPT_RELEASE_APP_ID" in workflow
    assert "vars.OPENADAPT_RELEASE_ACTOR_ID" in workflow
    assert "vars.OPENADAPT_RELEASE_APP_INSTALLATION_ID" in workflow
    assert 'RELEASE_APP_ID: "4730708"' in workflow
    assert 'RELEASE_APP_ACTOR_ID: "321543906"' in workflow
    assert 'RELEASE_APP_INSTALLATION_ID: "156835568"' in workflow
    assert "secrets.OPENADAPT_RELEASE_APP_PRIVATE_KEY" in workflow
    assert "repositories: openadapt-agent" in workflow
    assert "permission-contents: write" in workflow
    assert "permission-administration: read" in workflow
    assert "permission-metadata: read" in workflow
    assert 'git tag -a "${RELEASE_TAG}" "${EXPECTED_SOURCE_COMMIT}"' in workflow
    pushes = [line.strip() for line in workflow.splitlines() if "push origin" in line]
    assert pushes == ['git push origin "refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}"']
    assert "push origin HEAD" not in workflow
    assert "refs/heads/main:refs/heads/main" not in workflow
    assert "gh api /installation" in workflow
    assert "immutable-releases" in workflow
    assert "'.enabled'" in workflow


def test_publication_uses_separate_oidc_environments_without_token_fallbacks() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "environment: pypi" in workflow
    assert "environment: mcp-registry" in workflow
    assert workflow.count("id-token: write") == 3
    assert "pypa/gh-action-pypi-publish@" in workflow
    assert "skip-existing: true" in workflow
    assert "./mcp-publisher login github-oidc" in workflow
    for forbidden in (
        "ADMIN_TOKEN",
        "PYPI_API_TOKEN",
        "MCP_GITHUB_TOKEN",
        "PYPI_PUBLISH_METHOD",
        "MCP_PUBLISH_METHOD",
        "registry/releases/latest/download",
        "\n  release:\n",
        "github.event.release",
    ):
        assert forbidden not in workflow


def test_tag_jobs_fail_closed_and_recovery_reuses_the_exact_tag_run() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    publish_guard = (
        "if: ${{ github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') }}"
    )

    assert workflow.count(publish_guard) == 5
    assert "cancel-in-progress: false" in workflow
    assert "The exact annotated release tag already exists. No ref changed." in workflow
    assert "A release tag must be annotated." in workflow
    assert '--event-name "${GITHUB_EVENT_NAME}"' in workflow
    assert '--ref "${GITHUB_REF}"' in workflow
    assert "GITHUB_TRIGGERING_ACTOR" not in workflow
    assert "Could not inspect the release tag" in workflow
    assert "Could not verify that the release tag is absent" in workflow
    assert workflow.count("retention-days: 30") >= 3
    assert "Attest the exact wheel and source archive" in workflow
    assert "--inspect-publication" in workflow
    assert "--require-pypi-state complete" in workflow
    assert "--require-mcp-state complete" in workflow
    assert "recovery/" not in workflow


def test_unavailable_registry_schema_refuses_validation(tmp_path: Path) -> None:
    server_path, _ = _server_json(tmp_path)

    def unavailable(_url: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(RegistrySchemaError, match="could not download the pinned MCP schema"):
        validate_server_schema(server_path, fetch=unavailable)


def test_changed_registry_schema_bytes_refuse_validation(tmp_path: Path) -> None:
    server_path, _ = _server_json(tmp_path)

    with pytest.raises(RegistrySchemaError, match="pinned MCP schema digest mismatch"):
        validate_server_schema(server_path, fetch=lambda _url: b"{}")


def test_release_validation_uses_the_fail_closed_schema_guard() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "python scripts/validate_server_schema.py --server-json server.json" in workflow
    assert "skipping live schema check" not in workflow
    assert "release:\n    types: [published]" not in workflow
