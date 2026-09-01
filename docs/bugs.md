# Known bugs

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## The manager and private-chat sessions get topics nobody owns

*Reported from use, 2026-09-01. Confirmed by reading the code; not reproduced
under test.*

The bot creates a forum topic titled "telegram manager", and any message sent
in it answers "No FalconFox session owns this topic." The manager keeps talking
in General, so the topic is inert. The private-chat session ("telegram private
chat") gets the same dead topic, for the same reason.

Two halves disagree, but not the way it looks from the chat. Topic creation is
driven by two different sources:

* `_reconcile_topics` (`bot.py`), which lists sessions over
  `GET /api/sessions`. That call omits hidden sessions, and both infrastructure
  sessions are spawned `hidden=True`, so reconcile never sees them.
* the `session_added` websocket event (`bot.py:1316`), which the coordinator
  emits for *every* session including hidden ones (`coordinator.py:333`).

So the event path creates a topic the list path does not know about. The
private chat has no exclusion at all in `_ensure_topic`. The manager has one,
`session_id == self.manager_session_id`, but it loses a race it almost always
loses: `add_session` emits `session_added` before it awaits `session.start()`,
while the bot only assigns `self.manager_session_id` after the spawn POST
returns. The event arrives first, the guard compares against a stale id, and
the topic is made.

The binding is then destroyed by the next `_reconcile_topics`, which unbinds
every session in `topics.json` that is missing from the live list. Hidden
sessions are always missing from it, so the binding is dropped on the first
connect after creation while the Telegram topic stays. That is the state the
report describes: a real topic, no owner.

Same root cause, second symptom: because the binding is already gone, deleting
either session leaves its topic behind, since `session_removed` deletes only a
topic that is still bound.

The fix is to make one rule about hidden sessions and apply it in both places.
The narrow version is to skip hidden sessions in `_ensure_topic` (the flag is
on the `session_added` event already, so no id comparison and no race), which
also stops reconcile from ever having a hidden binding to unbind. The topics
already created have to be deleted by hand, or by deleting the sessions after
the binding is restored.

Workaround until then: ignore the two topics and talk to the manager in
General, and to the private chat in the private chat.

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
