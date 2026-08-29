# Forum topic capabilities, measured against the live bot (2026-08-29)

Everything here is a **live Bot API result**, not documentation. Measured in
the production bot's private chat (a positive chat id — the work chat *is*
the private chat, so threaded mode already applies to it) with
threaded mode enabled via @BotFather and user-created threads disabled.

**Constraint on method:** `getUpdates` was never called. Telegram allows one
poller per token and gives the rest 409s, so polling would have broken the
running bot. Everything below is send-side calls plus the bot's own journal.

## getMe reports the mode

```json
"has_topics_enabled": true,
"allows_users_to_create_topics": false
```

Neither field appears in the 9.3 changelog. The second mirrors the @BotFather
setting exactly. **The bot can detect its own topic configuration at startup**
— no new environment variable is needed to tell it.

## Capability matrix — private-chat forum

| Method | Result | Session verb it would serve |
|---|---|---|
| `createForumTopic` | ✅ works | spawn |
| `editForumTopic` | ✅ works | rename |
| `deleteForumTopic` | ✅ works | delete |
| `unpinAllForumTopicMessages` | ✅ works | — |
| `closeForumTopic` | ❌ `Bad Request: the chat is not a supergroup forum` | **stop** |
| `reopenForumTopic` | ❌ same | **resume** |

`createForumTopic` is **not listed** in the 9.3 changelog as extended to
private chats, but it works. That settles the case's opening question: the bot
can own the topic namespace, so the flow is the natural one — spawn a session,
a topic appears — rather than the inverted "open a topic, a session spawns".

**Stop and resume have no representation in route A, and no permission can
change that.** Re-tested on 2026-08-29 after granting the bot every setting
available: `getMe` came back byte-identical and both methods failed with the
same text. The error is a **chat-type** error, not a rights error — a private
chat is not a supergroup, and no permission alters what type a chat is.

Confirmed by contrast: the bot holds *zero* rights in the focus group (status
`member`, empty rights object) yet `createForumTopic` there fails with "the
chat is not a forum" rather than a rights complaint — Telegram checks chat
type before it checks permissions. In the private chat the bot is likewise no
kind of admin, and `createForumTopic` works anyway.

Every other lifecycle verb maps to a method; this one does not. Workarounds are cosmetic (rename
with a prefix, change the icon colour) rather than structural. A supergroup
forum (route B) has `closeForumTopic`/`reopenForumTopic` and would map exactly.
This is the strongest argument route B has.

## Where a message lands — the placement rule

This is the important discovery, and it is not in the changelog:

| How it is sent | Lands in |
|---|---|
| `message_thread_id` set | that topic |
| no thread id, but `reply_parameters` → a message in a topic | **that topic** |
| no thread id, no reply | General |

**A reply inherits its parent's topic.** Measured: replying to message `500`
(sent by the user from topic `91236`) with no `message_thread_id` returned
`message_thread_id: 91236`. A bare send returned `None`.

Other primitives: `sendMessage` accepts `message_thread_id` and echoes
`is_topic_message: true`; `reply_parameters` works inside a topic, so the
threaded-reply notification design survives unchanged; `sendChatAction`
accepts a thread id; and **`editMessageText` needs no thread id at all** —
chat id plus message id is sufficient, so the progress-message *edit* path
needs no change.

## What today's bot does in threaded mode

The placement rule means the current client is not uniformly broken — it is
**half right by accident**, because the turn-feedback case chose to thread
replies to their prompt for unrelated reasons (notifications cutting through
a muted chat).

| Message | Sent with | Lands in |
|---|---|---|
| progress message (`bot.py:859`) | `silent=True`, no reply | ❌ General |
| reply, first chunk (`bot.py:801`) | `reply_to=prompt_msg` | ✅ right topic |
| reply, chunks 2+ (`bot.py:802`) | `reply_to=None` | ❌ General |
| mid-turn refusal (`bot.py:827`) | `reply_to=prompt_msg` | ✅ right topic |
| lost/recovered/silent-turn notices | plain send | ❌ General |
| command output (`/status`, `/list`) | plain send | ❌ General |
| restart announcements | plain send | ❌ General |

The split reply is the nastiest of these: a long answer arrives with its
**first part in the topic and its continuation in General**, which reads as
lost text rather than as misrouting.

## Live routing test

Two topics were created — "TEST session A" (`91236`) and "TEST session B"
(`91251`) — and the user sent one message from each. The journal:

```
04:10:32 forward: session=69eff36c chat=402666258 chars=27
04:10:32 turn started: session=69eff36c turn=1980ce77
04:10:39 refused mid-turn message: session=69eff36c chat=402666258
```

Both topics resolved to **one session**, and the second message was refused by
the per-session mid-turn guard seven seconds after the first started a turn.
This is the whole case in three log lines: the topics exist, the user can
write in them, and the client collapses them into a single conversation.

`turns.json` for that turn recorded `"chat": 402666258` with no thread — the
exact field the overview names as needing to become a (chat, thread) pair.

## What this changes about the work

- The open question at the top of the overview is **answered** — the bot
  creates topics.
- Route A vs route B now turns on **one** thing: whether stop/resume needs to
  close/reopen a topic.
- The reply path already threads correctly. The work concentrates on the
  **progress message**, the **continuation chunks**, and the **notices** —
  all of which are plain sends today.
- No new config is needed to detect the mode (`getMe`).
