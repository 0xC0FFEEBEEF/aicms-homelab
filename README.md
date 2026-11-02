# AICMS

AICMS is a local FastAPI service that inventories hosts over SSH, stores scan snapshots in SQLite, exposes a local Jinja-rendered web UI, integrates with LM Studio, and checks a private GitHub repo for updates.

This version is focused on local inventory and control:
- host management
- SSH connectivity testing
- remote bootstrap for the scanner user and SSH key installation
- manual full and incremental scans
- project discovery and scan history
- deterministic machine fact collection
- topology-aware parent/guest relationship tracking
- discovered guest enrollment and bootstrap flows
- signature-based identity resolution and machine profile generation
- project-mapping preview and publishability controls
- settings management
- LM Studio model discovery and profile formatting
- optional MCP-backed profile enrichment
- optional Web Search / SearXNG enrichment
- GitHub update checks for a private repo
- connectivity helper commands for target VMs

Out of scope in this version:
- CMS publishing
- auto-posting
- automatic recurring scans

## Stack

- Python
- FastAPI
- Jinja templates
- SQLite
- SQLAlchemy 2.x
- Paramiko
- HTTPX

## Repo Layout

- `app/main.py`: FastAPI app factory and startup wiring
- `app/config.py`: environment-backed config
- `app/models.py`: SQLAlchemy models
- `app/signatures/`: deterministic service/platform signature catalog and resolver
- `app/profiles/`: machine classification, fact helpers, and machine profile generation
- `app/project_mapping/`: CMS-readiness preview generation and publishability overrides
- `app/publish_preview/`: website-preview adapter models and data shaping for public-style project previews
- `app/topology/`: parent/guest relationship sync and discovered guest workflows
- `app/scanner/`: SSH scanning and project detection
- `app/lmstudio/`: LM Studio client and services
- `app/mcp/`: optional bounded MCP integration
- `app/websearch/`: optional bounded Web Search / SearXNG integration
- `app/github/`: GitHub update check client and service
- `app/web/`: routes, templates, and static assets
- `src/`: imported public website source used as the visual reference and extraction basis for preview rendering
- `scripts/install_ubuntu.sh`: Ubuntu install + `systemd` registration
- `scripts/update_aicms.sh`: manual update helper
- `tests/`: core flow tests

## Setup For Local Development

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

2. Create the environment file:

```bash
cp .env.example .env
```

3. Start the app:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

4. Open:

```text
http://localhost:8000/web
```

## Ubuntu VM Installer

If you cloned the repo onto the Ubuntu VM, run:

```bash
./scripts/install_ubuntu.sh
```

What it does:
- installs required Ubuntu packages
- creates `.venv`
- installs `requirements.txt`
- creates `.env` from `.env.example` if needed
- creates `data/` and `logs/`
- installs and starts a `systemd` service named `aicms`

Useful service commands:

```bash
sudo systemctl status aicms
sudo systemctl restart aicms
sudo systemctl stop aicms
sudo journalctl -u aicms -f
```

## Updating

From the UI:
- open `Settings > GitHub`
- save the GitHub repo owner and repo name
- save a PAT if `gh` is not already installed and authenticated on the VM
- click `Check For Updates`
- use `Run Update Script` to launch the existing updater from the web UI
- optionally provide a one-time sudo password in that form if the AICMS service user needs it for the update

To apply the update on the VM:

```bash
./scripts/update_aicms.sh
```

The UI updater still calls the same `scripts/update_aicms.sh` helper. It runs as a detached background job, may restart the AICMS service during the update, shows the latest updater status/log on the GitHub settings tab, and does not persist the sudo password in SQLite.

## Main Routes

HTML routes:
- `GET /health`
- `GET /web`
- `GET /web/hosts`
- `GET /web/hosts/new`
- `GET /web/hosts/{id}`
- `GET /web/guests/{id}/enroll`
- `GET /web/projects`
- `GET /web/projects/preview`
- `GET /web/projects/{id}`
- `GET /web/projects/{id}/preview`
- `GET /web/scans`
- `GET /web/settings`
- `GET /web/lmstudio`

