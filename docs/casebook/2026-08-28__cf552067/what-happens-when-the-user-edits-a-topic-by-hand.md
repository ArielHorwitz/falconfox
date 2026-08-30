# What happens when the user edits a topic by hand

*Discussed 2026-08-30, no changes made. The client mirrors sessions onto
topics; nothing mirrors the other way, and these are the consequences.*

## The Bot API cannot enumerate topics

Measured by probing method names: **`getForumTopics` and `getForumTopic` do
not exist** (`Not Found`), while `getForumTopicIconStickers` does. There is no
way to list a forum's topics or inspect one.

This decides the shape of every answer below. Reconciliation cannot be
proactive — there is nothing to reconcile *against*. Any healing has to be
failure-driven: attempt the send, and treat the error as the signal.

## Renaming a topic by hand

It sticks, and the client does not fight it: `forum_topic_edited` arrives as a
service message and is deliberately ignored, so the `_topic_names` cache still
holds the old name and `_mirror_session` sees no change to correct.

The cost is silent divergence — `falconfox list` shows one name, the topic
header another — and the manual name is **overwritten without warning** the
next time the *session* is renamed.

The asymmetry underneath: the session is canonical and the topic is a mirror.
Arguably it should be the reverse, since the topic title is what the user
actually reads. Treating `forum_topic_edited` as a rename *of the session*
would make the two agree by construction.

## Deleting a topic by hand

**This strands the session silently.** There is no `forum_topic_deleted`
service message — the Message fields are created, edited, closed, reopened,
plus General hidden/unhidden — so the client learns nothing.

`topics.json` keeps a binding to a dead thread. The session still exists and
still works from the CLI, but has nowhere to talk. Sends to that thread fail:
topic *creation* failures now report to the private chat, but reply and
progress failures only log.

`_reconcile_topics` cannot fix it. It creates topics for sessions that lack
one and unbinds sessions that no longer exist; a binding pointing at a deleted
topic looks perfectly valid. And per the section above it cannot be made
proactive. The only available fix is reactive: on a send failure to a bound
thread, unbind and let `_ensure_topic` recreate.

## Closing a topic by hand

`forum_topic_closed` arrives and is ignored, so the topic looks stopped while
the session stays live and keeps holding memory.

Worse, **eviction also closes topics**, so "closed" now means two things that
look identical: *the daemon stopped this to free memory*, and *you closed it
and nothing happened*. Only one of them stopped anything.

## Which raises whether closing is worth doing at all

*User's question, and it holds up.* The justification for closing an evicted
session's topic was that the user should not be able to prompt something that
is not running. **That premise is false**: `coordinator.send` auto-resumes a
stored session, so typing into a stopped session works and is exactly the
recovery action. Closing discourages the one thing that fixes it, and for a
non-admin member would block it outright.

Closing costs a second, fragile representation of what the capacity notice
already says; bookkeeping that must survive restarts and currently does not
(see [bugs.md](../../bugs.md)); the ambiguity above; and calls against a
rate-limited surface.

It buys exactly one thing the notice does not: **persistence**. A notice
scrolls away; a closed topic still reads "stopped" a week later.

If that marker is worth keeping, the **topic icon** is the better carrier —
112 of them, settable and clearable, visible in the topic list without opening
anything, and not blocking posts. The decisive property: when the bookkeeping
desyncs, a stale icon is cosmetic while a stale closed state is functional
breakage.

Inclination: stop closing on eviction, keep the notice, and revisit the icon
when the state-channel work happens. That deletes the bookkeeping rather than
fixing it.

Note this weakens one of route B's original selling points — `closeForumTopic`
was the deciding argument over a private-chat forum. Route B remains right for
other reasons, but that particular justification does not survive auto-resume.

## Rename General to "Manager"

*User, 2026-08-30.* General is where the session manager lives, and "General"
says nothing about that. `editGeneralForumTopic` works, so the client can
retitle it on adoption. Cheap, and it makes the forum self-describing.

General is special in four ways, all measured: it has its own method family
(`*GeneralForumTopic`); it **cannot be deleted** (`deleteGeneralForumTopic`
returns `Not Found` while every sibling exists); it can be hidden, which
ordinary topics cannot; and it is addressed by *omitting* `message_thread_id`.
That undeletability is why the manager belongs there — the one channel that
spawns sessions cannot be tidied away by accident.
