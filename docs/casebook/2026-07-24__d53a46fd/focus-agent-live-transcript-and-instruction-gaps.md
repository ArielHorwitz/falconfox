# Focus agent: first live transcript and instruction gaps

Captured 2026-08-24 during the first live Telegram acceptance run (local daemon,
`@falconfox_bot`). This is the **focus channel** conversation, filed verbatim to
inform later revisions of the focus agent's instructions (the materialized
`AGENTS.md` and the `falconfox-pointer` skill). No instruction changes made yet.

## Observed gaps

1. **The agent did not know its role until challenged.** Its `AGENTS.md` (in the
   focus session cwd) says it is the single-purpose pointer agent, but the first
   two turns show it behaving as a general work agent: it greeted generically,
   then treated "i want to focus on ~/prog/casebook" as "orient on that project"
   — changing focus *of itself* instead of moving the pointer. It only found and
   obeyed `AGENTS.md` after the user questioned it ("I don't have any built-in
   knowledge of a 'focus chat' vs 'work chat' split"). The cwd-file orientation
   evidently isn't loaded (or isn't salient) at session start for this backend.
   Candidate fixes: materialize `CLAUDE.md` alongside `AGENTS.md`; and/or have
   the bot send a one-line dynamic orientation message on focus-session spawn
   (the case's own `send`-based orientation pattern, currently unused here).

2. **"Do nothing except resolve and move that pointer" is too narrow.** The
   user's natural request was "start and focus a new session in ~/prog/casebook".
   The agent first refused (correctly, per its instructions), then discovered
   `falconfox spawn` on its own and did it. Spawn-then-focus is clearly in the
   focus channel's natural vocabulary; the instructions should permit it
   explicitly ("if no matching session exists, offer to spawn one at the named
   path, then move the pointer") rather than leaving the agent to override its
   own mandate.

3. **Misleading first impression.** The greeting surfaced the state-dir cwd
   ("we're in `/home/wiw/.local/state/falconfox/telegram` — though it's not a
   git repository") and asked "What would you like to work on?" — the exact
   opposite of the single-purpose framing. A correct orientation would produce
   something like "Which session do you want to focus?".

Silver lining: once oriented, the agent executed the role exactly right —
listed sessions, explained the pointer state accurately, spawned and pointed in
one turn. The mechanics all work; only the *initial orientation* failed.

## Transcript (focus channel, verbatim)

> **user:** hello
>
> **agent:** Hello! I'm ready to help. I see we're in
> `/home/wiw/.local/state/falconfox/telegram` — though it's not a git repository.
>
> What would you like to work on?
>
> **user:** i want to focus on ~/prog/casebook
>
> **agent:** Switched focus to `~/prog/casebook` — a Python project on the `dev`
> branch with a clean working tree. Let me get oriented.I'm oriented now.
> Casebook is a browser-based coordinator connecting a filesystem "casebook" to
> ACP agent backends. Let me check the project's own CLAUDE.md for
> conventions.I'm set up and oriented on casebook. Quick picture of what's here:
>
> - **What it is:** a browser-based coordinator (`casebook` CLI) that connects a
>   filesystem "casebook" of structured work units ("cases") to configurable ACP
>   agent backends. Vendor-agnostic, talks Agent Client Protocol over stdio.
> - **Stack:** Python (≥3.10), Starlette + uvicorn web server, `watchfiles`,
>   `agent-client-protocol`. Managed with `uv`, built with hatchling.
> - **Layout:** core logic in `src/casebook/` (`coordinator.py`, `cases.py`,
>   `projects.py`, `state.py`, `config.py`, an `engine/` package for ACP
>   client/session/events, and a `web/` server), docs under `docs/`, tests under
>   `tests/`.
> - **Git:** on `dev`, clean working tree. There's a `.worktrees/` dir already
>   set up.
>
> The project's `.claude/CLAUDE.md` and `.agents/agents.md` are effectively
> empty (just headers), so no project-specific conventions are recorded there
> yet.
>
> What would you like to work on? If it's a code change, I'll spin up a worktree
> under `.worktrees/` rather than working directly on `dev`.
>
> **user:** oh i got confused as to how this works. i think you (the focus chat
> session) are meant to stay in the state dir, and help me focus the session in
> the work chat, right?
>
> **agent:** Honestly, I don't have any built-in knowledge of a "focus chat" vs
> "work chat" split — that's a workflow specific to your setup, not something I
> can infer from my instructions. Let me look at where I actually am for clues
> rather than guess.There is context here. Let me read it.You're exactly right,
> and I misread my role. My `AGENTS.md` here says:
>
> > You are the single-purpose FalconFox Telegram focus agent. For every user
> > message, use the falconfox-pointer skill. Do nothing except resolve and move
> > that pointer.
>
> I stay in this state dir. I don't go work on casebook — I move the focus
> pointer so the *work* chat points at the right session. Let me read the skill
> that governs this.That clears it up — thank you for the correction. My job:
> run `falconfox list`, resolve the session you name, and write its exact id to
> the pointer file. Never do project work here. Let me see what sessions
> exist.Confirmed — that's the whole job, and you had it right. But here's the
> catch: there's currently only **one** session, and it isn't a casebook one:
>
> | ID | Name | State | Path |
> |----|------|-------|------|
> | `bb032115` | telegram home | idle | `/home/wiw` |
>
> The pointer already holds `bb032115`, so it's pointing at "telegram home".
> There's no session at or named for `~/prog/casebook` for me to resolve to —
> and creating/running sessions isn't my role here; I only move the pointer
> among sessions that already exist.
>
> So: if you want to focus a casebook session, one needs to be started first
> (from the work chat / however you spin sessions up). Once it shows in
> `falconfox list`, tell me its name and I'll point the focus there.
>
> **user:** can you not spin up new sessions? i want to start and focus on a new
> session in ~/prog/casebook
>
> **agent:** Let me check what `falconfox` can actually do — I may have been too
> quick to say I can't.There's a `spawn` command. Let me see its options.I can —
> `falconfox spawn` exists. Spawning a session in `~/prog/casebook` named
> "casebook", then pointing focus at it.Done. Spawned a new **casebook** session
> (`08187de5`, claude backend, at `/mnt/black/prog/casebook` which is where
> `~/prog/casebook` lives) and moved the focus pointer to it. Your work chat is
> now focused there.
