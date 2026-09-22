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

## Command output is too wide for a phone

*Reported from use, 2026-09-10. Narrowed to mobile the same day.*

Anything that reaches Telegram as monospace is formatted for a terminal, and a
terminal is not what is reading it. `falconfox list` prints a fixed-width
table whose columns grow to fit their contents and whose last column is a full
path, which is ninety-odd columns for an ordinary listing. Shell job output
relayed by `_say_job` is whatever the command chose to print. Both land in a
`<pre>` block, where a phone has a fraction of that width to draw them in.

The bot's own listings do not have the problem, which is the useful contrast:
`_session_entry` writes one proportional-font line per session and expects it
to reflow. `<pre>` reflows as well — it wraps, on mobile and on desktop both —
but what goes into it was built assuming it would not.

So nothing is cut off or scrolled out of reach: it is wrapped. A wrapped row
of a fixed-width table has lost exactly the alignment that monospace was
chosen to protect, which `_say_job` says in as many words. `<pre>` is wide
enough that `/sh` and `/jobs` rarely trip this on desktop at fullscreen.
Mobile is not, even at the smallest font setting — and mobile is the case that
matters, since the phone is what falconfox is driven from.

The fix wants a notion of how wide the reader is, which nothing here has: the
CLI formats the same way whether a terminal or a phone is asking. A thinner
default layout, a configured width, or a narrow human form alongside `--json`
for the agent — undecided, and worth deciding before any of it is built.

## A restart prepends the previous round's turns

*Reported from use, 2026-09-11. Not investigated.*

After a daemon restart, a session's next turn appears to carry the previous
round's turns in front of it, and in a more verbose form than the original
exchange was.

The mechanism that would do exactly this is `_context_prompt`: a session whose
backend did not load natively gets its saved transcript prepended to the next
prompt, wrapped in `=== prior conversation ===`. It goes out with
`record=False`, so it never lands in the transcript itself, and it is
announced by a notice saying the context was re-sent imperfectly.

That notice is the cheap thing to look for. If it is there, the replay is
working as designed and the complaint is what it costs. If it is not,
something else is doing the prepending.

Worth settling either way whether the path should fire at all here. It is
conditioned on the resume having failed to load, so a backend that does resume
natively reaching it means the real defect is a silent resume failure, with
the replay only the symptom.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident. The standing decision in
[wishlist.md](wishlist.md) is to delete the assets soon, and possibly repair a
UI later; it is listed here only so that finding a broken UI does not read as
an undiscovered bug.
