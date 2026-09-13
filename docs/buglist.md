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

## Telegram calls from this host intermittently hang until the read timeout

*Diagnosed while dogfooding the turn-feedback simplification, 2026-09-12.
Cause unknown; the symptom is now contained rather than fixed.*

A call to the Bot API occasionally stops responding and sits there until
`urlopen`'s read timeout (40s, `REQUEST_TIMEOUT`). It is not load: the host
was idle, the bot's run-queue wait across a CPU-heavy stretch measured 0ms,
and round trips to `api.telegram.org` measure ~50ms when they work at all.
Today's journal has it on `getUpdates` ("Telegram polling failed:
TimeoutError"), and the comment in `_send_action` records it from 2026-08-25
on `sendChatAction`.

How it was found: "typing…" died mid-turn and the progress message arrived in
a 48-second batch. Watching the bot's sockets for 62s of a live turn showed
about six connections where a 4-second tick should produce fifteen — the
indicator loop was blocked inside one hung call.

What has been done about it: the chat action and the progress edit no longer
share a task, so one hang can no longer take the other down; the action gets
an 8-second timeout, since one older than that is worthless anyway; and
`_json_request` logs any call over 10s at WARNING, so the next occurrence
leaves a trace. None of that explains why the calls hang. If the WARNING lines
show a pattern (one method, one time of day, one IPv6 route — the bot reaches
Telegram over v6 here), that is the thread to pull.

## An extra "Working..." message appears after the reply

*Reported from use, 2026-09-08. Not investigated.*

Every so often an *extra* "Working..." message appears after the final
response has already arrived, and then never resolves to anything.

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

Seen first on 2026-09-09 and read then as client-side render lag. That
reading was wrong, see below.

## Topic icons intermittently do not reach clients

A tag change sometimes leaves a client showing the previous icon, at random:
most changes land, some do not. Measured on 2026-09-10, the half we own is
provably healthy. The daemon emits `session_updated`, the bot applies the
icon in well under a second, and Telegram accepts the edit. Re-sending the
same icon afterwards answers `TOPIC_NOT_MODIFIED`, which is the only way to
read a topic's icon back, since `getForumTopic` and `getForumTopics` do not
exist (re-measured 2026-09-10, still 404). So the server holds what the bot
believes it holds, and the divergence is downstream of that.

Two things were ruled out. It is not render lag: a stale icon survived a
full client cache clear, so the client was being served the stale value
rather than failing to draw the fresh one. It is not lost service messages
being replayed either, since the topic history contains none.

One aggravator is known. Several icon edits in quick succession (three in
1.3 seconds, while testing) left the authoritative icon correct but clients
stuck on the *first* of the burst, which suggests Telegram's own topic-list
index races rapid edits. Ordinary tagging does not usually burst like that,
so this explains a test artifact rather than the reported symptom.

The bot used to delete the "changed the topic icon" notice about 60ms after
causing it, which starved clients of the one durable record of the change.
That sweep was dropped on 2026-09-10 to take it out of the picture, and
whether the intermittency survives without it is the open question.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident. The standing decision in
[wishlist.md](wishlist.md) is to delete the assets soon, and possibly repair a
UI later; it is listed here only so that finding a broken UI does not read as
an undiscovered bug.
