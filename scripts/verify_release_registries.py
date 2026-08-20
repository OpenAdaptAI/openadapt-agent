#!/usr/bin/env python3
"""Verify exact public registry parity and emit an admission candidate.

This script proves that PyPI and the official MCP registry expose the exact
release bytes and metadata.  It then writes a content-bound input for the
canonical OpenAdapt Production lifecycle process.  It does not select, sign,
or activate a Production release.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PYPI_PROJECT = "openadapt-agent"
MCP_SERVER_NAME = "io.github.OpenAdaptAI/openadapt-agent"
MCP_REGISTRY = "https://registry.modelcontextprotocol.io"
CENTRAL_REPOSITORY = "OpenAdaptAI/.github"
TARGET_ID = "agent"
CLAIM_SCOPE = "qualified_agent_bridge_release"
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class ReleaseVerificationError(RuntimeError):
    """The public release differs from its exact local candidate."""


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(f"{label} is missing or invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseVerificationError(f"{label} must be a JSON object")
    return value


def _fetch_url(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "openadapt-agent-release-verifier/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ReleaseVerificationError(f"response exceeds size limit: {url}")
    return body


def _fetch_object(url: str, fetch: Callable[[str], bytes]) -> tuple[dict[str, Any], bytes]:
    try:
        body = fetch(url)
        value = json.loads(body)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(f"could not fetch valid JSON from {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseVerificationError(f"registry response must be an object: {url}")
    return value, body


def _artifact_kind(path: Path) -> str:
    if path.suffix == ".whl":
        return "wheel"
    if path.name.endswith(".tar.gz"):
        return "sdist"
    raise ReleaseVerificationError(f"unsupported distribution artifact: {path.name}")


def _local_artifacts(dist: Path) -> list[dict[str, Any]]:
    if not dist.is_dir():
        raise ReleaseVerificationError(f"distribution directory does not exist: {dist}")
    paths = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    artifacts = [
        {
            "name": path.name,
            "kind": _artifact_kind(path),
            "sha256": _digest_bytes(path.read_bytes()),
            "size_bytes": path.stat().st_size,
            "path": path,
        }
        for path in paths
    ]
    kinds = [item["kind"] for item in artifacts]
    if sorted(kinds) != ["sdist", "wheel"]:
        raise ReleaseVerificationError("release must contain exactly one sdist and one wheel")
    return sorted(artifacts, key=lambda item: (item["kind"], item["name"]))


def verify_pypi(
    dist: Path,
    version: str,
    *,
    fetch: Callable[[str], bytes] = _fetch_url,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify local bytes against the exact PyPI release and latest selector."""

    local = _local_artifacts(dist)
    version_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{quote(version, safe='')}/json"
    latest_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/json"
    metadata, metadata_body = _fetch_object(version_url, fetch)
    latest, latest_body = _fetch_object(latest_url, fetch)
    if metadata.get("info", {}).get("version") != version:
        raise ReleaseVerificationError("PyPI version metadata differs from the release")
    if latest.get("info", {}).get("version") != version:
        raise ReleaseVerificationError(
            "PyPI latest is not the exact release; a newer candidate won the race"
        )
    remote_files = metadata.get("urls")
    if not isinstance(remote_files, list):
        raise ReleaseVerificationError("PyPI release file inventory is invalid")
    expected_names = {item["name"] for item in local}
    actual_names = {item.get("filename") for item in remote_files if isinstance(item, dict)}
    if actual_names != expected_names or len(remote_files) != len(local):
        raise ReleaseVerificationError("PyPI release file inventory differs from the build")

    verified_artifacts: list[dict[str, Any]] = []
    for artifact in local:
        matches = [
            item
            for item in remote_files
            if isinstance(item, dict) and item.get("filename") == artifact["name"]
        ]
        if len(matches) != 1:
            raise ReleaseVerificationError(f"PyPI artifact is missing: {artifact['name']}")
        remote = matches[0]
        remote_url = remote.get("url")
        parsed = urlsplit(remote_url) if isinstance(remote_url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.netloc != "files.pythonhosted.org"
            or parsed.query
            or parsed.fragment
        ):
            raise ReleaseVerificationError(f"PyPI artifact URL is invalid: {artifact['name']}")
        expected = {
            "size": artifact["size_bytes"],
            "sha256": artifact["sha256"].removeprefix("sha256:"),
            "yanked": False,
        }
        actual = {
            "size": remote.get("size"),
            "sha256": remote.get("digests", {}).get("sha256"),
            "yanked": remote.get("yanked"),
        }
        if actual != expected:
            raise ReleaseVerificationError(
                f"PyPI metadata differs for {artifact['name']}: {actual}"
            )
        public_bytes = fetch(remote_url)
        if (
            len(public_bytes) != artifact["size_bytes"]
            or _digest_bytes(public_bytes) != artifact["sha256"]
        ):
            raise ReleaseVerificationError(f"PyPI bytes differ from the build: {artifact['name']}")
        verified_artifacts.append(
            {
                "name": artifact["name"],
                "kind": artifact["kind"],
                "authority": "pypi",
                "url": remote_url,
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
            }
        )
    return verified_artifacts, {
        "project": PYPI_PROJECT,
        "version": version,
        "version_metadata_url": version_url,
        "version_metadata_sha256": _digest_bytes(metadata_body),
        "latest_metadata_url": latest_url,
        "latest_metadata_sha256": _digest_bytes(latest_body),
    }


