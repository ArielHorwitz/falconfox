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

### 2. Who is allowed to talk to it — a new requirement

Today the two configured chat ids *are* the access control: anything else is
ignored, and the bot is unreachable by strangers by construction. Opening the
DM as a functional channel removes that property. Anyone who finds
`@falconfox_bot` can message it, and the setup session's whole job is
reconfiguring the deployment.

This needs an explicit **owner allowlist** — a Telegram user id the bot obeys
and ignores everything else. It is not a large piece of work, but it is a real
new requirement created by this change, and it should land *with* the feature
rather than after it. The daemon binds loopback and the project has
deliberately never needed authentication; this is the first thing that does.

## Shape, if built

An ephemeral session like the manager, in the DM, with a setup/repair skill
and permission to run `falconfox` plus read the bot's own configuration
state. Threaded mode is already enabled on the private chat, so it *could*
use topics — but the manager's General-only pattern is the simpler default
and there is no evident need for more than one thread here.

Not started; recorded while the forum rework was still being tested, so that
the reasoning survives the session that had it.
