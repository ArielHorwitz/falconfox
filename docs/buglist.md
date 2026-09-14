# Buglist

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## Telegram's IPv6 front end ignores about one connection attempt in ten

*Symptom recorded 2026-08-25 and 2026-09-12 as calls that "hang until the
read timeout, cause unknown". Cause measured 2026-09-14. Contained the same
day: the client now connects over IPv4 first, with IPv6 as the fallback, and
gives each attempt a three-second timeout of its own. The underlying loss is
Telegram's and remains.*

What is wrong: a plain TCP connection attempt from this host to
`api.telegram.org` over IPv6 goes unanswered roughly one time in ten, at a
steady rate of about one lost attempt every ten seconds regardless of how
many are made. Over five minutes of interleaved attempts, 18 of 120 to
Telegram were lost against 0 of 120 each to Cloudflare and Google, and an
IPv6 traceroute to Telegram shows no packet loss, so it is their connection
handling rather than the route. IPv4 never lost one.

What it did: this host prefers IPv6, and the client applied one 40-second
timeout to connecting and reading alike. An unanswered IPv6 attempt
therefore cost the full 40 seconds before the fallback to IPv4, after which
the call completed normally. That is why the journal showed some 240 polls a
day taking exactly 70 seconds (40 lost, then the 30-second hold) and
succeeding, and only 31 failing. During each of those 40 seconds the bot was
blind, which is where "the bot took 30 seconds to notice my /help" came
from. A reply or a progress edit that drew the same short straw sat for 40
seconds too.

Ruled out on the way: DNS (400 lookups, none slow), the bot's event loop
(one daemon reconnect in a day, not hundreds), and thread starvation (the
pool has 32 workers).

Left as it is: the loss itself. Nothing on this host can fix Telegram's
IPv6 ingress. With IPv4 tried first it costs nothing in normal operation,
and if IPv4 ever stops answering, the fallback to IPv6 costs three seconds
per call rather than the bot.

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
