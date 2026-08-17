# falconfox pivot: per-module plan

How the current casebook repo becomes falconfox. Companion to
[overview.md](overview.md) — that holds the *why*, this holds the *what changes
where*. Written against the repo as of 2026-07-27 (commit `6fadeae`, branch `dev`).

## The one structural change everything follows from

Today a session is identified by **`(project_id, case_id, agent_id)`** and lives
under `<project_root>/.casebook/sessions/<case_id>/<agent_id>/`.

In falconfox a session is identified by **`session_id` alone**, and carries a
**`path`** (its cwd) as ordinary metadata.

That single change is what deletes cases *and projects* as server-side concepts,
flattens the API, flattens the UI, and enables `falconfox spawn --path ~` (the
manager session, which is not a "project" at all). Everything below is a
consequence of it.

## Module-by-module

| Module | Fate | Notes |
|---|---|---|
| `engine/` (client, session, events, oneshot) | **Keep, ~untouched** | This is falconfox's core and already "knows nothing about cases". `AgentSession.project_root` → `path`. |
| `coordinator.py` (920 lines) | **Keep, de-case** | Session lifecycle, permissions, transcripts, usage, config options, fork/revert, naming all survive. Cut: case CRUD, `read_case_file`, `_case_summary`, case dir watching, directive injection, `promote_agent`. Becomes one global `SessionCoordinator`, not one per project. |
| `cases.py` (168) | **Delete** | Move `format_toml_value` first — `storage.py` imports it. |
| `templates.py` (67) | **Delete** | Directive injection dies with it; orientation is the skill's job. |
| `storage.py` | **Keep, repath** | See "Storage" below — the change with the nicest side effect. |
| `projects.py` | **Delete (probably)** | The project registry/path cache loses its purpose. The distinct `path`s in the session list *are* your recent projects, for free. |
| `web/server.py` (400) | **Keep, flatten routes** | See "API" below. |
| `web/static/app.js` (1659) | **Keep pane, redo navigation** | Session pane survives nearly intact; the case-centric IA is replaced. |
| `cli.py` (161) | **Rewrite/expand** | Today it is *only* a launcher. It becomes the control plane — see "CLI" below. |
| `state.py` | **Keep, extend** | `server.json` discovery grows host/token for remote clients. |
| `config.py` | **Keep, globalize** | Backends/hotkeys/UI stay. **Decided: per-path config does not survive** — falconfox is a global daemon that happens to spawn sessions at a given path, so config is daemon-global only. |
| `echo_backend.py`, `logsetup.py`, `_version.py` | **Keep** | Unchanged. |
| `docs/casebook/` | **Keep** | Now maintained by the casebook *skill*, like any other project. |

## Storage: falconfox stops writing into your repos

`SessionStore` currently writes to `<project_root>/.casebook/sessions/<case_id>/<agent_id>/`
— both the project root and the case id are baked into the path, and
`_ensure_dotdir()` creates a `.casebook/` + `.gitignore` inside every checkout.

Flat sessions with arbitrary spawn paths force a **central store**:
`~/.local/state/falconfox/sessions/<session_id>/{meta.toml,transcript.jsonl}`.

Consequences:
- `_ensure_dotdir()` disappears — **falconfox leaves no trace in your repos.** A
  clean property that also matches "the daemon persists sessions; the filesystem
  (via the skill) persists the work."
- `relocate()` is deleted (it existed only for scratch→case promotion).
- All store methods lose their `case_id` parameter.
- `load_all_meta()` stops walking case dirs; one flat directory of sessions.
- Sessions survive independently of the checkouts they point at.

## API: flatten the routes

Every route is currently scoped `/api/projects/{project_id}/...`, and the socket is
`/ws/{project_id}?case={case_id}`. These collapse to a flat, global surface:
`/api/sessions` (list/spawn), `/api/sessions/{id}`, and a single `/ws`.

**Protocol change — snapshot scoping.** `snapshot(case_id)` currently uses the case
as the scoping key so a browser doesn't receive every transcript. With no case,
a global socket would dump everything. Replace with:

