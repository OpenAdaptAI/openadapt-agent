"""Release ordering and registry-parity contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CANDIDATE_SCHEMA = ROOT / "schemas" / "production-lifecycle-admission-candidate.schema.json"
MCPB_IGNORE = ROOT / ".mcpbignore"
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
MCP_REGISTRY = VERIFY.MCP_REGISTRY
MCP_SERVER_NAME = VERIFY.MCP_SERVER_NAME
PYPI_PROJECT = VERIFY.PYPI_PROJECT
ReleaseVerificationError = VERIFY.ReleaseVerificationError
_policy_binding = VERIFY._policy_binding
build_candidate = VERIFY.build_candidate
verify_mcp_registry = VERIFY.verify_mcp_registry
verify_pypi = VERIFY.verify_pypi
RegistrySchemaError = SCHEMA_VALIDATOR.RegistrySchemaError
SCHEMA_URL = SCHEMA_VALIDATOR.SCHEMA_URL
validate_server_schema = SCHEMA_VALIDATOR.validate_server_schema
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


def _pypi_fetch(files: dict[str, bytes], *, latest: str = VERSION):
    urls = []
    bodies: dict[str, bytes] = {}
    for name, body in files.items():
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
    bodies[version_url] = _json_bytes({"info": {"version": VERSION}, "urls": urls})
    bodies[latest_url] = _json_bytes({"info": {"version": latest}})

    def fetch(url: str) -> bytes:
        return bodies[url]

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


def _mcp_fetch(server: dict, *, latest_version: str = VERSION):
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
    bodies = {
        version_url: _json_bytes({"server": normalized, "_meta": official}),
        latest_url: _json_bytes({"server": latest_server, "_meta": official}),
    }

    def fetch(url: str) -> bytes:
        return bodies[url]

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


def test_release_orders_publish_parity_then_candidate_and_pins_publisher() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    publish = workflow.index("./mcp-publisher publish")
    parity = workflow.index("python scripts/verify_release_registries.py")
    retain = workflow.index("Retain the content-bound admission candidate")
    assert publish < parity < retain
    assert "registry-parity:" in workflow
    assert "needs: [validate, mcp-registry-publish]" in workflow
    assert "releases/latest/download" not in workflow
    assert 'publisher_version="1.8.1"' in workflow
    assert "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc" in workflow
    assert "production-lifecycle-admissions.json" not in workflow


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
    assert 'release:\n    types: [published]' not in workflow