def _normalized_server(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only defaults that the official registry omits on read."""

    normalized = copy.deepcopy(dict(value))
    packages = normalized.get("packages")
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict):
                continue
            variables = package.get("environmentVariables")
            if isinstance(variables, list):
                for variable in variables:
                    if isinstance(variable, dict) and variable.get("isRequired") is False:
                        variable.pop("isRequired")
    return normalized


def _official_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = value.get("_meta")
    if not isinstance(metadata, dict):
        raise ReleaseVerificationError("MCP registry metadata is missing")
    official = metadata.get("io.modelcontextprotocol.registry/official")
    if not isinstance(official, dict):
        raise ReleaseVerificationError("official MCP registry metadata is missing")
    return official


def verify_mcp_registry(
    server_json: Path,
    version: str,
    *,
    fetch: Callable[[str], bytes] = _fetch_url,
) -> dict[str, Any]:
    """Verify the exact version and the MCP registry's current default."""

    expected = _load_object(server_json, "server.json")
    if expected.get("name") != MCP_SERVER_NAME or expected.get("version") != version:
        raise ReleaseVerificationError("server.json identity or version differs")
    encoded_name = quote(MCP_SERVER_NAME, safe="")
    encoded_version = quote(version, safe="")
    version_url = f"{MCP_REGISTRY}/v0.1/servers/{encoded_name}/versions/{encoded_version}"
    latest_url = f"{MCP_REGISTRY}/v0.1/servers/{encoded_name}/versions/latest"
    version_response, version_body = _fetch_object(version_url, fetch)
    latest_response, latest_body = _fetch_object(latest_url, fetch)
    expected_normalized = _normalized_server(expected)
    for label, response in (("version", version_response), ("latest", latest_response)):
        server = response.get("server")
        if not isinstance(server, dict) or _normalized_server(server) != expected_normalized:
            raise ReleaseVerificationError(f"MCP registry {label} metadata differs")
        official = _official_metadata(response)
        if official.get("status") != "active" or official.get("isLatest") is not True:
            raise ReleaseVerificationError(f"MCP registry {label} is not the active latest release")
    return {
        "server_name": MCP_SERVER_NAME,
        "version": version,
        "version_url": version_url,
        "version_response_sha256": _digest_bytes(version_body),
        "latest_url": latest_url,
        "latest_response_sha256": _digest_bytes(latest_body),
        "server_sha256": _digest_bytes(_canonical_bytes(expected_normalized)),
    }


def _policy_binding(policy_path: Path, source_commit: str) -> dict[str, Any]:
    if HEX40.fullmatch(source_commit) is None:
        raise ReleaseVerificationError("lifecycle source commit must be a full commit SHA")
    policy_bytes = policy_path.read_bytes()
    policy = _load_object(policy_path, "Production lifecycle policy")
    targets = policy.get("targets")
    if not isinstance(targets, list):
        raise ReleaseVerificationError("Production lifecycle targets are invalid")
    matches = [item for item in targets if isinstance(item, dict) and item.get("id") == TARGET_ID]
    if len(matches) != 1:
        raise ReleaseVerificationError("canonical Agent lifecycle target is missing")
    target = matches[0]
    expected = {
        "source_repository": "OpenAdaptAI/openadapt-agent",
        "release_kind": "public_package",
        "required_claim_scope": CLAIM_SCOPE,
        "required_artifact_kinds": ["sdist", "wheel"],
        "package_index_project": PYPI_PROJECT,
        "artifact_authority_by_kind": {"sdist": "pypi", "wheel": "pypi"},
    }
    actual = {key: target.get(key) for key in expected}
    if actual != expected:
        raise ReleaseVerificationError("canonical Agent lifecycle target differs")
    revision = policy.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ReleaseVerificationError("Production lifecycle policy revision is invalid")
    return {
        "repository": CENTRAL_REPOSITORY,
        "source_commit": source_commit,
        "policy_path": "production-lifecycle-policy.json",
        "policy_sha256": _digest_bytes(policy_bytes),
        "policy_revision": revision,
        "target": TARGET_ID,
        "claim_scope": CLAIM_SCOPE,
    }


def build_candidate(
    *,
    version: str,
    tag: str,
    source_commit: str,
    artifacts: list[dict[str, Any]],
    pypi: dict[str, Any],
    mcp_registry: dict[str, Any],
    lifecycle_authority: dict[str, Any],
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Build an unadmitted evidence input.  Never derive a Production selector."""

    if SEMVER.fullmatch(version) is None or tag not in {version, f"v{version}"}:
        raise ReleaseVerificationError("release version or tag is invalid")
    if HEX40.fullmatch(source_commit) is None:
        raise ReleaseVerificationError("release source commit must be a full commit SHA")
    instant = (verified_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "$schema": (
            "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-agent/"
            f"{source_commit}/schemas/production-lifecycle-admission-candidate.schema.json"
        ),
        "schema_version": "openadapt.production-lifecycle-admission-candidate/v1",
        "candidate_role": "production_admission_input",
        "admission_status": "not_admitted",
        "production_authority": lifecycle_authority,
        "release": {
            "kind": "public_package",
            "version": version,
            "tag": tag,
            "source_commit": source_commit,
            "immutable_release_url": (
                f"https://github.com/OpenAdaptAI/openadapt-agent/commit/{source_commit}"
            ),
            "artifacts": artifacts,
        },
        "registry_parity": {"pypi": pypi, "mcp": mcp_registry},
        "verified_at": instant.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def verify_and_write(args: argparse.Namespace) -> str:
    artifacts, pypi = verify_pypi(args.dist, args.version)
    mcp = verify_mcp_registry(args.server_json, args.version)
    authority = _policy_binding(args.lifecycle_policy, args.lifecycle_source_commit)
    candidate = build_candidate(
        version=args.version,
        tag=args.tag,
        source_commit=args.source_commit,
        artifacts=artifacts,
        pypi=pypi,
        mcp_registry=mcp,
        lifecycle_authority=authority,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    return _digest_bytes(payload.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--server-json", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--lifecycle-policy", type=Path, required=True)
    parser.add_argument("--lifecycle-source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--retry-seconds", type=int, default=15)
    args = parser.parse_args()
    if args.attempts < 1 or args.retry_seconds < 0:
        parser.error("attempts must be positive and retry-seconds cannot be negative")

    for attempt in range(1, args.attempts + 1):
        try:
            digest = verify_and_write(args)
        except (ReleaseVerificationError, OSError) as exc:
            if attempt == args.attempts:
                print(f"REFUSED: {exc}")
                return 1
            print(f"Attempt {attempt}: {exc}; waiting {args.retry_seconds}s")
            time.sleep(args.retry_seconds)
            continue
        print(f"Verified exact registry parity; unadmitted candidate digest: {digest}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