POST routes:
- `POST /api/hosts`
- `POST /api/hosts/{id}/test`
- `POST /api/hosts/{id}/scan/full`
- `POST /api/hosts/{id}/scan/incremental`
- `POST /api/scan/full`
- `POST /api/scan/incremental`
- `POST /api/settings/general`
- `POST /api/settings/scan`
- `POST /api/settings/profiles`
- `POST /api/settings/lmstudio`
- `POST /api/settings/github`
- `POST /api/lmstudio/test`
- `POST /api/lmstudio/models/refresh`
- `POST /api/github/check-updates`
- `POST /api/hosts/{id}/facts/refresh`
- `POST /api/hosts/{id}/topology/refresh`
- `POST /api/guests/{id}/enroll`
- `POST /api/guests/{id}/bootstrap`
- `POST /api/guests/{id}/link`
- `POST /api/guests/{id}/unmanaged`
- `POST /api/settings/mcp`
- `POST /api/mcp/test`
- `POST /api/settings/web-search`
- `POST /api/web-search/test`
- `POST /api/web-search/test-search`
- `POST /api/projects/{id}/profile`
- `POST /api/projects/{id}/profile/stop`
- `POST /api/projects/{id}/identity/refresh`
- `POST /api/projects/{id}/mapping/refresh`
- `POST /api/projects/{id}/overrides`
- `POST /api/projects/{id}/interpret`

## First Run Workflow

1. Open `/web/hosts/new` and add a host with one or more allowlisted roots.
2. If the target VM is not ready yet, use `Bootstrap + Save Host` or the bootstrap card on the host detail page to create/configure the scanner user and install the SSH public key.
3. Use `Test Connection` to verify SSH auth and root visibility.
4. Run a full scan from the dashboard or the host detail page.
5. Open `/web/projects` to inspect discovered projects.
6. Open `/web/settings?tab=lmstudio` and configure LM Studio.
7. Refresh models.
8. Open a project detail page and run `Generate profile` or `Refresh profile`.
9. Open `/web/settings?tab=github` to configure private-repo update checks.

## Topology Awareness

AICMS now persists machine relationships instead of treating every host as isolated.

That includes:
- Proxmox guest discovery stored as first-class guest inventory
- parent hypervisor to guest linkage
- guest type and guest ID tracking
- discovered guest enrollment into the normal host list
- manual linking of an existing enrolled host back to a discovered parent

On guest pages, AICMS can show:
- the parent host
- the parent platform
- guest type
- guest ID

On hypervisor pages, AICMS can show:
- enrolled guests
- discovered-only guests
- unmanaged guests

Discovered guests can be:
- added as hosts
- bootstrapped through the existing bootstrapper
- linked to an existing host record
- marked unmanaged

If a discovered guest is bootstrapped successfully, AICMS immediately runs:
- a connectivity test
- a host fact refresh

Profile refresh remains a separate explicit action.

## Default Scanner SSH Key Path

AICMS now has a canonical default scanner key path setting:

- `Settings > General > Default scanner SSH key path`
- default value: `~/.ssh/aicms_scanner_ed25519`

This value is used to prefill:
- the add-host form
- the edit-host form when the host does not already have a custom key path
- discovered guest enrollment forms
- bootstrap forms

The path is stored in the human-friendly `~/.ssh/...` form in settings and host records. It is expanded only when AICMS actually needs filesystem access.

## Machine Profiles

Machine Profiles are the main summary system in AICMS now.

Each scanned host is treated as a machine profile page that tries to answer:
- what this machine is for
- what resources it has
- how it is set up
- what it runs or contains
- how it fits into the rest of the environment

The flow is grounded and layered:
1. deterministic SSH collectors gather machine facts
2. topology and local project evidence are merged in
3. deterministic service/platform signatures are evaluated
4. the highest-confidence signature becomes the machine identity
5. if the identity or public copy is weak, AICMS runs bounded research to improve it
6. optional MCP and Web Search enrichment can add bounded supplemental context during that research stage
7. LM Studio optionally formats the machine-profile narrative fields
8. AICMS builds separate card copy and publish-ready copy for later CMS draft readiness

If LM Studio is disabled or unavailable, AICMS still generates a deterministic machine profile.

## Signatures And Identity Resolution

Signatures are deterministic definitions for recognizable machine roles.

Each signature includes:
- required indicators
- optional indicators
- confidence rules
- machine kind
- detected platform
- detected role
- stack tags
- a project-mapping hint
- a conservative default publishability recommendation

