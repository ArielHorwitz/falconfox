"""Two-channel Telegram client for FalconFox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import NamedTuple

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# Diagnostic machinery, not daemon protocol: the 2026-08-25 keepalive stalls
# could not even be attributed to a side, because neither process recorded its
# own freezes. Both run the same watchdog; sharing it crosses no boundary.
from falconfox.watchdog import StallWatchdog

from .api import ApiError, DaemonApi, TelegramApi
from .rendering import render_messages

log = logging.getLogger("falconfox.telegram")

# A daemon restart used to be invisible from the phone: the bot reconnected in
# silence, and unless a turn happened to be in flight nothing was ever said. Self
# -updating from inside a session makes restarts routine, so they get announced.
DAEMON_DOWN = "\u26a0\ufe0f Daemon connection lost \u2014 reconnecting."
DAEMON_UP = "\u2705 FalconFox is up"
BUSY_TURN = (
    "Still working on the previous message, so this one was not sent — send it "
    "again once the reply arrives."
)
# The recurring silent failure: a turn ends, nothing was ever delivered, and no
# layer had an error to report. Now the moment it happens, the chat hears it.
SILENT_TURN = "⚠️ The turn ended without delivering a reply ({detail})."
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
# The "stuck" half of turn feedback. Nothing can distinguish a long tool call
# from a hung turn from outside, so the bot states the observable fact -- how
# long since the daemon last said anything about this session -- exactly once
# per quiet spell, and leaves the judgement to the reader.
QUIET_TURN = (
    "⏳ Nothing from the agent in {minutes} min (last activity: {state}). A "
    "long tool call looks just like a stuck turn from here — /status shows "
    "the daemon's view."
)
class Dest(NamedTuple):
    """Where a turn talks: a chat, and a topic within it.

    The forum collapsed this to a bare thread id for a while, on the premise
    that the chat is always the forum. The owner's private chat breaks that
    premise -- it needs no configuration and exists before any forum does --
    so both halves travel together again.
    """

    chat: int
    thread: int | None = None


QUIET_TURN_SECONDS = 180
# Service messages that are not prompts. Topic events are the bot's own
# lifecycle calls echoing back through the update stream.
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
TURN_ACTIONS = {
    "starting": "choose_sticker",    # resuming or launching the ACP subprocess
    "working": "typing",             # alive, but producing nothing right now
    "thinking": "find_location",     # agent_thought_chunk
    "streaming": "record_voice",     # agent_message_chunk -- output is flowing
    "tool": "upload_document",       # a tool call is running
}
DEFAULT_ACTION = TURN_ACTIONS["working"]
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
# The progress message is plain text, created lazily (a turn with nothing to
# narrate gets none), updated from the activity loop so a hung edit can never
# stall the event pipeline, and capped by trimming its oldest lines.
PROGRESS_HEADER = "🛠 Working…"
PROGRESS_LIMIT = 3500
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
        # session -> the topic it owns, and the reverse. A turn's destination
        # is now just a thread id: the chat is always the forum. None means
        # General, which is the manager's topic.
        self._topics: dict[str, int] = {}
        self._threads: dict[int, str] = {}
        # Last title and closed-state mirrored onto each topic, so the steady
        # stream of session_updated events only acts on real transitions.
        self._topic_names: dict[str, str] = {}
        self._closed_topics: set[str] = set()
        self._turn_dest: dict[str, Dest] = {}
        self._activity_tasks: dict[str, asyncio.Task] = {}
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
        self._quiet_notified: set[str] = set()
        # The two-message turn: the user's prompt message (the final reply
        # threads to it), and the per-turn progress message with its
        # accumulated narration/tool lines.
        self._prompt_msg: dict[str, int] = {}
        self._progress_msg: dict[str, int] = {}
        self._progress_lines: dict[str, list[str]] = {}
        self._progress_dirty: set[str] = set()
        self._seen_tools: dict[str, set[str]] = {}
        self._thought_parts: dict[str, list[str]] = {}
        # Latest usage figures per session (context used/size, token totals),
        # merged from the daemon's usage events for the turn's final stamp.
        self._usage_view: dict[str, dict] = {}
        self._action_sends: set[asyncio.Task] = set()
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
        self._learned_forum: int | None = None
        self._bot_username: str | None = None

    async def run(self) -> None:
        StallWatchdog(logging.getLogger("falconfox.telegram.watchdog")).start()
        self._prepare_manager_workspace()
        self._load_forum()
        self._load_topics()
        ws_url = self.config.daemon_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/ws"
        # `async for ... in connect(...)` retries the connection with backoff,
        # so the bot survives daemon restarts (e.g. a self-update) instead of
        # dying with the connection.
        async for websocket in connect(ws_url):
            try:
                await self._run_connected(websocket)
            except (ConnectionClosed, ApiError, OSError) as error:
                log.warning("daemon connection lost (%s); reconnecting", error)
                await self._announce(DAEMON_DOWN)
                await asyncio.sleep(2)
                continue

    async def _run_connected(self, websocket) -> None:
        self._ws = websocket
        snapshot = json.loads(await websocket.recv())
        if snapshot.get("type") != "snapshot":
            raise RuntimeError("FalconFox did not send an initial snapshot")
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
            await self._spawn_manager_session()
        try:
            await self._reconcile_topics()
        except Exception:
            log.warning("topic reconciliation failed", exc_info=True)
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
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
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
        for activity in self._activity_tasks.values():
            activity.cancel()
        self._activity_tasks.clear()
        self._activity_state.clear()
        self._turn_working.clear()
        self._turn_id.clear()
        self._delivered.clear()
        self._turn_started_at.clear()
        self._consumed.clear()
        self._adopted.clear()
        self._last_event_at.clear()
        self._quiet_notified.clear()
        self._prompt_msg.clear()
        self._progress_msg.clear()
        self._progress_lines.clear()
        self._progress_dirty.clear()
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
            elif state in ("working", "starting"):
                self._adopt_turn(session_id, record)
                await self._set_activity(session_id, "working")
            else:
                await self._deliver_recovered_turn(session_id, record)
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
        # Seed the quiet clock: the turn has a past, but this process has no
        # event history for it. Without this, adoption instantly fired a
        # spurious "quiet for 10 min" warning (observed on the first live
        # adoption, 2026-08-25 16:23) because the fallback is the start time.
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
            detail = await self.daemon.session(session_id)
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

    SKILL_NAME = "falconfox-sessions"
    SETUP_SKILL_NAME = "falconfox-setup"

    def _prepare_workspace(self, root: Path, skill: str, orientation: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        skills_root = root.joinpath(".agents", "skills")
        skills_root.mkdir(parents=True, exist_ok=True)
        # Reconcile rather than write additively (bugs.md, fixed here): the
        # skill directory is owned by the bot, so a renamed or split skill
        # must not leave the old one discoverable beside the new. Before this,
        # renaming the skill left the agent reading *both*, with conflicting
        # instructions -- a silent failure that blocked exactly this rename.
        for stale in skills_root.iterdir():
            if stale.is_dir() and stale.name != skill:
                shutil.rmtree(stale, ignore_errors=True)
                log.info("pruned stale manager skill: %s", stale.name)
        skill_dir = skills_root.joinpath(skill)
        skill_dir.mkdir(parents=True, exist_ok=True)
        packaged_skill = files("falconfox_telegram").joinpath(
            "skills", skill, "SKILL.md"
        ).read_text()
        skill_dir.joinpath("SKILL.md").write_text(packaged_skill)
        # Claude discovers skills under .claude/skills; bridge with a symlink.
        claude_dir = root.joinpath(".claude")
        claude_dir.mkdir(exist_ok=True)
        skills_link = claude_dir.joinpath("skills")
        if not skills_link.is_symlink() and not skills_link.exists():
            skills_link.symlink_to(Path("..", ".agents", "skills"))
        # Both files, so every agent runtime picks the orientation up natively.
        root.joinpath("AGENTS.md").write_text(orientation)
        root.joinpath("CLAUDE.md").write_text(orientation)

    def _prepare_manager_workspace(self) -> None:
        orientation = (
            "You are the FalconFox Telegram session manager, running in the "
            "General topic of a forum where every session has its own topic. "
            "You are NOT a work agent — you manage the session lifecycle "
            "(spawn, rename, stop, delete), you do not work inside sessions. "
            "There is no focus pointer and no routing decision to make: the "
            "user talks to a session by writing in its topic. For every user "
            f"message, follow the {self.SKILL_NAME} skill "
            f"(.agents/skills/{self.SKILL_NAME}). You may run any `falconfox` "
            "command and nothing else. Confirm the target back to the user "
            "before `stop` or `delete`. Never orient on or work in any "
            "project. When greeting or unsure, ask what the user wants.\n"
        )
        self.manager_workspace = self.state_dir
        self._prepare_workspace(self.manager_workspace, self.SKILL_NAME, orientation)

    def _prepare_concierge_workspace(self) -> None:
        bot_name = self._bot_username or "your_bot"
        orientation = (
            "You are the FalconFox private chat: the channel that works "
            "without any configuration, so it is where the user arrives "
            "before a forum exists and returns if the forum breaks, and it is "
            "also the general help and meta channel. For every user message, "
            f"follow the {self.SETUP_SKILL_NAME} skill "
            f"(.agents/skills/{self.SETUP_SKILL_NAME}). Your bot username is "
            f"@{bot_name}. Read what the user actually wants rather than "
            "assuming something is broken. You may run any `falconfox` "
            "command and nothing else; never do project work here.\n"
        )
        self.concierge_workspace = self.state_dir.joinpath("concierge")
        self._prepare_workspace(self.concierge_workspace,
                                self.SETUP_SKILL_NAME, orientation)

    async def _spawn_manager_session(self) -> None:
        old = self.manager_session_id
        session = await self.daemon.spawn(
            path=str(self.manager_workspace), name="telegram manager",
            backend=self.config.manager_backend, ephemeral=True,
        )
        self.manager_session_id = session["session_id"]
        if old:
            try:
                await self.daemon.delete(old)
            except ApiError:
                log.warning("could not delete old manager session %s", old, exc_info=True)

    async def _ensure_concierge(self) -> str | None:
        """The private chat's session, spawned on first use."""
        if self.concierge_session_id:
            return self.concierge_session_id
        if self._bot_username is None:
            try:
                self._bot_username = (await self.telegram.call("getMe") or {}).get("username")
            except ApiError:
                log.warning("could not read the bot username", exc_info=True)
        self._prepare_concierge_workspace()
        try:
            session = await self.daemon.spawn(
                path=str(self.concierge_workspace), name="telegram private chat",
                backend=self.config.manager_backend, ephemeral=True,
            )
        except ApiError:
            log.warning("could not spawn the private-chat session", exc_info=True)
            return None
        self.concierge_session_id = session["session_id"]
        log.info("private-chat session spawned: %s", self.concierge_session_id)
        return self.concierge_session_id

    # --- topics ----------------------------------------------------------

    @property
    def forum_chat_id(self) -> int | None:
        """The forum in use: pinned by the environment, else learned, else none."""
        return self.config.forum_chat_id or self._learned_forum

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
        self._topics = {k: int(v) for k, v in raw.items() if isinstance(v, int)}
        self._threads = {v: k for k, v in self._topics.items()}

    def _persist_topics(self) -> None:
        """Write the session→topic map atomically. Never fatal."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._topics_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._topics))
            temporary.replace(self._topics_file)
        except OSError:
            log.warning("could not persist the topic map", exc_info=True)

    def _bind(self, session_id: str, thread: int) -> None:
        self._topics[session_id] = thread
        self._threads[thread] = session_id
        self._persist_topics()

    def _unbind(self, session_id: str) -> int | None:
        thread = self._topics.pop(session_id, None)
        if thread is not None:
            self._threads.pop(thread, None)
            self._persist_topics()
        return thread

    async def _ensure_topic(self, session: dict) -> int | None:
        """Give a session a topic, creating one if it has none."""
        session_id = session["session_id"]
        if session_id == self.manager_session_id:
            return None
        existing = self._topics.get(session_id)
        if existing is not None:
            return existing
        title = session.get("name") or session_id
        try:
            thread = await self.telegram.create_topic(self.forum_chat_id, title)
        except ApiError:
            log.warning("could not create a topic for %s", session_id, exc_info=True)
            return None
        self._bind(session_id, thread)
        self._topic_names[session_id] = title
        log.info("topic created: session=%s thread=%s name=%s", session_id, thread, title)
        return thread

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
            if item["session_id"] not in self._topics:
                # Sequential, not gathered: topic management is rate-limited
                # (429 retry-after observed), so a burst of creations on a
                # first run must not be fired all at once.
                await self._ensure_topic(item)

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

    async def _handle_update(self, update: dict) -> None:
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
            log.error("forum migrated to chat id %s -- set "
                      "FALCONFOX_TELEGRAM_FORUM_CHAT_ID to it and restart",
                      moved_to)
            return
        dest = Dest(chat_id, message.get("message_thread_id"))
        text = message.get("text")
        if not text:
            if any(key.startswith("forum_topic_") or key in _JOIN_EVENTS
                   for key in message):
                # Topic service messages are the bot's own lifecycle calls
                # echoing back; answering them would spam every topic it makes.
                return
            await self._say(dest, "Text messages only in this PoC.")
            return
        if chat_id == self.config.owner_id:
            # The owner's private chat. Answered whether or not a forum is
            # configured -- being reachable when nothing else is, is the whole
            # point of this channel.
            target = await self._ensure_concierge()
            if target is None:
                await self._say(dest, "Could not start the private-chat session.")
                return
            if text.startswith("/") and await self._command(dest, text):
                return
            await self._forward(target, dest, text,
                                prompt_msg=message.get("message_id"))
            return
        if chat_id != self.forum_chat_id:
            log.info("ignoring message from unconfigured chat %s", chat_id)
            return
        if text.startswith("/"):
            if await self._command(dest, text):
                return
        target = (self.manager_session_id if dest.thread is None
                  else self._threads.get(dest.thread))
        if not target:
            await self._say(dest, "No FalconFox session owns this topic.")
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
            sessions = await self.daemon.sessions()
            listing = "\n".join(
                f"{item['session_id']}  {item['name']}  [{item['state']}]  {item['path']}"
                for item in sessions
            ) or "No sessions."
            await self._say(dest, listing)
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
        return False

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
                    f"quiet={quiet} started {age}")
        return "\n".join(lines)

    def _start_activity(self, session_id: str, dest: Dest) -> bool:
        """Ensure a refresh loop is running. True if this call started one."""
        task = self._activity_tasks.get(session_id)
        # `task.done()` matters: a finished task is still *in* the dict, and the
        # old `session_id not in self._typing_tasks` guard read that as live. One
        # failed sendChatAction therefore silenced a turn permanently, with the
        # `working` safety net unable to restart it because it hit the same
        # guard.
        if task is not None and not task.done():
            return False
        self._activity_tasks[session_id] = asyncio.create_task(
            self._activity_loop(session_id, dest))
        return True

    async def _set_activity(self, session_id: str, state: str) -> None:
        """Record what the session is doing and show it in the chat."""
        if session_id not in self._turn_dest:
            return
        # None is a real destination (General), so membership is the test --
        # a `.get() is None` guard here would silently mute the manager topic.
        dest = self._turn_dest[session_id]
        # Before the equality check, so this doubles as the safety net that
        # revives a loop which died mid-turn.
        started = self._start_activity(session_id, dest)
        previous = self._activity_state.get(session_id)
        if previous == state:
            return
        self._activity_state[session_id] = state
        # Streamed output fires an event per chunk; only a *change* is worth an
        # API call. A fresh loop sends immediately, so it needs no second one.
        #
        # Detached, never awaited inline: this sits on the event-pipeline path,
        # and one hung Telegram call here stalls every queued daemon event
        # behind it. Observed live (2026-08-25, 09:06): a 40s read timeout
        # delayed a finished reply by 45 seconds. The indicator is droppable
        # decoration; the pipeline is not allowed to wait for it.
        if not started:
            task = asyncio.create_task(self._send_action(session_id, dest))
            self._action_sends.add(task)
            task.add_done_callback(self._action_sends.discard)

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

    async def _update_progress(self, session_id: str, dest: Dest, *,
                               final_note: str | None = None) -> None:
        """Create or edit the turn's progress message. Rides the activity loop
        (and the turn's finalization), never the event pipeline: a hung
        Telegram call here must not stall queued daemon events. Edits do not
        notify, so a muted chat stays quiet through any amount of progress."""
        if final_note is None and session_id not in self._progress_dirty:
            return
        lines = self._progress_lines.get(session_id) or []
        message_id = self._progress_msg.get(session_id)
        # Nothing accumulated and nothing on screen to stamp: stay silent. (A
        # normal turn has a message from _forward; this guards turns primed by
        # other paths, e.g. adopted ones whose creation failed.)
        if not lines and (final_note is None or message_id is None):
            return
        self._progress_dirty.discard(session_id)
        header = final_note or PROGRESS_HEADER
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
            self._progress_dirty.add(session_id)
            log.debug("progress update failed for %s: %s", session_id, error)

    async def _send_reply(self, session_id: str, dest: Dest) -> None:
        """Deliver the turn's answer: the text after the last tool call,
        threaded to the prompt that asked for it."""
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
            return
        log.info("reply: session=%s dest=%s chars=%d", session_id, dest, len(text))
        prompt_msg = self._prompt_msg.get(session_id)
        for index, rendered in enumerate(render_messages(text)):
            await self.telegram.html_message(
                dest.chat, rendered.html, rendered.plain,
                reply_to=prompt_msg if index == 0 else None, thread=dest.thread)
        self._delivered[session_id] = self._delivered.get(session_id, 0) + len(text)
        self._persist_turns()

    async def _send_action(self, session_id: str, dest: Dest) -> None:
        action = TURN_ACTIONS.get(
            self._activity_state.get(session_id, ""), DEFAULT_ACTION)
        try:
            await self.telegram.chat_action(dest.chat, action, thread=dest.thread)
        except ApiError as error:
            # Never fatal to the loop. A 429 from the rate limiter -- likeliest
            # on exactly the long turn that needs an indicator -- or one of the
            # read timeouts this deployment sees used to end the task outright
            # and leave the turn silent for the rest of its life.
            log.debug("chat action %s failed for %s: %s", action, session_id, error)

    async def _forward(self, session_id: str, dest: Dest, text: str,
                       prompt_msg: int | None = None) -> None:
        if session_id in self._turn_dest:
            # The daemon refuses a prompt while a turn is running, and says so
            # with an *info* notice -- which this client does not surface, so the
            # message vanished without a trace. Worse, forwarding it anyway reset
            # the buffers below and destroyed the reply already in flight. Refuse
            # here instead, and say so, so the text is never silently eaten.
            log.info("refused mid-turn message: session=%s dest=%s", session_id, dest)
            await self._say(dest, BUSY_TURN, reply_to=prompt_msg)
            return
        log.info("forward: session=%s dest=%s chars=%d", session_id, dest, len(text))
        self._turn_dest[session_id] = dest
        self._reply_parts[session_id] = []
        self._delivered[session_id] = 0
        self._consumed[session_id] = 0
        if prompt_msg is not None:
            self._prompt_msg[session_id] = prompt_msg
        self._progress_lines.pop(session_id, None)
        self._progress_msg.pop(session_id, None)
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
            message_id = await self._say(dest, PROGRESS_HEADER, silent=True)
            if message_id is not None:
                self._progress_msg[session_id] = message_id
                self._persist_turns()
        except ApiError as error:
            log.debug("could not create the progress message: %s", error)

    async def _receive_events(self) -> None:
        async for raw in self._ws:
            await self._handle_event(json.loads(raw))

    async def _handle_event(self, event: dict) -> None:
        session_id = event.get("session_id")
        if not session_id:
            return
        # Any event is a sign of life; a fresh one also ends a quiet spell, so
        # the next long silence gets its own notice.
        self._last_event_at[session_id] = time.monotonic()
        self._quiet_notified.discard(session_id)
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
        if event_type == "session_added":
            await self._ensure_topic(event)
            return
        if event_type == "session_removed":
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
        """Keep a session's topic looking like the session. Only transitions
        act, so the stream of `session_updated` events does not retitle or
        re-close a topic on every state change."""
        session_id = session.get("session_id")
        thread = self._topics.get(session_id)
        if thread is None:
            return
        name = session.get("name")
        if name and self._topic_names.get(session_id) != name:
            try:
                await self.telegram.rename_topic(self.forum_chat_id, thread, name)
                self._topic_names[session_id] = name
            except ApiError:
                log.warning("could not retitle topic %s", thread, exc_info=True)
        # A stopped session gets a closed topic: the record stays and the bot
        # can still write, but the user cannot prompt something that is not
        # running. Measured -- a closed topic still accepts bot writes.
        stopped = session.get("state") == "stored"
        was_stopped = session_id in self._closed_topics
        if stopped == was_stopped:
            return
        try:
            if stopped:
                await self.telegram.close_topic(self.forum_chat_id, thread)
                self._closed_topics.add(session_id)
            else:
                await self.telegram.reopen_topic(self.forum_chat_id, thread)
                self._closed_topics.discard(session_id)
        except ApiError:
            log.warning("could not %s topic %s",
                        "close" if stopped else "reopen", thread, exc_info=True)

    async def _finish_turn(self, session_id: str, event: dict | None) -> None:
        """Close out a turn: deliver the remainder, stop the indicator, account
        for what was handed over — and say so when that is nothing. Idempotent:
        the `idle` that follows a `turn_ended` finds nothing left to do."""
        self._turn_working.discard(session_id)
        activity = self._activity_tasks.pop(session_id, None)
        if activity:
            activity.cancel()
        self._activity_state.pop(session_id, None)
        had_turn = session_id in self._turn_dest
        dest = self._turn_dest.pop(session_id, None)
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
            await self._send_reply(session_id, dest)
        delivered = self._delivered.pop(session_id, 0)
        self._consumed.pop(session_id, None)
        self._adopted.discard(session_id)
        self._last_event_at.pop(session_id, None)
        self._quiet_notified.discard(session_id)
        self._reply_parts.pop(session_id, None)
        prompt_msg = self._prompt_msg.pop(session_id, None)
        self._progress_msg.pop(session_id, None)
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

    async def _activity_loop(self, session_id: str, dest: Dest) -> None:
        try:
            while True:
                await self._send_action(session_id, dest)
                await self._update_progress(session_id, dest)
                await self._check_quiet(session_id, dest)
                await asyncio.sleep(ACTION_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _check_quiet(self, session_id: str, dest: Dest) -> None:
        """Say -- once per spell -- that a turn has gone quiet for a long time."""
        if session_id in self._quiet_notified:
            return
        last = self._last_event_at.get(session_id) or self._turn_started_at.get(session_id)
        if last is None:
            return
        quiet = time.monotonic() - last
        if quiet < QUIET_TURN_SECONDS:
            return
        self._quiet_notified.add(session_id)
        state = self._activity_state.get(session_id) or "working"
        log.info("quiet turn: session=%s quiet=%.0fs state=%s", session_id, quiet, state)
        try:
            await self._say(dest, QUIET_TURN.format(
                minutes=int(quiet // 60), state=state),
                reply_to=self._prompt_msg.get(session_id))
        except Exception:
            # The notice is decoration; the loop it rides on is not.
            log.warning("could not report the quiet turn to %s", dest)
