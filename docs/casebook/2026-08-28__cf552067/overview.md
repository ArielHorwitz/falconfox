# Overview

FalconFox's Telegram client speaks to **one session at a time**. Two chats are
configured — a focus chat backed by a rotating ephemeral manager session, and
a work chat that forwards to whichever session a pointer file names — so
running sessions in parallel means switching the pointer back and forth, and
the chat's history interleaves work from different projects into one stream.

This case asks whether Telegram's **forum topics** can give each session its
own channel, and what that does to the focus/work divide, which exists largely
to answer "which single session is the work chat talking to" — a question that
stops being interesting once every session has its own place to talk.

## The idea (user, 2026-08-28)

> My idea is to enable multiple channels for multiple sessions in parallel.
> This will somewhat negate a large chunk of the "focus" session and "work"
> session structure / divide.

## Two routes, both confirmed available

Telegram calls the "group of groups" a **forum**: a chat that opens into a
list of sub-chats, each addressed by a `message_thread_id`. Two ways to get
one, and **both were checked on 2026-08-28 and work**.

### Route A — threaded mode in the bot's private chat

**Bot API 9.3 (2025-12-31), "Topics in private chats."** Enabled per-bot via
@BotFather; it turns the existing one-to-one chat with the bot into a forum.
No group, no admin rights, no member threshold. Telegram's own framing is that
it is "especially useful for AI chatbots".

- `has_topics_enabled` on `User`, returned only in `getMe` — the bot can
  detect its own mode.
- `message_thread_id` and `is_topic_message` on incoming `Message`.
- `message_thread_id` accepted on `sendMessage` and ~21 other send methods.

**Verified live:** the @BotFather toggle exists, was enabled for this bot, and
user-created threads can be turned off — so the bot can own the topic
namespace rather than racing the user for it.

### Route B — a supergroup with Topics enabled

**Bot API 6.3 (2022-11-05).** The full management surface exists here and only
here: `createForumTopic`, `editForumTopic`, `closeForumTopic`,
`reopenForumTopic`, `deleteForumTopic`, `unpinAllForumTopicMessages`,
`getForumTopicIconStickers`, plus `is_forum` on `Chat`. The bot must be an
administrator with `can_manage_topics`.

