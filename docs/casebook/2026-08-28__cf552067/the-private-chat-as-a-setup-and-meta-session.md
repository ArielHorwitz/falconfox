# The private chat as a setup, repair and meta session

*Proposed by the user, 2026-08-29, while the forum rework was being tested.*

The forum needs configuration before it can carry anything: a supergroup,
Topics enabled, the bot promoted, and its chat id in the environment. The
**private chat needs none** — Telegram gives a bot its DMs for free. That
asymmetry is the whole idea: the DM is reachable exactly when the forum is
missing, misconfigured, or has silently changed its id, which is precisely
when you need a way in.

So: messaging the bot directly opens a **session of its own**, whose job is
the deployment rather than any project. Set the forum up if there is none,
diagnose and repair it if it is wrong, and otherwise serve as a general
help/meta channel for FalconFox.

## What it replaces

Today `deploy/README.md` bootstraps by telling the operator to create the
group, say anything in it, and then **read the chat id out of `journalctl`**.
That is a terrible step: it is the one part of setup that cannot be done from
a phone, in a project whose whole claim is that the phone is enough. The bot
already receives the id — it logs it — so the information is there; only the
delivery is wrong.

The same applies to repair. Two failure modes are already known and both are
currently silent from the user's side:

- **The forum's chat id changes.** Enabling Topics upgrades a plain group to a
  supergroup and issues a new id (measured 2026-08-29). Every later message is
  then ignored as "unconfigured", and the only evidence is a log line.
- **The bot is not an administrator**, or the group is not a forum. Topic
  creation fails; the sessions exist with nowhere to talk.

A DM session can state all of this in a sentence and offer the fix.

## The chicken-and-egg is genuinely solved

Discovery works without the operator reading anything: when the bot is added
to a group, Telegram sends it the join as a service message carrying the chat
id. The flow becomes "add me to your group, then come back here" — no logs,
no copying ids.

**Implementation note:** the client polls with `allowed_updates: ["message"]`,
which is enough for the join service message but *not* for `my_chat_member`,
the update that reports promotion to administrator. Detecting "you added me
but did not promote me" needs that subscription widened.

## Two decisions this forces

### 1. Where configuration lives, and what wins

Configuration is environment variables read at startup from a systemd
`EnvironmentFile`. A setup session that *learns* the forum id cannot write
that, and cannot restart itself into it. So either:

- **Learned state in the bot's state dir**, beside `topics.json`, with the
  environment as an override for operators who want to pin it; or
- **the environment stays authoritative** and the setup session only ever
  tells the operator what to paste, which keeps the bad step and merely moves
  it into the chat.

The first is the one that matches the idea. It also means the forum id becomes
*mutable at runtime*, which the current code assumes it is not — a migration
today is deliberately logged loudly rather than followed, precisely because
nothing could act on it. With learned state, following it becomes possible,
and the loud log becomes a fallback rather than the whole answer.

### 2. Who is allowed to talk to it — config, not authentication

Today the two configured chat ids *are* the access control: anything else is
ignored. Opening the DM removes that property — anyone who finds
`@falconfox_bot` can message it — so the bot must check **who** is talking,
not just where.

That check is a configured **owner user id**, supplied at deploy time exactly
like the bot token, and compared against `message.from.id`. It is a filter
over a static config value, not an authentication mechanism: nothing is
exchanged, verified or granted, and it is the same shape as the chat-id checks
the client already does. The project's "no remote auth is planned" position is
untouched.

It should still land *with* the feature rather than after it, since the DM is
functional the moment it exists.

**This shrinks the deployment rather than growing it.** Today an operator must
supply a token and two chat ids, and obtaining the chat ids is the step that
cannot be done from a phone. The new shape is a token and a user id — two
values, both available before anything is running — with the group, its
topics and the bot's permissions all discovered or guided from the DM. That is
the real prize here, and it is worth more than the repair story: a new user on
a fresh VPS can deploy with what they already have and be talked through the
rest.

## How far setup can actually be automated (measured 2026-08-29)

**A bot cannot create a group.** There is no `createChat` / `createGroup` /
`createChannel` in the Bot API; chats are created by users. **Nor can a bot
enable Topics** — that is `channels.toggleForum` in MTProto, which requires
owner rights and is not exposed to bots at all.

So two steps are irreducibly the user's: *create the supergroup* and *enable
Topics*. Everything after that can be one tap, via a deep link that requests
admin rights at the moment the bot is added:

```
https://t.me/<bot>?startgroup&admin=manage_topics
```

`manage_topics` is among the permitted `admin=` keywords, alongside
`change_info`, `invite_users`, `pin_messages`, `manage_chat` and others,
combined with `+`. Following the link prompts the user to pick a group and
adds the bot **already promoted with exactly those rights** — collapsing "add
the bot", "promote it" and "grant Manage Topics" into a single action, with no
permission screens to navigate and nothing for the user to get wrong.

### Order matters, and it follows from the migration finding

Enabling Topics upgrades a plain group to a supergroup and **changes its chat
id**. So the flow must be:

1. user creates a group and enables Topics (the two irreducible steps);
2. bot sends the `startgroup&admin=manage_topics` link;
3. user taps it, bot is added as admin and learns the id from the join event;
4. bot verifies with `getChat` (`is_forum`) and `getChatMember`
   (`status`, `can_manage_topics`) and reports what it found.

Adding the bot *before* Topics is enabled would have it learn an id that goes
stale moments later. That is not hypothetical — it is exactly what happened
while testing on 2026-08-29, and it is why the client has migration handling
at all. Sequencing it this way avoids the problem rather than recovering from
it, and the migration handling stays as the safety net for a group that is
converted later.

### The residue

What cannot be removed: two user actions (create group, enable Topics) and one
tap. What the bot can then do unaided: discover the id, confirm it is a forum,
confirm its own rights, create every topic, and diagnose each of those if
wrong. The verification step is worth building even though it sounds
redundant — `getChat` and `getChatMember` turn "it silently does nothing" into
a specific sentence about which of the three conditions failed.

## Shape, if built

An ephemeral session like the manager, in the DM, with a setup/repair skill
and permission to run `falconfox` plus read the bot's own configuration
state. Threaded mode is already enabled on the private chat, so it *could*
use topics — but the manager's General-only pattern is the simpler default
and there is no evident need for more than one thread here.

Not started; recorded while the forum rework was still being tested, so that
the reasoning survives the session that had it.
