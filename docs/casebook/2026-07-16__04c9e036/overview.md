# Overview

This case reviewed how casebook uses the Agent Client Protocol (ACP) surface and
then unified how it handles **session config options**. It began as a discussion —
does ACP expose "effort"? can we surface slash commands? what else is unused? — and
converged on a single principle: **every option a backend advertises (model,
reasoning effort, approval mode, toggles) is handled identically; the model is not
special.** Implemented and merged to `dev` as commit `35e747f`.

**Status: open.** Config options is the first slice of this case — shipped,
documented, and written up below. The case stays open as the home for the rest of
the ACP surface (see [Roadmap](#roadmap--remaining-acp-surface-future-sessions)),
each item to be picked up in its own session.

## Background: the ACP finding

ACP exposes the model two ways. The **dedicated `models` field + `session/set_model`
is marked UNSTABLE** in the spec ("not part of the spec yet, may be removed"), while
**`config_options` + `session/set_config_option` is stable**. Probing both real
backends confirmed each drives the model through the stable path:

- **claude-agent-acp** — model as a `config_options` select with `category=="model"`;
  leaves the `models` field null.
- **codex-acp** — same `category=="model"` select (GPT-5.6-Terra/Luna, GPT-5.5),
  *plus* redundantly fills the `models` field as an ugly model×effort cross-product.
  Also exposes `mode` (read-only/agent/agent-full-access) and `reasoning_effort`
  (low→ultra) as config options.

So the unstable `models` field is redundant everywhere it appears. Reusable probe:
`.mydev/probe_models.py <command...>`.

## What shipped

### 1. One uniform config-options path — IMPLEMENTED
All options flow through a single pipeline: `capture_config_options()` normalizes a
session response (and live `config_option_update`s) into one list; the coordinator
stores/publishes it; the UI renders it. `set_config_option` is the only setter.

### 2. Model fully de-specialized — IMPLEMENTED
Removed every trace of model-specific handling: the `models`-field capture,
`session/set_model`, `available_models`, `_capture_models`, the `is_model` flag,
`current_model`, and `agent["model"]` (dropped from meta/fork/persist). The model is
just the option with `category=="model"`. Config options are live session state,
re-read from the agent each session — **not persisted per-session** (matches how
effort/mode were always treated).

### 3. Default config options — IMPLEMENTED
Per-backend defaults via `[backends.<name>.config_options]` (keyed by option id),
applied at session start. Unapplicable defaults warn (unknown id, or value matching
none of the choices) rather than vanishing. This **supersedes the per-backend
`default_model` decision** from case `2026-06-26__4844cff5` — `default_model` is
removed; use `config_options.model` instead.

### 4. Naming via an explicit backend — IMPLEMENTED
The "name session" button now requires an explicit top-level `naming_backend`; if
unset, naming is **disabled** (no fallback to the session's own backend, no implicit
`naming` backend). The naming backend's model comes from its own `config_options`.
`naming_model` is removed; `one_shot()` applies the backend's `config_options` and no
longer touches the `models` field.

### 5. UI: the session-options (⚙) popover — IMPLEMENTED
Options collapse behind a single gear button in the session header (the header was
crowded and bare dropdowns gave no hint what they controlled). Each popover row shows
the human label, the control, and a **copy-ready TOML snippet of the current value**
(e.g. `reasoning_effort = "high"`) — so a user sets an option in the UI, reads off the
exact `key = value`, and pastes it under `[backends.<name>.config_options]`. Option
ids/values are backend-defined, so this discoverability is the intended way to learn
them.

### 6. Docs — IMPLEMENTED
`docs/configuration/backends.md`: schema row `default_model` → `config_options`; the
"Models" section rewritten as **"Session options"**; the "Naming" section rewritten
for the required/disabled semantics; a new **Codex** quick-setup recipe; Claude and
Gemini recipes converted to `config_options`. `docs/configuration/README.md` and the
top-level `README.md` updated to match. Fixed a pre-existing `default = "claude"` →
`default_backend = "claude"` docstring typo.

### 7. echo backend — IMPLEMENTED
Exposes its model as a `category=="model"` config option (plus a demo `effort` select
and `loud` toggle), so the always-available backend is representative of the real ones
instead of relying on the now-ignored `models` field.

## Environment change

Added `[backends.codex] command = ["codex-acp"]` to the user's
`~/.config/casebook/config.toml` (backed up alongside). Codex is installed and
logged in; restart casebook / reload-config to use it.

## Roadmap — remaining ACP surface (future sessions)

The discussion surveyed the broader ACP surface. Config options (above) shipped
first; these are the remaining threads for this case, each its own future session:

- **Slash commands** (`available_commands_update`) — SHIPPED. Invocation already
  worked (plain prompt text, no RPC); the gap was discoverability. Added: client
  captures the update, coordinator caches + snapshots it (mirrors config options),
  and the UI gains a screen-centred command palette ("/" header button, or the
  `/` hotkey) plus inline `/`-autocomplete on the composer. The first-message
  directive collision was probed and is **unavoidable** (the leading-user-text
  splice is load-bearing; embedded-context delivery is refused as prompt
  injection) — so it is documented as a small edge, not fixed. Full writeup:
  [slash-commands-directive-injection-findings.md](slash-commands-directive-injection-findings.md).
- **Session modes** — codex/claude expose approval/plan mode. Codex publishes it *both*
  as a `mode` config option (now surfaced automatically via this work) and as the
  dedicated `SessionModeState`; the dedicated mode API is not otherwise used.
- **Session fork** (`session/fork`) — casebook's manual transcript-copy fork
  (`fork_agent`) is more flexible (arbitrary-point truncation, any backend); native
  fork is higher-fidelity but head-only. A hybrid was left for later.
- **Prompt capabilities** (image/audio, `embedded_context`) — casebook only sends text;
  supporting attachments needs capturing `prompt_capabilities` at spawn.
- **Terminal** — casebook sets `terminal=false`; enabling it (brokered like file I/O)
  is on-thesis but a large security surface. **Observed finding:** claude-agent-acp
  still runs commands (git commit, etc.) despite `terminal=false` — it executes out of
  band via its own machinery, not through casebook's brokered terminal methods. So this
  thread is less "unlock a missing capability" and more "decide whether casebook should
  broker/observe/police execution the backend already does on its own." First step for
  the session: probe each backend's behavior under `terminal=false`. (Separately: a
  backend's WebFetch/HTTP tool failing is likely network-egress sandboxing or an
  unexposed tool — *not* the ACP `terminal` capability, which governs command execution
  only; probe independently.)
- **Per-session config persistence** — config options currently reset to backend
  defaults on resume (or are restored by the agent on `session/load`). If per-session
  memory is wanted, it should persist *all* options uniformly, not just the model.
