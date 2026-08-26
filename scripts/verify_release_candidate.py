#!/usr/bin/env python3
"""Verify the reviewed release candidate and its exact workflow event binding."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = ROOT / "release-candidate.json"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MODULE_PATH = ROOT / "src" / "openadapt_agent" / "__init__.py"
SERVER_PATH = ROOT / "server.json"
MANIFEST_PATH = ROOT / "manifest.json"

SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
TAG_SEMVER = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
CANDIDATE_KEYS = {
    "schema_version",
    "candidate_role",
    "admission_status",
    "version",
    "tag",
    "previous_tag",
    "changelog_path",
    "changelog_heading",
}


class CandidateError(RuntimeError):
    """The release candidate or event binding is invalid."""


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateError(f"{label} is missing or invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise CandidateError(f"{label} must be a JSON object")
    return value


def _match_version(path: Path, pattern: str, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateError(f"could not read {label}: {exc}") from exc
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise CandidateError(f"could not find the version in {label}")
    return match.group(1)


def load_and_verify_candidate(root: Path = ROOT) -> dict[str, str]:
    """Verify the closed candidate, versions, and exact changelog heading."""

    candidate_path = root / "release-candidate.json"
    candidate = _load_object(candidate_path, "release candidate")
    if set(candidate) != CANDIDATE_KEYS:
        raise CandidateError("release candidate fields differ from the closed contract")

    fixed = {
        "schema_version": "openadapt.release-tag-candidate/v1",
        "candidate_role": "release_tag_input",
        "admission_status": "not_admitted",
    }
    if {key: candidate.get(key) for key in fixed} != fixed:
        raise CandidateError("release candidate role or admission state is invalid")

    values: dict[str, str] = {}
    for key in CANDIDATE_KEYS:
        value = candidate.get(key)
        if not isinstance(value, str) or not value:
            raise CandidateError(f"release candidate {key} must be a non-empty string")
        values[key] = value

    version = values["version"]
    tag = values["tag"]
    previous_tag = values["previous_tag"]
    if SEMVER.fullmatch(version) is None or tag != f"v{version}":
        raise CandidateError("release version and tag do not match")
    if TAG_SEMVER.fullmatch(previous_tag) is None:
        raise CandidateError("previous release tag is invalid")
    if previous_tag == tag:
        raise CandidateError("previous and candidate tags must differ")
    current_core = tuple(
        int(value) for value in re.split(r"[-+]", version, maxsplit=1)[0].split(".")
    )
    previous_core = tuple(
        int(value) for value in re.split(r"[-+]", previous_tag[1:], maxsplit=1)[0].split(".")
    )
    if previous_core >= current_core:
        raise CandidateError("previous release tag must be older than the candidate")

    server = _load_object(root / "server.json", "server.json")
    packages = server.get("packages")
    if not isinstance(packages, list) or len(packages) != 1 or not isinstance(packages[0], dict):
        raise CandidateError("server.json must contain one package descriptor")

    versions = {
        "pyproject.toml": _match_version(
            root / "pyproject.toml", r'^version\s*=\s*"([^"]+)"', "pyproject.toml"
        ),
        "openadapt_agent.__version__": _match_version(
            root / "src" / "openadapt_agent" / "__init__.py",
            r'^__version__\s*=\s*"([^"]+)"',
            "openadapt_agent.__version__",
        ),
        "server.json": str(server.get("version")),
        "server.json package": str(packages[0].get("version")),
        "manifest.json": str(_load_object(root / "manifest.json", "manifest.json").get("version")),
    }
    drift = {label: value for label, value in versions.items() if value != version}
    if drift:
        raise CandidateError(f"release version fields differ: {drift}")

    changelog_path = Path(values["changelog_path"])
    if changelog_path.is_absolute() or ".." in changelog_path.parts:
        raise CandidateError("changelog path must stay inside the repository")
    expected_heading = f"## [{version}]"
    if values["changelog_heading"] != expected_heading:
        raise CandidateError("release candidate changelog heading differs from the version")
    try:
        changelog = (root / changelog_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateError(f"could not read the release changelog: {exc}") from exc
    headings = [line for line in changelog.splitlines() if line == expected_heading]
    if len(headings) != 1:
        raise CandidateError("the release changelog needs one exact candidate heading")

    return values


def verify_event_binding(
    candidate: dict[str, str],
    *,
    event_name: str,
    ref: str,
    source_commit: str,
    requested_version: str | None = None,
    requested_source_commit: str | None = None,
) -> None:
    """Refuse every event, ref, version, or commit mismatch."""

    if HEX40.fullmatch(source_commit) is None:
        raise CandidateError("release source commit must be a full lowercase SHA")
    if event_name == "workflow_dispatch":
        if ref != "refs/heads/main":
            raise CandidateError("manual tag creation requires the main branch")
        if requested_version != candidate["version"]:
            raise CandidateError("requested release version differs from the reviewed candidate")
        if requested_source_commit != source_commit:
            raise CandidateError("requested source commit differs from the checked-out commit")
    elif event_name == "push":
        if ref != f"refs/tags/{candidate['tag']}":
            raise CandidateError("pushed tag differs from the reviewed candidate")
        if requested_version is not None or requested_source_commit is not None:
            raise CandidateError("tag publication cannot accept manual release inputs")
    elif event_name == "pull_request":
        if not ref.startswith("refs/pull/"):
            raise CandidateError("pull-request validation requires a pull request ref")
        if requested_version is not None or requested_source_commit is not None:
            raise CandidateError("pull-request validation cannot accept release inputs")
    else:
        raise CandidateError(f"unsupported release workflow event: {event_name}")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def verify_repository_binding(
    candidate: dict[str, str], source_commit: str, root: Path = ROOT
) -> None:
    """Bind the candidate to HEAD, the previous tag, and any existing release tag."""

    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != source_commit:
        raise CandidateError("release source commit differs from repository HEAD")

    previous_ref = f"refs/tags/{candidate['previous_tag']}"
    previous_type = _git(root, "cat-file", "-t", previous_ref)
    if previous_type.returncode != 0 or previous_type.stdout.strip() != "tag":
        raise CandidateError("previous release tag must exist and be annotated")
    ancestor = _git(
        root, "merge-base", "--is-ancestor", f"{previous_ref}^{{commit}}", source_commit
    )
    if ancestor.returncode != 0:
        raise CandidateError("previous release tag is not an ancestor of the candidate")

    candidate_ref = f"refs/tags/{candidate['tag']}"
    candidate_type = _git(root, "cat-file", "-t", candidate_ref)
    if candidate_type.returncode == 0:
        if candidate_type.stdout.strip() != "tag":
            raise CandidateError("existing candidate tag must be annotated")
        peeled = _git(root, "rev-parse", f"{candidate_ref}^{{commit}}")
        if peeled.returncode != 0 or peeled.stdout.strip() != source_commit:
            raise CandidateError("existing candidate tag points to a different commit")
    elif candidate_type.returncode not in {1, 128}:
        raise CandidateError("could not inspect the candidate tag")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--requested-version")
    parser.add_argument("--requested-source-commit")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        candidate = load_and_verify_candidate()
        verify_event_binding(
            candidate,
            event_name=args.event_name,
            ref=args.ref,
            source_commit=args.source_commit,
            requested_version=args.requested_version,
            requested_source_commit=args.requested_source_commit,
        )
        verify_repository_binding(candidate, args.source_commit)
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"version={candidate['version']}\n")
                output.write(f"tag={candidate['tag']}\n")
                output.write(f"source_commit={args.source_commit}\n")
    except (CandidateError, OSError) as exc:
        print(f"RELEASE CANDIDATE REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"Verified release candidate {candidate['tag']} at {args.source_commit} "
        f"for {args.event_name}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
