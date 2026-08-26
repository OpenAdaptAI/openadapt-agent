# Distribution & discoverability — openadapt-agent

The Agent target's product state comes from its signed Production admission.
A missing, expired, revoked, mismatched, or unverifiable admission means **not
actively admitted**. This document describes how the package is made
installable and discoverable as an MCP server, the security-relevant
distinction between the *public capability* and a *user's private
bundle*, and the protected steps to publish and list it. The registry jobs are
automated after the founder approves one exact release. First-time account and
directory claims remain founder actions.

## 1. The public capability vs the user's private artifact

There are two very different things people mean by "an OpenAdapt MCP
server". Keep them separate.

| | Public / official | Private / per-user |
| --- | --- | --- |
| **What it is** | the `openadapt-agent` package and its MCP server *program* | a user's compiled `openadapt-flow` workflow *bundle* |
| **What it exposes** | PHI-safe workflow and Needs Attention projections plus opt-in governed run and attended-action tools | one customer's specific recorded workflow (its steps, parameters, recorded example values) |
| **Where it lives** | PyPI + MCP registries (official, Smithery, mcp.so, Glama, PulseMCP) | the operator's own disk, passed at launch via `--bundles` |
| **Ships in the package?** | yes — code only | **never** — no bundle is embedded in the wheel, `server.json`, or any registry listing |

**Design consequence:** the published server takes the bundle directory
as a launch-time argument (`--bundles <dir>`) and reads nothing about a
user's workflow at build or publish time. A compiled bundle can contain
recorded example values that are business- or PHI-sensitive (see the
workspace license/PHI rules); it is the operator's artifact and stays
inside their trust boundary. The registry listing advertises the
*capability*, not any workflow.

**Read-only by default when registry-launched.** The official `server.json`
omits `--allow-run`, and the MCPB `manifest.json` leaves both execution and
attended actions off until the operator enables them in the install form. A
default install yields PHI-safe inspection and Needs Attention tools only
(`list_workflows`, `get_workflow`, `get_run_report`,
`list_needs_attention`, and `get_attention_item`). Enabling workflow runs
adds the dynamic `run_workflow_<opaque-id>` tools; enabling attended actions
adds Reject, Teach, and Escalate, while a qualified deployment config also makes
Continue and Skip available. Clients without MCP form elicitation use Flow's
attended console/CLI, where all five capabilities remain available. This
matches the security model in [`DESIGN.md`](DESIGN.md).

> There is intentionally **no** hosted, multi-tenant "official OpenAdapt
> workflow server" that exposes OpenAdapt-operated workflows to the
> public. v2 is stdio-only, single-user, local. A hosted control plane is
> a separate, proprietary surface (`openadapt-cloud`) and is out of scope
> for this package.

## 2. Canonical registry metadata

Every directory (official registry, Smithery, mcp.so, Glama, PulseMCP)
indexes the same core fields. Keep this block as the single source of
truth; the machine-readable copies are [`../server.json`](../server.json)
and [`../manifest.json`](../manifest.json).

- **Name (reverse-DNS, official registry):** `io.github.OpenAdaptAI/openadapt-agent`
- **Display name:** OpenAdapt Agent (openadapt-flow bridge)
- **PyPI package:** `openadapt-agent`
- **Version:** `2.0.2`
- **Description:** Local bridge for governed openadapt-flow workflows and attended actions.
- **Homepage / docs:** https://docs.openadapt.ai
- **Repository:** https://github.com/OpenAdaptAI/openadapt-agent
- **License:** MIT
- **Transport:** stdio
- **Run command (uvx):** `uvx openadapt-agent serve --bundles <BUNDLES_DIR> [--allow-run] [--allow-attended-actions]`
- **Config:** `--bundles` (required), `--runs-dir`, `--allow-run`, `--allow-attended-actions`, qualified `--config` for Continue/Skip, and optional secret `OPENADAPT_BUNDLE_KEY`
- **Tools:**
  - `list_workflows` / `get_workflow` — PHI-safe structural bundle projections with opaque IDs.
  - `get_run_report` — PHI-safe status and count summary; raw evidence stays local unless protected export was explicitly enabled.
  - `list_needs_attention` / `get_attention_item` — PHI-safe durable-pause cards and current signed-capability metadata.
  - `run_workflow_<opaque-id>` — execute through the governed `openadapt-flow run` CLI when `--allow-run`; returns `success` | `halt` | `refused` | `timeout` | `error`.
  - `continue_attention` / `skip_attention` / `reject_attention` / `teach_attention` / `escalate_attention` — exact, elicited attended decisions under Flow's capability, idempotency, verification, and audit contract.
- **Categories/tags:** mcp, agent-skills, automation, workflow, gui, governed, healthcare, rpa