The resolver ranks all signatures against locally observed facts such as:
- hostname and machine name
- topology context
- running services and systemd unit names
- known file paths and config paths
- detected stack markers and project paths
- selected process names when collected
- owner-supplied notes and overrides

The primary identity is chosen deterministically. LM Studio does not choose it.

Current built-in signatures include:
- Proxmox hypervisor
- Proxmox Backup Server
- Wazuh stack
- Golem workload
- Grafana
- MCP gateway
- Nextcloud
- Docker host
- Open WebUI
- Home Assistant
- generic project VM
- generic service node

If evidence is weak, AICMS falls back conservatively and marks the identity as needing review.

### Bounded Research

When the identity result, title candidate, or summary candidate is still weak, AICMS can run a bounded research stage before rebuilding public copy.

This stage:
- starts from local facts, topology, service names, paths, markers, and owner hints
- only uses MCP or Web Search when those integrations are enabled
- stores research runs, sources, and findings separately
- records whether a finding affected identity, title generation, summary generation, or tech tags

Local facts remain authoritative. External research is supplemental only.

### Wazuh Recognition

AICMS now tries to recognize Wazuh-like machines from observed local evidence such as:
- `wazuh-manager`
- `wazuh-indexer`
- `wazuh-dashboard`
- `/var/ossec`
- `/etc/filebeat`
- `filebeat`
- OpenSearch or Elasticsearch-related services when they are directly observed

If only part of the stack is visible, the identity confidence is lowered instead of inventing missing components.

### What Gets Collected

For Linux hosts and VMs, AICMS collects metadata such as:
- hostname
- FQDN when available
- operating system and kernel
- CPU cores and threads
- RAM and swap totals
- root disk sizing
- mounted filesystem summary
- IP addresses
- uptime when available
- virtualization hints
- selected running services

For Proxmox hosts, AICMS also tries to collect:
- Proxmox version
- storage pool summary
- `qm list` guest inventory
- `pct list` guest inventory
- guest config summaries from `qm config` and `pct config`
- guest IDs, names, status, cores, RAM, disk summary, and config paths
- guest-to-parent topology linkage
- discovered guest enrollment state

### How LM Studio Participates

LM Studio no longer decides the machine identity from scratch.

Instead, AICMS sends a structured machine-profile packet that already includes:
- deterministic host facts
- deterministic signature-based identity
- topology facts
- guest inventory
- service inventory
- detected setup details
- owner-supplied notes and instructions
- project-mapping context
- optional MCP enrichment evidence
- optional Web Search enrichment evidence

LM Studio is only used to format narrative fields such as:
- lead paragraph
- project role summary
- setup bullets
- why-it-matters bullets
- notes
- unknowns

The prompt tells LM Studio to:
- use first-person ownership language with `my`
- never use `your`
- never rename the machine
- never invent cores, RAM, storage, guests, services, or roles
- say less rather than guessing
- treat local facts and topology as authoritative
- treat MCP and Web Search as supplemental only

### Profile Types

AICMS currently formats these main profile types:
- Proxmox hypervisor profiles
- project VM profiles
- service/control-layer profiles
- generic Linux node profiles
- unknown/fallback profiles

### Proxmox Host Pages

If AICMS classifies a host as a Proxmox hypervisor, the project page renders as a machine profile with sections for:
- lead paragraph
- host resources
- storage
- contained LXCs
- contained VMs
- linked and discovered guest relationships
- how it is set up
- observed indicators
- enrichment evidence when available
- notes and unknowns

### Normal VM Pages

If AICMS classifies a host as a project VM or service/control node, the page focuses on:
- lead paragraph
- hosted-on topology when known
- host resources
- deterministic identity and signature evidence
- project role
- how it is set up
- detected components or services
- project-mapping preview and publishability controls
- enrichment evidence when available
- why it matters in the environment when applicable
- notes and unknowns

## Project Mapping Preview

AICMS does not publish anything to a CMS in this version.

Instead, each machine now generates a deterministic project-mapping preview that is meant to prepare for later CMS draft export.

The preview computes fields such as:
- `card_title_candidate`
- `card_summary_candidate`
- `card_tech_tags_candidate`
- `publish_title_candidate`
- `publish_summary_candidate`
- `publish_long_description_candidate`
- `publish_sections`
- compatibility fields like `project_title_candidate` and `project_summary_candidate`
- `publishable_kind`
- `recommended_status`
- `demo_url_candidate`
- `repo_url_candidate`
- `publishability_reason`
- `internal_only_reason`

