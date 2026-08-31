# Known bugs

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## Deleting a topic by hand strands its session

*Found by reasoning through the forum rework, 2026-08-30. Not yet hit in use.*

There is no `forum_topic_deleted` service message — the Message fields are
created, edited, closed and reopened, plus General hidden/unhidden — so the
client never learns that a topic is gone. `topics.json` keeps a binding to a
dead thread, and the session goes on existing with nowhere to talk.

Sends to that thread then fail. Topic *creation* failures report to the
private chat; reply and progress failures only log, so from the chat the
session simply stops answering.

`_reconcile_topics` cannot repair it: it creates topics for sessions that lack
one and unbinds sessions that no longer exist, and a binding pointing at a
deleted topic looks perfectly valid. Nor can it be made proactive — the Bot
API has **no way to enumerate topics** (`getForumTopics` and `getForumTopic`
do not exist, measured 2026-08-30), so there is nothing to reconcile against.

The only available fix is reactive: on a send failure to a bound thread,
unbind and let `_ensure_topic` make a new one. Deliberately not done on the
eve of a stability soak, since it adds a code path to the hot send path.

Workaround until then: do not delete a session's topic by hand. Delete the
*session* (`falconfox delete`), which removes its topic as a consequence.

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
