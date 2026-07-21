# Distribution & discoverability — openadapt-agent

**Status: Experimental (v2).** This document describes how the package is
made installable and discoverable as an MCP server, the security-relevant
distinction between the *public capability* and a *user's private
bundle*, and the exact founder-owned steps to publish and list it. The
publish/submission steps are **not automated here** — they mint public,
first-party identities and are outward-facing founder actions.

## 1. The public capability vs the user's private artifact

There are two very different things people mean by "an OpenAdapt MCP
server". Keep them separate.

| | Public / official | Private / per-user |
| --- | --- | --- |
| **What it is** | the `openadapt-agent` package and its MCP server *program* | a user's compiled `openadapt-flow` workflow *bundle* |
| **What it exposes** | the OpenAdapt capability: inspect a compiled bundle, and (opt-in) run it under governance | one customer's specific recorded workflow (its steps, parameters, recorded example values) |
| **Where it lives** | PyPI + MCP registries (official, Smithery, mcp.so, Glama, PulseMCP) | the operator's own disk, passed at launch via `--bundles` |
| **Ships in the package?** | yes — code only | **never** — no bundle is embedded in the wheel, `server.json`, or any registry listing |

**Design consequence:** the published server takes the bundle directory
as a launch-time argument (`--bundles <dir>`) and reads nothing about a
user's workflow at build or publish time. A compiled bundle can contain
recorded example values that are business- or PHI-sensitive (see the
workspace license/PHI rules); it is the operator's artifact and stays
inside their trust boundary. The registry listing advertises the
*capability*, not any workflow.

**Read-only by default when registry-launched.** The `server.json` and
`smithery.yaml` launch the server WITHOUT `--allow-run`, so a one-click
install from a directory yields the inspection tools only
(`list_workflows`, `get_workflow`, `get_run_report`). Executing a
workflow (`run_<slug>`) is a deliberate operator act: they add
`--allow-run` (and typically `--policy`/`--config`) when they run the
server against their own bundles. This matches the security model in
[`DESIGN.md`](DESIGN.md).

> There is intentionally **no** hosted, multi-tenant "official OpenAdapt
> workflow server" that exposes OpenAdapt-operated workflows to the
> public. v2 is stdio-only, single-user, local. A hosted control plane is
> a separate, proprietary surface (`openadapt-cloud`) and is out of scope
> for this package.

## 2. Canonical registry metadata

Every directory (official registry, Smithery, mcp.so, Glama, PulseMCP)
indexes the same core fields. Keep this block as the single source of
truth; the machine-readable copies are [`../server.json`](../server.json)
and [`../smithery.yaml`](../smithery.yaml).

- **Name (reverse-DNS, official registry):** `io.github.OpenAdaptAI/openadapt-agent`
- **Display name:** OpenAdapt Agent (openadapt-flow bridge)
- **PyPI package:** `openadapt-agent`
- **Version:** `2.0.0.dev0` (Experimental; pick the final release version at publish — see step 3.1)
- **Description:** Expose compiled openadapt-flow workflow bundles as governed MCP tools (Experimental).
- **Homepage / docs:** https://docs.openadapt.ai
- **Repository:** https://github.com/OpenAdaptAI/openadapt-agent
- **License:** MIT
- **Transport:** stdio
- **Run command (uvx):** `uvx openadapt-agent serve --bundles <BUNDLES_DIR> [--allow-run]`
- **Config:** `--bundles` (required, dir), `--allow-run` (opt-in execution), `OPENADAPT_BUNDLE_KEY` (optional secret, for encrypted bundles)
- **Tools:**
  - `list_workflows` — list exposed bundles (slug, name, params, step count, load errors).
  - `get_workflow` — inspect one workflow (step intents, declared params with recorded example values, certification status).
  - `get_run_report` — fetch the persisted `report.json` for a prior run (halt/success evidence).
  - `run_<slug>` — execute a bundle via the governed `openadapt-flow run` CLI (only when `--allow-run`). Returns `success` | `halt` | `refused` | `timeout` | `error`.
- **Categories/tags:** mcp, agent-skills, automation, workflow, gui, governed, healthcare, rpa

## 3. Founder-action submission plan (do NOT automate)

Each of these creates or mutates a public, first-party identity. Perform
them intentionally, in order. Prerequisites: a PyPI account with an API
token for the `OpenAdaptAI` org, `uv`/`twine` installed, the `mcp-publisher`
CLI, a Smithery account linked to the GitHub org, and owner/admin on the
`OpenAdaptAI/openadapt-agent` GitHub repo.