**Verified live:** Topics enabled in a group with **one member**. Telegram's
[Topics 2.0 post](https://telegram.org/blog/ultimate-privacy-topics-2-0) still
says "groups from 100 members or more", and third-party pages say 200; both
are stale. Treat the published threshold as gone, not as a constraint to
design around.

Then measured end to end (2026-08-29) with the bot promoted to administrator
with `can_manage_topics`: **every method passes, including the two a private
chat refuses.** Route B is a strict superset of route A.

### Answered: the bot creates topics (2026-08-29)

The opening question — whether the HTTP Bot API can create a topic in a
private-chat forum — was **measured live and the answer is yes**, even though
the 9.3 changelog does not list `createForumTopic` as extended to private
chats. So the flow is the natural one: spawn a session, a topic appears for
it; the session list *is* the topic list. It is not inverted.

Full results in
[bot-api-forum-topic-capabilities-measured-live.md](bot-api-forum-topic-capabilities-measured-live.md).
The three findings that change the work:

- **Stop/resume has no representation in route A.** `closeForumTopic` and
  `reopenForumTopic` fail with "the chat is not a supergroup forum"; every
  other lifecycle verb maps to a working method. This is now the *only*
  substantive argument for route B.
- **A reply inherits its parent's topic.** A message sent with no thread id
  but with `reply_parameters` pointing into a topic lands in that topic. The
  turn-feedback case threaded replies to their prompt for notification
  reasons, so the reply path is already correct here by accident.
- **The break is the plain sends**: the progress message, reply continuation
  chunks, and every notice. A long answer today splits — first part in the
  topic, continuation in General — which reads as lost text, not misrouting.

`getMe` also reports `has_topics_enabled` and `allows_users_to_create_topics`,
so no new configuration is needed to detect the mode.

## What this does to the focus/work divide

Routing today is **chat id → session** (`bot.py:561-581`): the focus chat
resolves to `focus_session_id`, anything else resolves to the pointer value.
With topics it becomes **(chat, thread) → session**, and three things lose
their reason to exist:

- the **pointer file** (`FALCONFOX_TELEGRAM_POINTER_FILE`), whose whole job is
  naming the one session the work chat talks to;
- **`/switch`**, which is pointer-writing with a nicer face;
- the **work chat** as a distinct configured chat.

What survives is the focus session as a **manager** — spawn, rename, stop,
delete, and possibly topic creation — but no longer as a *router*. That is the
half of its job the wishlist already says the `falconfox-pointer` name
undersells.

## What the code already does right

The parallelism is largely built. This is a **routing** change, not a
concurrency one:

- `_busy_ids` in the coordinator is a **set** (`coordinator.py:59`) — the
  daemon already tracks many sessions working at once.
- The mid-turn refusal is per session, not global (`bot.py:820`:
  `if session_id in self._turn_chat`).
- Every per-turn dict in the bot is keyed by `session_id` — `_turn_chat`,
  `_reply_parts`, `_delivered`, `_consumed`, `_progress_msg`, `_prompt_msg`,
  `_turn_id`, `_turn_started_at`, `_progress_lines`.

So N sessions can already run N turns; the bot simply has nowhere to *put*
more than one conversation.

## What has to change

- **`api.py`** — thread `message_thread_id` through `message`,
  `html_message`, `edit_message` and `chat_action`. All four already take a
  `chat_id`; this is a second addressing component alongside it.
- **`_turn_chat`** — holds a chat id today, needs a (chat, thread) pair.
- **`turns.json`** — the persisted turn map stores `"chat": <int>` per
  session. Changing it needs a migration or a deliberate one-time reset;
  in-flight turns across a bot restart are the thing it exists to protect.
- **Topic lifecycle** — decide what a spawn / rename / stop / delete does to
  the corresponding topic. `editForumTopic` makes rename cheap; close vs
  delete on stop is a real choice, not a detail.
- **Config** — `FALCONFOX_TELEGRAM_WORK_CHAT_ID` and the pointer file are
  replaced by whatever addresses the forum.

## Open decisions

1. ~~**Route A or B.**~~ **Settled: route B** (a supergroup forum). It is a
   strict superset — everything route A does, plus `closeForumTopic` /
   `reopenForumTopic`. And a **closed topic still accepts bot writes**, which
   is precisely the shape a stopped session wants: it stays in the list as a
   record, the user cannot type into a session that is not running, and the
   bot can still deliver a final notice. The cost is one-time setup (a
   supergroup, bot promoted) which is already done.
2. **Where the manager lives.** The General topic is the obvious home, but a
   manager that can also *be* the thing creating topics may want its own.
3. ~~**Who owns the topic namespace.**~~ Settled: the bot creates topics, and
   user-created threads can be disabled at @BotFather.
4. **Notifications per topic.** The turn-feedback case settled on a silent
   progress message and a threaded reply that pings; per-topic muting is a
   new lever that may change that balance.
5. **What a session's state looks like at a glance.** `editForumTopic` sets a
   topic icon from **112** custom emoji, per topic, persistent, visible in
   the topic list without opening anything. That is a far better channel for
   *idle / working / stuck* than the five-state chat action the turn-feedback
   case settled for — and it was outside that case's design space, because
   sessions had no per-topic identity to hang it on. This case should own the
   question.

## Next: the private chat as a setup and meta session

*Proposed by the user, 2026-08-29.* The forum needs configuration before it
can carry anything; the private chat needs none, because Telegram gives a bot
its DMs for free. So the DM becomes a session of its own whose subject is the
deployment: set the forum up if there is none, diagnose and repair it if it is
wrong, and serve as a general help channel otherwise.

It replaces the worst step in the current bootstrap — `deploy/README.md` tells
the operator to read the forum's chat id out of `journalctl`, which is the one
part of setup that cannot be done from a phone. The bot already receives that
id; only the delivery is wrong.

It also **shrinks the deployment**: a token and an owner user id, both
available before anything runs, instead of a token and two chat ids whose
values only exist after the group does. Design and the decisions it forces —
configuration that can be *learned* rather than only read from the
environment, and an owner-id check that is config rather than authentication —
in
[the-private-chat-as-a-setup-and-meta-session.md](the-private-chat-as-a-setup-and-meta-session.md).

Anticipated, not yet scheduled: migrating the **production** instance onto a
forum of its own and turning threaded mode back off on its private chat, so
the DM is the single setup/meta conversation rather than a forum in its own
right.

## Related wishlist entries

Two entries in [wishlist.md](../../wishlist.md) are touched, one of them
possibly deleted rather than done:

- **"Rename the `falconfox-pointer` skill to match its job"** — if the pointer
  disappears, this entry is *obsoleted*, not completed. Worth noticing before
  someone does the rename.
- **"Don't drop a message sent mid-turn — queue it or interrupt"** — parallel
  topics reduce the pressure without removing the fault. A busy session is
  still busy; you can just go and talk to a different one meanwhile.

Adjacent but separate: **"Make use of Telegram message streaming"**
(`sendMessageDraft`, also Bot API 9.3). It arrived in the same API version and
touches the same messages, but it is a question about what a *turn* looks
like, not about where turns live. Keep the two apart.
