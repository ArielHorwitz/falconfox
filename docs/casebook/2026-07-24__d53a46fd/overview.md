# Overview

This case designs **falconfox**: a small, vendor-agnostic ACP **session-manager
daemon** built for remote control, on top of which a telegram/voice client and the
casebook workflow are *layers*, not native features. It began as a question about
working with agents by voice on long commutes and converged on a deliberately
minimal daemon whose only job is remote access + session management, with agents (a
built-in "manager" role) as the intelligence that drives it.

**Decision: pivot this repo into falconfox** (rename + generalize in place), rather
than starting a new one — see [Repo strategy](#repo-strategy--pivot-in-place).

**Status: open — deployed and live on the VPS (`lemcel`) as of 2026-08-24; the
remaining step is the user's first real phone-driven session, then closing.**

**Handoff (2026-08-24):** the laptop session that built and deployed the PoC is
closed; from here the case advances **from VPS sessions driven via Telegram**.
The single source of truth is the **`falconfox` branch on origin** — the VPS
deploy checkout (`~/falconfox` on `lemcel`) follows it via `deploy/update.sh`,
and the laptop's `.worktrees/falconfox` is a mirror, not an active workplace.
Keep active work on one side at a time (VPS now); `dev` was pushed at the
handoff so nothing lives only on the laptop. Eventual closing includes merging
`falconfox` back to the trunk. The live run
surfaced two findings, **both fixed the same day**: focus-agent instruction gaps
(transcript and analysis:
[focus-agent-live-transcript-and-instruction-gaps.md](focus-agent-live-transcript-and-instruction-gaps.md))
and unrendered markdown replies
([telegram-markdown-parse-mode-finding.md](telegram-markdown-parse-mode-finding.md)).
The fixes, the bot's new reconnect loop, and the decisions taken during the
autonomous run — plus the exact VPS bootstrap checklist — are in
[afk-run-decisions-and-vps-bootstrap-checklist.md](afk-run-decisions-and-vps-bootstrap-checklist.md).

Implementation and verification details:
[poc-implementation-and-verification.md](poc-implementation-and-verification.md).

**VPS deployment tooling landed 2026-08-24** (`deploy/` — see its README): systemd
user units for daemon + bot, one-command bootstrap (`setup.sh`), and a self-update
script (`update.sh`) with health check and automatic rollback, designed for the
dogfooding goal that after one manual clone the *only* interface is Telegram —
including updating falconfox itself from inside a falconfox session
(`--detach-restart` schedules the restart after the agent's turn ends). Finding
folded into the design rather than fixed in code: the bot has **no websocket
reconnect loop**, so every daemon restart kills it — `Restart=always` in the unit
compensates; a reconnect loop remains a worthwhile future hardening.

## Current goal: a local PoC — daemon + telegram only

The case's first concrete milestone. Get the whole thesis working **locally**, before
deploying to the VPS. Telegram is the hard and novel part; once it works, the desktop
client is comparatively trivial.

**Implemented 2026-07-31.** The flattened daemon, central store, global API,
ephemeral sessions, `falconfox` CLI, permission fix, and separate
`falconfox-telegram` process are built and covered by local tests. The daemon was
also exercised end to end with the echo ACP backend, including stop/automatic
resume and a real daemon restart with transcript recovery. A live Telegram Bot API
run still needs the user's bot token and chat ids, so the case remains open until
that final credential-dependent acceptance pass.

**In scope**

1. **Flatten** the session model and API (session keyed by `session_id`, carrying
   `path`; central store; de-cased coordinator).
2. **Ephemeral sessions** — `spawn --ephemeral`: never persisted, hidden from default
   listings. Needed for rotating focus-channel sessions (see the UX doc's clutter
   feedback loop), but generic: any scripted/one-shot use wants it. Note
   `engine/oneshot.py` is **not** reusable for this — it hard-denies filesystem and
   terminal, while a focus agent must run `falconfox list` and write the pointer file.
   Implementation is small: short-circuit `_should_persist()` and filter from `list`.
3. **CLI**: `spawn` (with `--path`, `--name`, `--backend`, `--ephemeral`), `list`,
   `send`, `read`, `resume`, `stop`, `delete`, `rename`.
4. **Telegram bot** (its own package/process): focus channel + work channel, bot-owned
   pointer file, `sendChatAction("typing")` for the duration of a turn, one message per
   turn with tool calls suppressed. Build spec:
   [telegram-bot-poc-spec.md](telegram-bot-poc-spec.md).
5. **Skills**: the pointer skill (shipped with the bot) plus the user's `administrator`.
6. **Always-allow as the only permission posture** — no interactive approval in the
   PoC. Includes fixing the hang where `_request_permission()` falls through to a
   pending future when `_auto_allow_option()` returns `None` (empty options list); it
   must resolve as denied instead.

**Deliberately out of scope**

- **Remote access + auth** — the PoC is local/loopback; this is what gets added when it
  moves to the VPS.
- **The web UI** — flattening breaks the case-centric frontend. Accepted: during the
  PoC the only views into a session are `falconfox read` and telegram. Leave the UI
  files in place but unwired rather than deleting them.
- **Voice** — orthogonal to the architecture (a transcribe step in front of the forward
  path). Text first; add voice as the final PoC step so the novel parts are front-loaded.
- **Full package rename** — but rename the **CLI entry point early**
  (`[project.scripts]`, one line), since the skill and every agent invocation reference
  `falconfox` by name.

**Success criterion:** from telegram, spawn a session in a project, have it do real
work, switch sessions via the focus channel, and resume a session after restarting the
daemon. Plus: rotated focus sessions leave no trace in `falconfox list`, and a long
turn shows "typing…" rather than appearing to hang.

### Where an implementing session should start

1. This file — the goal above, then [Architecture](#architecture-base-daemon--layers)
   and [Layers](#layers) for the boundary that must not be violated (the daemon stays
   opinion-light; workflow, focus, and orientation live in layers).
2. [falconfox-pivot-plan-per-module.md](falconfox-pivot-plan-per-module.md) — what
   changes in the existing code, module by module, plus the ordered sequencing. Start
   at its step 1.
3. [telegram-bot-poc-spec.md](telegram-bot-poc-spec.md) — the new bot package.
4. [remote-control-ux-desktop-and-telegram.md](remote-control-ux-desktop-and-telegram.md)
   — the rationale behind the UX decisions, worth reading before changing any of them
   (it records two designs that were tried and rejected, with reasons).

**Design is settled — the implementing session should build, not re-litigate.** Where
something genuinely does not work, record the finding here rather than quietly
diverging.

## Motivation

The user has long commutes and wants to work with agents hands-free (voice), from a
phone or laptop, with no vendor lock-in and no dependence on local laptop state. The
target setup: agents run on a **VPS** (machine-agnostic, resumable across devices),
driven by voice/text. ACP already brokers both vendors (`claude-agent-acp`,
`codex-acp`), so the daemon is vendor-agnostic for free.

Guiding principle: **rely on agents as much as possible.** The daemon does not need
to be smart — it needs to (a) be reachable remotely and (b) let agents manage
sessions. "Agents managing sessions" is the multiplier. Everything opinionated (how
work is organized, how a phone chat behaves, how a session is oriented) lives in a
layer or in the agent runtime — not in the daemon.

## The key reframes that got us here

- **Harness, not runtime.** falconfox replaces the *client/harness* you talk to
  agents through, never the agent's execution loop. The ACP backends remain the
  interchangeable runtimes. This is what preserves vendor-agnosticism.
- **A session is just `(scope/path) + (metadata)`.** "Manager," "project," and
  "case" are conventions, not native types. The manager is a session at the machine
  root/home; a case session is a project session that a skill + one orientation
  message make case-aware.
- **Orientation is not the daemon's job** (this replaced the earlier "instruction
  presets" idea — dropped for simplicity):
  - *Static orientation* (how casebook works, the manager's root-console role) comes
    from **cwd files the agent runtime already loads** — skills in `.agents/skills`
    (the cross-vendor standard; Claude reads `.claude/skills`, resolved by symlink)
    and `AGENTS.md`/`CLAUDE.md`. The daemon does nothing.
  - *Dynamic orientation* (the one per-session variable — "you're on case X") is one
    line, delivered as a message via `falconfox send` by whoever spawned the session.
  - Net bonus: the transcript is *cleaner* than casebook today — the bulky how-to
    lives in a skill (never in the transcript), only the short "which case" line
    appears. No preset registry, no instruction templates, no variable interpolation.
- **Casebook is a layer**, not native — and as of 2026-07-27 this is **proven, not
  theoretical**: the casebook workflow now exists as a standalone **skill**
  (<https://github.com/ArielHorwitz/agent-skills>, installed to `~/.agents/skills/`;
  Claude bridged by symlink) and is in successful daily use. It turned out to need
  *no CLI and no binary* — pure filesystem operations — and therefore **requires zero
  features from the daemon**. The workflow never needed the app; it needed
  instructions. This is the strongest possible validation of the layer boundary:
  the layer works with no daemon at all.

## Architecture: base daemon + layers

```
┌─────────────────────────────────────────────────────────────┐
│ falconfox daemon (the base — the pivot target for this repo)  │
│  • session primitive: (path, name) → id                      │
│  • lifecycle: spawn / send / list / resume / stop / delete    │
│  • persistence: throwaway + resumable sessions               │
│  • remote access: bind off-loopback + bearer token (Tailscale)│
│  • dual control plane over the SAME ops:                     │
│      – CLI  (falconfox spawn/send/list/…) → agents manage them│
│      – client API (WS/REST, already present) → bots / UIs     │
│  • converse + stream (messages, tool_calls, permissions, …)  │
│  (NO instruction injection, NO presets — orientation is       │
│   handled by cwd files + `send`, above the daemon)           │
└─────────────────────────────────────────────────────────────┘
        ▲                    ▲                      ▲
   ┌────┴─────┐      ┌───────┴────────┐     ┌───────┴─────────┐
   │ telegram │      │  casebook      │     │  web UI         │
   │  bot     │      │  (CLI + skill, │     │  (this repo's,  │
   │ (client) │      │   installable) │     │   as falconfox's│
   │          │      │                │     │   desktop UI)   │
   └──────────┘      └────────────────┘     └─────────────────┘
```

## The daemon's feature set (the whole base)

1. **Session lifecycle** — `spawn / send / list / resume / stop-close / delete`,
   persisted; throwaway-and-resumable. Most of this exists in the current `engine/`.
   `send` must work on a session no client is attached to (so a spawner can orient a
   session it isn't connected to).
2. **Remote access** — bind beyond loopback + a bearer token, Tailscale-friendly.
   The genuinely new capability vs. the current code.
3. **Dual control plane** over the same operations — a **CLI** (so a shell-capable
   agent is the control plane; this is the multiplier) and a **client API** (WS/REST,
   already present).
4. **Converse + stream** — send prompts; stream messages, tool calls, permission
   requests, usage. Already present.

Explicitly **out of the daemon**: orientation/instructions (cwd files + `send`),
cases (casebook layer), focus/cursor and navigation classification (telegram bot),
any workflow opinion.

## Layers

How these are actually used — entry-point sessions, the desktop UI, the telegram bot,
and the division of labour between them:
[remote-control-ux-desktop-and-telegram.md](remote-control-ux-desktop-and-telegram.md).

- **~~Manager role~~ — dropped; there is no manager agent.** It decomposed into (a) a
  **default scope** (new sessions default to home/root — a one-line daemon default,
  not a type) and (b) **two user-installed skills**: `administrator` ("you are a
  natural-language console to this machine") and `falconfox` (how to drive the daemon
  CLI). Skills in `~/.agents/skills/` are ambient, so *any* session can drive
  falconfox if asked — session management is tied to invoking a skill, not to scope.
  Entry-point sessions are **ephemeral, not long-lived**: reconnecting gives a fresh
  home session, because the *session list* is the memory. Existing trivial-session
  cleanup (`_should_persist` / `close_agent`) means unused ones evaporate, so this
  needs no new feature.
- **Telegram bot (a client).** Maps one chat ↔ one session via a **pointer that the
  bot owns on disk** — focus is a client concept and the daemon has no notion of it.
  Voice messages are transcribed and sent as prompts. Two chats: a **focus channel**
  (a single-purpose, stateless, rotating session that does nothing but move the
  pointer, in natural language) and a **work channel** (forwarded to whatever the
  pointer resolves to). The focus channel must stay single-purpose — that is what
  makes "switch to the admin session" unambiguous. The focus agent writes the bot's
  pointer file over ordinary shell access; the bot watches it (`watchfiles`) and
  announces changes.
- **Casebook (external skill — DONE).** A pure **skill** in
  [agent-skills](https://github.com/ArielHorwitz/agent-skills), no CLI and no binary:
  the agent creates the case dir, `case.toml`, and `overview.md` with ordinary
  filesystem operations. Orientation needs no `falconfox send` either — with the
  skill installed, the user's own first message ("work on case X") is enough for the
  agent to self-orient. The daemon persists *sessions*; casebook persists *the
  effort* in the case directory on the filesystem — two orthogonal persistences,
  which is why throwaway sessions keep working as a layer. (The same repo also ships
  an `iac` skill for inter-agent coordination over a filesystem channel — another
  signal that coordination belongs above the daemon, not inside it.)

## CLI sketch (falconfox)

```
falconfox daemon                                  # run the daemon
falconfox spawn --path ~ --name root              # manager (oriented by ~ cwd files)
falconfox spawn --path ~/projects/b2 --name foo   # a bare project session
falconfox send <id> "…"                           # prompt / orient a session
falconfox list                                    # id / name / path / created
```

Casebook needs no composition on top of this — a session spawned at a project path
with the skill installed is already case-capable; the user (or the manager agent)
just says which case to work on.

## Telegram flow (illustrative — single channel + focus cursor)

```
> /new manager
< New manager session started (id: 1a2b3c).
> Rename this session as b2 init
< Session renamed to `b2-init`
> Start a new project b2: create the dir, git init, cargo init.
< [manager runs the commands via shell]
> Open a casebook case "brainstorm" and spawn a case session "b2 brainstorm".
< [manager runs `casebook new …`, then the casebook spawn wrapper]
> Switch to this session
< This chat is now focused on session id 3c2b1a.
> …brainstorm…
```

## Build order

1. **Pivot this repo into falconfox** (first deliverable). The structural change
   everything follows from: a session is keyed by **`session_id` alone** and carries
   its **`path` (cwd) as metadata** — which deletes *both* cases and projects as
   server-side concepts. Keep `engine/` (~untouched), the de-cased coordinator, and
   the session pane; delete `cases.py`/`templates.py`/`projects.py`; move session
   storage out of project checkouts into a central store; flatten the API; rebuild the
   UI navigation as a flat session list; then rename, add the `spawn`/`send`/`list`
   CLI, and add remote access + auth. Per-module plan and sequencing:
   [falconfox-pivot-plan-per-module.md](falconfox-pivot-plan-per-module.md).
2. **Telegram bot** — a client: chat↔session focus cursor, voice→text, navigation
   intercept + content forwarding, render replies/permissions.
3. ~~**Casebook, re-expressed**~~ — **DONE (2026-07-27), ahead of the daemon.**
   Shipped as a pure skill in
   [agent-skills](https://github.com/ArielHorwitz/agent-skills) and in daily use. No
   daemon work is required to support it. This project can now focus entirely on the
   daemon and remote control, and **drop any ambition of supporting a casebook-style
   workflow natively**.

## Repo strategy — pivot in place

Chosen over a new repo. Once presets are gone, the casebook-specific residue is
*small* (`cases.py`, `templates.py`, and the case corners of the UI); the bulk —
engine, server, config/storage/projects/state, and especially the web UI's
conversation/session/project views — is generic falconfox substrate. Greenfielding
would throw away a working daemon *and* a working web UI to re-earn them. "Clean" is
achieved by **extracting** the case layer out, not by fresh git history. So: rename
this repo to falconfox, extract casebook into its own CLI+skill package.

## Open questions / seams to resolve

- ~~Pivot mechanics~~ — **RESOLVED: sessions go flat.** "Case" was doing double duty
  as both the workflow *and* the grouping structure for sessions; with the workflow
  externalized, the grouping goes too. Sessions become a **flat list**, each carrying
  metadata (name, path, backend/model, created/last-active, state). This is the point
  of the skill: a session is demoted to *one conversation about a case*, which records
  anything of value into the case directory anyway — so sessions are disposable and
  need no hierarchy. If a flat list ever feels thin, the answer is **richer metadata
  and better naming, not structure**; any grouping is a client-side filter, never a
  server concept. The UI is to be redone around the flat list (the session pane itself
  survives nearly intact). Full breakdown:
  [falconfox-pivot-plan-per-module.md](falconfox-pivot-plan-per-module.md).
- **Telegram navigation classification (client-only, not a daemon concern):** in a
  single channel, something must classify each message as *navigation* ("switch",
  "new", "list") vs *content* (forwarded to the focused session). Recommended: the
  **bot owns focus**, intercepts a small explicit navigation vocabulary, forwards
  everything else, and **confirms every focus change** (a misclassification is caught
  by the wrong confirmation). "this/the new session" resolves via the daemon's
  `list` (most-recently-created), not by scraping agent output. Defer agent-driven
  focus directives until the hands-free flow demands them.
- **Destructive manager ops + lossy voice:** even under always-allow, keep a
  readback-confirm on *destructive, global* manager operations — not distrust of the
  agent, but because voice transcription is lossy and the blast radius (a whole
  project/case) has no diff/undo.
- **Deterministic orientation fallback (optional):** orientation-by-message is a
  notch less bulletproof than an injected system prompt. If it ever matters, a single
  generic `--context` flag on `spawn` is a trivial, vendor-neutral fallback — but not
  planned for the base.

## Related casebook work

- ACP surface roadmap — case `2026-07-16__04c9e036`: config options (shipped), slash
  commands (shipped), remaining threads (prompt media, terminal/execution =
  *observe/police, don't execute*, per-session config persistence). These become
  falconfox concerns once the daemon is the base.
- Auto-approve classifier — case `2026-06-22__69cec96c`: per-session permission
  posture, though the user currently runs always-allow.
- Multi-project support — case `2026-06-22__f5f7bdb5`: the project/coordinator model
  falconfox generalizes.
