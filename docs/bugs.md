# Known bugs

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## A resumed session's topic stays closed forever

*Found in use, 2026-08-30.*

Eviction closes the session's topic and records the id in `_closed_topics`;
resuming reopens it and discards the id. Only transitions act, so the set is
the sole record that a topic is owed a reopen — and it lives **in memory
only**, unlike `topics.json`, `forum.json` and `infra.json` beside it.

So a bot restart between the close and the resume loses it. The session comes
back, `_mirror_session` computes `stopped=False` against a `was_stopped` that
is now also false, sees no transition, and never reopens. The topic stays
closed permanently, while the session behind it is running.

Two candidate fixes, and the second may delete the bug rather than repair it:
persist the set beside the other maps, or **stop closing topics on eviction
at all** — the premise that closing prevents prompting a stopped session is
false, since `send` auto-resumes. See
[the topics case](casebook/2026-08-28__cf552067/what-happens-when-the-user-edits-a-topic-by-hand.md).

## The test suite can reach a live daemon

*Found by finding six of its sessions in production, 2026-08-30.*

Tests construct a `FalconFoxTelegramBot` to exercise `_ensure_manager` and
`_ensure_concierge`. Where `bot.daemon` is not stubbed, it falls through to a
real `DaemonApi`, whose default `FALCONFOX_URL` is `http://127.0.0.1:9721` —
the production daemon.

Every affected run therefore **spawned a real session in production**, with a
`tempfile` directory as its working path. Six accumulated over one day of
development, each holding an ACP subprocess, on a host with 951 MB where
memory exhaustion was under active investigation. Some of that day's measured
pressure was self-inflicted.

The defect is not what the tests did on arrival; it is that a unit test can
reach a live service at all. Fix by pointing `FALCONFOX_URL` at an unroutable
address in test setup, so the fall-through fails fast instead of succeeding
quietly.

Worth pairing with a habit: `falconfox list` makes this obvious in one look —
six sessions named `telegram manager` under `/tmp` are unmistakable — and it
went unnoticed for hours because nobody listed production's sessions.

## A rebuilt VPS loses the Python 3.12 shim

*From the first phone session, 2026-08-24.*

On Ubuntu 22.04 the bare word `python3` is **3.10**, which cannot import
`tomllib`. Any tool documented as "run it with python3" therefore fails on a
stock host — the casebook skill's CLI did exactly that.

The fix in place is a host-local symlink, `~/.local/bin/python3 ->
/usr/bin/python3.12`, made by hand. It carries **no commit**, so it is not part
of any deployment: a rebuilt or second host silently reverts to 3.10 and the
same failure returns. `setup.sh` does not create it, deliberately — but the
bootstrap checklist has to, or this recurs.

falconfox itself is immune, since uv provisions its own interpreter and the
daemon, bot and deploy scripts all use absolute venv paths. The blast radius is
tools invoked as plain `python3` from inside a session.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident — it is tracked as the desktop-client effort
in [wishlist.md](wishlist.md), and is listed here only so that finding a broken
UI does not read as an undiscovered bug.