This preview is derived from:
- local facts
- topology
- the resolved signature identity
- machine-profile content
- owner overrides and related hosts

Low-confidence or unclear machines stay conservative by default.

### Card Copy vs Publish Copy

AICMS now keeps public copy in two distinct layers:

- `project_card_copy`
  - concise
  - title + short summary + tech tags
  - intended for the public Projects grid
- `project_publish_copy`
  - richer
  - title + short summary + long description + optional bullet sections
  - intended for future CMS/export work

The same paragraph is not reused for both anymore.

Hypervisors use this split heavily:
- card copy stays short and project-like
- publish copy can include guest counts, featured workloads, and guest bullet lists

## AI Clarification Workspace

Each project page now includes an internal-only `AI Clarification Workspace`.

This is an editorial/project-Q&A area where AICMS can ask targeted clarification questions when:
- identity is ambiguous
- the public title is still weak or generic
- publishability is unclear
- standalone-vs-parent positioning is unclear
- public summary framing is still too generic

The system is intentionally conservative:
- it does not ask on every refresh
- it only creates high-value questions that can materially improve identity, mapping, or public copy
- ignored or resolved questions are not recreated unless you explicitly choose to revisit them later

Each clarification thread supports:
- open
- answered
- resolved
- ignored

You can:
- answer inline
- mark a thread resolved
- ignore it
- ask a follow-up
- regenerate title, summary, mapping, or public copy from saved answers

Answered questions are turned into durable project guidance such as:
- preferred public title
- preferred role wording
- preferred summary framing
- standalone vs part-of-parent preference
- publishable vs internal-only intent
- preferred project group/family

This guidance is then reused by later:
- deterministic identity resolution
- project mapping preview generation
- public title generation
- card/public copy generation

Important:
- the clarification workspace is internal/admin-only
- raw question text, rationale, and answers do not appear in website previews
- only approved/generated public copy derived from that guidance can appear in previews later

This fits into the future CMS workflow as an editorial memory layer, but it does not publish anything and it does not integrate with the CMS yet.

## Interactive Investigation Workspace

AICMS now also supports a separate interactive investigation workflow.

This is different from a normal scan or a one-shot profile refresh:
- you start a live investigation session against an existing host, a discovered guest, or a manual SSH target
- AICMS collects facts and builds a draft report
- the AI can ask targeted follow-up questions when clarification would materially improve the result
- you can answer inside the same session
- the draft findings, report, and proposed host/project changes update as the session evolves
- nothing is applied until you explicitly confirm and then apply the proposals

The workspace is available at:
- `GET /web/investigations`
- `GET /web/investigations/new`
- `GET /web/investigations/{id}`

The API layer currently supports:
- `POST /api/investigations`
- `POST /api/investigations/{id}/message`
- `POST /api/investigations/{id}/collect`
- `POST /api/investigations/{id}/regenerate`
- `POST /api/investigations/{id}/confirm`
- `POST /api/investigations/{id}/apply`

Each investigation keeps:
- target information
- a live message thread between you, the AI, and system status updates
- normalized findings
- versioned draft reports
- proposed host and project changes
- final confirmation/apply state

Important behavior:
- investigation chat is internal/admin-only
- AICMS does not auto-apply host or project changes
- user answers are treated as high-priority guidance for the current session
- confirmed answers are fed back into the existing clarification/guidance system when a project already exists
- the final report, chat history, findings, and applied proposals are preserved after apply

This also keeps the service boundaries ready for later MCP/Open WebUI exposure, because the core actions are already split into:
- start a session
- send a message
- collect facts
- regenerate the draft
- confirm
- apply

## Publishability Controls

Not every machine should become a public project later.

Each machine page now includes per-host override fields for:
- project label override
- public title override
- public summary hint
- publishable project override
- internal-only override
- related hosts
- public tech tag overrides
- project group or family
- notes for later CMS export

If neither override is set, AICMS uses signature defaults conservatively.

CMS integration is not implemented yet. This layer only prepares structured preview data for later.

## Website Preview

AICMS now includes website preview pages for:

- the full Projects listing: `GET /web/projects/preview`
- `GET /web/projects/{id}/preview`

