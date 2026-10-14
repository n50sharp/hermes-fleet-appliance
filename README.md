# Hermes Fleet Appliance

A clean, installable, shareable multi-agent Hermes appliance packaged as a production Docker image and Docker Compose deployment.

This repository provides a **blank-slate fleet** ready for deployment on any fresh host. It bundles 6 specialized agent profiles, a live Mission Control management dashboard, an automated markdown knowledge wiki, and an Obsidian note vault.

---

## Fleet Architecture

The appliance comes pre-configured with 6 decoupled profiles:

| Profile | Name | Role | Focus |
|---|---|---|---|
| `default` | **JV** | Orchestrator | Fleet coordination, multi-agent task routing, system state |
| `dev` | **Dev** | Engineering Specialist | Dashboards, backend scripts, automations, API integrations |
| `dev2` | **Dev2** | Junior Engineer | Focused build sub-tasks, test runs, and glue code |
| `scout` | **Scout** | Research Specialist | Tech scouting, competitive analysis, documentation recon |
| `scribe` | **Scribe** | Technical Writer | Guides, architectural records, changelogs, reporting |
| `reach` | **Reach** | Growth & Outreach | Distribution channels, announcements, external platforms |

---

## Bundled Components

1. **Mission Control Dashboard** (`:51763`)
   - Real-time fleet overview, agent status, and active task tracking.
   - Kanban board backed by SQLite schemas (`board.db`, `missions.db`).
   - Standalone Python stdlib backend (`server.py`).

2. **Blank Knowledge Wiki** (`~/wiki`)
   - Structured markdown knowledge base with rulebook (`WIKI.md`), index (`index.md`), and activity log (`log.md`).
   - Organized directory skeleton (`processed/{personal,business,research,mission,journal}`, `raw`, `_meta`, `_quarantine`).
   - Automated `wiki.sh` CLI utility in `_bin/wiki.sh`.

3. **Obsidian Vault** (`~/Obsidian-vaults/note-vault`)
   - Pre-configured `.obsidian/` directory with sensible core plugins, graph view, and note templates.

---
## Prerequisites

- **Docker Engine** (version 24.0+) or **Docker Desktop**
- **Docker Compose** (Compose V2)

---

## Quick Start

### 1. Clone & Build

```bash
git clone https://github.com/n50sharp/hermes-fleet-appliance.git
cd hermes-fleet-appliance

# Build using docker compose
docker compose build
```

Or build bare Docker image:
```bash
docker build -t hermes-fleet-appliance .
```

### 2. Run Appliance

```bash
docker compose up -d
```

The container will initialize all databases, set up git repositories for the wiki and vault, and launch Mission Control.

### 3. Access Mission Control

Open your browser to:
```
http://localhost:51763
```

### 4. First-Run Setup (Add API Keys)

The appliance ships with **zero API keys** baked in. Configure your LLM provider on first run:

```bash
docker exec -it hermes-appliance hermes setup
```

Follow the interactive prompts to choose your provider (OpenRouter, Anthropic, OpenAI, MiniMax, etc.) and enter your API key.

---

## Working with Profiles

Open an interactive shell in the context of any profile:

```bash
# Run as Orchestrator (JV)
docker exec -it hermes-appliance hermes chat

# Run as Dev
docker exec -it hermes-appliance hermes chat -p dev

# Run as Scout
docker exec -it hermes-appliance hermes chat -p scout

# Run as Scribe
docker exec -it hermes-appliance hermes chat -p scribe

# Run as Reach
docker exec -it hermes-appliance hermes chat -p reach

# Run as Dev2
docker exec -it hermes-appliance hermes chat -p dev2
```

---

## Data Persistence

Docker named volumes ensure all data added after deployment persists across container updates and restarts:

| Volume | Destination | Contents |
|---|---|---|
| `hermes_data` | `/home/hermes/.hermes` | Agent configs, auth, session history, memory databases |
| `wiki_data` | `/home/hermes/wiki` | Knowledge wiki markdown pages and git repository |
| `obsidian_data` | `/home/hermes/Obsidian-vaults` | Note vault and user documents |
| `mc_data` | `/home/hermes/agent-mission-control/data` | Mission Control operational database storage |
| `workspaces_data` | `/home/hermes/fleet-workspaces` | Workspace directories for agent tasks and projects |

---

## What is NOT Included

This appliance is strictly a **clean slate** distribution:

- **NO Credentials or Secrets**: Zero API keys, passwords, platform tokens, or SSH keys.
- **NO Real Memories**: All `memories/` directories are initialized empty.
- **NO Personal/Business Data**: No proprietary trading, client, business, or operational records.
- **NO Session Logs**: No past conversation histories or run dumps.
- **NO Mission-Specific Projects**: External demo apps and private repositories have been excluded.

---

## Updating the Appliance

To pull updates and rebuild without losing persistent data:

```bash
git pull origin main
docker compose build
docker compose up -d
```