1. connect → snapshot of **session metadata only** (id, name, path, backend, state,
   created, last-active, usage)
2. client opens a session → server replies with its transcript

The machinery already exists: `load_persisted()` reads only `meta.toml`,
`_ensure_transcript()` loads lazily, and the `transcript_reset` event (already used
by `resume_agent`) is exactly the "here is the transcript" message. So this is a
small change, and it serves the telegram bot too — a chat focuses one session and
needs only that one.

## CLI: from launcher to control plane

`cli.py` today only manages the server process (start/stop/restart/foreground +
open browser). There is **no CLI→daemon communication** beyond `server.json`. The
new CLI is a *client* of the daemon's API:

```
falconfox daemon                              # run the daemon (today's launcher)
falconfox spawn --path ~ --name root          # → session id
falconfox spawn --ephemeral …                 # never persisted, hidden from `list`
falconfox list                                # id / name / path / backend / activity
falconfox send <id> "…"                       # prompt an unattached session
falconfox read <id>                           # its transcript (the pair to `send`)
falconfox resume|stop|delete|rename <id>
```

**Ephemeral sessions** short-circuit `_should_persist()` to always-false and are
filtered from default listings. `engine/oneshot.py` cannot serve this role: it
initializes with `_NO_FILES` (no fs, no terminal) and auto-denies permissions, which
suits naming queries but not a focus agent that must run `falconfox list` and write a
pointer file.

No `pointer` command: focus/pointers are **client** state (the telegram bot owns a
file on disk). The daemon has no notion of focus.

This is the multiplier: it is what lets the **manager agent** manage sessions with
nothing but shell access. `send` must work on a session no client is attached to.

**Auth is simpler than feared:** the manager agent and the telegram bot both run
*on the VPS*, so they reach the daemon over loopback and need no credentials. Only
the **browser from a laptop/phone** crosses the network — so Tailscale is the
boundary and the bearer token is defense-in-depth, not a login system.

## UI: keep the pane, redo the navigation

- **Keep:** the session pane — conversation, streaming, tool calls, permission
  prompts, usage, the config-options (⚙) popover, the slash-command palette. This
  is the bulk of the UI's value and is session-scoped already.
- **Cut:** home-as-case-list, the case page, the case file browser, project routes.
- **Build:** a flat session list as home (name, path, backend/model, state,
  created/last-active), a spawn dialog (path + name + backend), and open sessions as
  panes. Grouping, if ever wanted, is a **client-side filter over metadata** (e.g.
  by path) — never a server concept.
- **Naming matters more now.** `rename_agent` / `name_agent` (LLM autonaming via
  `naming_backend`) already exist and become the primary way a flat list stays
  legible.

## Sequencing (ordered for the local PoC)

**PoC — daemon + telegram, local only:**

1. **Flatten the model** — session keyed by `session_id` + `path`; central store;
   de-case the coordinator; delete `cases.py`/`templates.py`/`projects.py`. The web UI
   breaks here; expected and accepted (leave its files unwired, do not delete).
2. **Flatten the API** — global routes + the metadata-snapshot/transcript-on-open
   protocol.
3. **Ephemeral sessions** — the `--ephemeral` flag.
4. **CLI control plane** — `spawn`/`list`/`send`/`read`/`resume`/`stop`/`delete`/
   `rename`, plus renaming the **CLI entry point** to `falconfox` (`[project.scripts]`,
   one line — the full package rename can wait, but agents invoke `falconfox` by name).
5. **Telegram bot** — separate package/process (keep the daemon's four-dependency list
   clean): focus channel + work channel, bot-owned pointer file, per-turn replies.
   Text first.
6. **Voice** — transcription step in front of the forward path.

**After the PoC:**

7. **Remote access** — bind off-loopback, bearer token, Tailscale; deploy to the VPS.
8. **Rebuild the UI navigation** — flat session list + spawn dialog, reusing the pane.
9. **Full package rename** — modules, config paths, docs, `.casebook/` remnants.

Steps 1–2 are one connected refactor and should land together on a branch.
