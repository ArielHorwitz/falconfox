# Overview

This case covers improving casebook's user experience for first-time and
day-to-day use. The initial discussion evaluated options across three areas and
produced handoff documents for each, intended to springboard into separate
implementation sessions.

## Areas

### 1. App launching (auto-start + browser open) — IMPLEMENTED
**Decision:** Option C — a single `casebook` command that auto-starts the server
if not running and opens the browser. `--fg` for explicit foreground mode, `--stop`
to kill the daemon. No subcommands — flags only. Port is auto-selected (base
9721, increments on conflict) and recorded in `server.json`. See
`handoff-app-launching-auto-start-and-browser-open.md`.

### 2. Backend installation and configuration — REVISED, PARTIALLY IMPLEMENTED
**Original decision:** A known-backends registry (data file bundled with
casebook) plus a UI for installing, configuring, and managing backends.
See `handoff-backend-installation-and-configuration-ux.md` for the original
design.

**Revised decision:** No in-app registry or UI. Configuration stays file-only
(`config.toml`). Instead, a backends guide document covers common backends with
full install-and-configure walkthroughs. Users of this tool are expected to be
comfortable editing config files. The added complexity of a settings UI, config
writing, install streaming, etc. is not justified.

**Implemented:**
- **Per-backend `default_model`** — moved from top-level config into each
  `[backends.*]` table. The top-level `default_model` key is removed. Docs
  updated in `docs/configuration/`.
- **Config hot-reload** — `reload_config()` on coordinator, `POST /api/reload`
  endpoint, reload button (↻) in the topbar. The frontend handles the
  `config_changed` WebSocket event to refetch hotkeys, UI, and backends.

**Done:** `docs/configuration/backends.md` now covers the built-in backends,
worked config examples (Claude, Gemini, npx, custom), and a Models section with
install notes. See
`handoff-backends-guide-config-reload-and-per-backend-model.md` (item 3) for the
original scope.

### 3. Settings page and first-time onboarding — DROPPED
**Original decision:** An in-app settings page covering backends, defaults,
hotkeys, and UI preferences. First-time onboarding via contextual banners.
See `handoff-settings-page-and-first-time-onboarding.md` for the original
design.

**Dropped:** No settings page. Configuration is file-only. Onboarding is handled
through documentation (README, backends guide) rather than in-app UI.

### 4. Friendly installation
**Decision:** Use `uv tool install git+https://github.com/<owner>/casebook` as
the primary install method. The package already has a standard `pyproject.toml`
with a `[project.scripts]` entrypoint and hatchling build backend, so this works
out of the box — users get an isolated venv, the `casebook` CLI on PATH, and
upgrades via `uv tool upgrade casebook`.

**Prerequisites to verify:**
- The built wheel must include everything needed at runtime (frontend assets).
  The current `hatch.build.targets.wheel.packages` only lists `src/casebook`;
  bundled frontend files may need explicit inclusion.
- A build step (e.g. `justfile` target) should produce frontend assets before
  `uv build` for development/release workflows.

**Future considerations:**
- PyPI publication (`uv tool install casebook` without git URL) — friendlier but
  deferred until the project is ready for broader distribution.
- `pipx install` as an alternative for users without uv — same mechanism, worth
  mentioning in docs.

### 5. README rewrite and doc cleanup — IMPLEMENTED
**README** rewritten from the ground up for new users who have no prior notion
of casebook. Covers what casebook is (concepts: cases, sessions, backends, ACP),
getting started (install, backend setup, running), using the app, and
configuration with links to the detailed docs. No references to the old CLI
workflow or preamble copy-pasting.

**Deleted outdated design docs:** `docs/vision.md`, `docs/architecture.md`, and
`docs/implementation-decisions.md` — all were planning/transition documents from
the initial rework that no longer reflect the current system. Cleaned up
source-code comments that referenced them.

## Deferred

- **Single binary packaging** (PyInstaller, Tauri, Rust rewrite) — deferred as a
  separate concern. The current architecture works well for Option C; packaging
  can be revisited when distribution becomes a priority.
