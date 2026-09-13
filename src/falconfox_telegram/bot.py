"""Two-channel Telegram client for FalconFox."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import mimetypes
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# Diagnostic machinery, not daemon protocol: the 2026-08-25 keepalive stalls
# could not even be attributed to a side, because neither process recorded its
# own freezes. Both run the same watchdog; sharing it crosses no boundary.
from falconfox import config as falconfox_config
from falconfox import state as falconfox_state
from falconfox.watchdog import StallWatchdog

from .api import ApiError, DaemonApi, TelegramApi
from .rendering import TELEGRAM_MESSAGE_LIMIT, render_messages
from .shell import ShellRunner, TmuxMissing, tail

log = logging.getLogger("falconfox.telegram")

# A daemon restart used to be invisible from the phone: the bot reconnected in
# silence, and unless a turn happened to be in flight nothing was ever said. Self
# -updating from inside a session makes restarts routine, so they get announced.
DAEMON_DOWN = "\u26a0\ufe0f Daemon connection lost \u2014 reconnecting."
DAEMON_UP = "\u2705 FalconFox is up"
# Telegram's answer when an edit would change nothing. It is a 400, but it
# means the topic is already how it was asked to be -- which is success for
# anything that sets a state rather than performs an action. Read as failure
# it is worse than noise: the caller never records what it wanted, so it asks
# again on the next event, forever.
TOPIC_UNCHANGED = "TOPIC_NOT_MODIFIED"
# Telegram's answer when a message is addressed to a thread that is not there
# any more, which is what a topic deleted by hand leaves behind. Matched as a
# substring of the description, since it arrives prefixed ("Bad Request: ...")
# and the Bot API has no numeric code for it. There is no service message for
# a deleted topic and no way to enumerate topics, so a refused send is the
# only notice the bot will ever get.
TOPIC_GONE = "message thread not found"
# Said once, in the new topic. The old one and everything in it is gone, so
# there is nothing to point at -- only the fact, so that a conversation
# reappearing somewhere else is not a mystery.
TOPIC_REPLACED = ("🧵 The previous topic for this session was gone, so this is "
                  "a new one.")
# Seconds to wait after a reconcile call that actually fired. See the comment
# in `_reconcile_topics` for why this is paced on work rather than on loops.
RECONCILE_PACE = 4.0

# Queued rather than refused. The first one explains itself, because the two
# ways out are not discoverable; the rest just count, in the progress
# message's header, because the whole point is to add less to the chat than
# retyping would.
#
# Said once per turn, not once per message: this used to be the half of the
# story a 👀 reaction could not tell, and the reaction covered the rest. The
# reactions are gone (user decision, 2026-09-12) and the header covers them
# now.
QUEUED_FIRST = (
    "📥 Queued — it goes out when this turn ends.\n"
    "/stop ends the turn now · /unqueue drops it · /fullstop does both."
)
# The recurring silent failure: a turn ends, nothing was ever delivered, and no
# layer had an error to report. Now the moment it happens, the chat hears it.
SILENT_TURN = "⚠️ The turn ended without delivering a reply ({detail})."
# A queue only outlives the bot if its turn does, so this is the one case
# where queued words are genuinely lost: the session they were for is gone.
LOST_QUEUE = ("⚠️ {count} queued message(s) went with it, and were not sent. "
              "Their text is still above, in the messages you typed.")
# Reconciliation messages: what a fresh connection says about turns it found in
# the persisted map. The old behaviour -- declaring the reply gone the moment
# the connection dropped -- was usually false: the daemon keeps every chunk in
# the session transcript, so the reply is recoverable once we can ask for it.
RECOVERED_TURN = (
    "♻️ A turn outlived the bot's last connection — recovered the undelivered "
    "part of its reply:"
)
LOST_TURN = (
    "⚠️ A turn was in flight for session {session_id}, but the session no "
    "longer exists — anything not already delivered is gone."
)
# The "stuck" half of turn feedback was a message of its own, said once per
# quiet spell after three minutes of silence. It is now the progress header's
# clock (user decision, 2026-09-12): the same observable fact, stated
# continuously instead of once, and by a message that already exists. What a
# reader does with it is unchanged, because nothing here can tell a long tool
# call from a hung turn -- only /status can.


class Dest(NamedTuple):
    """Where a turn talks: a chat, and a topic within it.

    The forum collapsed this to a bare thread id for a while, on the premise
    that the chat is always the forum. The owner's private chat breaks that
    premise -- it needs no configuration and exists before any forum does --
    so both halves travel together again.
    """

    chat: int
    thread: int | None = None


# Service messages that are not prompts. Topic events are the bot's own
# lifecycle calls echoing back through the update stream.
# What a non-text message can carry, and what to call it when Telegram sends
# no name of its own. A photo never has one, and voice and video notes never
# do either, so the kind supplies the stem and `getFile` supplies the
# extension. Nothing else is read into the name: inventing
# `IMG_20260909_142530.jpg` would be inventing provenance the API did not give.
#
# Order matters. One message can carry several of these fields at once, and
# the first match is what the user actually sent.
ATTACHMENT_KINDS = {
    "photo": "photo",
    "document": "document",
    "video": "video",
    "animation": "animation",
    "audio": "audio",
    "voice": "voice",
    "video_note": "video-note",
}

# The tray receipt. It carries two things and needs both: the id, because
# that is what `/tray` removes by, and the sentence, because a file that waits
# silently reads as a file the bot ignored. See the case: a chat has no
# compose step, so nothing else ties a photo to the message about it.
TRAY_RECEIPT = "🗂 Filed {name} as {id}. It goes out with your next message."
TRAY_EMPTY = "🗂 The tray is empty."


def _attachment_of(message: dict) -> Optional[dict]:
    """What the user sent, or None if this message carries no file."""
    for kind, stem in ATTACHMENT_KINDS.items():
        item = message.get(kind)
        if not item:
            continue
        if kind == "photo":
            # A list of renditions, smallest first. The last is the best one
            # Telegram kept, and the only one worth having.
            item = item[-1]
        return {"kind": kind, "stem": stem, "file_id": item.get("file_id"),
                "name": item.get("file_name"), "size": item.get("file_size") or 0}
    return None


def _attached_line(item: dict) -> str:
    """One tray item, as the agent sees it.

    A path rather than the bytes, which is today's single text block rather
    than a preference -- an agent that can read a file loses little by opening
    it itself.
    """
    caption = (item.get("caption") or "").strip().replace("\n", " ")
    return f"attached: {item['path']}" + (f" ({caption})" if caption else "")


def _format_bytes(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f}MB"
    return f"{max(1, count // 1024)}kB"


_JOIN_EVENTS = {"new_chat_members", "left_chat_member", "group_chat_created",
                "supergroup_chat_created", "migrate_from_chat_id"}


@dataclass(frozen=True)
class BotConfig:
    token: str
    # The only user the bot obeys. This is deploy-time config in the same
    # shape as the token, not authentication: nothing is exchanged or
    # verified. It exists because the private chat is a functional channel,
    # so "which chat" no longer answers "who".
    owner_id: int
    # One forum supergroup holds every session as a topic; the manager lives
    # in General, which cannot be deleted and always sorts first. Optional:
    # unset means "not configured yet, or learn it", which is the state a
    # fresh deployment starts in. When set it PINS the forum, overriding
    # anything learned.
    forum_chat_id: int | None = None
    daemon_url: str = "http://127.0.0.1:9721"
    state_dir: Path = Path.home().joinpath(".local/state/falconfox/telegram")
    manager_backend: str | None = None
    default_path: Path = Path.home()

    @classmethod
    def from_env(cls) -> "BotConfig":
        try:
            token = os.environ["FALCONFOX_TELEGRAM_TOKEN"]
            owner = int(os.environ["FALCONFOX_TELEGRAM_OWNER_ID"])
        except (KeyError, ValueError) as error:
            raise ValueError(
                "set FALCONFOX_TELEGRAM_TOKEN and FALCONFOX_TELEGRAM_OWNER_ID"
            ) from error
        pinned = os.environ.get("FALCONFOX_TELEGRAM_FORUM_CHAT_ID")
        return cls(
            token=token,
            owner_id=owner,
            forum_chat_id=int(pinned) if pinned else None,
            daemon_url=os.environ.get("FALCONFOX_URL", "http://127.0.0.1:9721"),
            state_dir=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_STATE_DIR",
                str(Path.home().joinpath(".local/state/falconfox/telegram")),
            )).expanduser(),
            manager_backend=os.environ.get("FALCONFOX_TELEGRAM_MANAGER_BACKEND") or None,
            default_path=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_DEFAULT_PATH", str(Path.home())
            )).expanduser(),
        )


# Telegram has no "thinking", "working" or "stuck" chat action: every one of the
# eleven valid values describes the bot producing a kind of content. So the
# vocabulary gets spent as a code -- one distinct action per state we can
# actually tell apart -- which is the most this channel can carry. Each lasts
# about five seconds, hence the refresh loop below.
#
# Which glyph means what is deliberately arbitrary for now. What matters is that
# the states are distinguishable in the chat; the mapping is a table of one-line
# choices to reshuffle here, in one place, once we have watched it in use.
#
# Note `record_voice` is on loan: it is the honest action for a reply that is
# itself a voice message, which is what the deferred voice work would produce.
# If that lands, move audio to `upload_voice` or move streaming to one of the
# unused actions (`choose_sticker` aside, `find_location`, `upload_photo`, the
# video ones).
# Telegram's own ceiling for a compressed photo, against 50MB for a file.
PHOTO_LIMIT_BYTES = 10 * 1000 * 1000
# What each media type buys over a plain file: rendering in the chat instead of
# a download. Everything absent from here is sent as-is.
_UPLOAD_KINDS = {
    "image/jpeg": ("sendPhoto", "photo"),
    "image/png": ("sendPhoto", "photo"),
    "image/webp": ("sendPhoto", "photo"),
    "image/gif": ("sendAnimation", "animation"),
    "video/mp4": ("sendVideo", "video"),
}


def _upload_kind(source: Path, raw: bool) -> tuple[str, str]:
    """How to send this file: displayed in the chat, or exactly as it is.

    `raw` is the caller saying fidelity matters more than convenience --
    Telegram re-encodes photos, which is invisible on a photograph and very
    visible on a screenshot of text.
    """
    if raw:
        return "sendDocument", "document"
    kind, _ = mimetypes.guess_type(source.name)
    method, field = _UPLOAD_KINDS.get(kind or "", ("sendDocument", "document"))
    if method == "sendPhoto":
        try:
            oversized = source.stat().st_size > PHOTO_LIMIT_BYTES
        except OSError:
            # Unreadable is the upload's problem to report, not this
            # function's to guess about. Downgrading here would hide it.
            oversized = False
        if oversized:
            return "sendDocument", "document"
    return method, field


def _write_atomic(path: Path, body: str) -> None:
    """Write via a temporary name and rename into place.

    The daemon reads these files on every spawn, so a plain write leaves a
    window in which it can read half a file. Rename within a directory is
    atomic, so a reader sees either the old file or the whole new one.
    """
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(body)
    temporary.replace(path)


# The Telegram client, described for a session that may be reached through it.
#
# Unconditional: every session gets this whether or not it is being spoken to
# through Telegram right now, because a session started here may be resumed
# from another client later, and the reverse.
CLIENT_ORIENTATION = """# Talking through Telegram

One of the clients relaying your messages is a Telegram bot. When a user
writes to you from a phone, this is the shape of what they see.

**Forums and topics.** The bot lives in a Telegram *forum*: a group chat split
into *topics*, which are separate threads within it. Every session gets a topic
of its own, and the user talks to a session by writing in its topic, so
sessions run side by side without interfering. `General`, the forum's default
topic, belongs to the session manager. There is also a private chat with the
bot, which is where a user goes before a forum exists or when one breaks.

**Typing is expensive.** The user is often on a phone, one-handed, sometimes
walking. Session ids and other things they will have to hand back are sent as
tap-to-copy text, and commands are single words. Prefer answers they can act on
by tapping over answers they must retype. Assume a message may have been
transcribed from speech, so a name that is almost right is more likely a
mis-transcription than a new thing.

**Commands.** The user has a set of chat commands the bot handles itself, so
they never reach you and you cannot run one. `/help` shows them a short list.
Run `falconfox help telegram.commands` for what each one does, which is what
you need to answer a question about them or to tell a user which to type.

**Files arrive in a tray, not in a message.** A chat has no compose step: a
photo is its own message, and an album arrives as several with nothing marking
the last. So a file the user sends does not reach you on its own. It waits in
that session's *tray*, and the next real message they write sweeps the tray and
carries it, as one `attached: <path>` line per file in the order they arrived,
with any caption the file came with on the same line.

Three things follow, and they are the ones that bite.

1. **A caption is not a message.** If the user sends a photo captioned "what
   is this?", you get nothing at all, because the caption travels with the
   file and the album problem is why. They have to write again. The bot tells
   them so on every file, but if they are waiting on you, that is what
   happened.
2. **You are handed a path, so open it.** The file is on this host, under the
   session's own storage, and it stays there for as long as the session does.
   You may read it now or twenty turns later.
3. **The user can drop a file before it reaches you**, with `/tray`, which is
   also how they see what is waiting. A removed file is deleted.

**A photo may not be the original.** Telegram re-encodes images sent as photos.
An image you are given may be a degraded copy of something sharper, and asking
the user to resend it as a *file* rather than a photo gets you the original.
Nothing is transcribed: a voice message reaches you as an audio file to open,
not as text.
"""


# Looked up rather than told: the commands are the user's to type, so a session
# needs to recognise them and answer questions about them, but only sometimes.
# The one-line summaries in /help are written for a user mid-task; this is
# written for an agent being asked what something does.
COMMANDS_HELP = """# Telegram commands

Commands the **user** types in the chat. The bot handles them itself and they
never reach you, so you cannot run one: when a command is the answer, say
which one to type. `/help` shows the user a short version of this list.

Where a command acts on "this session", it means whichever session speaks in
the chat it was typed in -- the session that owns that topic, the session
manager in General, or the private chat's own session.

## Sessions and the daemon

- `/list` lists every session, most recently active first, with each id as
  tap-to-copy text. Ordering is by activity because the message is capped and
  the least recently used are the ones dropped.
- `/new [path] [name]` spawns a session, defaulting to the configured path.
  `/home [name]` always uses the default path. The new session gets its own
  topic; nobody creates topics by hand.
- `/status` reports the daemon, the topics it knows and any turn in flight.
  It is the first thing to ask for when something looks stuck.

## This session

- `/id` prints this chat's session id, tap-to-copy. Cheaper than asking an
  agent, which costs a turn.
- `/tag [tags...]` shows this session's tags, or replaces them; `-` clears.
  Every tag with a configured glyph is drawn at the front of the topic title,
  in the order the tags were set. The call replaces the whole list, so tags
  are carried forward by repeating them.
- `/tray [ids...]` shows the files waiting to be sent with the next
  message, or removes them by id; `-` clears the lot. Note the sense is the
  opposite of `/tag`: arguments **remove**, they do not replace. Removing a
  file deletes it, since it was never going to reach you.