## 3. Release automation vs. founder one-time actions

The build-and-publish pipeline lives in
[`.github/workflows/release.yml`](../.github/workflows/release.yml). The
founder reviews one exact protected-main commit before the release App creates
its annotated tag. That tag starts the separate PyPI and MCP registry jobs.
The App can't push a commit to `main`.

### 3.0 What the workflow does

| Trigger | Jobs that run | Publishes? |
| --- | --- | --- |
| Pull request touching release files | `validate` | No. This is a dry run. |
| Manual dispatch on exact current `main` | `validate`, then founder review in `release-identity`, then `create-release-tag` | No package publish. The App creates only the annotated tag. |
| Push of the exact reviewed annotated tag | `validate` -> `pypi-publish` -> `mcp-registry-publish` -> `registry-parity` | Yes, after the two protected publication environments approve it. |

- **`validate`** builds the sdist+wheel and local MCPB, runs
  [`scripts/check_release_artifacts.py`](../scripts/check_release_artifacts.py)
  (fails if a wheel or sdist carries a bundle, `.enc`, run outputs, keys, or
  any non-code payload), checks the MCPB for
  workflow/evidence payloads, runs `twine check`, validates `server.json`
  against the exact MCP schema at the pinned `2025-12-11` URL and SHA-256
  `3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0`,
  and runs the
  version-consistency guard (`tests/test_distribution.py`). It runs on PRs
  and manual dispatch so the pipeline is testable **without** publishing.
  An unavailable schema or changed schema bytes fail validation before any
  publisher can run.
- **`create-release-tag`** runs only after the manual version and full commit
  inputs match the reviewed candidate on exact current `main`. The
  founder-reviewed `release-identity` environment supplies the release App
  identity. The App creates one annotated `vX.Y.Z` tag and pushes that ref.
  It doesn't push a version commit or update `main`.
- **`pypi-publish`** runs only for the exact candidate tag. It rechecks the
  archives and uploads with PyPI Trusted Publishing in the protected `pypi`
  environment. There is no API-token path.
- **`mcp-registry-publish`** waits for the new version
  to be live on PyPI (the registry validates package existence), then
  runs `mcp-publisher login github-oidc` and `mcp-publisher publish` in the
  protected `mcp-registry` environment. It needs no repository token
  because the repo lives under the `OpenAdaptAI` org that owns the
  `io.github.OpenAdaptAI` namespace. The workflow downloads a fixed
  `mcp-publisher` version and verifies its SHA-256 before execution.
- **Registry parity and admission input** run after both publishes. The
  workflow downloads each PyPI artifact and proves byte-for-byte equality
  with the archives produced by the protected build. It also proves that the
  exact and `latest` MCP registry records equal the reviewed `server.json`.
  Only then does it retain a 30-day
  `production-admission-candidate-<version>` artifact. That record binds the
  source commit, artifact hashes, both public registry observations, and one
  exact commit of the canonical lifecycle policy.

The retained record is explicitly `not_admitted`. It has no admission ID,
release sequence, or Production channel selector. PyPI `latest` and MCP
`latest` are distribution checks only. They never grant Production status.
The active, signed ledger in `OpenAdaptAI/.github` is the sole Production
authority and requires its separate acceptance evidence and activation.

To cut a release, update the synchronized version fields, `CHANGELOG.md`, and
`release-candidate.json` in one reviewed change. Merge it. Then open the
`Release` workflow on `main` and enter the exact version and full current-main
commit. The protected job checks that `main` hasn't advanced before it creates
the tag.

If publication fails after the tag exists, rerun the failed job in that exact
tag workflow run. Don't create a recovery tag or branch.

### 3.a Required repo configuration and secrets (FOUNDER, one-time)

Complete this setup before the first App-created tag:

1. **Release App identity.** Install the `openadapt-release` GitHub App on the
   reviewed public repository set. Give it `Contents: write`; don't give it
   another write permission. Add `OPENADAPT_RELEASE_APP_ID` as an environment
   variable and `OPENADAPT_RELEASE_APP_PRIVATE_KEY` as an environment secret
   in `release-identity`. Restrict that environment to `main` and require the
   founder's review.
2. **PyPI Trusted Publishing.** On https://pypi.org, keep the publisher for
   project `openadapt-agent` configured as:
   owner `OpenAdaptAI`, repo `openadapt-agent`, workflow `release.yml`,
   environment `pypi`. (PyPI account with 2FA required; the project is
   created on first OIDC upload.)
3. **Publication environments.** Restrict `pypi` and `mcp-registry` to `v*`
   tags. Both jobs use OIDC. `io.github.OpenAdaptAI/*` is authorized by the
   repository's MCP registry identity.

