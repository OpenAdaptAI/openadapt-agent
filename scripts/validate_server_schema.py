#!/usr/bin/env python3
"""Validate ``server.json`` against one exact official MCP registry schema."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
SCHEMA_SHA256 = "3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0"
MAX_SCHEMA_BYTES = 1024 * 1024


class RegistrySchemaError(RuntimeError):
    """The exact registry schema or the descriptor failed validation."""


def _fetch_schema(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "openadapt-agent-release-schema-validator/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(MAX_SCHEMA_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RegistrySchemaError(f"could not download the pinned MCP schema: {exc}") from exc
    if len(body) > MAX_SCHEMA_BYTES:
        raise RegistrySchemaError("the pinned MCP schema exceeds the size limit")
    return body


def _load_object(value: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrySchemaError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RegistrySchemaError(f"{label} must be a JSON object")
    return document


def validate_server_schema(
    server_json: Path,
    *,
    fetch: Callable[[str], bytes] = _fetch_schema,
    expected_sha256: str = SCHEMA_SHA256,
) -> None:
    """Fetch, authenticate, and apply the exact registry schema."""

    try:
        descriptor_bytes = server_json.read_bytes()
    except OSError as exc:
        raise RegistrySchemaError(f"could not read {server_json}: {exc}") from exc
    descriptor = _load_object(descriptor_bytes, "server.json")
    if descriptor.get("$schema") != SCHEMA_URL:
        raise RegistrySchemaError(f"server.json must declare the pinned schema URL: {SCHEMA_URL}")

    try:
        schema_bytes = fetch(SCHEMA_URL)
    except RegistrySchemaError:
        raise
    except Exception as exc:
        raise RegistrySchemaError(f"could not download the pinned MCP schema: {exc}") from exc
    actual_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise RegistrySchemaError(
            "pinned MCP schema digest mismatch: "
            f"expected {expected_sha256}, received {actual_sha256}"
        )

    schema = _load_object(schema_bytes, "pinned MCP schema")
    validator_class = validator_for(schema)
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise RegistrySchemaError(f"pinned MCP schema is invalid: {exc.message}") from exc
    validator = validator_class(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(descriptor), key=lambda item: list(item.absolute_path))
    if errors:
        details = "; ".join(
            f"{'.'.join(str(value) for value in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise RegistrySchemaError(f"server.json does not match the pinned MCP schema: {details}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-json", type=Path, default=Path("server.json"))
    args = parser.parse_args()
    try:
        validate_server_schema(args.server_json)
    except RegistrySchemaError as exc:
        print(f"MCP SCHEMA CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"server.json matches the pinned MCP schema ({SCHEMA_SHA256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