This is a preview-only feature:
- it does not publish anything
- it does not integrate with the CMS yet
- it does not leak internal-only notes into the public-style preview

The preview uses AICMS as the data source:
- machine identity result
- machine profile
- project-mapping preview
- publishability controls and public overrides

The preview look is based on the imported website source in `src/`.

What was reused from the imported website:
- the public Projects card structure from `src/pages/ProjectsPage.tsx`
- the public project card classes and styling from `src/index.css`
- the public shell/theme vocabulary such as `public-shell`, `site-nav`, `glass-section`, `project-grid`, `project-card`, `project-tags`, `project-links`, and `chip-grid`

What was adapted:
- there was no standalone public project-detail page in the imported site, so AICMS builds the full preview page from the same public shell/theme primitives instead of inventing a separate unrelated mockup
- AICMS maps machine/profile/mapping data into a preview view model through `app/publish_preview/`

Preview modes:
- Projects Page: renders the imported public Projects page structure using the concise card copy
- Card Preview: shows the reusable public project card in isolation using card copy only
- CMS Field Preview: shows the mapped publish-ready fields plus the richer publish copy, including long description and any structured bullet sections

From `/web/projects`, the `Website Preview` button opens the public-style Projects page preview for the current project set. Individual project rows also have their own `Preview` link.

Readiness warnings are shown on the preview page for cases like:
- internal-only machines
- needs-review mappings
- low-confidence identity
- missing title or summary
- missing demo or repo URL
- missing publishability approval
- generated fallback wording still being used

## MCP And Web Search Enrichment

LM Studio, MCP, and Web Search are separate integrations.

### MCP

The MCP integration is optional and disabled by default.

Settings are under:
- `Settings > MCP Integration`

Important behavior:
- HTTP-only MCP transport
- read-only / search-only mode is enabled by default
- bounded tool usage using the configured allowlist and limits
- non-allowlisted or obviously unsafe tools are rejected during enrichment

MCP is used only as supplemental profile context. It does not replace local SSH facts or topology.

### Web Search / SearXNG

Web Search is also optional and separate from LM Studio and MCP.

Settings are under:
- `Settings > Web Search`

The current implementation supports:
- `searxng`
- `disabled`

You can configure:
- a query URL template containing `<query>`
- language
- result count
- concurrency and timeout bounds
- domain filters
- optional web-loader behavior

Web Search evidence is stored separately and shown on the machine profile page as supplemental context.

### Provenance And Safety

The profile pipeline is:
1. local host facts
2. topology facts
3. local services and components
4. local project markers and evidence
5. deterministic signature resolution
6. optional MCP enrichment
7. optional Web Search enrichment
8. final machine profile formatting
9. project-mapping preview generation

Local scanning and topology remain the source of truth.

MCP and Web Search are:
- optional
- bounded
- read-only or search-oriented
- supplemental only

## Safety Rules In This Implementation

- Only allowlisted roots are scanned.
- The scanner never defaults to the full filesystem.
- Secret-bearing files and sensitive paths are ignored and excluded from stored shallow trees.
- Machine profile generation sends compact metadata packets only, not repository contents or secrets.
- Signature-based identity resolution uses local facts first and does not let LM Studio invent machine identity.
- MCP and Web Search are supplemental only and can be disabled independently.
- Enrichment evidence is stored separately from local scan facts.
- Project mapping preview is CMS-readiness only. No CMS publishing or draft creation is performed here.
- Host inspection remains read-only.
- If LM Studio or GitHub is unavailable, the rest of the app continues working.
- If MCP or Web Search is unavailable, the rest of the app continues working.

## Tests

Run the test suite with:

```bash
.venv/bin/pytest -q
```

## About this copy

A public snapshot of my private working repo (its `Test` branch). Example addresses and host names in the
tests and defaults are stand-ins. More about the project: [nbdevlab.com/projects](https://www.nbdevlab.com/projects).


## About this copy

A public snapshot of my private working repo (its `Test` branch). Example addresses and host names in the
tests and defaults are stand-ins. More about the project: [nbdevlab.com/projects](https://www.nbdevlab.com/projects).

## License

GNU General Public License v3.0 or later, see [`LICENSE`](LICENSE).

<sub>Preparing this public copy from my private repo was done with help from Claude.</sub>
