# What a topic displays: tags in the title, a constant icon

*Settled in discussion, 2026-09-11 and 2026-09-12. Not built yet.*

A topic has two display slots and they were being made to fight. Tags were
drawn as the topic icon, which holds exactly one value out of 112, so most of
a session's tags were invisible and tag *order* had to double as a priority
rule to decide which one won.

The dev config is the evidence. It carries 29 tags across three axes, and its
own prose instructs the user to *"put the status first unless the kind is the
thing worth seeing"*. That sentence exists only to work around the one slot.

The settled split: **the title describes, and the icon is a constant.**

## Tags go in the title

The title becomes the session's tag glyphs followed by its name. Glyphs run
together with no spaces between them and a single space before the name.

```
❗️⚡️falconfox-topic-icons
```

Consequences, all of them simplifications:

- **Any emoji works.** Titles are free text, so the 112-icon ceiling does not
  apply. Tag glyphs stop being resolved against
  `getForumTopicIconStickers` and stop being validated at all.
- **Every tag shows.** One slot became many, so tag order stops being a
  priority rule and becomes display order. The config prose telling the user
  to order tags by importance is deleted, not reworded.
- **The 128-character cap is spent on the name.** Truncate the name, never the
  glyphs. `rename_topic` already slices the tail, so this falls out.
- **The bare name stays authoritative.** The title is always recomputed as
  `glyphs + " " + name` and never parsed back, so `/name` and a user who puts
  an emoji in a session name cannot fight. `_topic_names` changes meaning to
  the *composed* title, so a rename fires when either the name or the tags
  change.

Prefixed glyphs push the name rightwards and truncate its tail, which is where
these session names differ from each other (`falconfox-tray`,
`falconfox-session-limits`). Raised, and deliberately accepted.

## The icon is one configurable constant

Every topic gets the same icon, configurable, defaulting to 💬. Nothing
computes it and nothing changes it at runtime.

It still has to be one of the 112, so it is still resolved through
`getForumTopicIconStickers` and still validated. It is set inside
`createForumTopic`, where an icon costs nothing, so every topic made from now
on is free forever.

**This is what makes the rest cheap.** No runtime icon edits means no service
messages, no contention for the topic list preview row, no cadence question,
and no need to couple `_apply_icon` to the message path. The plumbing to set
an icon per session stays in place, unused, for whenever the slot earns a
signal.

## Why the icon carries nothing yet

Two candidates were worked through and set aside. Both rejections are worth
keeping, because both looked obviously right at the outset.

**Turn state was rejected on cadence.** It changes twice per turn, each change
costing a service message, against a group budget of roughly twenty messages a
minute that every topic in the forum shares. It also duplicates channels that
already exist and cost nothing: the chat action already animates "working" in
the topic list, the prompt reaction already carries per-message status, and the
progress message is already stamped `✅ Turn finished · 132s · 14 tool calls`.
The architecture agrees: `agent_state` does not emit `session_updated`, so the
per-turn states are not even reachable from the icon path without new
plumbing.

**Liveness (`live` versus `stored`) was the leading candidate for a day and
was set aside.** It is genuinely the better signal of the two. Its cadence
tracks session switches rather than turns, at `max_live_sessions = 1` it
answers which session replies instantly and which costs a cold resume, it is
self-limiting because only the cap's worth of topics can wear it, and `live`
is the daemon's own bookkeeping rather than the agent's self-report, which can
go stale.

What took it down:

1. **It is not actionable.** `send` auto-resumes a stored session, so the user
   never needs to know before acting. The cost of not knowing is a slower
   reply, not a wrong move.
2. **There is no dormant glyph in the 112.** No moon, no zzz, nothing that
   reads as asleep without being taught. 📁 works only as a convention already
   spent on the `archived` tag.
3. **Absence cannot mean asleep.** Clearing the icon falls back to a default
   badge drawn from the title's first character, which renders a **question
   mark** once titles start with a glyph. So liveness would need two glyphs,
   not one and a blank.

**Errors were considered and rejected on a checkable fact.** A latched ❗️ on a
failed turn looked strong until the error path was read. `_finish_turn` stamps
the progress message `⚠️ Turn ended with an error` and then calls
`_send_reply`, which on a hard failure has nothing to send. The stamped
message is therefore already the last one in the topic and already the preview
row. The durable marker exists.

### The blind brainstorm

Three agents (Opus, Fable, Sonnet) were given the slot's constraints and costs
with no knowledge of the conclusions above, and asked what deserves it.

They **unanimously rejected turn state**, which is the firmest result. Their
top picks diverged: liveness, a latched error marker, and leaving the slot
user-curated. Opus reached liveness independently, which is why it survived as
long as it did.

Findings from that exercise worth keeping:

- The `Topics` pack is a **subject vocabulary, not a status vocabulary**. It
  has no ⏸, ⏳, 🔴, 🟢, 💤, ❌, ⚠️ or 🔒. Any status scheme built from it is
  convention the user must learn.
- 👀 and ✍️ are already spoken for as `REACT_QUEUED` and `REACT_RUNNING`.
  Using either as an icon puts one glyph in one chat with two meanings.
- **Unmeasured and worth knowing before the slot is ever automated:** does an
  icon-change service message increment the unread badge? If it does, every
  edit manufactures a false "something new here" and fights the one signal
  Telegram gives for free.

## Migration is the only place this costs anything

On the first run after this lands, every existing topic needs a rename to add
its glyphs *and* an edit to set the default icon. That is two service messages
per topic, across roughly a dozen topics, against a budget of about twenty a
minute shared forum-wide.

`_reconcile_topics` therefore paces itself: **four seconds after any call that
actually fires**. Pacing on work done rather than on iteration matters, because
reconcile runs on every start and finds nothing to change once migrated, so
pacing the loop itself would add a minute of dead time to every restart
forever.

## What else moves in the same change

**`/tags` becomes `/tag`**, a clean break with no alias. It lives only in the
`COMMANDS` tuple, the dispatch, the help prose and the tests, since there is no
`setMyCommands` anywhere.

**The `/tag` report** concerns itself with tags and their glyphs, not the
composed title. The `Topic icon:` line goes, because the icon no longer has
anything to do with tags. The unmapped-tag warning added on 2026-09-10 stays
but talks about glyphs. The "first mapped tag wins" rule leaves both the report
and `/help`.

**The config** gains `default_topic_icon` and renames `topic_icons` to
`tag_icons`, which have deliberately different rules: tag glyphs are any emoji
and unvalidated, the default icon must be one of the 112 and is validated. No
compatibility read for the old key, following `78411f8`. Only the dev config
changes, because stable runs `master` and would lose its icons until the
deploy catches up.

**What dies:** the tag-to-icon resolution. `_icon_for` over tags, and the
emoji-to-id lookup as applied to the tag map. What survives of
`_load_icon_map` resolves a single configured emoji.
