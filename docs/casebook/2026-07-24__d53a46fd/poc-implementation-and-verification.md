# FalconFox local PoC: implementation and verification

Implemented 2026-07-31.

## What landed

- The server now has one global `SessionCoordinator`. Sessions are keyed by
  `session_id` and carry `path`, `name`, backend, state, creation/activity times,
  and the ephemeral marker as metadata.
- Persistence is flat and central at
  `$XDG_STATE_HOME/falconfox/sessions/<session_id>/`; FalconFox no longer creates
  `.casebook` state inside working repositories.
- The API is global: `/api/sessions`, `/api/sessions/{id}`, action endpoints, and
  one `/ws`. The initial socket snapshot contains metadata only; a transcript is
  loaded on explicit open/read.
- Ephemeral sessions never persist and stay out of default lists. Stopping one
  deletes its live process/state.
- The `falconfox` control plane implements `daemon`, `spawn`, `list`, `send`,
  `read`, `resume`, `stop`, `delete`, and `rename`. `send` automatically resumes
  a stored session.
- ACP subprocesses receive `FALCONFOX_SESSION_ID`. The CLI rejects self-stop,
  self-delete, and daemon stop/restart from inside a managed session.
- The PoC uses always-allow only. Permission requests with no choices now resolve
  as denied immediately instead of waiting forever on an unattached UI future.
- `falconfox-telegram` is a separate package/process with no Telegram framework
  dependency. It implements Telegram long polling, the focus/work channel split,
  a bot-owned watched pointer file, rotating ephemeral focus sessions, explicit
  `/new`, `/list`, `/switch`, `/name`, `/home` fast paths, typing refresh every
  four seconds, tool-call suppression, and one final reply per turn.
- The bot ships the `falconfox-pointer` skill and materializes it with an
  `AGENTS.md` containing the configured pointer path in the focus session cwd.
- Voice, remote auth/deployment, and rebuilt web navigation remain deferred as
  designed. The legacy web assets remain present but unwired.

## Verification performed

Automated tests (`python -m unittest discover -s tests -v`) cover:

1. flat metadata/transcript storage round-trip;
2. immediate denial for empty permission options;
3. ephemeral persistence/list filtering;
4. metadata-only WebSocket snapshot behavior;
5. session self-delete protection;
6. Telegram working → typing → idle behavior, single assembled reply, and
   tool-call suppression.

The wheel and source distribution build successfully, and the wheel contains
both Telegram modules and the pointer skill.

An isolated background-daemon check also verified `server.json` discovery:
`falconfox daemon` started the singleton, a separate `falconfox list` found it
without `FALCONFOX_URL`, and `falconfox daemon --stop` shut it down cleanly.

An end-to-end loopback smoke test with the built-in echo ACP backend verified:

1. spawn a named session at a real path;
2. send and receive a turn through the daemon;
3. read the saved transcript;
4. stop it and have a later `send` automatically resume it;
5. spawn an ephemeral focus session and confirm it is absent from `falconfox list`;
6. stop and restart the daemon against the same state directory;
7. see only the persistent session return as `stored`, with the complete
   transcript intact.

## Remaining acceptance step

Run `falconfox-telegram` with a real bot token plus focus/work chat ids, then
exercise the Telegram Bot API path end to end: create work, perform a real file
edit, switch naturally through the focus agent, and observe typing throughout a
long turn. This is blocked only on external Telegram credentials/chats; the code
path and local event behavior are implemented.
