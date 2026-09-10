# Buglist

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## `setup.sh install-units` writes units where systemd will not look

*Hit while renaming the stable checkout, 2026-09-10. Reproduced, not fixed.*

`UNIT_DIR` is `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`, read from the
environment of whoever runs the script. The systemd user manager was started
at login and looks in `~/.config/systemd/user` regardless, so any caller with
`XDG_CONFIG_HOME` set renders the units into a directory nothing reads. It
exits 0, and the units on disk keep their old contents.

Every dev session has `XDG_CONFIG_HOME=~/.config/falconfox-dev` set by its
unit, which makes an agent session the likely caller to hit it. That is also
the caller [deploy/README.md](../deploy/README.md) recommends, since
`update.sh --detach-restart` exists to be run from a chat.

It is worse inside `update.sh` than on its own: `restart_services` calls
`install-units` before every restart, on the rollback path as well as the
forward one. A deploy that changes a unit file would then restart the old
unit and report itself healthy, because the health check tests the daemon and
the bot, not which unit text they came from.

`CONFIG_DIR` in the same script has the same shape, so the bootstrap path
checks for `telegram.env` and `config.toml` in the wrong place too.

Fix is to stop honouring the variable for these two paths: the systemd user
directory and the deployment's config directory are `$HOME`-relative facts,
not per-caller ones.

## The bot warns about orientation it goes on to register

*Seen on every restart, 2026-09-10. Cosmetic.*

Restarting both units starts the bot before the daemon has published
`server.json`, so the bot logs `the daemon published no client directory;
sessions will spawn without Telegram orientation` at WARNING. It then
reconnects on its normal retry and logs `orientation registered at ...` a few
seconds later.

The race is expected and self-correcting. The warning is not: it states a
consequence that does not happen, at a level that says something needs
attention, on an ordinary restart. Either say it is waiting for the daemon,
or say nothing until a retry has actually failed.

## Chat actions lag the session's state

*Reported from use, 2026-09-08. Not investigated.*

The actions the bot uses to show what a session is doing (typing, sending a
file, recording voice) seem to be delayed somewhat behind the state they
report.

## An extra "Working..." message appears after the reply

*Reported from use, 2026-09-08. Not investigated.*

Every so often an *extra* "Working..." message appears after the final
response has already arrived, and then never resolves to anything.

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

## A lost topic icon cannot be repaired by setting the same tag again

`_apply_icon` skips the API call when the icon it remembers for a session
already matches the one the tags ask for. That is deliberate -- every edit
posts a service message into the topic, so acting on non-changes would be
chat noise -- but it means the bot's memory, not the topic, decides whether
the call happens.

So if an icon change is ever genuinely lost on the way to a client, re-setting
the same tag does nothing: the bot believes the topic already wears it. The
workaround is to tag through a different value and back, which forces two real
edits.

Seen once, 2026-09-09, as an icon that did not appear to change on setting a
tag. That instance turned out to be a client-side render lag rather than a
lost update -- the call went out and Telegram accepted it -- so this is the
fragility the incident exposed rather than the incident itself.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident. The standing decision in
[wishlist.md](wishlist.md) is to delete the assets soon, and possibly repair a
UI later; it is listed here only so that finding a broken UI does not read as
an undiscovered bug.