### 3.1 Decide the release version

`2.0.0.dev0` is a dev pre-release; `pip install openadapt-agent` will not
select it without `--pre`. For a real launch choose one of:
- `2.0.0rc1` (recommended first public: pre-release, opt-in via `--pre`), or
- `2.0.0` (final; drop the Experimental-only caveats only when honest to).

Whatever you choose, set it in **three places that a CI test pins
together**: `pyproject.toml` `version`, `src/openadapt_agent/__init__.py`
`__version__`, and both `version` fields in `server.json`. The
`tests/test_distribution.py` guard fails if they drift.

### 3.2 Publish to PyPI

```bash
# from a clean checkout of the tagged release commit
python -m build                      # builds sdist + wheel into dist/
twine check dist/*
# License-hygiene gate (workspace rule 4.1): confirm no copyleft/benchmark
# files rode along in the artifacts before uploading.
python -m zipfile -l dist/openadapt_agent-*.whl
twine upload dist/*                  # or: uv publish
```

Verify: `pip index versions openadapt-agent` shows the new version, and
`uvx openadapt-agent@<version> --version` prints it. Only after this does
`uvx openadapt-agent ...` work for everyone.

### 3.3 Submit to the official MCP registry

The registry verifies namespace ownership against your authenticated
GitHub identity, so `io.github.OpenAdaptAI/...` requires publishing as a
member of the `OpenAdaptAI` org.

```bash
# install the publisher CLI (see modelcontextprotocol/registry releases)
mcp-publisher login github          # opens GitHub OAuth; must be OpenAdaptAI
mcp-publisher validate server.json  # schema + package-existence checks
mcp-publisher publish server.json   # requires the PyPI package to already exist
```

Notes:
- The registry checks that the PyPI package in `server.json` actually
  exists at that version, so do **3.2 first**.
- To prove the PyPI package belongs to this server, add the registry's
  ownership marker to the package metadata if prompted (a
  `Project-URL`/README line the publisher instructs you to add), then
  re-run publish.

### 3.4 List on Smithery

`smithery.yaml` is already at the repo root.
- Sign in at https://smithery.ai with the GitHub org account.
- "Add Server" -> point at `github.com/OpenAdaptAI/openadapt-agent`.
- Smithery reads `smithery.yaml` (stdio `startCommand`, `configSchema`,
  `commandFunction`). Confirm the generated config form shows
  `bundlesDir` (required), `allowRun` (default off), `bundleKey` (secret).
- Publish. Smithery hosts the connection config; it does not host bundles.

### 3.5 mcp.so

- Submit at https://mcp.so (Submit / "Add MCP Server") with the repo URL.
- It scrapes the README + `server.json`; ensure the "How results come
  back" and tool table render. No account action beyond the submission
  form.

### 3.6 Glama

- Glama (https://glama.ai/mcp/servers) auto-indexes public GitHub MCP
  servers and re-scores on push. Claim the server with the GitHub org
  account to control metadata, and confirm the MIT license + repo
  metadata are picked up.

### 3.7 PulseMCP

- Submit at https://www.pulsemcp.com (their "Submit a server" flow) with
  the repo + PyPI package. PulseMCP indexes description, install command,
  and tool list from this doc's canonical block.

### 3.8 Agent Skills directories

The Agent Skill this package emits (`openadapt-agent emit-skill`) is a
per-bundle artifact a user generates locally; it embeds a copy of *their*
bundle, so it is **not** something to publish centrally on their behalf.
For discoverability of the *capability*:
- Add openadapt-agent to any first-party "awesome MCP / Agent Skills"
  list OpenAdapt maintains, and to the `OpenAdaptAI/OpenAdapt` README's
  integrations section.
- Do not upload user-emitted skill folders to a public skills directory:
  a skill folder contains the compiled bundle (potentially sensitive
  recorded values). Publishing one is the user's decision, per the PHI /
  artifact-egress rules.

## 4. Post-publish consistency checklist

- [ ] `uvx openadapt-agent serve --bundles <dir>` starts read-only for a fresh installer.
- [ ] `server.json` version == PyPI version == `__version__` (CI-pinned).
- [ ] Registry descriptions match the canonical metadata block (§2).
- [ ] README badges point at the real PyPI project once published.
- [ ] docs.openadapt.ai links back to this repo (add an "Agents & MCP" page — see the OpenAdapt docs repo; that deploy is a separate founder action).