- `/name <name>` renames the session whose topic it is typed in, and retitles
  the topic to match. Only in a topic: General and the private chat have no
  work session to rename.
- `/clear` deletes this chat's session and starts a fresh one, losing the
  conversation. Only for General and the private chat, since a work session's
  conversation *is* the work. It is also how a session picks up changed
  orientation, which never reaches sessions that already exist.

## Stopping a turn

A message written while a turn is running is queued rather than refused, and
goes out when the turn ends.

- `/stop` ends the running turn. Anything queued still goes out afterwards.
- `/unqueue` drops what is queued and leaves the turn running.
- `/fullstop` does both. It exists because doing them separately races: after
  a `/stop` the flush is already coming.

## The host

- `/sh <command>` runs a command on the host, detached in tmux, and reports
  the output. It does not run inside any session.
- `/jobs` lists what those commands are doing, `/tail <id>` re-reads a job's
  output, and `/kill <id>` stops one. Jobs outlive a bot restart; `tmux ls` on
  the host is the ground truth.
"""


# Telegram's own role. Registered from here rather than the daemon because it
# exists only because Telegram does: a private chat is the way in before a
# forum exists.
CONCIERGE_ORIENTATION = """# The Telegram private chat

You are the session behind the bot's **private chat**: the one-to-one
conversation between the user and the bot, outside the forum entirely. It is
the one channel that needs no configuration, so it is where the user arrives
before a forum exists, where they come back if the forum breaks, and the
general help and meta channel besides.

Read what the user actually wants: set things up when they want to start,
diagnose when they report something wrong, answer when they ask. Most messages
here are none of those, so do not sweep for problems on every one.

## Find out rather than assume

FalconFox moves fast, so anything written here about its current state would be
stale before you read it. Run `falconfox` commands, ask Telegram, and say what
you found.

## Three things you cannot discover by looking

Facts about Telegram, not about this deployment:

1. A bot cannot create a group, and cannot enable Topics. Both are the user's
   to do; everything after them can be automated. Never imply otherwise.
2. Topics must be enabled *before* the bot is added. Enabling them upgrades the
   group to a supergroup and changes its chat id, so a bot added first holds an
   id that goes stale moments later. This is the most likely way a setup
   silently half-works.
3. The bot can be added already promoted, in one tap, with
   `https://t.me/{bot_name}?startgroup&admin=manage_topics`.
   Offer the link rather than describing permission screens.

So the short path is: the user creates a group and enables Topics, then taps
that link. The bot learns the group by being added and checks the rest itself.

A working forum is a supergroup with `is_forum`, the bot an administrator, and
`can_manage_topics`. When one is missing, say which one. "The bot is not an
admin there" is useful; "setup failed" is not.

`can_delete_messages` is not needed. Changing a topic icon posts a "changed
the topic icon" notice and the bot leaves it alone, so there is nothing here
that deletes messages.

## Where work belongs

Work belongs in a session's own topic, which has an agent, a directory and a
transcript of its own. This chat has none of those, so when the user wants work
done, help them get a forum and suggest a topic for it.