### 3.1 Decide the release version

The MCPB distribution release is `2.0.2`. Future releases must keep the package,
module, registry, and MCPB metadata versions synchronized.

Set every future version in **four files that a CI test pins together**:
`pyproject.toml` `version`, `src/openadapt_agent/__init__.py` `__version__`,
both `version` fields in `server.json`, and `manifest.json` `version`. The
`tests/test_distribution.py` guard fails if they drift. Add one exact version
heading to `CHANGELOG.md`, then update the tag, previous tag, and changelog
binding in `release-candidate.json`.

### 3.2 Publish to PyPI (`pypi-publish` job)

This job runs for the exact reviewed tag after the `pypi` environment allows
it. You can reproduce its build and checks locally without publishing:

```bash
python -m build                             # sdist + wheel into dist/
python scripts/check_release_artifacts.py   # license/boundary gate
twine check dist/*
```

Verify after a release: `pip index versions openadapt-agent` shows the new
version and `uvx openadapt-agent@<version> --version` prints it.

### 3.3 Submit to the official MCP registry (`mcp-registry-publish` job)

This job runs after PyPI, using `mcp-publisher login github-oidc`. The
job waits for the PyPI version to be installable first, because the
registry validates package existence. For a local descriptor check, use the
publisher's interactive GitHub login:

```bash
mcp-publisher login github    # interactive OAuth
mcp-publisher publish         # reads ./server.json
```

If the registry asks you to prove the PyPI package belongs to this server,
add the ownership marker it names to the package metadata and re-run.

### 3.4 List on Smithery — MCPB upload after namespace claim

Smithery's current URL form accepts public **Streamable HTTP** servers. This
repository is intentionally a local stdio server, so do not paste the GitHub
URL into the MCP Server URL field and do not add an unauthenticated HTTP shim.
Smithery's supported local path is an MCPB bundle.

Build and inspect the bundle from the exact release source:

```bash
npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json
mkdir -p mcpb-dist
npx -y @anthropic-ai/mcpb@2.1.2 pack . mcpb-dist/openadapt-agent-2.0.2.mcpb
npx -y @anthropic-ai/mcpb@2.1.2 info mcpb-dist/openadapt-agent-2.0.2.mcpb
```

The release workflow performs the same validation and creates an `mcpb`
artifact. The bundle contains the public server code and configuration only;
the operator chooses their own bundle and evidence directories during local
installation. It never contains a compiled workflow or run evidence.

One-time publication then uses Smithery's local/MCPB flow:

```bash
smithery auth login
smithery namespace use <claimed-namespace>
smithery mcp publish mcpb-dist/openadapt-agent-2.0.2.mcpb \
  -n <claimed-namespace>/openadapt-agent
```

Confirm the generated install form shows workflow bundles (required), run
evidence, governed execution (off by default), attended actions (off by
default), optional deployment config, headed mode, and the masked bundle key.
The initial namespace claim and outward-facing publication remain founder
actions; the MCPB build itself is reproducible and automated.

### 3.5 mcp.so — MANUAL (one-time submission)

- Submit at https://mcp.so (Submit / "Add MCP Server") with the repo URL.
- It scrapes the README + `server.json`; ensure the "How results come
  back" and tool table render. No account action beyond the submission
  form.

### 3.6 Glama — MOSTLY AUTOMATIC (claim to control metadata)

- Glama (https://glama.ai/mcp/servers) auto-indexes public GitHub MCP
  servers and re-scores on push. Claim the server with the GitHub org
  account to control metadata, and confirm the MIT license + repo
  metadata are picked up.

### 3.7 PulseMCP — MANUAL (one-time submission)

- Submit at https://www.pulsemcp.com (their "Submit a server" flow) with
  the repo + PyPI package. PulseMCP indexes description, install command,
  and tool list from this doc's canonical block.

### 3.8 Agent Skills directories — MANUAL (first-party listing)

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

The generic repository is therefore ready for Smithery's **MCP server**
catalog through MCPB, not for Smithery's GitHub-backed **Skill** catalog.
Creating a separate bundle-free product skill could be considered later, but
it would be a new public surface rather than a substitute for this server.

## 4. Post-publish consistency checklist

- [ ] `uvx openadapt-agent serve --bundles <dir>` starts read-only for a fresh installer.
- [ ] The exact release source validates and packs with `@anthropic-ai/mcpb@2.1.2`.
- [ ] `server.json` version == PyPI version == `__version__` (CI-pinned).
- [ ] Registry descriptions match the canonical metadata block (§2).
- [ ] README badges point at the real PyPI project once published.
- [ ] docs.openadapt.ai links back to this repo (add an "Agents & MCP" page — see the OpenAdapt docs repo; that deploy is a separate founder action).
