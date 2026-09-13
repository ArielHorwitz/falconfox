# Package: the wire contract, the deploy scripts, and the copy

You are implementing, in a git worktree at
`/home/ariel/projects/falconfox/.worktrees/hardening-edges` (branch
`hardening-edges`). Work only there. Commit as you go, one commit per step,
with messages in the repo's existing style (`fix:`/`feat:`/`refactor:` prefix,
a short imperative subject, a body that says why). Do not push. Do not touch
`src/falconfox/web/static/` or `.worktrees/`. Do not restart any daemon,
bot, or systemd unit, and do not run `setup.sh`, `update.sh` or
`provision.sh` against the host: test them only in a temporary directory
with `HOME` pointed there.

## Read first

1. `docs/casebook/2026-09-12__07b4eb32/overview.md`, including its Progress
   section: two packages have already landed on this branch's parent, so
   the coordinator now has a `SessionRecord` and locks, and the bot has a
   `Turn` record. Read the code as it is now, not as the survey describes.
2. `docs/casebook/2026-09-12__07b4eb32/survey-edges-contract-tests-deploy.md`
   for your area in detail. Line numbers there predate the other packages.
3. `README.md` and `docs/buglist.md`.

The project's own conventions: `.claude/CLAUDE.md` in the repo root. The
user's preferences that matter here: no em-dashes in prose or docs,
descriptive names (never single-letter variables outside loop indices),
`Optional[T]` not `T | None` in new code, `pathlib` for paths.

## Decisions already made

- `revert`, `fork` and `set_config_option` **stay**. No live client can
  reach them and that is accepted; they leave together with the dead web UI
  in a later case. They must be in the registry and keep working.
- The two `send` semantics **stay**: HTTP `send` awaits the whole turn (the
  CLI relies on it), websocket `send` returns at once and the turn streams
  as events (the bot relies on it). Define this once and say it in one
  place.
- No shell test framework for `deploy/`. A smoke run in a temp `HOME` is
  enough.

## Steps, in order

Each step: a test first where one is possible, then the change, then a
commit. Run the whole suite (`uv run pytest`) before every commit.

1. **One action registry.** The HTTP action handler and the websocket
   `_dispatch` in `web/server.py` become two thin adapters over a single
   table of actions: name, the coordinator coroutine it calls, how its
   arguments are pulled from the request, and whether the HTTP transport
   awaits it or detaches it. An unknown action is an error on both
   transports: HTTP 404 with the existing `{"error": ...}` shape, and over
   the websocket an event the client can log. Check how
   `src/falconfox_telegram/bot.py` treats an event type it does not know
   before you choose the shape; it must not break the bot. `cli.py` and
   the bot's `api.py` are consumers: they may stay as they are, but if the
   registry can also produce the list the CLI validates against, do that.
   Test: a table test over the registry that every registered action
   dispatches over both transports, and that an unknown one errors on
   both.

2. **Wire tests.** One Starlette `TestClient` test per HTTP route for the
   happy path and the error shape, and one websocket test that connects,
   receives the snapshot, sends an action, and receives the resulting
   event. Use the `echo` backend or the coordinator fakes the existing
   tests use. This is what makes `server.py` safe to change.

3. **A protocol version.** The websocket snapshot carries a protocol
   version integer, defined next to the registry. The bot logs at WARNING
   on connect if it differs from the one it was built against, and carries
   on. The CLI does the same on its first request per invocation if cheap;
   otherwise skip the CLI. Test both sides.

4. **Deploy paths are facts about the host, not the caller.** In
   `deploy/setup.sh` and `deploy/update.sh`, the systemd user unit
   directory and the deployment config directory are `$HOME`-relative and
   ignore `XDG_CONFIG_HOME`. The health check in `update.sh` additionally
   verifies that the unit text systemd has loaded matches what was just
   rendered (compare `systemctl --user cat` output, or a rendered checksum
   stamped into the unit as a comment), so a deploy cannot report healthy
   on stale units. Smoke-test in a temp `HOME` with `systemctl` stubbed by
   a shell function or a fake on `PATH`; do not touch the real user
   manager. Delete the buglist entry "`setup.sh install-units` writes units
   where systemd will not look" in the same commit.

5. **The bot's restart warning.** Buglist: "The bot warns about
   orientation it goes on to register". On startup before the daemon has
   published its client directory, the bot says at WARNING that sessions
   will spawn without orientation, then registers a few seconds later.
   Make it say it is waiting for the daemon at INFO, and warn only once a
   retry has actually failed. Delete the buglist entry in the same commit.
   Test: the first miss logs at INFO, a miss after a successful connection
   logs at WARNING.

6. **Copy that describes the product as it is.** `README.md` says a turn
   "suppresses tool calls and sends the final reply as one message"; the
   progress message that carries narration and tool markers is not
   mentioned. Rewrite that paragraph to describe the two-message turn as it
   is today (read `bot.py`'s progress-message code and the turn-feedback
   case in `docs/casebook/2026-08-24__165f0606/overview.md` and
   `docs/casebook/2026-09-12__c716ca46/overview.md`). Also check the
   README's "Daemon and CLI" section against `cli.py`'s actual commands and
   fix any drift. Prose rules: no em-dashes, short sentences.

## Do not

- Do not change the coordinator's locking or records, or the bot's turn
  machinery, beyond what step 5 needs. Note anything you see instead.
- Do not add dependencies.
- Do not add or edit files under `docs/casebook/`.

## Report

Your final message: the commits with one line each, what each test proves,
any decision the lead should know about, anything found outside your scope,
anything unfinished and why, and the suite summary line.