That is a preference, not a prohibition. If the forum is broken and this is the
only channel left, repairing FalconFox from here is what this chat is for.
"""


# The commands, in three sections plus a preamble. One text, identical in
# every chat (user decision, 2026-09-08): /help is where the whole vocabulary
# is learned, and an earlier pass that tailored it per chat made a command
# appear only where you had already thought to look for it. Where a command
# is refused is said on the command's own line instead.
#
# Sections group by what a command acts on. That is a sharper cut than "here
# versus elsewhere": /sh and /new are both usable anywhere and have nothing
# else in common.
MANAGEMENT = "Management"
SESSION = "Session"
EXECUTION = "Execution"
PREAMBLE = None      # rendered above the sections, without one of its own

SECTIONS = (
    (MANAGEMENT, "Manage FalconFox and its sessions."),
    (SESSION, "For specific sessions."),
    (EXECUTION, "Execute commands outside of FalconFox."),
)

# Sent as HTML for the bold headers. That costs nothing that mattered: the
# old plain send was to keep a bare /command tappable, and Telegram parses
# those into bot_command entities in HTML too (checked against the live API
# -- all sixteen come back tappable). Only a <code> or <pre> span would
# swallow them, which is why the usages are not marked up.
COMMANDS = (
    ("/help", "this list. Ask the bot for more about anything in it.", PREAMBLE),
    ("/list", "list sessions", MANAGEMENT),
    ("/new [path] [name]", "spawn a session", MANAGEMENT),
    ("/home [name]", "spawn in the default path", MANAGEMENT),
    ("/status", "show daemon status", MANAGEMENT),
    ("/id", "session id", SESSION),
    ("/tag [tags...]", "show or set tags (`-` clears)", SESSION),
    ("/tray [ids...]", "show waiting files, or remove them (`-` clears)", SESSION),
    ("/stop", "end the turn", SESSION),
    ("/unqueue", "drop the queue", SESSION),
    ("/fullstop", "drop queue and end turn", SESSION),
    ("/name <name>", "rename session (topic only)", SESSION),
    ("/clear", "clear session (special sessions only)", SESSION),
    ("/sh <command>", "run a command in tmux", EXECUTION),
    ("/jobs", "list running commands", EXECUTION),
    ("/tail <id>", "re-read a job's output", EXECUTION),
    ("/kill <id>", "stop a job", EXECUTION),
)


# One action for the whole turn (user decision, 2026-09-12). The chat action
# used to name the activity state -- find_location for thinking,
# record_voice for streaming, upload_document for a tool call -- which was
# added before the progress message existed and was the only account of what
# a turn was doing. The progress message now says all of that, in words, with
# the tool names in it, so the mime was left saying the same thing worse: a
# bot "recording voice" describes nothing a reader can act on. What survives
# is the one bit the progress message cannot carry, since an edit does not
# notify: that the turn is still alive.
#
# Telegram expires an action after about 5 seconds, so the refresh has to be
# under that. Faster buys nothing -- it re-arms the same timer -- and costs
# real budget, because this is the loop that also edits the progress message.
TURN_ACTION = "typing"
ACTION_REFRESH_SECONDS = 4

# A turn produces two kinds of text and the chat now separates them (user
# decision, 2026-08-25): the remarks an agent makes *between* tool calls are
# working narration, shown in a single per-turn progress message that is
# edited in place as the work proceeds and left standing when it ends; the
# text after the last tool call is the actual answer, sent as its own message
# threaded to the prompt it answers. Concatenating both into one reply is what
# produced the run-on garbage this replaces -- narration glued together with
# its referents (the tool calls) invisible.
#
# The progress message is plain text, created by _forward at the very start of
# the turn (so a turn that narrates nothing still has one), updated from the
# activity loop so a hung edit can never stall the event pipeline, and capped
# by trimming its oldest lines.
#
# The header carries a clock (user decision, 2026-09-12), and it is what
# replaced the quiet notice: a turn that has gone silent used to be reported
# once, in its own message, after three minutes of nothing. A clock climbing
# above narration that has stopped changing says the same thing continuously
# and costs no message at all.
#
# It is the one part of this message that changes with no new content, so it
# is paced on its own rather than on the 4-second tick that carries content:
# four refreshes a minute, which reads as live without spending an edit every
# tick on a turn that is saying nothing. Narration is not throttled -- when
# there is something new to show, the next tick shows it.
#
# What this is *not* is a calculated fit to a documented budget. Telegram
# publishes three limits (one message per second per chat, 20 a minute to the
# same group, ~30 a second overall) and all three are about *sending*; neither
# the FAQ nor the API reference says whether an edit counts against them, or
# whether a forum's topics share one allowance. So this is chosen on how it
# reads, at a rate modest under any plausible answer. If the real limit ever
# bites it will arrive as a 429, which is worth logging loudly.
PROGRESS_HEADER = "🛠 Working…"
PROGRESS_LIMIT = 3500
# How often a clock that is the *only* thing to have changed may spend an edit.
PROGRESS_CLOCK_SECONDS = 15
# What is waiting behind this turn, shown in the header beside the clock. The
# first queued message says this in words, threaded to itself; the ones after
# it used to say it with a 👀 reaction and now say it here, where a count is
# more use than a mark on each.
PROGRESS_QUEUED = "📥 {count} queued"
# Thought blocks join the progress message (user decision, 2026-08-25: the
# chain of thought streams into it), but trimmed: a single thinking block can
# run to thousands of characters and would evict everything else. The opening
# of a thought states its intent, so the head is the part worth showing.
THOUGHT_PREVIEW_CHARS = 280


def _format_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def _inline_code(text: str) -> str:
    """HTML-escape, then let a `backticked` run through as an inline code
    span. Unbalanced backticks are left alone rather than guessed at: a
    swallowed half of a line is worse than a stray character."""
    escaped = html.escape(text, quote=False)
    parts = escaped.split("`")
    if len(parts) % 2 == 0:
        return escaped
    return "".join(part if index % 2 == 0 else f"<code>{part}</code>"
                   for index, part in enumerate(parts))


def _build_help() -> tuple[str, str]:
    """The help message, as (html, plain). Built once: it does not depend on
    who asked or from where, and making that structural is the point."""
    rich, plain = [], []
    for usage, what, section in COMMANDS:
        if section is PREAMBLE:
            rich.append(f"{_inline_code(usage)} — {_inline_code(what)}")
            plain.append(f"{usage} — {what}")
    for title, explains in SECTIONS:
        rich += ["", f"<b>{title}</b>", f"<i>{explains}</i>", ""]
        plain += ["", title, explains, ""]
        for usage, what, section in COMMANDS:
            if section == title:
                rich.append(f"{_inline_code(usage)} — {_inline_code(what)}")
                plain.append(f"{usage} — {what}")
    return "\n".join(rich), "\n".join(plain)


HELP_HTML, HELP_PLAIN = _build_help()


# Room left for the "and N more" line when a listing is capped, so that the
# note itself can never be the thing that pushes the message over.
LISTING_OVERFLOW_BUDGET = 80


def _format_age(when: Optional[str]) -> str:
    """How long ago, coarsely. `/list` is ordered by this, and an ordering
    nothing on screen explains reads as no ordering at all."""
    if not when:
        return ""
    try:
        moment = datetime.fromisoformat(when)
    except ValueError:
        return ""
    seconds = max(0.0, (datetime.now(moment.tzinfo) - moment).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


class FalconFoxTelegramBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.daemon = DaemonApi(config.daemon_url)
        self.telegram = TelegramApi(config.token)
        self.state_dir = config.state_dir.resolve()
        self.manager_session_id: str | None = None
        # The private chat's own session. Spawned lazily, on the first message
        # that arrives there: a deployment whose forum works may never need it,
        # and an unused session is a live subprocess against the cap.
        self.concierge_session_id: str | None = None
        self._reply_parts: dict[str, list[str]] = {}
        # Shell jobs started with /sh. They run detached in tmux, so this is a
        # view of them rather than ownership: a job outlives the bot, and a
        # restarted bot forgets the ids while the tmux sessions carry on.
        self._shell = ShellRunner(self.state_dir)
        # session -> the topic it owns, and the reverse. A turn's destination
        # is now just a thread id: the chat is always the forum. None means
        # General, which is the manager's topic.
        self._topics: dict[str, int] = {}
        self._threads: dict[int, str] = {}
        # Last *composed* title mirrored onto each topic -- glyphs and name
        # together -- so the steady stream of session_updated events only acts
        # on a real change, whether the name moved or the tags did.
        self._topic_names: dict[str, str] = {}
        # Last icon applied to each topic ("" for none). Remembered rather
        # than read back, because the Bot API cannot report a topic's current
        # icon at all -- there is no getForumTopic. Without this the bot
        # would re-apply on every startup, and every re-application is a
        # service message in the topic.
        self._topic_icons: dict[str, str] = {}
        # tag -> glyph, from `[telegram.tag_icons]`. Drawn into the title,
        # which is free text, so these are used as configured: no resolution
        # against the forum icon set and nothing to validate.
        self._icon_emoji: dict[str, str] = {}
        # The one icon every topic wears, resolved once at startup from
        # `telegram.default_topic_icon`. "" when unset or unresolvable.
        self._default_icon: str = ""
        # Serialises topic creation. Reconciling runs alongside the event
        # loop now, so a `session_added` and a reconcile pass can both reach
        # `_ensure_topic` for one session, and the check that it has no topic
        # is separated from the create by an await. Without this that race
        # makes two topics for one session, and the loser is unreachable.
        self._topic_lock = asyncio.Lock()
        self._turn_dest: dict[str, Dest] = {}
        # Messages typed while a turn was running, per session, each with the
        # message id that carried it. Held here rather than in the daemon,
        # which refuses a mid-turn prompt on purpose and should keep doing so.
        self._queues: dict[str, list[dict]] = {}
        self._activity_tasks: dict[str, asyncio.Task] = {}
        self._progress_tasks: dict[str, asyncio.Task] = {}
        self._activity_state: dict[str, str] = {}
        self._turn_working: set[str] = set()
        # The daemon's id for the turn this client is carrying, plus what this
        # client has actually handed to Telegram for it — the two facts that
        # let a turn which delivered nothing be caught instead of shrugged at.
        self._turn_id: dict[str, str] = {}
        self._delivered: dict[str, int] = {}
        self._turn_started_at: dict[str, float] = {}
        # Raw stream characters removed from the buffer by flushes (pre-strip,
        # unlike _delivered). This is the offset that lets a reply be rebuilt
        # from the session transcript: transcript_text[consumed:] is exactly
        # what this chat has not seen yet.
        self._consumed: dict[str, int] = {}
        # Sessions whose turn was adopted from the persisted map after a
        # restart or reconnect. Their buffers are missing everything streamed
        # while the bot was away, so they deliver from the transcript instead.
        self._adopted: set[str] = set()
        self._last_event_at: dict[str, float] = {}
        # The two-message turn: the user's prompt message (the final reply
        # threads to it), and the per-turn progress message with its
        # accumulated narration/tool lines.
        self._prompt_msg: dict[str, int] = {}
        self._progress_msg: dict[str, int] = {}
        self._progress_lines: dict[str, list[str]] = {}
        self._progress_dirty: set[str] = set()
        # What the header said the last time an edit went out, and when that
        # was: the first tells a clock that has moved from one that has not,
        # the second paces a refresh that carries nothing but the clock.
        self._progress_sent: dict[str, tuple[str, float]] = {}
        self._seen_tools: dict[str, set[str]] = {}
        self._thought_parts: dict[str, list[str]] = {}
        # Latest usage figures per session (context used/size, token totals),
        # merged from the daemon's usage events for the turn's final stamp.
        self._usage_view: dict[str, dict] = {}
        self._ws = None
        self._ws_lock = asyncio.Lock()
        # The turn→chat map, persisted so it survives the process. A bot
        # restart mid-turn used to orphan the reply: the daemon kept running
        # the turn, but the new process had no idea which chat it belonged to.
        self._turns_file = self.state_dir.joinpath("turns.json")
        # session -> topic, persisted for the same reason as the turn map: a
        # restart that forgot it would create a second topic per session.
        self._topics_file = self.state_dir.joinpath("topics.json")
        # The forum the bot has learned, when none is pinned in the
        # environment. Kept beside the topic map because it is the same kind
        # of fact: something discovered at runtime that a restart must not
        # forget, or it would ask the user to set up a forum that exists.
        self._forum_file = self.state_dir.joinpath("forum.json")
        # Which sessions are this client's manager and private chat. They are
        # hidden but persistent now, so they can sleep to free memory and wake
        # with their conversation intact -- which means a restart has to find
        # them again, or it would make a second pair, then a third.
        self._infra_file = self.state_dir.joinpath("infra.json")
        # The tray: files that have arrived for a session and not yet been
        # carried into a prompt. Persisted for the same reason as the maps
        # above, and it is the client's own state rather than the daemon's --
        # the tray exists because a chat has no compose step, which is a fact
        # about Telegram and about nothing else. The daemon holds the bytes;
        # what is still pending is this file.
        self._trays: dict[str, list[dict]] = {}
        self._tray_file = self.state_dir.joinpath("tray.json")
        self._learned_forum: int | None = None
        self._bot_username: str | None = None
        # Directories for the two infrastructure sessions to run in. Nothing
        # is written into them any more -- orientation reaches a session
        # through its first prompt -- but a session still needs a cwd.
        self.manager_workspace = self.state_dir
        self.concierge_workspace = self.state_dir.joinpath("concierge")

    async def run(self) -> None:
        StallWatchdog(logging.getLogger("falconfox.telegram.watchdog")).start()
        self._register_orientation()
        self._load_forum()
        self._load_infra()
        self._load_topics()
        self._load_tray()
        ws_url = self.config.daemon_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/ws"
        # `async for ... in connect(...)` retries the connection with backoff,
        # so the bot survives daemon restarts (e.g. a self-update) instead of
        # dying with the connection.
        async for websocket in connect(ws_url):
            try:
                await self._run_connected(websocket)
            # The socket, and nothing else. `ApiError` used to be caught here
            # too, which made every Telegram refusal a daemon outage: the chat
            # was told the daemon was lost, the bot reconnected to a daemon
            # that had never gone anywhere, and the recovered turn was resent
            # to the destination that had just refused it. Telegram failures
            # are handled where they happen now -- per event, per send.
            except (ConnectionClosed, OSError) as error:
                log.warning("daemon connection lost (%s); reconnecting", error)
                await self._announce(DAEMON_DOWN)
                await asyncio.sleep(2)
                continue

    async def _run_connected(self, websocket) -> None:
        self._ws = websocket
        snapshot = json.loads(await websocket.recv())
        if snapshot.get("type") != "snapshot":
            raise RuntimeError("FalconFox did not send an initial snapshot")
        # On every connection, because the client directory is named after the
        # daemon's process: a daemon we have just (re)connected to may be a
        # different one, reading a directory this bot has never written to.
        self._register_orientation()
        # Announced on every connection, not only on a reconnect: a deploy
        # restarts the bot too, so the process that saw the daemon go down is
        # rarely the one that sees it return. A bare "up" after a bot-only
        # restart is worth saying anyway -- it reports the restart.
        await self._announce_daemon_up()
        # Before anything can rotate or delete sessions: settle what the
        # persisted turn map says against what the daemon actually has.
        try:
            await self._reconcile_persisted_turns()
        except Exception:
            log.warning("turn reconciliation failed", exc_info=True)
        if self.forum_chat_id is not None:
            await self._ensure_manager()
        try:
            await self._load_icons()
        except Exception:
            log.warning("could not load the topic icon map", exc_info=True)
        # Reconciling paces itself (see RECONCILE_PACE), so a first run after
        # the tag vocabulary changes is minutes of work, not milliseconds.
        # Awaiting it here left the bot deaf for that whole window -- no
        # Telegram polling and no daemon events -- on exactly the restart
        # doing the most work. It runs alongside the loops instead, which is
        # what `_topic_lock` is for.
        reconciler = asyncio.create_task(self._reconcile_topics_guarded())
        loops = [asyncio.create_task(coroutine) for coroutine in (
            self._receive_events(), self._poll_telegram(),
        )]
        try:
            # All three loops are endless, so any completion means the
            # connection (or a loop) is gone; surface its outcome.
            done, _pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        finally:
            reconciler.cancel()
            for loop in loops:
                loop.cancel()
            await asyncio.gather(reconciler, *loops, return_exceptions=True)
            # In-memory turn state dies with the connection, but the persisted
            # map survives on purpose: the next connection reconciles it
            # against the daemon -- adopting turns still running, recovering
            # finished replies from the transcript -- rather than declaring
            # them lost the moment the link blips. The old "anything not sent
            # is gone" message here was usually false, and during the 2026-08
            # keepalive stalls it filled the chat with copies of itself.
            self._reset_connection_state()

    async def _say(self, dest: Dest, text: str, *,
                   reply_to: int | None = None, silent: bool = False) -> int | None:
        """Send to a destination. A thread of None is the chat itself --
        General in a forum, or simply the conversation in a private chat."""
        return await self.telegram.message(
            dest.chat, text, reply_to=reply_to, silent=silent, thread=dest.thread)

    async def _announce(self, text: str) -> None:
        """Tell the manager topic something about the bot itself. Never fatal."""
        try:
            forum = self.forum_chat_id
            if forum is None:
                return  # nothing configured yet; nowhere to announce
            await self._say(Dest(forum, None), text)
        except Exception:
            # An announcement failing must not take down the connection it is
            # announcing -- that would turn a blip into an outage.
            log.warning("could not announce to the manager topic: %s", text)

    async def _announce_daemon_up(self) -> None:
        # Over the API rather than importing falconfox: the bot is a client of
        # the daemon, and the revision it reports should be the daemon's own.
        try:
            version = (await self.daemon.version()).get("version")
        except Exception:
            version = None
        await self._announce(f"{DAEMON_UP} ({version})." if version else f"{DAEMON_UP}.")

    def _reset_connection_state(self) -> None:
        """Clear per-connection state. The persisted turn map is left alone:
        reconciliation on the next connect decides each turn's real fate."""
        self._ws = None
        for task in (*self._activity_tasks.values(), *self._progress_tasks.values()):
            task.cancel()
        self._activity_tasks.clear()
        self._progress_tasks.clear()
        self._activity_state.clear()
        self._turn_working.clear()
        self._turn_id.clear()
        self._delivered.clear()
        self._turn_started_at.clear()
        self._consumed.clear()
        self._adopted.clear()
        self._last_event_at.clear()
        self._prompt_msg.clear()
        self._progress_msg.clear()
        self._progress_lines.clear()
        self._progress_dirty.clear()
        self._progress_sent.clear()
        self._seen_tools.clear()
        self._thought_parts.clear()
        self._usage_view.clear()
        self._turn_dest.clear()
        self._reply_parts.clear()

    def _persist_turns(self) -> None:
        """Write the in-flight turn map to disk, atomically. Never fatal."""
        now_wall, now_mono = time.time(), time.monotonic()
        entries = {}
        for session_id, dest in self._turn_dest.items():
            started = self._turn_started_at.get(session_id)
            entries[session_id] = {
                "chat": dest.chat,
                "thread": dest.thread,
                "turn_id": self._turn_id.get(session_id),
                "consumed": self._consumed.get(session_id, 0),
                "delivered": self._delivered.get(session_id, 0),
                "prompt_msg": self._prompt_msg.get(session_id),
                "progress_msg": self._progress_msg.get(session_id),
                "progress": self._progress_lines.get(session_id, []),
                "queued": self._queues.get(session_id, []),
                # Wall time, because the reader is a different process with a
                # different monotonic clock.
                "started": now_wall - (now_mono - started) if started else now_wall,
            }
        try:
            self._turns_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._turns_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(entries))
            temporary.replace(self._turns_file)
        except OSError:
            log.warning("could not persist the turn map", exc_info=True)

    async def _reconcile_persisted_turns(self) -> None:
        """Settle persisted turns against the daemon on a fresh connection.

        Three outcomes per turn: the session is still working, so the new
        process adopts the turn as its own; the turn ended while the bot was
        away, so the undelivered remainder is recovered from the transcript
        and delivered now; or the session is gone, which is the only case
        where the reply truly is lost -- and the only one that says so.
        """
        try:
            entries = json.loads(self._turns_file.read_text())
        except (OSError, ValueError):
            return
        if not entries:
            return
        states = {item["session_id"]: item["state"] for item in await self.daemon.sessions()}
        for session_id, record in entries.items():
            if "chat" not in record or "thread" not in record:
                # Written by a pre-forum build, whose "chat" ids mean nothing
                # here. Dropping is right: the cutover changed the config, so
                # such a record cannot be delivered anywhere sensible.
                log.info("dropping pre-forum persisted turn for %s", session_id)
                continue
            dest = Dest(record["chat"], record["thread"])
            if dest.thread is None and dest.chat == self.forum_chat_id:
                # Manager turns are session-management chatter, and the
                # manager session is ephemeral and respawned on every connect.
                log.info("dropping persisted manager turn for %s", session_id)
                continue
            state = states.get(session_id)
            if state is None:
                log.warning("persisted turn lost: session=%s no longer exists", session_id)
                await self._say(dest, LOST_TURN.format(session_id=session_id))
                if record.get("queued"):
                    # The turn's own reply is gone with the session; say the
                    # queued messages went with it rather than dropping them
                    # silently, since the user is still expecting them to send.
                    await self._say(dest, LOST_QUEUE.format(
                        count=len(record["queued"])))
            elif state in ("working", "starting"):
                self._adopt_turn(session_id, record)
                await self._set_activity(session_id, "working")
            else:
                await self._deliver_recovered_turn(session_id, record)
                # The turn ended while the bot was away, so nothing will call
                # the flush from _finish_turn. This is that moment, late.
                if record.get("queued"):
                    self._queues[session_id] = list(record["queued"])
                    await self._flush_queue(session_id, dest)
        self._persist_turns()

    def _adopt_turn(self, session_id: str, record: dict) -> None:
        log.info("adopting in-flight turn: session=%s turn=%s consumed=%d",
                 session_id, record.get("turn_id"), record.get("consumed", 0))
        self._turn_dest[session_id] = Dest(record["chat"], record["thread"])
        self._turn_id[session_id] = record.get("turn_id") or ""
        self._consumed[session_id] = record.get("consumed", 0)
        self._delivered[session_id] = record.get("delivered", 0)
        self._reply_parts[session_id] = []
        self._turn_started_at[session_id] = time.monotonic() - max(
            0.0, time.time() - record.get("started", time.time()))
        if record.get("prompt_msg"):
            self._prompt_msg[session_id] = record["prompt_msg"]
        if record.get("progress_msg"):
            self._progress_msg[session_id] = record["progress_msg"]
        if record.get("progress"):
            self._progress_lines[session_id] = list(record["progress"])
        if record.get("queued"):
            self._queues[session_id] = list(record["queued"])
        # The turn has a past, but this process has no event history for it,
        # so "last heard from" starts now. /status reads this; the header's
        # clock does not, and shows the turn's real age from the record above.
        self._last_event_at[session_id] = time.monotonic()
        self._turn_working.add(session_id)
        self._adopted.add(session_id)

    async def _deliver_recovered_turn(self, session_id: str, record: dict) -> None:
        """The turn ended while the bot was away; hand over what never arrived."""
        text = await self._turn_text_from_transcript(session_id)
        remainder = (text or "")[record.get("consumed", 0):].strip()
        dest = Dest(record["chat"], record["thread"])
        prompt_msg = record.get("prompt_msg")
        if remainder:
            log.info("recovered turn: session=%s chars=%d", session_id, len(remainder))
            await self._say(dest, RECOVERED_TURN)
            for index, rendered in enumerate(render_messages(remainder)):
                await self.telegram.html_message(
                    dest.chat, rendered.html, rendered.plain,
                    reply_to=prompt_msg if index == 0 else None, thread=dest.thread)
        elif not record.get("delivered"):
            await self._say(dest, SILENT_TURN.format(
                detail="it ended while the bot was away, and nothing had been "
                       "produced"), reply_to=prompt_msg)

    async def _turn_text_from_transcript(self, session_id: str) -> str | None:
        """Everything the agent has said in the current turn, from the daemon.

        The transcript stores the same chunk events the websocket streams, so
        concatenating the agent messages after the last user message yields
        byte-for-byte the text a connected client would have accumulated.
        """
        try:
            detail = await self.daemon.session(session_id, include_transcript=True)
        except ApiError:
            log.warning("could not fetch transcript for %s", session_id, exc_info=True)
            return None
        transcript = detail.get("transcript") or []
        last_user = -1
        for index, event in enumerate(transcript):
            if event.get("type") == "message" and event.get("role") == "user":
                last_user = index
        return "".join(
            event.get("text", "")
            for event in transcript[last_user + 1:]
            if event.get("type") == "message" and event.get("role") == "agent"
        )


    def _register_orientation(self) -> None:
        """Write this client's orientation where the daemon reads it.

        Written on every start *and* every reconnect: the directory is named
        after the daemon's process, so a daemon restart moves it and a client
        that wrote only once would leave its orientation somewhere nothing
        reads any more.

        The daemon takes the namespace from the directory name, so what makes
        this Telegram's `concierge` rather than anyone else's is where the file
        is, not anything the file claims.
        """
        root = self._clients_dir()
        if root is None:
            return
        mine = root.joinpath("telegram")
        try:
            mine.joinpath("roles").mkdir(parents=True, exist_ok=True)
            _write_atomic(mine.joinpath("orientation.md"), CLIENT_ORIENTATION)
            _write_atomic(mine.joinpath("roles", "concierge.md"),
                          self._concierge_orientation())
            mine.joinpath("help").mkdir(exist_ok=True)
            _write_atomic(mine.joinpath("help", "commands.md"), COMMANDS_HELP)
        except OSError:
            log.warning("could not write orientation to %s -- sessions will "
                        "spawn without it", mine, exc_info=True)
            return
        log.info("orientation registered at %s", mine)

    def _clients_dir(self) -> Optional[Path]:
        """This daemon run's client directory, as the daemon published it."""
        info = falconfox_state.read_server_info()
        directory = getattr(info, "clients_dir", None) if info else None
        if not directory:
            log.warning("the daemon published no client directory; sessions "
                        "will spawn without Telegram orientation")
            return None
        return Path(directory)

    def _concierge_orientation(self) -> str:
        """Everything the private chat needs.

        Telegram-specific and whole on purpose. This is the channel that has to
        work *before* a forum exists, so what it repeats from the client
        orientation is the point rather than an oversight.
        """
        bot_name = self._bot_username or "your_bot"
        return CONCIERGE_ORIENTATION.replace("{bot_name}", bot_name)

    async def _ensure_manager(self) -> str | None:
        """The manager session, spawned on demand.

        A forum adopted *after* the connection came up has no manager yet --
        the connect-time spawn already ran and found no forum. Spawning here
        as well means General works from the moment a forum exists, however it
        came to exist.
        """
        if await self._still_exists(self.manager_session_id):
            return self.manager_session_id
        if self.forum_chat_id is None:
            return None
        try:
            self.manager_workspace.mkdir(parents=True, exist_ok=True)
            await self._spawn_manager_session()
        except ApiError:
            log.warning("could not spawn the manager session", exc_info=True)
            return None
        return self.manager_session_id

    async def _spawn_manager_session(self) -> None:
        # No longer rotated on every connect: the manager persists and sleeps
        # instead, keeping its conversation across restarts. Rotating now
        # would leak a session rather than replace one.
        session = await self.daemon.spawn(
            path=str(self.manager_workspace), name="telegram manager",
            backend=self.config.manager_backend, hidden=True,
            roles=[".manager"],
        )
        self.manager_session_id = session["session_id"]
        self._persist_infra()

    async def _ensure_concierge(self) -> str | None:
        """The private chat's session: remembered, resumed, or made.

        Sending to a stored session resumes it in the daemon, so a sleeping
        private chat wakes on the next message with its history intact, and
        costs a resume rather than a permanent slot.
        """
        if await self._still_exists(self.concierge_session_id):
            return self.concierge_session_id
        if self._bot_username is None:
            try:
                self._bot_username = (await self.telegram.call("getMe") or {}).get("username")
            except ApiError:
                log.warning("could not read the bot username", exc_info=True)
        self.concierge_workspace.mkdir(parents=True, exist_ok=True)
        try:
            session = await self.daemon.spawn(
                path=str(self.concierge_workspace), name="telegram private chat",
                backend=self.config.manager_backend, hidden=True,
                roles=["telegram.concierge"],
            )
        except ApiError:
            log.warning("could not spawn the private-chat session", exc_info=True)
            return None
        self.concierge_session_id = session["session_id"]
        self._persist_infra()
        log.info("private-chat session spawned: %s", self.concierge_session_id)
        return self.concierge_session_id

    # --- topics ----------------------------------------------------------

    @property
    def forum_chat_id(self) -> int | None:
        """The forum in use: pinned by the environment, else learned, else none."""
        return self.config.forum_chat_id or self._learned_forum

    def _load_infra(self) -> None:
        try:
            saved = json.loads(self._infra_file.read_text())
        except (OSError, ValueError):
            return
        self.manager_session_id = saved.get("manager")
        self.concierge_session_id = saved.get("concierge")

    def _persist_infra(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._infra_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"manager": self.manager_session_id,
                                             "concierge": self.concierge_session_id}))
            temporary.replace(self._infra_file)
        except OSError:
            log.warning("could not persist the infrastructure ids", exc_info=True)

    async def _still_exists(self, session_id: str | None) -> bool:
        """Is a remembered session still in the daemon? A deleted one is gone
        for good, so a fresh one has to be made rather than resumed."""
        if not session_id:
            return False
        try:
            await self.daemon.session(session_id)
            return True
        except ApiError:
            return False

    def _load_forum(self) -> None:
        if self.config.forum_chat_id:
            return  # pinned; nothing learned can override an explicit choice
        try:
            self._learned_forum = int(json.loads(self._forum_file.read_text())["chat_id"])
        except (OSError, ValueError, KeyError, TypeError):
            self._learned_forum = None

    def _learn_forum(self, chat_id: int) -> None:
        self._learned_forum = chat_id
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._forum_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"chat_id": chat_id}))
            temporary.replace(self._forum_file)
        except OSError:
            log.warning("could not persist the learned forum", exc_info=True)
        log.info("forum learned: %s", chat_id)

    def _load_topics(self) -> None:
        try:
            raw = json.loads(self._topics_file.read_text())
        except (OSError, ValueError):
            raw = {}
        # The file was once a flat session→thread map, before it had to carry
        # the applied icon too. Read either shape: a version that dropped the
        # old one would make a second topic for every existing session.
        topics = raw.get("topics", raw) if isinstance(raw, dict) else {}
        icons = raw.get("icons", {}) if isinstance(raw, dict) else {}
        names = raw.get("names", {}) if isinstance(raw, dict) else {}
        self._topics = {k: int(v) for k, v in topics.items() if isinstance(v, int)}
        self._threads = {v: k for k, v in self._topics.items()}
        self._topic_icons = {k: str(v) for k, v in icons.items()
                             if k in self._topics}
        # Titles are remembered across restarts rather than guessed at. The
        # Bot API cannot read a topic's title back, so the alternative is to
        # re-send it on every start and let Telegram reject it -- which is a
        # wasted call per session per start, and the reconciler now pauses
        # after every call that fires.
        self._topic_names = {k: str(v) for k, v in names.items()
                             if k in self._topics}

    def _persist_topics(self) -> None:
        """Write the session→topic map atomically. Never fatal."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._topics_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(
                {"topics": self._topics, "icons": self._topic_icons,
                 "names": self._topic_names}))
            temporary.replace(self._topics_file)
        except OSError:
            log.warning("could not persist the topic map", exc_info=True)

    # --- the tray ----------------------------------------------------------

    def _load_tray(self) -> None:
        try:
            raw = json.loads(self._tray_file.read_text())
        except (OSError, ValueError):
            raw = {}
        self._trays = {session: [item for item in items if isinstance(item, dict)]
                       for session, items in raw.items() if items}

    def _persist_tray(self) -> None:
        """Write the tray atomically. Never fatal.

        Losing this file is a recoverable state rather than a corrupt one: the
        files are still in the daemon's store and nothing is pending, which is
        the same as a tray that has just been swept.
        """
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._tray_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._trays))
            temporary.replace(self._tray_file)
        except OSError:
            log.warning("could not persist the tray", exc_info=True)

    def _forget_tray(self, session_id: str) -> None:
        """Drop a gone session's tray. The daemon has already taken the files
        with the session, so there is nothing here but names for them."""
        if self._trays.pop(session_id, None) is not None:
            self._persist_tray()

    def _sweep_tray(self, session_id: str) -> list[dict]:
        """Take what is pending. The bytes stay: the agent is handed a path
        and may read it during the turn or ten turns later."""
        carried = self._trays.pop(session_id, [])
        if carried:
            self._persist_tray()
        return carried

    async def _receive_file(self, session_id: str, dest: Dest, message: dict,
                            attachment: dict) -> None:
        """Take a file into the session's tray, and say what became of it."""
        prompt_msg = message.get("message_id")
        size = attachment["size"]
        if size > self.telegram.DOWNLOAD_LIMIT_BYTES:
            # Checked from the message rather than discovered by a failing
            # download, so the refusal is immediate and names the real reason.
            await self._say(dest, f"That file is {_format_bytes(size)}. Telegram will "
                            f"not let a bot download more than 20MB, though it lets "
                            f"one send 50MB. Not our limit, and no way around it.",
                            reply_to=prompt_msg)
            return
        try:
            remote = await self.telegram.file_path(attachment["file_id"])
            name = attachment["name"] or f"{attachment['stem']}{Path(remote).suffix}"
            with tempfile.TemporaryDirectory(prefix="falconfox-inbound-") as scratch:
                local = Path(scratch, "download")
                await self.telegram.download(remote, local)
                # Handed over as a path: the client and the daemon share a
                # filesystem, so the bytes never cross the socket. The daemon
                # copies, which is what lets this scratch directory go.
                stored = await self.daemon.add_file(session_id, str(local), name)
        except (ApiError, OSError) as error:
            log.warning("could not take a file for %s", session_id, exc_info=True)
            await self._say(dest, f"Could not take that file: {error}",
                            reply_to=prompt_msg)
            return
        self._trays.setdefault(session_id, []).append({
            "file_id": stored["file_id"], "path": stored["path"],
            "name": stored["name"], "caption": message.get("caption"),
            "message_id": prompt_msg,
        })
        self._persist_tray()
        log.info("tray add: session=%s file=%s name=%s depth=%d", session_id,
                 stored["file_id"], stored["name"], len(self._trays[session_id]))
        # The receipt is the whole of it now. It says what happened, once,
        # threaded to the file it happened to; a 👀 on that message used to
        # say the file was *still* waiting, and /tray is what answers that.
        await self._say_html(
            dest,
            TRAY_RECEIPT.format(name=html.escape(stored["name"], quote=False),
                                id=f"<code>{stored['file_id']}</code>"),
            TRAY_RECEIPT.format(name=stored["name"], id=stored["file_id"]),
            reply_to=prompt_msg)

    def _bind(self, session_id: str, thread: int) -> None:
        self._topics[session_id] = thread
        self._threads[thread] = session_id
        self._persist_topics()

    def _unbind(self, session_id: str) -> int | None:
        thread = self._topics.pop(session_id, None)
        if thread is not None:
            self._threads.pop(thread, None)
            self._topic_icons.pop(session_id, None)
            self._topic_names.pop(session_id, None)
            self._persist_topics()
        return thread

    async def _load_icons(self) -> None:
        """Read the tag glyphs, and resolve the one configured topic icon.

        The two halves have deliberately different rules. Tag glyphs are drawn
        into the *title*, which is free text, so they are taken as written and
        any emoji works. The topic icon slot accepts only Telegram's own forum
        set, so that one value is checked against
        `getForumTopicIconStickers`, which turns an emoji into the
        nineteen-digit id for free -- no arguments, no admin rights. A raw id
        is passed through, for anything the endpoint does not list.

        Failure is not fatal: the forum works without either, so a bad entry
        drops out with a warning rather than taking the client down.
        """
        self._icon_emoji = falconfox_config.tag_icons()
        if self._icon_emoji:
            log.info("tag glyphs configured for: %s", sorted(self._icon_emoji))
        configured = falconfox_config.default_topic_icon()
        if not configured:
            return
        if configured.isdigit():
            self._default_icon = configured
            return
        try:
            stickers = await self.telegram.icon_stickers()
        except ApiError:
            log.warning("could not read the topic icon set; the default "
                        "topic icon is off", exc_info=True)
            return
        by_emoji = {item.get("emoji"): item.get("custom_emoji_id")
                    for item in stickers if item.get("custom_emoji_id")}
        if configured not in by_emoji:
            log.warning("the default topic icon %r is not an allowed forum "
                        "icon; the icon slot is left alone", configured)
            return
        self._default_icon = by_emoji[configured]
        log.info("default topic icon: %s", configured)

    def _glyphs_for(self, session: dict) -> str:
        """The tag glyphs a session's title carries, in tag order.

        Every mapped tag is drawn, because the title has room for all of them.
        That is the whole point of moving them here: the icon slot held one,
        so tag order had to mean priority, and now it only means order.
        """
        return "".join(self._icon_emoji[tag]
                       for tag in session.get("tags") or []
                       if tag in self._icon_emoji)

    def _title_for(self, session: dict) -> str:
        """The composed topic title: tag glyphs, then the session name.

        Always recomputed from the name and never parsed back out of an
        existing title, so a session whose *name* contains an emoji cannot be
        mistaken for one wearing a glyph.

        The 128-character cap is spent on the name: the slice takes the tail,
        which is the name's end, and leaves the glyphs standing.
        """
        name = session.get("name") or session.get("session_id") or ""
        glyphs = self._glyphs_for(session)
        return f"{glyphs} {name}"[:128] if glyphs else name[:128]

    async def _apply_icon(self, session: dict, thread: int) -> bool:
        """Put the default icon on a topic, if it is not there already.

        Every topic wears the same one, so this fires once per topic ever, and
        only for topics that predate the setting -- a topic created since gets
        its icon inside `createForumTopic`, where an icon is free. The slot
        carries no signal yet; this keeps the path warm for when it does.

        Returns whether a call was actually made, because the caller paces
        itself on work done rather than on topics seen.
        """
        if not self._default_icon:
            return False
        session_id = session.get("session_id")
        icon = self._default_icon
        if self._topic_icons.get(session_id, "") == icon:
            return False
        try:
            await self.telegram.set_topic_icon(self.forum_chat_id, thread, icon)
        except ApiError as error:
            if TOPIC_UNCHANGED not in str(error):
                log.warning("could not set the icon on topic %s", thread,
                            exc_info=True)
                return False
            # Already wearing it -- someone set it by hand, or a previous run
            # did and the memory of it was lost. Record it and stop asking.
            log.info("topic %s already had the icon asked for", thread)
        self._topic_icons[session_id] = icon
        self._persist_topics()
        log.info("topic icon set: session=%s thread=%s icon=%s",
                 session_id, thread, icon or "(none)")
        return True

    async def _ensure_topic(self, session: dict) -> int | None:
        """Give a session a topic, creating one if it has none.

        Hidden sessions get none. They are this client's own plumbing -- the
        manager speaks in General, the private chat in the private chat -- and
        the flag is read from the event rather than compared against a
        remembered id, which is what made this wrong: `session_added` arrives
        over the websocket before the spawn's HTTP response has been read, so
        the id to compare against was still the previous one.
        """
        session_id = session["session_id"]
        if session.get("hidden"):
            return None
        existing = self._topics.get(session_id)
        if existing is not None:
            return existing
        async with self._topic_lock:
            return await self._create_topic(session, session_id)

    async def _create_topic(self, session: dict, session_id: str) -> int | None:
        """Make the topic. Called only under `_topic_lock`."""
        # Re-read under the lock: whoever held it may have been creating this
        # very session's topic, and the caller's check predates the wait.
        existing = self._topics.get(session_id)
        if existing is not None:
            return existing
        title = self._title_for(session)
        icon = self._default_icon
        try:
            thread = await self.telegram.create_topic(self.forum_chat_id, title, icon)
        except ApiError as error:
            log.warning("could not create a topic for %s", session_id, exc_info=True)
            # Silent here means a session spawns cleanly and simply never
            # appears, with nothing anywhere the user can see. Say it in the
            # private chat, which works even when the forum does not.
            await self._tell_owner(
                f"Could not make a topic for “{session.get('name') or session_id}” "
                f"— the forum is not usable ({error}). Message me here and we can "
                f"sort it out.")
            return None
        self._bind(session_id, thread)
        self._topic_names[session_id] = title
        self._topic_icons[session_id] = icon

        self._persist_topics()
        log.info("topic created: session=%s thread=%s name=%s", session_id, thread, title)
        return thread

    async def _pace(self) -> None:
        """Wait out the forum's message budget after a call that fired.

        Every rename and every icon edit is also a service message, and a
        forum is one group sharing one budget across every topic in it. The
        first run after the tag vocabulary changes wants two calls for every
        session at once, which is exactly the burst that budget cannot take.

        Paced on calls rather than on topics seen: once the forum agrees with
        the daemon this makes no calls at all, so a settled restart never
        waits.
        """
        await asyncio.sleep(RECONCILE_PACE)

    async def _reconcile_topics_guarded(self) -> None:
        """`_reconcile_topics`, never fatal. It runs as its own task now, so
        an exception here would otherwise be swallowed by the task rather
        than logged."""
        try:
            await self._reconcile_topics()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("topic reconciliation failed", exc_info=True)

    async def _reconcile_topics(self) -> None:
        """Make the topic map agree with the daemon's session list. Sessions
        the daemon has lost keep their topic (it holds the conversation) but
        release the binding; sessions with no topic get one."""
        sessions = await self.daemon.sessions()
        live = {item["session_id"] for item in sessions}
        for session_id in [s for s in self._topics if s not in live]:
            thread = self._unbind(session_id)
            log.info("topic orphaned: session=%s thread=%s no longer exists",
                     session_id, thread)
        for item in sessions:
            session_id = item["session_id"]
            if session_id not in self._topics:
                if await self._ensure_topic(item) is not None:
                    await self._pace()
            else:
                # Tags and names can have moved while the bot was down, and
                # topics that predate the default icon still need it. The
                # remembered title and icon make all of this a no-op in the
                # ordinary case, so a restart is silent.
                thread = self._topics[session_id]
                # Two separate calls, so two separate pauses. Pausing once per
                # topic put two service messages into every four seconds,
                # which is thirty a minute against a budget of about twenty,
                # and Telegram throttled it by holding requests for forty
                # seconds rather than refusing them. Measured on the first
                # migration, 2026-09-12.
                if await self._apply_title(item, thread):
                    await self._pace()
                if await self._apply_icon(item, thread):
                    await self._pace()

    async def _poll_telegram(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.telegram.updates(offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)
            except ApiError as error:
                log.warning("Telegram polling failed: %s", error)
                await asyncio.sleep(2)
            except Exception:
                # One bad update must not end the bot. Before this, a single
                # unhandled error in _handle_update propagated out of the poll
                # loop, through asyncio.wait().result(), and killed the
                # process -- a stale /new call took the whole client down and
                # it stayed down. Losing one update is a far smaller failure
                # than losing every future one.
                log.exception("dropping an update that could not be handled")
                await asyncio.sleep(1)

    async def check_forum(self, chat_id: int) -> tuple[bool, str]:
        """Is this chat usable as the forum? Names the failing condition.

        Three conditions, and saying *which* one failed is the whole value:
        "the bot is not an admin there" is actionable, "setup failed" is not.
        """
        try:
            chat = await self.telegram.get_chat(chat_id)
        except ApiError as error:
            return False, f"the chat cannot be read ({error})"
        if not chat.get("is_forum"):
            return False, ("Topics are not enabled there — a bot cannot enable "
                           "them, so this one is yours to turn on")
        try:
            me = await self.telegram.call("getMe") or {}
            member = await self.telegram.get_member(chat_id, me.get("id"))
        except ApiError as error:
            return False, f"the bot's membership cannot be read ({error})"
        if member.get("status") != "administrator":
            return False, "the bot is not an administrator there"
        if not member.get("can_manage_topics"):
            return False, "the bot lacks the Manage Topics right there"
        return True, f"{chat.get('title') or chat_id} is a usable forum"

    async def _maybe_adopt_forum(self, chat_id: int) -> None:
        """Take a group as the forum when there is no working one."""
        if self.config.forum_chat_id is not None:
            return  # pinned by the environment; an explicit choice wins
        if self.forum_chat_id == chat_id:
            return
        if self.forum_chat_id is not None:
            usable, _ = await self.check_forum(self.forum_chat_id)
            if usable:
                return  # already have a working forum; do not steal focus
        usable, detail = await self.check_forum(chat_id)
        if not usable:
            await self._tell_owner(f"I was added to a group, but {detail}.")
            return
        self._learn_forum(chat_id)
        # The connect-time spawn already ran and found no forum, so General
        # would have no manager until the next reconnect.
        await self._ensure_manager()
        await self._tell_owner(f"Forum set: {detail}. Sessions will get their "
                               f"own topics there from now on.")
        try:
            await self._reconcile_topics()
        except Exception:
            log.warning("topic reconciliation after adoption failed", exc_info=True)

    async def _tell_owner(self, text: str) -> None:
        """Say something in the private chat. Never fatal."""
        try:
            await self._say(Dest(self.config.owner_id, None), text)
        except Exception:
            log.warning("could not reach the private chat: %s", text)

    async def _handle_membership(self, event: dict) -> None:
        chat = event.get("chat") or {}
        chat_id = chat.get("id")
        status = (event.get("new_chat_member") or {}).get("status")
        if chat_id is None or status is None:
            return
        log.info("membership change: chat=%s status=%s", chat_id, status)
        if status in ("administrator", "member"):
            await self._maybe_adopt_forum(chat_id)
        elif chat_id == self.forum_chat_id:
            await self._tell_owner(
                "I am no longer in the forum group. Message me here and I can "
                "help you set up a new one.")

    async def _handle_update(self, update: dict) -> None:
        membership = update.get("my_chat_member")
        if membership:
            if ((membership.get("from") or {}).get("id") == self.config.owner_id
                    or not membership.get("from")):
                await self._handle_membership(membership)
            return
        message = update.get("message") or {}
        sender = (message.get("from") or {}).get("id")
        if sender is not None and sender != self.config.owner_id:
            # "Which chat" used to answer "who": every configured chat was the
            # owner's. The private chat is functional now, and anyone can open
            # one with a bot, so identity has to be checked directly.
            log.info("ignoring message from non-owner %s", sender)
            return
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            # Not a message update at all; nothing to route and nothing worth
            # logging -- otherwise every one of them reads as a stray chat.
            return
        moved_to = message.get("migrate_to_chat_id")
        if moved_to is not None and chat_id == self.forum_chat_id \
                and moved_to != self.forum_chat_id:
            # Enabling Topics upgrades a plain group to a supergroup and gives
            # it a NEW chat id (observed live). Loud, because every later
            # message would otherwise be silently ignored as "unconfigured".
            #
            # Both guards matter: Telegram replays the historical migration
            # notice from the OLD chat, whose target is the id we are already
            # configured with. Without them this fires on every restart and
            # reports a migration that has already been followed.
            if self.config.forum_chat_id is None:
                # Learnable config makes this followable rather than merely
                # reportable: enabling Topics moves a group and issues a new
                # id, and the bot now simply moves with it.
                log.info("forum migrated to chat id %s -- following", moved_to)
                self._learn_forum(moved_to)
                await self._tell_owner(
                    "The forum group was upgraded and changed id; I followed it.")
            else:
                log.error("forum migrated to chat id %s -- it is pinned by "
                          "FALCONFOX_TELEGRAM_FORUM_CHAT_ID, so update that "
                          "and restart", moved_to)
            return
        dest = Dest(chat_id, message.get("message_thread_id"))
        text = message.get("text")
        # A file does not prompt the agent by itself: it goes to the tray and
        # the next real message carries it. A caption travels with the file
        # rather than counting as that message, because Telegram puts an
        # album's caption on one of its parts, so a captioned photo would fire
        # a turn while the rest of the album was still arriving.
        attachment = None if text else _attachment_of(message)
        if not text and attachment is None:
            if any(key.startswith("forum_topic_") or key in _JOIN_EVENTS
                   for key in message):
                # Topic service messages are the bot's own lifecycle calls
                # echoing back; answering them would spam every topic it makes.
                return
            await self._say(dest, "I can take files, but not that.")
            return
        if chat_id == self.config.owner_id:
            # The owner's private chat. Answered whether or not a forum is
            # configured -- being reachable when nothing else is, is the whole
            # point of this channel.
            target = await self._ensure_concierge()
            if target is None:
                await self._say(dest, "Could not start the private-chat session.")
                return
            if attachment is not None:
                await self._receive_file(target, dest, message, attachment)
                return
            if text.startswith("/") and await self._command(dest, text):
                return
            await self._forward(target, dest, text,
                                prompt_msg=message.get("message_id"))
            return
        if chat_id != self.forum_chat_id:
            # A message from an unknown group is also a discovery signal. The
            # membership update only fires once, at the moment of joining, and
            # is gone forever after -- so a bot that was already in the group
            # when it lost its forum could never find it again. "Say anything
            # in the group" is a recovery path that always works.
            if chat_id < 0:
                await self._maybe_adopt_forum(chat_id)
                if chat_id == self.forum_chat_id:
                    return
            log.info("ignoring message from unconfigured chat %s", chat_id)
            return
        if text and text.startswith("/"):
            if await self._command(dest, text):
                return
        if dest.thread is None:
            target = await self._ensure_manager()
            if not target:
                await self._say(dest, "The session manager is not running yet.")
                return
        else:
            target = self._threads.get(dest.thread)
            if not target:
                await self._say(dest, "No FalconFox session owns this topic.")
                return
        if attachment is not None:
            await self._receive_file(target, dest, message, attachment)
            return
        await self._forward(target, dest, text, prompt_msg=message.get("message_id"))

    async def _command(self, dest: Dest, text: str) -> bool:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            await self._say(dest, f"Invalid command: {error}")
            return True
        command = parts[0].split("@", 1)[0]
        if command == "/status":
            # Diagnosis from the phone: what the daemon knows about sessions,
            # and what this bot *believes* is in flight — the five parallel
            # dicts that every silent failure so far has been a hidden state of.
            await self._say(dest, await self._status_report())
            return True
        if command == "/list":
            rich, plain = await self._sessions_listing()
            await self._say_html(dest, rich, plain)
            return True
        if command in ("/new", "/home"):
            path = (str(self.config.default_path)
                    if command == "/home" or len(parts) < 2 else parts[1])
            name_start = 1 if command == "/home" else 2
            name = " ".join(parts[name_start:]) or None
            session = await self.daemon.spawn(path=path, name=name)
            # Nothing to point at any more: the daemon's session_added event
            # gives the session its topic. Confirm here anyway, because the
            # topic appears elsewhere in the forum and a silent /new in
            # General reads as a command that did nothing.
            await self._say(dest, f"Spawned {session.get('name') or 'session'} "
                                    f"({session['session_id']}) — see its topic.")
            return True
        # /switch is gone with the pointer: a session is addressed by writing
        # in its topic, so there is nothing left to switch.
        if command == "/help":
            await self._say_html(dest, HELP_HTML, HELP_PLAIN)
            return True
        if command == "/clear":
            await self._clear_chat_session(dest)
            return True
        if command == "/id":
            # A topic's own session id, inline and tap-to-copy. The chat shows
            # names, and names are ambiguous exactly when it matters: asking
            # the manager to act on "the falconfox one" is how the wrong
            # session gets deleted.
            session_id = self._chat_session(dest)
            if session_id is None:
                await self._say(dest, "No FalconFox session owns this chat.")
                return True
            label = self._session_label(session_id)
            await self._say_html(
                dest,
                f"{html.escape(label, quote=False)} <code>"
                f"{html.escape(session_id, quote=False)}</code>",
                f"{label} {session_id}")
            return True
        if command in ("/sh", "/jobs", "/tail", "/kill"):
            await self._shell_command(dest, command, text, parts)
            return True
        if command == "/name":
            target = (self._threads.get(dest.thread)
                      if dest.thread is not None else None)
            if len(parts) < 2 or target is None:
                await self._say(dest, "Usage: /name <new name> — in a session's topic.")
                return True
            await self.daemon.rename(target, " ".join(parts[1:]))
            # The topic retitle follows from the daemon's session_updated
            # event, so it happens whoever renamed the session.
            await self._say(dest, f"Renamed session to {' '.join(parts[1:])}.")
            return True
        if command == "/tag":
            await self._tag_command(dest, parts[1:])
            return True
        if command == "/tray":
            await self._tray_command(dest, parts[1:])
            return True
        if command in ("/stop", "/unqueue", "/fullstop"):
            await self._stop_command(dest, command)
            return True
        return False

    async def _tag_command(self, dest: Dest, tags: list[str]) -> None:
        """Show this session's tags, or replace them.

        No arguments shows rather than clears, because showing is what you
        want nine times out of ten and clearing by accident is not
        recoverable from the chat. `-` is the explicit clear.
        """
        session_id = self._chat_session(dest)
        if session_id is None:
            await self._say(dest, "No FalconFox session speaks in this chat.")
            return
        if not tags:
            sessions = await self.daemon.sessions(include_hidden=True)
            current = next((item.get("tags") or [] for item in sessions
                            if item["session_id"] == session_id), [])
            await self._say(dest, self._tag_report(current, vocabulary=True))
            return
        try:
            session = await self.daemon.tag(session_id, [] if tags == ["-"] else tags)
        except ApiError as error:
            await self._say(dest, f"Could not set tags: {error}")
            return
        # The icon follows from the daemon's session_updated event, so it
        # lands whoever set the tags -- here, the CLI, or the manager.
        await self._say(dest, self._tag_report(session.get("tags") or []))

    async def _tray_command(self, dest: Dest, ids: list[str]) -> None:
        """Show this session's tray, or remove from it.

        The shape is borrowed from `/tag` and the meaning is inverted:
        `/tag` arguments replace the set, these remove from it. Removal fits
        the case that actually happens, which is dropping one bad photo out of
        five, so the help line says "remove" rather than leaving it to be
        inferred from the other command.
        """
        session_id = self._chat_session(dest)
        if session_id is None:
            await self._say(dest, "No FalconFox session speaks in this chat.")
            return
        tray = self._trays.get(session_id) or []
        if not ids:
            if not tray:
                await self._say(dest, TRAY_EMPTY)
                return
            rich = ["🗂 Waiting for your next message:"]
            plain = ["🗂 Waiting for your next message:"]
            for item in tray:
                caption = f" — {item['caption']}" if item.get("caption") else ""
                rich.append(f"<code>{html.escape(item['file_id'], quote=False)}</code> "
                            f"{html.escape(item['name'] + caption, quote=False)}")
                plain.append(f"{item['file_id']} {item['name']}{caption}")
            await self._say_html(dest, "\n".join(rich), "\n".join(plain))
            return
        # `-` deletes the bytes with no grace period. Recovering a mistaken
        # clear was considered and rejected: a deliberate `-` does not warrant
        # the complexity, and nothing here is irreplaceable -- the user still
        # has whatever they sent.
        dropped = tray if ids == ["-"] else [item for item in tray
                                             if item["file_id"] in set(ids)]
        for item in dropped:
            try:
                # Deleted rather than merely unlisted: a file dropped from the
                # tray is never going to reach the agent, so nothing is left
                # to keep.
                await self.daemon.remove_file(session_id, item["file_id"])
            except ApiError:
                log.warning("could not delete tray file %s", item["file_id"],
                            exc_info=True)
        remaining = [item for item in tray if item not in dropped]
        if remaining:
            self._trays[session_id] = remaining
        else:
            self._trays.pop(session_id, None)
        self._persist_tray()
        if not dropped:
            await self._say(dest, f"Nothing in the tray with {'that id' if len(ids) == 1 else 'those ids'}.")
            return
        await self._say(dest, f"🗑 Removed {len(dropped)} file(s). "
                        + (f"{len(remaining)} still waiting." if remaining
                           else "The tray is empty."))

    def _tag_report(self, tags: list[str], vocabulary: bool = False) -> str:
        """The session's tags, each with the glyph it draws.

        Concerned with the tags and their glyphs only. What the composed title
        looks like is visible in the topic itself, and repeating it here would
        be a second thing to keep true.

        The configured vocabulary is only for a bare `/tag`, which is the
        question "what can I set?". Repeating it after every set answered a
        question nobody asked, and it is the longest part of the message.
        """
        drawn = [tag for tag in tags if tag in self._icon_emoji]
        if tags:
            shown = "  ".join(
                f"{self._icon_emoji[tag]}{tag}" if tag in self._icon_emoji
                else tag for tag in tags)
            lines = [f"🏷 {shown}"]
        else:
            lines = ["🏷 No tags."]
        if tags and not drawn and self._icon_emoji:
            # Tags that draw nothing are perfectly ordinary, but staying quiet
            # about it reads as "it worked", and a title that did not change
            # is the thing worth saying.
            warning = "⚠️ No glyphs for these."
            if not vocabulary:
                # The bare form lists them just below, so pointing at it would
                # point at the message it is already in.
                warning += " Send /tag to see the ones that draw."
            lines.append(warning)
        if vocabulary and self._icon_emoji:
            # One per line: this is a list to read down and pick from, and
            # separator-joined it wrapped into an unreadable run.
            lines.append("")
            lines.append("Configured glyphs:")
            lines.extend(f"{glyph} {tag}"
                         for tag, glyph in self._icon_emoji.items())
        return "\n".join(lines)

    async def _stop_command(self, dest: Dest, command: str) -> None:
        """End the turn, drop the queue, or both.

        `/fullstop` exists because the pair races otherwise: after a `/stop`
        the flush is already coming, so unqueue-then-stop works while
        stop-then-unqueue is a coin flip -- not something to reason about
        mid-turn, so it gets one command that cannot be ordered wrongly.
        """
        session_id = self._chat_session(dest)
        if session_id is None:
            await self._say(dest, "No FalconFox session speaks in this chat.")
            return
        dropped = (self._drop_queue(session_id)
                   if command in ("/unqueue", "/fullstop") else [])
        if command == "/unqueue":
            await self._say(dest, f"🗑 Dropped {len(dropped)} queued message(s)."
                            if dropped else "Nothing was queued.")
            return
        if session_id not in self._turn_dest:
            # Cancelling anyway would be harmless, but claiming to have
            # stopped a turn that was not running is how a user learns to
            # distrust the feedback.
            await self._say(dest, "No turn is running."
                            + (f" Dropped {len(dropped)} queued message(s)."
                               if dropped else ""))
            return
        try:
            await self.daemon.cancel(session_id)
        except ApiError as error:
            log.warning("could not cancel the turn for %s", session_id, exc_info=True)
            await self._say(dest, f"Could not stop the turn: {error}")
            return
        log.info("turn stop requested: session=%s command=%s dropped=%d",
                 session_id, command, dropped)
        # Deliberately not "stopped": cancellation is a request, and the turn
        # ends when the daemon says so. That moment already stamps "Turn
        # cancelled" on the progress message, which is the real confirmation.
        note = "🛑 Stopping the turn…"
        if dropped:
            note += f" Dropped {len(dropped)} queued message(s)."
        await self._say(dest, note)

    # --- attachments -------------------------------------------------------

    async def _deliver_attachment(self, event: dict) -> None:
        """Send a file a session asked to hand to the user, then say so.

        The reply matters as much as the upload: `falconfox attach` waits on
        it, so a silent failure here becomes an agent that believes it sent
        something it did not.
        """
        session_id = event.get("session_id")
        source = Path(event.get("path") or "")
        dest = self._attachment_dest(session_id)
        error = None
        if dest is None:
            error = "this session has no chat to send to"
        else:
            method, field = _upload_kind(source, bool(event.get("raw")))
            try:
                await self._upload(dest, source, method, field, event.get("caption"))
                log.info("attachment sent: session=%s file=%s as=%s",
                         session_id, source, method)
            except (ApiError, OSError) as failure:
                error = str(failure)
                log.warning("attachment failed: session=%s file=%s (%s)",
                            session_id, source, failure)
        if error is not None and dest is not None:
            await self._say(dest, f"Could not send {source.name}: {error}")
        await self._report_attachment(event.get("request_id"), error)

    async def _upload(self, dest: Dest, source: Path, method: str, field: str,
                      caption: Optional[str]) -> None:
        """Upload, falling back to a plain file if the rich method refuses.

        Telegram rejects a photo whose sides sum past 10000 or whose ratio
        exceeds 20 -- which is an ordinary full-page screenshot. Failing there
        would deny the user a file that could have arrived, so the fallback
        sends it as-is rather than reporting an error.
        """
        try:
            await self.telegram.send_file(dest.chat, source, method, field,
                                          caption=caption, thread=dest.thread)
        except ApiError:
            if method == "sendDocument":
                raise
            log.info("%s refused for %s; sending as a file", method, source.name)
            await self.telegram.send_file(dest.chat, source, "sendDocument",
                                          "document", caption=caption,
                                          thread=dest.thread)

    def _attachment_dest(self, session_id: str) -> Optional[Dest]:
        """Where a session's files go: its topic, or the chat it lives in."""
        if session_id == self.concierge_session_id:
            return Dest(self.config.owner_id, None)
        if self.forum_chat_id is None:
            return None
        if session_id == self.manager_session_id:
            return Dest(self.forum_chat_id, None)
        thread = self._topics.get(session_id)
        return Dest(self.forum_chat_id, thread) if thread is not None else None

    async def _report_attachment(self, request_id: Optional[str],
                                 error: Optional[str]) -> None:
        if request_id is None or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "action": "attachment_result", "request_id": request_id,
                "ok": error is None, "error": error,
            }))
        except (ConnectionClosed, OSError):
            # Nothing to do: the daemon's wait will time out and say so, which
            # is the same answer arriving more slowly.
            log.warning("could not report attachment %s", request_id)

    # --- shell ------------------------------------------------------------

    async def _shell_command(self, dest: Dest, command: str, text: str,
                             parts: list[str]) -> None:
        if command == "/sh":
            # Deliberately not shlex-split: the argument is a command line,
            # and re-joining split tokens would quietly rewrite quoting.
            body = text.split(None, 1)[1].strip() if len(parts) > 1 else ""
            if not body:
                await self._say(dest, "Usage: /sh <command>")
                return
            await self._run_shell(dest, body)
            return
        if command == "/jobs":
            if not self._shell.jobs:
                await self._say(dest, "No jobs from this bot process. Jobs "
                                      "outlive a restart: `tmux ls` on the host "
                                      "shows any that are still open.")
                return
            listing = await self._jobs_listing()
            await self._say_block(dest, f"{len(self._shell.jobs)} job(s)", listing)
            return
        job = self._shell.jobs.get(parts[1]) if len(parts) > 1 else None
        if job is None:
            await self._say(dest, f"Usage: {command} <job id> — see /jobs.")
            return
        if command == "/tail":
            await self._say_job(dest, job)
            return
        killed = await self._shell.kill(job)
        await self._say(dest, f"Killed {job.job_id}." if killed
                        else f"Could not kill {job.job_id}; it may have finished.")

    async def _run_shell(self, dest: Dest, body: str) -> None:
        cwd = await self._shell_cwd(dest)
        try:
            job = await self._shell.start(body, cwd)
        except (TmuxMissing, RuntimeError, OSError) as error:
            await self._say(dest, f"Could not start the command: {error}")
            return
        log.info("shell job=%s cwd=%s command=%s", job.job_id, cwd, body)
        await self._shell.wait(job)
        await self._say_job(dest, job)

    async def _shell_cwd(self, dest: Dest) -> Path:
        """A session's topic runs in that session's directory.

        Resolved from the daemon, and falling back to the default path when it
        cannot be reached -- a wedged daemon is precisely when /sh is worth
        having, so it must not be what stops a command from running.
        """
        session_id = self._threads.get(dest.thread) if dest.thread is not None else None
        if session_id is not None:
            try:
                meta = await self.daemon.session(session_id)
            except ApiError:
                log.warning("could not read the cwd for session %s", session_id)
            else:
                path = meta.get("path")
                if path:
                    return Path(path)
        return Path(self.config.default_path)

    async def _say_job(self, dest: Dest, job) -> None:
        """Report a job: a plain header, then its output as a code block.

        Output is monospace for the reason any terminal is: a command's
        alignment carries meaning, and Telegram's proportional font destroys
        it. It also stops output that happens to contain markup from being
        read as formatting.
        """
        status = job.read_status()
        header = [f"{self._job_mark(job, status)} — {job.cwd}", f"$ {job.command}"]
        if status is None:
            header.append(f"/tail {job.job_id} · /kill {job.job_id} · "
                          f"tmux attach -t {job.session}")
        # The budget is what is left of the message once the header is paid
        # for. Markup is free: measured against the live API, the 4096 limit
        # counts a message's rendered length, so the <pre> wrapper costs
        # nothing and an escaped `&amp;` in the output counts as the one
        # character it draws. This used to be budgeted after escaping, which
        # clipped output that would have fit.
        spent = len("\n".join(header)) + 80
        body, clipped = tail(job.read_output(), TELEGRAM_MESSAGE_LIMIT - spent)
        if clipped:
            header.append(f"(tail only; whole output in {job.log_path})")
        await self._say_block(dest, "\n".join(header), body or "(no output)")

    def _job_mark(self, job, status: Optional[int]) -> str:
        if status is None:
            return f"⏳ {job.job_id} still running ({job.elapsed:.0f}s)"
        return f"{'✅' if status == 0 else '❌'} {job.job_id} exited {status}"

    async def _say_html(self, dest: Dest, rich: str, plain: str,
                        reply_to: int | None = None) -> None:
        """Send pre-built HTML, with the plain text to fall back to. Both are
        the caller's: only it knows which parts of the line are markup."""
        await self.telegram.html_message(dest.chat, rich, plain, reply_to=reply_to,
                                         thread=dest.thread)

    async def _say_block(self, dest: Dest, header: str, body: str) -> None:
        """Send `header` as text and `body` as a code block.

        Built directly rather than through render_messages: the body is
        arbitrary command output, and handing it to a markdown renderer would
        let a stray fence or asterisk in it decide the formatting.
        """
        await self.telegram.html_message(
            dest.chat,
            f"{html.escape(header, quote=False)}\n<pre>{html.escape(body, quote=False)}</pre>",
            f"{header}\n{body}",
            thread=dest.thread)

    async def _sessions_listing(self) -> tuple[str, str]:
        """Every session as (html, plain), most recently active first.

        The id is a <code> span of its own and the rest of the line is not:
        an id exists to be pasted into another command, and Telegram makes a
        code span tap-to-copy while leaving the surrounding text alone. A
        whole-line block would copy the name and the path along with it,
        which is the thing being fixed.

        Ordering is by activity because the listing is capped and a cap has
        to drop something. The session untouched for a week is a better loss
        than the one being worked in right now.
        """
        sessions = await self.daemon.sessions()
        if not sessions:
            return "No sessions.", "No sessions."
        entries = [
            self._session_entry(item)
            for item in sorted(sessions,
                               key=lambda item: item.get("last_active") or "",
                               reverse=True)
        ]
        # Whole entries are dropped, never characters. Telegram counts a
        # message's rendered length -- markup is free -- so the budget is the
        # plain line's, and cutting mid-entry could only ever split a tag.
        budget = TELEGRAM_MESSAGE_LIMIT - LISTING_OVERFLOW_BUDGET
        kept, spent = [], 0
        for entry in entries:
            if spent + len(entry[1]) + 1 > budget:
                break
            kept.append(entry)
            spent += len(entry[1]) + 1
        rich = [entry[0] for entry in kept]
        plain = [entry[1] for entry in kept]
        dropped = len(entries) - len(kept)
        if dropped:
            note = f"…and {dropped} more, least recently active."
            rich.append(note)
            plain.append(note)
        return "\n".join(rich), "\n".join(plain)

    def _session_entry(self, item: dict) -> tuple[str, str]:
        """One session's line, formatted twice: once with the id marked up,
        once as the plain text that has to survive an HTML send failing."""
        session_id = item["session_id"]
        age = _format_age(item.get("last_active"))
        rest = f" {item['name']} [{item['state']}]"
        if age:
            rest += f" · {age}"
        rest += f" · {item['path']}"
        return (f"<code>{html.escape(session_id, quote=False)}</code>"
                f"{html.escape(rest, quote=False)}",
                f"{session_id}{rest}")

    async def _jobs_listing(self) -> str:
        live = await self._shell.live_sessions()
        lines = []
        for job in self._shell.jobs.values():
            status = job.read_status()
            state = "running" if status is None else f"exited {status}"
            if job.session not in live:
                # The pane is gone, so there is nothing left to attach to --
                # worth saying, since the id still reads as usable otherwise.
                state += ", pane gone"
            lines.append(f"{job.job_id}  [{state}]  {job.cwd}  $ {job.command}")
        return "\n".join(lines)

    async def _clear_chat_session(self, dest: Dest) -> None:
        """Start General or the private chat over with a fresh session.

        Deleting and respawning rather than truncating in place: a new session
        is told what it is running inside on its first message, which is the
        one piece of context a cleared session should still have.

        Only for the two infrastructure chats. A work session's conversation
        is the work, and clearing one from the chat would be a delete with a
        gentler name.
        """
        session_id = self._chat_session(dest)
        if session_id is None or session_id not in (self.manager_session_id,
                                                    self.concierge_session_id):
            await self._say(dest, "/clear is only for General and the private "
                                  "chat. To start a session over, spawn a new one.")
            return
        manager = session_id == self.manager_session_id
        try:
            await self.daemon.delete(session_id)
        except ApiError as error:
            await self._say(dest, f"Could not clear this session: {error}")
            return
        log.info("cleared %s session %s",
                 "manager" if manager else "concierge", session_id)
        if manager:
            self.manager_session_id = None
        else:
            self.concierge_session_id = None
        self._persist_infra()
        fresh = await (self._ensure_manager() if manager else self._ensure_concierge())
        if fresh is None:
            await self._say(dest, "Cleared, but the replacement session could "
                                  "not be started. Try again in a moment.")
            return
        await self._say(dest, f"Cleared. Everything said here before is gone, "
                              f"and this is a new session ({fresh}).")

    def _chat_session(self, dest: Dest) -> Optional[str]:
        """Which session speaks in this chat: a topic's, or the chat's own."""
        if dest.chat == self.config.owner_id:
            return self.concierge_session_id
        if dest.thread is None:
            return self.manager_session_id
        return self._threads.get(dest.thread)

    def _session_label(self, session_id: str) -> str:
        """What to call this session in front of its id. Every label ends
        ready for one: the id follows on the same line now, not underneath."""
        if session_id == self.manager_session_id:
            return "The session manager:"
        if session_id == self.concierge_session_id:
            return "The private chat:"
        return f"Session {self._topic_names.get(session_id) or session_id}:"

    async def _status_report(self) -> str:
        try:
            version = (await self.daemon.version()).get("version")
        except Exception:
            version = "daemon unreachable"
        sessions = await self.daemon.sessions()
        names = {item["session_id"]: item["name"] for item in sessions}
        lines = [f"FalconFox {version}"]
        lines.append(f"Forum: {self.forum_chat_id} — "
                     f"{len(self._topics)} topic(s) bound")
        for item in sessions:
            thread = self._topics.get(item["session_id"])
            where = "General" if item["session_id"] == self.manager_session_id else (
                f"topic {thread}" if thread is not None else "no topic")
            lines.append(f"  {item['session_id']}  {item['name']}  "
                         f"[{item['state']}]  {where}")
        if not self._turn_dest:
            lines.append("No turn in flight (bot view).")
        else:
            lines.append("In flight (bot view):")
            now = time.monotonic()
            for session_id, turn_dest in self._turn_dest.items():
                chat = ("General" if turn_dest.thread is None
                        else f"topic {turn_dest.thread}")
                started = self._turn_started_at.get(session_id)
                age = f"{now - started:.0f}s ago" if started is not None else "unknown"
                buffered = sum(len(part) for part in self._reply_parts.get(session_id, []))
                last = self._last_event_at.get(session_id, started)
                quiet = f"{now - last:.0f}s" if last is not None else "?"
                lines.append(
                    f"  {names.get(session_id, session_id)}: chat={chat} "
                    f"turn={self._turn_id.get(session_id) or '?'} "
                    f"activity={self._activity_state.get(session_id) or '?'} "
                    f"buffered={buffered} delivered={self._delivered.get(session_id, 0)} "
                    f"quiet={quiet} started {age} "
                    f"queued={len(self._queues.get(session_id, []))}")
        return "\n".join(lines)

    def _start_activity(self, session_id: str, dest: Dest) -> None:
        """Ensure both refresh loops are running.

        Two tasks, not one, and that is the whole point of them being here
        twice (found by dogfooding, 2026-09-12). They used to share a task,
        which meant they shared a stall: a Telegram call from this host can
        hang until its read timeout, and while the progress edit hung, the
        chat action behind it in the same loop never went out. The indicator
        died for forty seconds at a time and nothing anywhere said why.

        Neither loop can now stop the other. Each still survives its own
        failures, which is what `task.done()` is for below: a finished task is
        still *in* the dict, and reading membership as "alive" is how one
        failed call used to silence a turn permanently.
        """
        for tasks, loop in ((self._activity_tasks, self._activity_loop),
                            (self._progress_tasks, self._progress_loop)):
            task = tasks.get(session_id)
            if task is not None and not task.done():
                continue
            tasks[session_id] = asyncio.create_task(loop(session_id, dest))

    async def _set_activity(self, session_id: str, state: str) -> None:
        """Record what the session is doing, and keep the indicator alive.

        Recording only. A state change used to send its own chat action,
        because the action named the state and a change meant a different
        word; with one action for the whole turn there is nothing new to
        send, and the loop's own 4-second tick is the only thing that has to
        keep running. The state itself still matters -- /status reports it.

        That also takes this call off the Telegram path entirely, which is
        where it wanted to be: it sits on the event pipeline, and one hung
        send here stalled every queued daemon event behind it (observed live
        2026-08-25, 09:06: a 40-second read timeout delayed a finished reply
        by 45 seconds).
        """
        if session_id not in self._turn_dest:
            return
        # None is a real destination (General), so membership is the test --
        # a `.get() is None` guard here would silently mute the manager topic.
        dest = self._turn_dest[session_id]
        # Unconditional, so it doubles as the safety net that revives a loop
        # which died mid-turn -- either of them.
        self._start_activity(session_id, dest)
        self._activity_state[session_id] = state

    def _close_block(self, session_id: str) -> None:
        """A tool call has interrupted the text: what came before it is
        narration, not the answer. Move it to the progress message."""
        raw = "".join(self._reply_parts.get(session_id, []))
        if not raw:
            return
        self._reply_parts[session_id] = []
        self._consumed[session_id] = self._consumed.get(session_id, 0) + len(raw)
        if raw.strip():
            self._progress_lines.setdefault(session_id, []).append(raw.strip())
            self._progress_dirty.add(session_id)
        self._persist_turns()

    def _close_thought(self, session_id: str) -> None:
        """A thought has ended (text or a tool call followed it): show its
        head in the progress message. Thoughts never touch the reply buffer or
        the consumed offset -- they are not part of the transcript's agent
        text, so recovery arithmetic must not know about them."""
        raw = "".join(self._thought_parts.pop(session_id, []))
        preview = " ".join(raw.split())
        if not preview:
            return
        if len(preview) > THOUGHT_PREVIEW_CHARS:
            preview = preview[:THOUGHT_PREVIEW_CHARS].rstrip() + " …"
        self._progress_lines.setdefault(session_id, []).append(f"💭 {preview}")
        self._progress_dirty.add(session_id)
        self._persist_turns()

    def _add_tool_marker(self, session_id: str, title: str) -> None:
        """One compact line per tool call, consecutive repeats collapsed."""
        lines = self._progress_lines.setdefault(session_id, [])
        marker = f"⚙️ {title}"
        if lines and lines[-1] == marker:
            lines[-1] = f"{marker} ×2"
        elif lines and lines[-1].startswith(f"{marker} ×"):
            lines[-1] = f"{marker} ×{int(lines[-1].rsplit('×', 1)[1]) + 1}"
        else:
            lines.append(marker)
        self._progress_dirty.add(session_id)

    def _progress_header(self, session_id: str) -> str:
        """The live header: what the turn is doing, for how long, and what is
        waiting behind it. Rebuilt on every tick, and its text is what decides
        whether the tick spends an edit."""
        started = self._turn_started_at.get(session_id)
        header = PROGRESS_HEADER
        if started is not None:
            header += f" ({_format_elapsed(time.monotonic() - started)})"
        queued = len(self._queues.get(session_id, ()))
        if queued:
            header += f" · {PROGRESS_QUEUED.format(count=queued)}"
        return header

    async def _update_progress(self, session_id: str, dest: Dest, *,
                               final_note: str | None = None) -> None:
        """Create or edit the turn's progress message. Rides the activity loop
        (and the turn's finalization), never the event pipeline: a hung
        Telegram call here must not stall queued daemon events. Edits do not
        notify, so a muted chat stays quiet through any amount of progress."""
        if final_note is None and session_id not in self._turn_dest:
            # The turn is over. Cancelling the activity loop is not
            # instantaneous: a tick already inside an HTTP call finishes it,
            # and would create a fresh "Working…" *after* the reply had
            # landed. `final_note` is the finalization itself, which runs
            # after the destination is popped, so it is exempt.
            return
        header = final_note or self._progress_header(session_id)
        if final_note is None and session_id not in self._progress_dirty:
            sent, sent_at = self._progress_sent.get(session_id, ("", 0.0))
            if header == sent:
                # The common tick: nothing new, and not even the clock has
                # moved on from what is already on screen.
                return
            if time.monotonic() - sent_at < PROGRESS_CLOCK_SECONDS:
                # Only the clock moved, and it moved recently enough that this
                # tick has nothing worth an edit. Anything with content to show
                # marks itself dirty and never reaches here.
                return
        lines = self._progress_lines.get(session_id) or []
        message_id = self._progress_msg.get(session_id)
        # Nothing accumulated and nothing on screen to stamp: stay silent. (A
        # normal turn has a message from _forward; this guards turns primed by
        # other paths, e.g. adopted ones whose creation failed.) A message that
        # does exist is edited even with no lines under it, because the header
        # alone is the whole point on a turn that has narrated nothing yet.
        if not lines and message_id is None:
            return
        self._progress_dirty.discard(session_id)
        self._progress_sent[session_id] = (header, time.monotonic())
        text = "\n".join([header, "", *lines]) if lines else header
        while len(text) > PROGRESS_LIMIT and len(lines) > 1:
            del lines[0]
            text = "\n".join([header, "", "… (earlier progress trimmed)", *lines])
        try:
            if message_id is None:
                message_id = await self._say(dest, text, silent=True)
                if message_id is not None:
                    self._progress_msg[session_id] = message_id
                    self._persist_turns()
            else:
                await self.telegram.edit_message(dest.chat, message_id, text)
        except ApiError as error:
            # Progress is decoration; a failed update waits for the next tick.
            # The recorded header goes with it: nothing reached the screen, so
            # remembering what was sent would silence the retry.
            self._progress_dirty.add(session_id)
            self._progress_sent.pop(session_id, None)
            log.debug("progress update failed for %s: %s", session_id, error)

    async def _replace_topic(self, session_id: str) -> Optional[int]:
        """The session's topic is gone: unbind it and make a fresh one.

        The reactive fix the buglist sketched and deferred. It cannot be
        proactive -- `getForumTopic` and `getForumTopics` do not exist, so
        there is nothing to reconcile a binding against -- and it cannot be
        reported by Telegram either, since a deleted topic sends no service
        message. A send that comes back "thread not found" is the whole of
        the evidence available.
        """
        self._unbind(session_id)
        try:
            session = await self.daemon.session(session_id)
        except ApiError:
            # Only the title suffers: a topic named after the id is worse than
            # one named after the session, and far better than no topic.
            log.warning("could not read session %s while replacing its topic",
                        session_id, exc_info=True)
            session = {}
        return await self._ensure_topic({**session, "session_id": session_id})

    async def _send_for_turn(self, session_id: str, dest: Dest, send) -> Dest:
        """Send on a session's behalf, replacing a topic that has gone.

        `send` takes the destination rather than closing over it, because the
        retry goes somewhere else: a new topic, made here, and answered with
        for the rest of the turn.

        Only the "thread not found" class unbinds. A rate limit or a read
        timeout says nothing about the topic, and acting on one would throw a
        live topic away and start a second beside it.
        """
        try:
            await send(dest)
            return dest
        except ApiError as error:
            if (dest.thread is None or self._topics.get(session_id) != dest.thread
                    or TOPIC_GONE not in str(error).lower()):
                raise
            gone = error
        log.warning("topic %s for session %s is gone (%s); replacing it",
                    dest.thread, session_id, gone)
        thread = await self._replace_topic(session_id)
        if thread is None:
            # `_create_topic` has already told the owner in the private chat
            # that the forum is unusable. The original failure is still the
            # caller's to hear about.
            raise gone
        dest = Dest(self.forum_chat_id, thread)
        await self._say(dest, TOPIC_REPLACED)
        await send(dest)
        return dest

    async def _send_reply(self, session_id: str, dest: Dest) -> Dest:
        """Deliver the turn's answer: the text after the last tool call,
        threaded to the prompt that asked for it. Answers with where it
        actually landed, which is a new topic when the old one had gone."""
        raw = "".join(self._reply_parts.get(session_id, []))
        self._reply_parts[session_id] = []
        self._consumed[session_id] = self._consumed.get(session_id, 0) + len(raw)
        text = raw.strip()
        if not text:
            # The agent said its piece before a trailing tool call, so the
            # last narration paragraph is the closest thing to an answer.
            # It is already visible in the progress message, but the reply
            # is what threads -- and what pings through a muted chat.
            text = next((line for line in reversed(
                self._progress_lines.get(session_id, []))
                if not line.startswith("⚙️")), "")
        if not text:
            return dest
        log.info("reply: session=%s dest=%s chars=%d", session_id, dest, len(text))
        prompt_msg = self._prompt_msg.get(session_id)
        for index, rendered in enumerate(render_messages(text)):

            async def deliver(where: Dest, rendered=rendered, index=index) -> None:
                # The prompt lives in the old topic when this is a retry, and
                # `allow_sending_without_reply` is what makes that harmless.
                await self.telegram.html_message(
                    where.chat, rendered.html, rendered.plain,
                    reply_to=prompt_msg if index == 0 else None, thread=where.thread)

            dest = await self._send_for_turn(session_id, dest, deliver)
        self._delivered[session_id] = self._delivered.get(session_id, 0) + len(text)
        self._persist_turns()
        return dest

    async def _send_action(self, session_id: str, dest: Dest) -> None:
        if session_id not in self._turn_dest:
            # Same race as the progress message: cancelling the loop is not
            # instantaneous, and a tick already inside its HTTP call finishes
            # it, so a stale one would show "typing…" after the answer landed.
            return
        try:
            await self.telegram.chat_action(dest.chat, TURN_ACTION,
                                            thread=dest.thread)
        except ApiError as error:
            # Never fatal to the loop. A 429 from the rate limiter -- likeliest
            # on exactly the long turn that needs an indicator -- or one of the
            # read timeouts this deployment sees used to end the task outright
            # and leave the turn silent for the rest of its life.
            log.debug("chat action failed for %s: %s", session_id, error)

    async def _enqueue_message(self, session_id: str, dest: Dest, text: str,
                               prompt_msg: int | None = None) -> None:
        """Hold a mid-turn message and say that it is held."""
        queue = self._queues.setdefault(session_id, [])
        queue.append({"text": text, "message_id": prompt_msg})
        self._persist_turns()
        log.info("queued mid-turn message: session=%s dest=%s depth=%d",
                 session_id, dest, len(queue))
        # The depth is in the header, and it is the receipt for something the
        # user just did, so it goes out on the next tick rather than waiting
        # for the clock's turn to come round.
        self._progress_dirty.add(session_id)
        if len(queue) == 1:
            await self._say(dest, QUEUED_FIRST, reply_to=prompt_msg)

    def _drop_queue(self, session_id: str) -> list[dict]:
        dropped = self._queues.pop(session_id, [])
        if dropped:
            self._persist_turns()
            # Same as queueing: the header's count answers for this, so it
            # must not sit at a depth that is no longer true.
            self._progress_dirty.add(session_id)
        return dropped

    async def _flush_queue(self, session_id: str, dest: Dest) -> None:
        """Send what was queued, as one prompt.

        Consecutive messages on a phone are usually one thought split by the
        send button, so they are joined rather than run as separate turns.
        The reply threads to the last of them, which is the one still on
        screen.

        Called only from the end of a turn -- by design, since that is the one
        moment the daemon will accept a prompt again, and it makes a stopped
        turn and a finished one take the same path.
        """
        queued = self._queues.pop(session_id, [])
        if not queued:
            return
        self._persist_turns()
        text = "\n\n".join(item["text"] for item in queued)
        log.info("flushing queue: session=%s messages=%d chars=%d",
                 session_id, len(queued), len(text))
        # They are one prompt now, and the last of them is its address: the
        # turn threads its reply there.
        await self._forward(session_id, dest, text,
                            prompt_msg=queued[-1].get("message_id"))

    async def _forward(self, session_id: str, dest: Dest, text: str,
                       prompt_msg: int | None = None) -> None:
        if session_id in self._turn_dest:
            # The daemon refuses a prompt while a turn is running, and says so
            # with an *info* notice -- which this client does not surface, so
            # the message vanished without a trace. Forwarding it anyway was
            # worse: it reset the buffers below and destroyed the reply
            # already in flight. So it is kept here instead, and sent when the
            # turn ends -- retyping on a phone is the thing this exists to
            # avoid.
            await self._enqueue_message(session_id, dest, text, prompt_msg)
            return
        carried = self._sweep_tray(session_id)
        if carried:
            # Leading the message rather than trailing it: the files are what
            # the message is about, so they read as its context. One line each,
            # in arrival order, each caption on its own line rather than merged
            # into the user's words.
            text = "\n".join(_attached_line(item) for item in carried) + "\n\n" + text
            log.info("tray swept: session=%s files=%d", session_id, len(carried))
        log.info("forward: session=%s dest=%s chars=%d", session_id, dest, len(text))
        self._turn_dest[session_id] = dest
        self._reply_parts[session_id] = []
        self._delivered[session_id] = 0
        self._consumed[session_id] = 0
        if prompt_msg is not None:
            self._prompt_msg[session_id] = prompt_msg
        self._progress_lines.pop(session_id, None)
        self._progress_msg.pop(session_id, None)
        self._progress_sent.pop(session_id, None)
        self._seen_tools.pop(session_id, None)
        self._thought_parts.pop(session_id, None)
        self._turn_started_at[session_id] = time.monotonic()
        self._last_event_at[session_id] = time.monotonic()
        self._turn_working.discard(session_id)
        self._persist_turns()
        # Type from the moment the prompt goes out. Waiting for the daemon to
        # report `working` leaves the whole backend-startup window silent: a
        # stored session resumes an ACP subprocess first, and the daemon carries
        # that as a `starting` state on session_updated, which this client does
        # not consume. That gap is exactly when a turn looks like it hung.
        await self._set_activity(session_id, "working")
        async with self._ws_lock:
            await self._ws.send(json.dumps({
                "action": "send", "session_id": session_id, "text": text,
            }))
        # The progress message exists from the first moment of the turn (user
        # decision, 2026-08-25) -- sent after the prompt so a slow Telegram
        # call never delays the actual work, and silently: progress is
        # ambient, only the response should ping.
        try:
            header = self._progress_header(session_id)
            message_id = await self._say(dest, header, silent=True)
            if message_id is not None:
                self._progress_msg[session_id] = message_id
                self._progress_sent[session_id] = (header, time.monotonic())
                self._persist_turns()
        except ApiError as error:
            log.debug("could not create the progress message: %s", error)

    async def _receive_events(self) -> None:
        """Read the daemon's event stream. Handling one event is total: its
        failure is that event's alone.

        The same shape as `_poll_telegram`, and for the same reason. An
        exception here ended the loop, `asyncio.wait` reported it to
        `_run_connected`, and the connection came down -- taking every other
        session's turn with it. A Telegram send that refuses is the likeliest
        way to get one, and it says nothing whatever about the daemon.
        """
        async for raw in self._ws:
            event = {}
            try:
                event = json.loads(raw)
                await self._handle_event(event)
            except Exception:
                log.warning("dropping a daemon event that could not be handled:"
                            " session=%s type=%s", event.get("session_id"),
                            event.get("type"), exc_info=True)

    async def _handle_event(self, event: dict) -> None:
        session_id = event.get("session_id")
        if not session_id:
            return
        # Any event is a sign of life, and /status reports how long ago the
        # last one was.
        self._last_event_at[session_id] = time.monotonic()
        event_type = event.get("type")
        if event_type == "message":
            role = event.get("role")
            if role == "agent":
                # Text ends a thought; flush its preview first so the progress
                # lines keep the stream's order.
                self._close_thought(session_id)
                self._reply_parts.setdefault(session_id, []).append(event.get("text", ""))
                await self._set_activity(session_id, "streaming")
            elif role == "thought":
                # Never part of the reply; its head joins the progress message
                # when the thought ends.
                if session_id in self._turn_dest:
                    self._thought_parts.setdefault(session_id, []).append(
                        event.get("text", ""))
                await self._set_activity(session_id, "thinking")
            return
        if event_type == "usage":
            view = self._usage_view.setdefault(session_id, {})
            for key, value in event.items():
                if key not in ("type", "session_id", "ts") and value is not None:
                    view[key] = value
            return
        if event_type == "tool_call":
            # A tool call is a block boundary: the text before it was written
            # to introduce it, which makes it narration for the progress
            # message, not part of the answer. The call itself becomes one
            # compact line there -- never a message of its own, which is the
            # part of "tool calls stay suppressed" that still stands.
            status = event.get("status")
            if session_id in self._turn_dest:
                tool_id = event.get("tool_call_id")
                seen = self._seen_tools.setdefault(session_id, set())
                if tool_id is None or tool_id not in seen:
                    if tool_id is not None:
                        seen.add(tool_id)
                    # Stream order: any pending text predates any pending
                    # thought (text arriving closes thoughts), so close in
                    # that order before the marker.
                    self._close_block(session_id)
                    self._close_thought(session_id)
                    self._add_tool_marker(session_id, event.get("title")
                                          or event.get("tool_kind") or "tool")
            await self._set_activity(
                session_id, "working" if status in ("completed", "failed") else "tool")
            return
        if event_type == "attachment":
            await self._deliver_attachment(event)
            return
        if event_type == "session_added":
            await self._ensure_topic(event)
            return
        if event_type == "session_removed":
            self._forget_tray(session_id)
            thread = self._unbind(session_id)
            if thread is not None:
                try:
                    await self.telegram.delete_topic(self.forum_chat_id, thread)
                except ApiError:
                    log.warning("could not delete topic %s for removed session %s",
                                thread, session_id, exc_info=True)
            return
        if event_type == "session_updated":
            await self._mirror_session(event)
            # The daemon carries a resuming ACP subprocess as `starting`, the
            # slowest part of a cold turn. The client used to ignore this event
            # entirely, so that whole window looked identical to working.
            if event.get("state") == "starting":
                await self._set_activity(session_id, "starting")
            return
        if event_type == "notice" and event.get("kind") == "capacity":
            # Capacity notices are about the session itself rather than a
            # turn, so they go to its topic whether or not it is mid-turn --
            # and a closed topic still accepts bot writes, so this lands even
            # when it follows the close.
            thread = self._topics.get(session_id)
            if thread is not None and self.forum_chat_id is not None:
                await self._say(Dest(self.forum_chat_id, thread),
                                f"⏸ {event.get('message', '')}")
            return
        if event_type == "notice" and event.get("level") == "error":
            if session_id in self._turn_dest:
                await self._say(
                    self._turn_dest[session_id],
                    f"FalconFox error: {event.get('message', '')}",
                    reply_to=self._prompt_msg.get(session_id))
            return
        if event_type == "turn_started":
            # The daemon's own name for the turn this chat is waiting on. Turns
            # driven by other clients (the focus agent's CLI sends, the web UI)
            # have no chat here and are none of our business.
            if session_id in self._turn_dest:
                self._turn_id[session_id] = event.get("turn_id") or ""
                self._persist_turns()
                log.info("turn started: session=%s turn=%s", session_id, event.get("turn_id"))
            return
        if event_type == "turn_ended":
            # The authoritative end of a turn. `idle` below stays only as a
            # backstop — it is a state, not an event, and reading it as "turn
            # over" is how replies used to vanish.
            await self._finish_turn(session_id, event)
            return
        if event_type != "agent_state":
            return
        state = event.get("state")
        if state == "working":
            # Normally already active since _forward; this covers a turn that
            # began before the indicator did, and revives a loop that has died.
            self._turn_working.add(session_id)
            await self._set_activity(session_id, "working")
            return
        if state != "idle":
            return
        if (session_id in self._turn_dest and session_id not in self._turn_working
                and not self._reply_parts.get(session_id)):
            # Resuming a stored session emits `idle` *before* the turn starts
            # (engine/session.py sets it once the ACP subprocess is up). Treating
            # that as the end of the turn tore down _turn_chat before a single
            # chunk had arrived, so the real reply streamed into a session with
            # nowhere to send it and was dropped in silence -- every first turn
            # after a daemon restart. A turn ends only if it ever began.
            #
            # The empty-buffer condition is the safety catch: if anything has
            # streamed, the turn plainly began, so an idle ends it whatever the
            # state flags say. Without it, one confused flag strands the session
            # forever -- observed live, with the indicator left running for 54
            # minutes and every later message refused.
            log.info("ignoring pre-turn idle for session=%s", session_id)
            return
        # Normally a no-op: turn_ended has already finalized, and _finish_turn
        # is idempotent. Kept so a daemon that never sent one (or a turn whose
        # end this client somehow missed) still cannot strand the session.
        await self._finish_turn(session_id, None)

    async def _mirror_session(self, session: dict) -> None:
        """Keep a session's topic titled like the session.

        Only a real change acts, because `session_updated` arrives constantly.

        Eviction deliberately does NOT close the topic: `send` auto-resumes a
        stored session, so closing would discourage the very action that
        recovers it, and the closed state needed bookkeeping that outlived a
        bot restart badly (a reopen owed but forgotten left topics shut for
        good). The capacity notice already says what happened.
        """
        session_id = session.get("session_id")
        thread = self._topics.get(session_id)
        if thread is None:
            return
        await self._apply_title(session, thread)
        await self._apply_icon(session, thread)

    async def _apply_title(self, session: dict, thread: int) -> bool:
        """Mirror the composed title onto a topic, if it has changed.

        Returns whether a call was actually made, because the reconciler
        paces itself on work done rather than on topics seen.
        """
        session_id = session.get("session_id")
        title = self._title_for(session)
        if not title or self._topic_names.get(session_id) == title:
            return False
        try:
            await self.telegram.rename_topic(self.forum_chat_id, thread, title)
        except ApiError as error:
            if TOPIC_UNCHANGED not in str(error):
                log.warning("could not retitle topic %s", thread, exc_info=True)
                return True
            # Already titled that; remembering it is the whole point.
            log.info("topic %s already had the title asked for", thread)
        self._topic_names[session_id] = title
        self._persist_topics()
        return True

    async def _finish_turn(self, session_id: str, event: dict | None) -> None:
        """Close out a turn: deliver the remainder, stop the indicator, account
        for what was handed over — and say so when that is nothing. Idempotent:
        the `idle` that follows a `turn_ended` finds nothing left to do."""
        self._turn_working.discard(session_id)
        running = [task for task in (self._activity_tasks.pop(session_id, None),
                                     self._progress_tasks.pop(session_id, None))
                   if task is not None]
        had_turn = session_id in self._turn_dest
        dest = self._turn_dest.pop(session_id, None)
        for task in running:
            # Awaited, not merely cancelled: cancellation lands at the task's
            # next await, so an unawaited cancel leaves a tick still in flight
            # while the reply is being sent. Popping the destination first
            # means anything that does slip through finds the turn ended.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._activity_state.pop(session_id, None)
        turn_id = self._turn_id.pop(session_id, None) or (event or {}).get("turn_id")
        started = self._turn_started_at.pop(session_id, None)
        outcome = (event or {}).get("outcome")
        stop = (event or {}).get("stop_reason")
        elapsed = time.monotonic() - started if started is not None else -1.0
        if had_turn:
            if session_id in self._adopted:
                # The buffer holds only what streamed after adoption; the
                # transcript holds the whole turn. Rebuild the undelivered
                # remainder from the settled transcript -- the turn is over,
                # so there is no race with chunks still in flight.
                text = await self._turn_text_from_transcript(session_id)
                if text is not None:
                    self._reply_parts[session_id] = [
                        text[self._consumed.get(session_id, 0):]]
                else:
                    log.warning("adopted turn %s: transcript unavailable; "
                                "delivering the post-adoption tail only", session_id)
            # Stamp the progress message and leave it standing (user decision:
            # the chain of work stays in the chat), then deliver the answer.
            self._close_thought(session_id)
            tools = len(self._seen_tools.get(session_id, ()))
            if outcome == "error":
                note = "⚠️ Turn ended with an error"
            elif stop == "cancelled":
                note = "✖️ Turn cancelled"
            else:
                note = "✅ Turn finished"
            if elapsed >= 0:
                note += f" · {_format_elapsed(elapsed)}"
            if tools:
                note += f" · {tools} tool calls"
            usage = self._usage_view.get(session_id) or {}
            tokens = usage.get("total_tokens") or usage.get("output_tokens")
            if tokens:
                note += f" · {_format_count(tokens)} tokens"
            elif usage.get("used") and usage.get("size"):
                note += (f" · ctx {_format_count(usage['used'])}"
                         f"/{_format_count(usage['size'])}")
            await self._update_progress(session_id, dest, final_note=note)
            # The reply may have had to make a new topic on the way out; what
            # follows it -- the silent-turn notice, the queue flush -- belongs
            # in the topic that exists rather than the one that did.
            dest = await self._send_reply(session_id, dest)
        delivered = self._delivered.pop(session_id, 0)
        self._consumed.pop(session_id, None)
        self._adopted.discard(session_id)
        self._last_event_at.pop(session_id, None)
        self._reply_parts.pop(session_id, None)
        prompt_msg = self._prompt_msg.pop(session_id, None)
        self._progress_msg.pop(session_id, None)
        self._progress_sent.pop(session_id, None)
        self._progress_lines.pop(session_id, None)
        self._progress_dirty.discard(session_id)
        self._seen_tools.pop(session_id, None)
        self._thought_parts.pop(session_id, None)
        self._persist_turns()
        if had_turn:
            log.info("turn ended: session=%s turn=%s outcome=%s stop=%s "
                     "delivered=%d chars in %.1fs",
                     session_id, turn_id, outcome, stop, delivered, elapsed)
            if delivered == 0 and outcome != "error" and stop != "cancelled":
                # An errored turn already surfaced its error notice, and a
                # cancelled one is empty on purpose. Anything else that ends
                # with nothing delivered is the silent failure this client
                # kept producing -- so it stops being silent, in both places.
                streamed = (event or {}).get("output_chars")
                if streamed:
                    detail = (f"the agent wrote {streamed} characters "
                              "that were lost on the way to this chat")
                else:
                    detail = f"the agent produced no output; stop reason: {stop or 'unknown'}"
                log.warning("turn delivered nothing: session=%s turn=%s %s",
                            session_id, turn_id, detail)
                await self._say(dest, SILENT_TURN.format(detail=detail),
                                            reply_to=prompt_msg)
        # Last, and outside the had_turn branch: a queue drains whenever a turn
        # ends, however it ended. /stop does not flush anything itself -- it
        # ends the turn, and this is what ending a turn does.
        if dest is not None:
            await self._flush_queue(session_id, dest)

    async def _activity_loop(self, session_id: str, dest: Dest) -> None:
        """Keep "typing…" alive. Nothing else belongs in here: this is the one
        signal that says the turn is not dead, and every await added to it is
        another way for it to stop saying so."""
        try:
            while True:
                await self._send_action(session_id, dest)
                await asyncio.sleep(ACTION_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _progress_loop(self, session_id: str, dest: Dest) -> None:
        """Keep the progress message current. Slower than it looks: most ticks
        find nothing to do, since content marks itself dirty and the clock is
        paced apart again (see `_update_progress`)."""
        try:
            while True:
                await self._update_progress(session_id, dest)
                await asyncio.sleep(ACTION_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
