from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import json
import logging
import os
import shutil
import tempfile
import tomllib
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from falconfox.cli import CliError, _guard_self_target, build_parser, cmd_daemon
from falconfox.coordinator import SessionCoordinator
from falconfox.engine.session import AgentSession
from falconfox.storage import SessionStore
from falconfox.watchdog import StallWatchdog
from falconfox_telegram.api import ApiError, _json_request
from falconfox_telegram.bot import (BUSY_TURN, Dest, DAEMON_DOWN, QUIET_TURN_SECONDS,
                                    TURN_ACTIONS, BotConfig, FalconFoxTelegramBot)
from falconfox_telegram.rendering import TELEGRAM_MESSAGE_LIMIT, render_messages
from falconfox_telegram.shell import ShellRunner, tail


# Port 9 is the discard port: nothing listens, so a DaemonApi that is not
# stubbed fails fast instead of quietly reaching a real daemon. Without this,
# tests exercising _ensure_manager fell through to BotConfig's default of
# 127.0.0.1:9721 -- production -- and spawned real sessions there.
UNREACHABLE_DAEMON = "http://127.0.0.1:9"


class StorageTests(unittest.TestCase):
    def test_flat_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            meta = {
                "session_id": "0123abcd", "name": "work", "path": "/tmp",
                "backend": "echo", "named": True,
            }
            store.write_meta(meta)
            store.append_event("0123abcd", {"type": "message", "role": "user", "text": "hi"})
            self.assertEqual(store.load_all_meta(), [meta])
            self.assertEqual(store.read_transcript("0123abcd")[0]["text"], "hi")
            self.assertTrue(Path(directory, "0123abcd", "meta.toml").exists())


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_home.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config_home.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.coordinator = SessionCoordinator(Path(self.temporary.name))

    async def test_hidden_survives_a_daemon_restart(self):
        # Without this the plumbing reappears in every listing after a restart
        # and starts competing for slots as if it were the user's work.
        self.coordinator._metadata["infra"] = {
            "session_id": "infra", "name": "telegram manager", "path": "/tmp",
            "backend": "echo", "always_allow": True, "ephemeral": False,
            "hidden": True, "state": "idle", "live": True,
            "created": "1", "last_active": "1",
        }
        self.coordinator._auto_named["infra"] = False
        self.coordinator._persist_meta("infra")
        stored = tomllib.loads(
            Path(self.temporary.name, "infra", "meta.toml").read_text())
        self.assertIs(stored.get("hidden"), True,
                      "the flag must round-trip, or plumbing reappears as work")

    async def test_empty_permission_options_are_denied_immediately(self):
        result = await asyncio.wait_for(
            self.coordinator._request_permission({"session_id": "missing", "options": []}),
            timeout=0.1,
        )
        self.assertIsNone(result)

    async def test_ephemeral_sessions_never_persist_or_appear_by_default(self):
        self.coordinator._metadata["focus"] = {
            "session_id": "focus", "name": "focus", "path": "/tmp",
            "backend": "echo", "always_allow": True, "ephemeral": True,
            "hidden": True,
            "state": "idle", "live": True, "created": "1", "last_active": "1",
        }
        self.coordinator._auto_named["focus"] = False
        self.coordinator._transcripts["focus"] = [
            {"type": "message", "role": "user", "text": "switch"}
        ]
        self.coordinator._persist_meta("focus")
        self.assertEqual(self.coordinator.list_sessions(), [])
        self.assertEqual(self.coordinator.list_sessions(include_hidden=True)[0]["session_id"],
                         "focus")
        self.assertFalse(Path(self.temporary.name, "focus").exists())

    async def test_a_turn_that_produced_no_output_is_a_warning(self):
        # The recurring failure shape: a turn ends with nothing to show and
        # nobody notices. The daemon now notices, at the moment it happens.
        self.coordinator._metadata["s"] = {
            "session_id": "s", "name": "quiet", "path": "/tmp", "backend": "echo",
            "always_allow": True, "ephemeral": True, "state": "working",
            "live": True, "created": "1", "last_active": "1",
        }
        turn = {"type": "turn_ended", "session_id": "s", "turn_id": "t1",
                "outcome": "completed", "stop_reason": "end_turn", "duration": 1.0,
                "message_chunks": 0, "output_chars": 0, "thought_chunks": 0,
                "tool_calls": 0}
        with self.assertLogs("falconfox.coordinator", level="WARNING") as captured:
            self.coordinator._emit(dict(turn))
        self.assertIn("NO output", captured.output[0])
        # A turn that did produce output logs at INFO, not WARNING.
        with self.assertLogs("falconfox.coordinator", level="INFO") as captured:
            self.coordinator._emit({**turn, "output_chars": 42, "message_chunks": 3})
        self.assertNotIn("WARNING", captured.output[0])
        self.assertIn("turn complete", captured.output[0])

    async def test_snapshot_contains_metadata_not_transcripts(self):
        self.coordinator._metadata["one"] = {
            "session_id": "one", "name": "one", "path": "/tmp", "backend": "echo",
            "always_allow": True, "ephemeral": False, "state": "stored", "live": False,
            "created": "1", "last_active": "1",
        }
        self.coordinator._transcripts["one"] = [{"type": "message", "text": "large"}]
        snapshot = self.coordinator.snapshot()
        self.assertEqual(snapshot["sessions"][0]["session_id"], "one")
        self.assertNotIn("transcripts", snapshot)


class LiveSessionCapTests(unittest.IsolatedAsyncioTestCase):
    """A ceiling on sessions holding a live agent subprocess.

    Sessions are the unit of memory cost -- each runs its own ACP backend --
    and the daemon was OOM-killed carrying ten of them on a 951 MB host.
    """

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_home.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config_home.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.coordinator = SessionCoordinator(Path(self.temporary.name))
        self.stopped = []

        async def _stop(session_id):
            self.stopped.append(session_id)
            meta = self.coordinator._metadata[session_id]
            meta.update(state="stored", live=False)
        self.coordinator.stop_session = _stop

    def _live(self, session_id, *, last_active, state="idle", infrastructure=False):
        self.coordinator._metadata[session_id] = {
            "session_id": session_id, "name": session_id, "path": "/tmp",
            "backend": "echo", "always_allow": True, "ephemeral": False,
            "hidden": infrastructure,
            "state": state, "live": True, "created": "1", "last_active": last_active,
        }

    def _limit(self, value):
        self.coordinator.config = replace(self.coordinator.config,
                                          max_active_sessions=value)

    async def test_a_slot_is_free_below_the_limit(self):
        self._limit(3)
        self._live("a", last_active="1")
        self.assertTrue(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, [])

    async def test_the_least_recently_used_idle_session_is_evicted(self):
        # Not the oldest *activation*: that would take the session you have
        # had open all day. last_active is what "stopped touching" means.
        self._limit(2)
        self._live("old-but-busy", last_active="1", state="working")
        self._live("stale", last_active="2")
        self._live("fresh", last_active="9")
        self.assertTrue(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, ["stale"])

    async def test_a_working_session_is_never_evicted(self):
        # At the floor with everything busy: no candidate, and no turn in
        # flight is destroyed to make one.
        self._limit(3)
        for name in ("busy", "also busy", "still busy"):
            self._live(name, last_active="1", state="working")
        self.assertFalse(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, [])

    async def test_a_configured_limit_of_one_means_one(self):
        # No floor: infrastructure is resumable and evicted last, so a small
        # limit degrades rather than deadlocking, and the number is honoured
        # exactly as written.
        self._limit(1)
        self._live("manager", last_active="1", infrastructure=True)
        self.assertTrue(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, ["manager"],
                         "with nothing else live, infrastructure yields")

    async def test_infrastructure_takes_its_turn_like_any_other_session(self):
        # It used to sort last, from when stopping it destroyed its
        # conversation. Resumable infrastructure is the cheapest thing to
        # evict, and privileging it meant a limit of 2 bought one work session.
        self._limit(3)
        self._live("infra", last_active="1", infrastructure=True)
        self._live("mine", last_active="9")
        self._live("also mine", last_active="8")
        self.assertTrue(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, ["infra"], "oldest goes, whatever it is")

    async def test_infrastructure_never_waits_for_a_slot(self):
        self._limit(3)
        for name in ("a", "b", "c"):
            self._live(name, last_active="1", state="working")
        self.assertTrue(await self.coordinator._ensure_slot(infrastructure=True))
        self.assertFalse(await self.coordinator._ensure_slot())
        self.assertEqual(self.stopped, [])

    async def test_zero_disables_the_cap(self):
        self._limit(0)
        self._live("a", last_active="1", state="working")
        self.assertTrue(await self.coordinator._ensure_slot())

    async def test_a_queued_send_holds_the_text_instead_of_losing_it(self):
        self._limit(3)
        for name in ("busy", "also busy", "still busy"):
            self._live(name, last_active="1", state="working")
        self.coordinator._metadata["waiting"] = {
            "session_id": "waiting", "name": "waiting", "path": "/tmp",
            "backend": "echo", "always_allow": True, "ephemeral": False,
            "state": "stored", "live": False, "created": "1", "last_active": "1",
        }
        await self.coordinator.send("waiting", "do the thing")
        self.assertEqual(self.coordinator._queued, {"waiting": "do the thing"})

    async def test_a_queued_session_is_retried_when_one_goes_idle(self):
        self._limit(1)
        self._live("busy", last_active="1", state="working")
        self.coordinator._queued["waiting"] = None
        drained = []
        self.coordinator._drain_queue = lambda: drained.append(True) or asyncio.sleep(0)
        self.coordinator._emit({"type": "agent_state", "session_id": "busy",
                                "state": "idle"})
        await asyncio.sleep(0)
        self.assertEqual(drained, [True])


class EngineTurnTests(unittest.IsolatedAsyncioTestCase):
    """The turn as a first-class fact: id, boundaries, and what it produced."""

    def _session(self, events):
        session = AgentSession(
            session_id="s", name="n", path=Path("/tmp"), backend=None,
            emit=events.append, request_permission=None,
        )
        session._acp_session_id = "acp"
        return session

    async def test_a_turn_reports_its_own_start_end_and_output(self):
        events = []
        session = self._session(events)

        class FakeConn:
            async def prompt(self, **_kwargs):
                # What the ACP client would emit while the prompt runs.
                session._guarded_emit({"session_id": "s", "type": "message",
                                       "role": "agent", "text": "hello"})
                session._guarded_emit({"session_id": "s", "type": "message",
                                       "role": "thought", "text": "hmm"})
                session._guarded_emit({"session_id": "s", "type": "tool_call",
                                       "tool_call_id": "t1", "status": "pending"})
                session._guarded_emit({"session_id": "s", "type": "tool_call",
                                       "tool_call_id": "t1", "status": "completed"})

                class Response:
                    stop_reason = "end_turn"
                    usage = None
                return Response()

        session._conn = FakeConn()
        await session.send("hi")
        types = [event["type"] for event in events]
        started = next(event for event in events if event["type"] == "turn_started")
        ended = next(event for event in events if event["type"] == "turn_ended")
        self.assertEqual(started["turn_id"], ended["turn_id"])
        self.assertEqual(ended["outcome"], "completed")
        self.assertEqual(ended["stop_reason"], "end_turn")
        self.assertEqual(ended["output_chars"], len("hello"))
        self.assertEqual(ended["message_chunks"], 1)
        self.assertEqual(ended["thought_chunks"], 1)
        # Two updates for one tool call count once.
        self.assertEqual(ended["tool_calls"], 1)
        # The end of the turn is announced before the idle state, so clients
        # can finalize on the fact and treat the state as the no-op it is.
        self.assertLess(types.index("turn_ended"), len(types) - 1)
        self.assertEqual(events[-1], {"session_id": "s", "type": "agent_state",
                                      "state": "idle"})

    async def test_a_failed_prompt_still_ends_its_turn(self):
        events = []
        session = self._session(events)

        class BrokenConn:
            async def prompt(self, **_kwargs):
                raise RuntimeError("backend fell over")

        session._conn = BrokenConn()
        await session.send("hi")
        ended = next(event for event in events if event["type"] == "turn_ended")
        self.assertEqual(ended["outcome"], "error")
        self.assertEqual(ended["output_chars"], 0)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_loop_is_reported(self):
        log = logging.getLogger("falconfox.test.watchdog")
        dog = StallWatchdog(log, interval=0.05, threshold=0.2)
        dog.start()
        try:
            with self.assertLogs(log, level="WARNING") as captured:
                time.sleep(0.8)  # block the event loop, not just this coroutine
                await asyncio.sleep(0.2)  # let the heartbeat land again
            self.assertTrue(any("stall" in line for line in captured.output))
        finally:
            dog.stop()


class CliSafetyTests(unittest.TestCase):
    def test_session_cannot_delete_itself(self):
        with patch.dict(os.environ, {"FALCONFOX_SESSION_ID": "deadbeef"}):
            with self.assertRaises(CliError):
                _guard_self_target("deadbeef", "delete")
            _guard_self_target("another", "delete")


class DaemonPortTests(unittest.TestCase):
    """A pinned port must be bound as given, never searched past.

    The reason it matters: two instances on one host both search up from 9721,
    so whoever starts second silently lands on the other's neighbouring port
    and a bot pinned to a literal URL then drives the wrong daemon.
    """

    def _serve_args(self, argv):
        args = build_parser().parse_args(argv)
        with patch("falconfox.web.server.serve") as serve, \
                patch("falconfox.state.find_available_port", return_value=9999) as search:
            cmd_daemon(args)
        return serve.call_args.kwargs, search.called

    def test_pinned_port_skips_the_search(self):
        kwargs, searched = self._serve_args(["daemon", "--foreground", "--port", "9725"])
        self.assertEqual(kwargs["port"], 9725)
        self.assertFalse(searched)

    def test_unpinned_port_still_searches(self):
        kwargs, searched = self._serve_args(["daemon", "--foreground"])
        self.assertEqual(kwargs["port"], 9999)
        self.assertTrue(searched)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.message_replies = []
        self.message_silent = []
        self.html_messages = []
        self.html_replies = []
        self.edits = []
        self.actions = []
        self.action_error = None
        self._next_id = 100

    async def message(self, chat_id, text, reply_to=None, silent=False, thread=None):
        self.messages.append((thread, text))
        self.message_replies.append(reply_to)
        self.message_silent.append(silent)
        self._next_id += 1
        return self._next_id

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        self.html_messages.append((thread, html_text, plain_fallback))
        self.html_replies.append(reply_to)

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    chat_info = {"is_forum": True, "title": "forum"}
    member_info = {"status": "administrator", "can_manage_topics": True}

    async def call(self, method, body=None):
        if method == "getMe":
            return {"username": "test_bot", "id": 500}
        return {}

    async def get_chat(self, chat_id):
        return dict(self.chat_info)

    async def get_member(self, chat_id, user_id):
        return dict(self.member_info)

    async def create_topic(self, chat_id, name):
        self.topics = getattr(self, "topics", [])
        self.topics.append(name)
        return 900 + len(self.topics)

    async def rename_topic(self, chat_id, thread, name):
        self.renamed = getattr(self, "renamed", [])
        self.renamed.append((thread, name))

    async def close_topic(self, chat_id, thread):
        self.closed = getattr(self, "closed", [])
        self.closed.append(thread)

    async def reopen_topic(self, chat_id, thread):
        self.reopened = getattr(self, "reopened", [])
        self.reopened.append(thread)

    async def delete_topic(self, chat_id, thread):
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append(thread)

    async def chat_action(self, chat_id, action, thread=None):
        if self.action_error is not None:
            raise self.action_error
        self.actions.append((thread, action))


class TelegramEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_signals_each_state_and_sends_one_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            bot._turn_dest["session"] = Dest(-1001, 20)
            bot._reply_parts["session"] = []
            # The drains between events: action sends are detached tasks now
            # (a hung one must not stall the pipeline), so give each a tick to
            # land before the next state change.
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "hello "})
            await asyncio.sleep(0)
            await bot._handle_event({"type": "tool_call", "session_id": "session",
                                     "title": "hidden"})
            await asyncio.sleep(0)
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "**world**"})
            await asyncio.sleep(0)
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            await asyncio.sleep(0)
            # One action per state *change*: a chunk-by-chunk stream must not
            # produce a call per chunk, and the tool call is visible as state
            # without being rendered as a message of its own.
            self.assertEqual(fake.actions, [
                (20, TURN_ACTIONS["working"]),
                (20, TURN_ACTIONS["streaming"]),
                (20, TURN_ACTIONS["tool"]),
                (20, TURN_ACTIONS["streaming"]),
            ])
            # The text before the tool call was narration introducing it; both
            # land in the finalized progress message. The reply is only the
            # final block -- the answer, not the working chatter.
            self.assertEqual(len(fake.messages), 1)
            self.assertIn("✅ Turn finished", fake.messages[0][1])
            self.assertIn("hello", fake.messages[0][1])
            self.assertIn("⚙️ hidden", fake.messages[0][1])
            self.assertEqual(fake.html_messages,
                             [(20, "<b>world</b>", "**world**")])


    async def test_activity_starts_when_the_prompt_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            sent = []

            class FakeWebSocket:
                async def send(self, payload):
                    sent.append(json.loads(payload))

            bot._ws = FakeWebSocket()
            await bot._forward("session", Dest(-1001, 20), "do the thing")
            await asyncio.sleep(0)
            # The indicator is live before the daemon has reported any state at
            # all -- the backend may still be starting up or resuming.
            self.assertEqual(fake.actions, [(20, TURN_ACTIONS["working"])])
            self.assertEqual(sent, [{"action": "send", "session_id": "session",
                                     "text": "do the thing"}])
            # A later `working` event must not start a second loop, nor repeat
            # the action for a state that has not changed.
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            self.assertEqual(fake.actions, [(20, TURN_ACTIONS["working"])])
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            self.assertEqual(bot._activity_tasks, {})


    async def test_a_failed_chat_action_does_not_silence_the_turn(self):
        # The bug this replaces: _typing_loop caught only CancelledError, so one
        # ApiError -- a 429 from the rate limiter, or a read timeout -- ended the
        # task. The dead task stayed in the dict, the restart guard read it as
        # live, and the rest of the turn went silent with nothing logged.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            bot._turn_dest["session"] = Dest(-1001, 20)
            bot._reply_parts["session"] = []

            fake.action_error = ApiError("Too Many Requests: retry after 1")
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            self.assertEqual(fake.actions, [])
            self.assertFalse(bot._activity_tasks["session"].done(),
                             "one failed chat action must not end the loop")

            # Recovered: the next state change reaches the chat.
            fake.action_error = None
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "hi"})
            await asyncio.sleep(0)
            self.assertEqual(fake.actions, [(20, TURN_ACTIONS["streaming"])])
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            self.assertEqual(bot._activity_tasks, {})

    async def test_a_dead_activity_loop_is_revived_by_the_next_state(self):
        # The second half of the same bug: even if the loop dies for a reason the
        # ApiError guard does not cover, the guard must not mistake a finished
        # task for a running one.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            bot._turn_dest["session"] = Dest(-1001, 20)
            await bot._set_activity("session", "working")
            dead = bot._activity_tasks["session"]
            dead.cancel()
            try:
                await dead
            except asyncio.CancelledError:
                pass
            self.assertTrue(dead.done())

            await bot._set_activity("session", "streaming")
            self.assertIsNot(bot._activity_tasks["session"], dead)
            self.assertFalse(bot._activity_tasks["session"].done())
            bot._activity_tasks["session"].cancel()

    def _bot_mid_turn(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
            default_path=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._turn_dest["session"] = Dest(-1001, 20)
        bot._reply_parts["session"] = []
        # A real turn always reports `working` before it streams; without it the
        # bot now (correctly) refuses to treat `idle` as the turn ending.
        bot._turn_working.add("session")
        return bot

    async def _stream(self, bot, text):
        await bot._handle_event({"type": "message", "session_id": "session",
                                 "role": "agent", "text": text})

    async def _tool_call(self, bot):
        await bot._handle_event({"type": "tool_call", "session_id": "session",
                                 "title": "hidden"})

    async def _idle(self, bot):
        await bot._handle_event({"type": "agent_state", "session_id": "session",
                                 "state": "idle"})

    async def test_the_run_on_narration_bug_is_structurally_gone(self):
        # The report that forced this design (2026-08-25): three remarks made
        # between tool calls arrived glued together with no separators, each
        # colon pointing at an action the chat suppresses. Narration now lives
        # in the progress message as distinct lines with its tool markers, and
        # the reply carries only the final block.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "Now the new test class:")
            await self._tool_call(bot)
            await self._stream(bot, "Add the quiet field:")
            await self._tool_call(bot)
            await self._stream(bot, "All 44 tests pass.")
            self.assertEqual(bot.telegram.html_messages, [],
                             "nothing is delivered as a reply mid-turn")
            self.assertEqual(bot._progress_lines["session"], [
                "Now the new test class:", "⚙️ hidden",
                "Add the quiet field:", "⚙️ hidden",
            ])
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "All 44 tests pass.")

    async def test_the_progress_message_is_created_once_then_edited(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "first remark")
            await self._tool_call(bot)
            await bot._update_progress("session", Dest(-1001, 20))
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("Working", bot.telegram.messages[0][1])
            self.assertIn("first remark", bot.telegram.messages[0][1])
            message_id = bot._progress_msg["session"]

            await self._stream(bot, "second remark")
            await self._tool_call(bot)
            await bot._update_progress("session", Dest(-1001, 20))
            # Edited in place: no new message, and the edit carries the tail.
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertEqual(len(bot.telegram.edits), 1)
            self.assertEqual(bot.telegram.edits[0][1], message_id)
            self.assertIn("second remark", bot.telegram.edits[0][2])
            # Nothing dirty, nothing sent: the refresh tick must be a no-op.
            await bot._update_progress("session", Dest(-1001, 20))
            self.assertEqual(len(bot.telegram.edits), 1)
            await self._idle(bot)

    async def test_repeated_tool_calls_collapse_into_one_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            for _ in range(3):
                await self._tool_call(bot)
            self.assertEqual(bot._progress_lines["session"], ["⚙️ hidden ×3"])
            await self._idle(bot)

    async def test_a_trailing_tool_call_does_not_eat_the_answer(self):
        # A turn that says its piece and then runs one last trivial tool would
        # otherwise file its real answer as narration and reply with nothing.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "the real answer, stated before a cleanup step")
            await self._tool_call(bot)
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2],
                             "the real answer, stated before a cleanup step")

    async def test_the_progress_message_appears_the_moment_the_turn_starts(self):
        # User decision: immediate, not lazy -- and silent, since progress is
        # ambient and only the response should ping. Even an empty turn gets
        # its final stamp.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()

            class FakeWebSocket:
                async def send(self, payload):
                    pass

            bot._ws = FakeWebSocket()
            await bot._forward("session", Dest(-1001, 20), "question", prompt_msg=1)
            self.assertEqual(bot.telegram.messages, [(20, "🛠 Working…")])
            self.assertEqual(bot.telegram.message_silent, [True])
            self.assertIn("session", bot._progress_msg)
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 0})
            self.assertEqual(len(bot.telegram.edits), 1)
            self.assertIn("✅ Turn finished", bot.telegram.edits[0][2])

    async def test_thoughts_stream_into_the_progress_message_but_not_the_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "thought", "text": "long pondering " * 40})
            # Ended by the text that follows it; trimmed to its head.
            await self._stream(bot, "the answer")
            thought_line = bot._progress_lines["session"][0]
            self.assertTrue(thought_line.startswith("💭 long pondering"))
            self.assertLessEqual(len(thought_line), 290)
            self.assertTrue(thought_line.endswith("…"))
            await self._idle(bot)
            self.assertEqual(bot.telegram.html_messages[0][2], "the answer",
                             "thoughts must never leak into the reply")

    async def test_the_final_stamp_carries_elapsed_time_and_context_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turn_started_at["session"] = time.monotonic() - 135
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000})
            await self._tool_call(bot)
            await self._stream(bot, "the answer")
            await self._idle(bot)
            stamp = bot.telegram.messages[-1][1]
            self.assertIn("✅ Turn finished", stamp)
            self.assertIn("2m15s", stamp)
            self.assertIn("ctx 217k/1M", stamp)

    async def test_the_reply_threads_to_the_prompt_message(self):
        # Threading is also the notification story: in a group, a reply (like
        # a mention) cuts through a muted chat, so the progress message can be
        # spam-tolerant while the answer still pings.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()

            class FakeWebSocket:
                async def send(self, payload):
                    pass

            bot._ws = FakeWebSocket()
            await bot._forward("session", Dest(-1001, 20), "question", prompt_msg=555)
            await self._stream(bot, "answer")
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 6})
            self.assertEqual(bot.telegram.html_messages[0][2], "answer")
            self.assertEqual(bot.telegram.html_replies, [555])

    async def test_the_idle_from_resuming_a_stored_session_is_not_the_turn_ending(self):
        # Sending to a stored session resumes it, and engine/session.py sets
        # `idle` once the ACP subprocess is up -- before the prompt runs. Taking
        # that for the end of the turn dropped _turn_dest before any chunk
        # arrived, so the real reply had nowhere to go and vanished with nothing
        # logged. It cost the first reply after every daemon restart.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            sent = []

            class FakeWebSocket:
                async def send(self, payload):
                    sent.append(json.loads(payload))

            bot._ws = FakeWebSocket()
            # _bot_mid_turn primes a turn; this test needs the real entry point,
            # which now refuses to forward while one is in flight.
            bot._turn_dest.clear()
            bot._reply_parts.clear()
            await bot._forward("session", Dest(-1001, 20), "do the thing")

            # The resume's idle, before the turn has ever reported working.
            await self._idle(bot)
            self.assertIn("session", bot._turn_dest,
                          "a turn that never began cannot have ended")

            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await self._stream(bot, "the real reply")
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "the real reply")
            self.assertNotIn("session", bot._turn_dest)

    async def test_a_message_arriving_mid_turn_is_refused_not_swallowed(self):
        # Observed live: a message sent while a turn was running was forwarded,
        # the daemon refused it with an *info* notice the client never shows, and
        # the forward itself reset _reply_parts -- destroying the reply in flight.
        # The user lost both their message and the answer they were waiting for.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            sent = []

            class FakeWebSocket:
                async def send(self, payload):
                    sent.append(json.loads(payload))

            bot._ws = FakeWebSocket()
            await self._stream(bot, "half a reply so far")

            await bot._forward("session", Dest(-1001, 20), "a second message, mid-turn")
            self.assertEqual(sent, [], "nothing may reach the daemon mid-turn")
            self.assertEqual(bot._reply_parts["session"], ["half a reply so far"],
                             "the in-flight reply must survive")
            self.assertEqual(bot.telegram.messages, [(20, BUSY_TURN)])

            # The original turn still finishes and delivers.
            await self._idle(bot)
            self.assertEqual(bot.telegram.html_messages[0][2], "half a reply so far")

    async def test_a_hung_chat_action_does_not_stall_the_event_pipeline(self):
        # Observed live (2026-08-25, 09:06): one Telegram sendChatAction hit its
        # 40s read timeout inside the event handler, and every daemon event
        # queued behind it -- a finished reply reached the chat 45 seconds late.
        # The indicator send must be detached from the pipeline.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            release = asyncio.Event()

            class HangingTelegram(FakeTelegram):
                async def chat_action(self, chat_id, action, thread=None):
                    await release.wait()
                    await super().chat_action(chat_id, action, thread=thread)

            bot.telegram = HangingTelegram()
            # Must return promptly even though the chat action never has.
            await asyncio.wait_for(self._stream(bot, "chunk"), timeout=0.5)
            await asyncio.wait_for(self._tool_call(bot), timeout=0.5)
            release.set()
            await asyncio.sleep(0)

    async def test_turn_ended_finalizes_and_the_following_idle_is_a_no_op(self):
        # The turn's end is now a fact the daemon states, not a state the client
        # infers. The idle that follows must find nothing left to do.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "the reply")
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 9})
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "the reply")
            self.assertNotIn("session", bot._turn_dest)
            self.assertEqual(bot._activity_tasks, {})
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1,
                             "the idle after turn_ended must not deliver twice")
            self.assertEqual(bot.telegram.messages, [],
                             "a delivered turn must not be reported as silent")

    async def test_a_turn_that_delivered_nothing_is_said_out_loud(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "refusal", "output_chars": 0})
            self.assertEqual(bot.telegram.html_messages, [])
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("without delivering", bot.telegram.messages[0][1])
            self.assertIn("refusal", bot.telegram.messages[0][1])

    async def test_output_lost_in_the_client_reads_differently_from_no_output(self):
        # The daemon streamed 500 characters; none reached this chat. That is a
        # client-side loss -- the resume-idle bug's exact shape -- and the report
        # must not blame the agent for it.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 500})
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("lost", bot.telegram.messages[0][1])
            self.assertIn("500", bot.telegram.messages[0][1])

    async def test_an_errored_or_cancelled_turn_is_not_double_reported(self):
        # The error notice already told the chat; a cancelled turn is empty on
        # purpose. Neither deserves a second message.
        for outcome, stop in (("error", None), ("completed", "cancelled")):
            with tempfile.TemporaryDirectory() as directory:
                bot = self._bot_mid_turn(directory)
                await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                         "turn_id": "t1", "outcome": outcome,
                                         "stop_reason": stop, "output_chars": 0})
                self.assertEqual(bot.telegram.messages, [],
                                 f"outcome={outcome} stop={stop} must stay quiet")
                self.assertNotIn("session", bot._turn_dest)

    async def test_status_reports_the_daemon_and_the_bot_view(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turn_id["session"] = "t123"
            bot._turn_started_at["session"] = time.monotonic() - 5
            bot._last_event_at["session"] = time.monotonic() - 3
            bot._activity_state["session"] = "streaming"
            bot._reply_parts["session"] = ["buffered text"]
            bot._bind("session", 20)

            class FakeDaemon:
                async def version(self):
                    return {"version": "9.9-test"}

                async def sessions(self):
                    return [{"session_id": "session", "name": "work thing",
                             "state": "working", "path": "/tmp"}]

            bot.daemon = FakeDaemon()
            handled = await bot._command(Dest(-1001, 20), "/status")
            self.assertTrue(handled)
            report = bot.telegram.messages[0][1]
            for expected in ("9.9-test", "work thing", "t123", "streaming",
                             f"buffered={len('buffered text')}", "quiet=3s"):
                self.assertIn(expected, report)

    async def test_a_started_turn_is_never_stranded_by_the_pre_turn_guard(self):
        # The guard ignores an idle for a turn that never reported working. If a
        # flag is wrong, that must not strand the session: streamed output is
        # proof the turn began, so the idle ends it regardless.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turn_working.discard("session")
            await self._stream(bot, "output proves the turn began")
            await self._idle(bot)
            self.assertNotIn("session", bot._turn_dest)
            self.assertEqual(bot._activity_tasks, {})
            self.assertEqual(len(bot.telegram.html_messages), 1)

    async def test_the_daemon_coming_back_is_announced_with_its_revision(self):
        # A restart used to be invisible from the phone unless a turn happened
        # to be in flight. Self-updating from inside a session makes restarts
        # routine, so both edges get said out loud.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)

            class FakeDaemon:
                async def version(self):
                    return {"version": "0.1.0-abc1234"}

            bot.daemon = FakeDaemon()
            await bot._announce_daemon_up()
            self.assertEqual(len(bot.telegram.messages), 1)
            chat_id, text = bot.telegram.messages[0]
            self.assertIsNone(chat_id)  # General: an announcement is bot-level
            self.assertIn("0.1.0-abc1234", text)

    async def test_an_unreachable_daemon_still_announces_that_it_is_up(self):
        # The version lookup is a nicety; failing it must not swallow the
        # announcement, which is the part the user actually needs.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)

            class BrokenDaemon:
                async def version(self):
                    raise ApiError("connection refused")

            bot.daemon = BrokenDaemon()
            await bot._announce_daemon_up()
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("up", bot.telegram.messages[0][1])

    async def test_a_failed_announcement_never_propagates(self):
        # An announcement that raised would turn a reconnect blip into an
        # outage -- it is called from the reconnect path itself.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)

            class BrokenTelegram(FakeTelegram):
                async def message(self, chat_id, text, reply_to=None,
                                  silent=False, thread=None):
                    raise ApiError("Too Many Requests")

            bot.telegram = BrokenTelegram()
            await bot._announce(DAEMON_DOWN)  # must not raise

    async def test_read_timeout_is_an_api_error_not_a_teardown(self):
        # urllib wraps a connect failure in URLError but lets a read timeout
        # through as TimeoutError. Escaping as OSError kills the polling task
        # and takes the daemon websocket -- and the in-flight reply -- with it.
        def raise_timeout(*_args, **_kwargs):
            raise TimeoutError("The read operation timed out")

        with patch("urllib.request.urlopen", raise_timeout):
            with self.assertRaises(ApiError):
                await _json_request("http://localhost/nowhere")

    async def test_dropped_connection_keeps_the_turn_map_for_reconciliation(self):
        # The old behaviour told the chat "anything not sent is gone" the moment
        # the connection dropped -- usually false, since the daemon keeps every
        # chunk. Now the in-memory state dies with the connection, but the
        # persisted map survives, and the next connection settles it.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            bot._turn_dest["session"] = Dest(-1001, 20)
            bot._reply_parts["session"] = ["half an answer"]
            bot._persist_turns()
            bot._reset_connection_state()
            self.assertEqual(bot._turn_dest, {})
            self.assertEqual(bot._reply_parts, {})
            self.assertEqual(bot.telegram.messages, [])
            persisted = json.loads(bot._turns_file.read_text())
            self.assertEqual(persisted["session"]["thread"], 20)

    async def test_a_long_quiet_turn_is_said_once_per_spell(self):
        # "Stuck" cannot be told apart from a long tool call from outside, so
        # the bot states the observable fact -- how long since the daemon last
        # said anything -- once per quiet spell, not once per tick.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._last_event_at["session"] = time.monotonic() - QUIET_TURN_SECONDS - 1
            await bot._check_quiet("session", Dest(-1001, 20))
            await bot._check_quiet("session", Dest(-1001, 20))
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("Nothing from the agent", bot.telegram.messages[0][1])
            # An event ends the spell; the next long silence is its own news.
            await self._stream(bot, "sign of life")
            bot._last_event_at["session"] = time.monotonic() - QUIET_TURN_SECONDS - 1
            await bot._check_quiet("session", Dest(-1001, 20))
            self.assertEqual(len(bot.telegram.messages), 2)
            await self._idle(bot)


class TurnEndRacesTests(unittest.IsolatedAsyncioTestCase):
    """Nothing may write for a turn that has already ended.

    Observed in use: an extra "🛠 Working…" arriving after the final reply,
    because cancelling the activity loop is not instantaneous and a tick
    already inside an HTTP call still completes.
    """

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        return bot

    async def test_a_stale_progress_tick_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._progress_lines["session"] = ["⚙️ did a thing"]
            bot._progress_dirty.add("session")
            # No entry in _turn_dest: the turn is over.
            await bot._update_progress("session", Dest(-1001, 20))
            self.assertEqual(bot.telegram.messages, [],
                             "no Working… may appear after the reply")

    async def test_the_final_stamp_is_still_allowed(self):
        # It runs *after* the destination is popped, so it must be exempt.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._progress_lines["session"] = ["⚙️ did a thing"]
            await bot._update_progress("session", Dest(-1001, 20),
                                       final_note="✅ done in 3s")
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("✅ done in 3s", bot.telegram.messages[0][1])

    async def test_a_stale_chat_action_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._send_action("session", Dest(-1001, 20))
            self.assertEqual(bot.telegram.actions, [],
                             "no typing… after the answer has arrived")

    async def test_finish_turn_waits_for_the_activity_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._turn_dest["session"] = Dest(-1001, 20)
            started = asyncio.Event()

            async def _loop():
                started.set()
                await asyncio.sleep(3600)
            task = asyncio.create_task(_loop())
            bot._activity_tasks["session"] = task
            await started.wait()
            await bot._finish_turn("session", {"turn_id": "t1"})
            self.assertTrue(task.done(),
                            "the loop must be settled before the reply is sent")


class TurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The persisted turn map: a bot restart mid-turn must not orphan the reply.

    Observed live before this existed: a scheduled bot restart landed five
    seconds into a fresh turn, the new process had no idea which chat the
    reply belonged to, and the reply was never delivered -- with the
    silent-turn report unable to fire, since nothing was tracking the turn.
    """

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
            default_path=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        return bot

    class _Daemon:
        def __init__(self, state="working", transcript=None):
            self._state = state
            self._transcript = transcript or []

        async def sessions(self):
            if self._state is None:
                return []
            return [{"session_id": "session", "name": "work thing",
                     "state": self._state, "path": "/tmp"}]

        async def session(self, session_id):
            return {"session_id": session_id, "transcript": self._transcript}

    @staticmethod
    def _transcript(*agent_chunks):
        return [{"type": "message", "role": "user", "text": "do the thing"},
                *({"type": "message", "role": "agent", "text": chunk}
                  for chunk in agent_chunks)]

    async def test_a_forwarded_turn_is_persisted_and_removed_when_it_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            sent = []

            class FakeWebSocket:
                async def send(self, payload):
                    sent.append(payload)

            bot._ws = FakeWebSocket()
            await bot._forward("session", Dest(-1001, 20), "do the thing")
            persisted = json.loads(bot._turns_file.read_text())
            self.assertEqual(persisted["session"]["thread"], 20)
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 0})
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_a_restarted_bot_adopts_a_turn_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, 20)
            # Long-running: without a seeded quiet clock, adoption would fire
            # a spurious "quiet" warning off the inherited start time.
            old._turn_started_at["session"] = time.monotonic() - 600
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon(
                "working", self._transcript("the full", " reply"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_dest, {"session": Dest(-1001, 20)})
            self.assertIn("session", bot._adopted)
            await bot._check_quiet("session", Dest(-1001, 20))
            self.assertEqual(bot.telegram.messages, [],
                             "adoption must not trigger the quiet warning")
            # Post-adoption chunks accumulate but must not be delivered from
            # the gappy buffer: the settled transcript at turn end is the only
            # complete source, and nothing may be sent twice.
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": " reply"})
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 14})
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "the full reply")
            self.assertEqual(bot.telegram.messages, [],
                             "an adopted, delivered turn has nothing to warn about")
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})
            self.assertEqual(bot._activity_tasks, {})

    async def test_a_turn_that_ended_while_the_bot_was_away_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, 20)
            old._turn_started_at["session"] = time.monotonic()
            # Six raw characters were already flushed before the restart.
            old._consumed["session"] = 6
            old._delivered["session"] = 6
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript("before", " and after"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_dest, {}, "an ended turn is not adopted")
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("recovered", bot.telegram.messages[0][1].lower())
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "and after")
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_an_ended_turn_with_nothing_undelivered_stays_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, 20)
            old._turn_started_at["session"] = time.monotonic()
            old._consumed["session"] = len("the whole reply")
            old._delivered["session"] = len("the whole reply")
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript("the whole reply"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot.telegram.messages, [])
            self.assertEqual(bot.telegram.html_messages, [])

    async def test_an_ended_turn_that_produced_nothing_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, 20)
            old._turn_started_at["session"] = time.monotonic()
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript())
            await bot._reconcile_persisted_turns()
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("without delivering", bot.telegram.messages[0][1])

    async def test_a_vanished_session_is_the_only_true_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, 20)
            old._turn_started_at["session"] = time.monotonic()
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon(state=None)
            await bot._reconcile_persisted_turns()
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("no longer exists", bot.telegram.messages[0][1])
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_manager_turns_die_with_the_bot(self):
        # The manager session is ephemeral and respawned on every connect,
        # and it lives in General (thread None); its turns are
        # session-management chatter, not work output worth reviving.
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_dest["session"] = Dest(-1001, None)
            old._turn_started_at["session"] = time.monotonic()
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("working")
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_dest, {})
            self.assertEqual(bot.telegram.messages, [])
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})


class ForumTopicTests(unittest.IsolatedAsyncioTestCase):
    """Routing by topic, and keeping each topic looking like its session."""

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        return bot

    def _update(self, thread, text, chat=-1001, message_id=1):
        message = {"chat": {"id": chat}, "text": text, "message_id": message_id,
                   "from": {"id": 7}}
        if thread is not None:
            message["message_thread_id"] = thread
        return {"message": message}

    async def test_each_topic_routes_to_its_own_session(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("alpha", 11)
            bot._bind("beta", 22)
            forwarded = []
            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append((session, dest.thread, text))
            bot._forward = _forward
            await bot._handle_update(self._update(11, "for alpha"))
            await bot._handle_update(self._update(22, "for beta"))
            self.assertEqual(forwarded,
                             [("alpha", 11, "for alpha"), ("beta", 22, "for beta")])

    async def test_general_spawns_a_manager_when_the_forum_came_later(self):
        # A forum adopted after connect has no manager: the connect-time spawn
        # already ran and found none. General must still work.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            self.assertIsNone(bot.manager_session_id)
            spawned = []

            class _Daemon:
                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    spawned.append(name)
                    return {"session_id": "mgr", "name": name}
            bot.daemon = _Daemon()
            forwarded = []

            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append(session)
            bot._forward = _forward
            await bot._handle_update(self._update(None, "hello"))
            self.assertEqual(spawned, ["telegram manager"])
            self.assertEqual(forwarded, ["mgr"])

    async def test_general_routes_to_the_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = "manager"

            class _Mgr:
                async def session(self, session_id):
                    return {"session_id": session_id}
            bot.daemon = _Mgr()
            forwarded = []
            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append((session, dest.thread))
            bot._forward = _forward
            await bot._handle_update(self._update(None, "spawn me one"))
            self.assertEqual(forwarded, [("manager", None)])

    async def test_an_unbound_topic_says_so_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = "manager"
            await bot._handle_update(self._update(99, "nobody owns this"))
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("No FalconFox session owns", bot.telegram.messages[0][1])

    async def test_topic_service_messages_are_not_answered(self):
        # The bot's own createForumTopic echoes back as a service message; a
        # reply to each would spam every topic it makes.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_thread_id": 5, "message_id": 2,
                "forum_topic_created": {"name": "session-one"}}})
            self.assertEqual(bot.telegram.messages, [])

    async def test_a_replayed_migration_notice_is_not_an_alarm(self):
        # Telegram replays the migration service message from the OLD chat,
        # and its target is the id we are already configured with. Treating
        # that as "the forum moved" fired on every restart -- observed live
        # the first time the dev bot started.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            records = []
            with self.assertLogs("falconfox.telegram", level="ERROR") as caught:
                logging.getLogger("falconfox.telegram").error("sentinel")
                await bot._handle_update({"message": {
                    "chat": {"id": -5481438232}, "message_id": 3,
                    "migrate_to_chat_id": -1001}})
                records = caught.output
            self.assertEqual(records, ["ERROR:falconfox.telegram:sentinel"])

    async def test_a_real_migration_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            with self.assertLogs("falconfox.telegram", level="ERROR") as caught:
                await bot._handle_update({"message": {
                    "chat": {"id": -1001}, "message_id": 3,
                    "migrate_to_chat_id": -1002}})
            self.assertIn("migrated to chat id -1002", caught.output[0])

    async def test_a_non_message_update_is_ignored_quietly(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            with self.assertNoLogs("falconfox.telegram", level="INFO"):
                await bot._handle_update({"my_chat_member": {"chat": {"id": -1001}}})

    async def test_new_spawns_and_confirms_without_a_pointer(self):
        # /new used to write the focus pointer. The pointer is gone, so the
        # call raised AttributeError -- which killed the whole bot, observed
        # live. The topic now comes from the daemon's session_added event.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            spawned = []

            class _Daemon:
                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    spawned.append((path, name))
                    return {"session_id": "new1", "name": name}
            bot.daemon = _Daemon()
            handled = await bot._command(Dest(-1001, None), "/new /tmp a name")
            self.assertTrue(handled)
            self.assertEqual(spawned, [("/tmp", "a name")])
            self.assertIn("new1", bot.telegram.messages[0][1])

    async def test_one_bad_update_does_not_kill_the_poll_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            seen = []

            async def _explode(update):
                seen.append(update)
                if len(seen) == 1:
                    raise RuntimeError("boom")

            bot._handle_update = _explode

            class _Telegram:
                def __init__(self):
                    self.calls = 0

                async def updates(self, offset):
                    self.calls += 1
                    if self.calls > 2:
                        raise asyncio.CancelledError
                    return [{"update_id": self.calls}]
            bot.telegram = _Telegram()
            with self.assertLogs("falconfox.telegram", level="ERROR"):
                with contextlib.suppress(asyncio.CancelledError):
                    await bot._poll_telegram()
            # The second update was still handled: the loop survived the first.
            self.assertEqual(len(seen), 2)

    def _unpinned(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, state_dir=Path(directory)))
        bot.telegram = FakeTelegram()
        return bot

    async def test_being_added_to_a_usable_forum_adopts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._unpinned(directory)

            class _Daemon:
                async def sessions(self):
                    return []

                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    return {"session_id": "mgr", "name": name}
            bot.daemon = _Daemon()
            await bot._handle_update({"my_chat_member": {
                "chat": {"id": -2002}, "from": {"id": 7},
                "new_chat_member": {"status": "administrator"}}})
            self.assertEqual(bot.forum_chat_id, -2002)
            self.assertIn("Forum set", bot.telegram.messages[0][1])

    async def test_a_group_that_is_not_a_forum_says_which_condition_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._unpinned(directory)
            bot.telegram.chat_info = {"is_forum": False, "title": "plain"}
            await bot._handle_update({"my_chat_member": {
                "chat": {"id": -2002}, "from": {"id": 7},
                "new_chat_member": {"status": "administrator"}}})
            self.assertIsNone(bot.forum_chat_id)
            self.assertIn("Topics are not enabled", bot.telegram.messages[0][1])

    async def test_a_forum_without_manage_topics_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._unpinned(directory)
            bot.telegram.member_info = {"status": "administrator",
                                        "can_manage_topics": False}
            await bot._handle_update({"my_chat_member": {
                "chat": {"id": -2002}, "from": {"id": 7},
                "new_chat_member": {"status": "administrator"}}})
            self.assertIsNone(bot.forum_chat_id)
            self.assertIn("Manage Topics", bot.telegram.messages[0][1])

    async def test_a_pinned_forum_is_never_replaced_by_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)          # pinned to -1001
            await bot._handle_update({"my_chat_member": {
                "chat": {"id": -2002}, "from": {"id": 7},
                "new_chat_member": {"status": "administrator"}}})
            self.assertEqual(bot.forum_chat_id, -1001)

    async def test_a_migration_is_followed_when_the_forum_is_learned(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._unpinned(directory)
            bot._learn_forum(-5000)
            await bot._handle_update({"message": {
                "chat": {"id": -5000}, "from": {"id": 7}, "message_id": 1,
                "migrate_to_chat_id": -1006000}})
            self.assertEqual(bot.forum_chat_id, -1006000)

    async def test_being_removed_from_the_forum_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._unpinned(directory)
            bot._learn_forum(-2002)
            await bot._handle_update({"my_chat_member": {
                "chat": {"id": -2002}, "from": {"id": 7},
                "new_chat_member": {"status": "left"}}})
            self.assertIn("no longer in the forum", bot.telegram.messages[0][1])

    async def test_the_private_chat_reaches_a_session_of_its_own(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = "manager"
            spawned = []

            class _Daemon:
                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    spawned.append(name)
                    return {"session_id": "concierge", "name": name}
            bot.daemon = _Daemon()
            forwarded = []

            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append((session, dest))
            bot._forward = _forward
            await bot._handle_update({"message": {
                "chat": {"id": 7}, "from": {"id": 7},
                "message_id": 1, "text": "is my forum ok?"}})
            self.assertEqual(forwarded, [("concierge", Dest(7, None))])
            self.assertEqual(spawned, ["telegram private chat"])

    async def test_a_remembered_infrastructure_session_is_reused(self):
        # They persist and sleep now, so a restart must find them again --
        # otherwise every restart would make another manager, then another.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id, bot.concierge_session_id = "mgr", "conc"
            bot._persist_infra()
            again = self._bot(directory)
            again._load_infra()
            self.assertEqual((again.manager_session_id, again.concierge_session_id),
                             ("mgr", "conc"))

    async def test_a_remembered_session_that_is_gone_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.concierge_session_id = "deleted"
            spawns = []

            class _Daemon:
                async def session(self, session_id):
                    raise ApiError("no such session")

                async def spawn(self, path, name=None, backend=None,
                                ephemeral=False, hidden=None):
                    spawns.append(name)
                    return {"session_id": "fresh", "name": name}
            bot.daemon = _Daemon()
            self.assertEqual(await bot._ensure_concierge(), "fresh")
            self.assertEqual(len(spawns), 1)

    async def test_infrastructure_is_hidden_but_not_ephemeral(self):
        # Hidden keeps it out of the listing; NOT ephemeral is what lets it be
        # stopped and resumed instead of destroyed.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            kwargs = {}

            class _Daemon:
                async def session(self, session_id):
                    raise ApiError("none")

                async def spawn(self, path, name=None, backend=None,
                                ephemeral=False, hidden=None):
                    kwargs.update(ephemeral=ephemeral, hidden=hidden)
                    return {"session_id": "x", "name": name}
            bot.daemon = _Daemon()
            await bot._ensure_concierge()
            self.assertEqual(kwargs, {"ephemeral": False, "hidden": True})

    async def test_the_private_chat_session_is_spawned_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            spawns = []

            class _Daemon:
                async def session(self, session_id):
                    return {"session_id": session_id}

                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    spawns.append(name)
                    return {"session_id": "concierge", "name": name}
            bot.daemon = _Daemon()

            async def _forward(session, dest, text, prompt_msg=None):
                pass
            bot._forward = _forward
            for _ in range(3):
                await bot._handle_update({"message": {
                    "chat": {"id": 7}, "from": {"id": 7},
                    "message_id": 1, "text": "hello"}})
            self.assertEqual(len(spawns), 1)

    async def test_the_private_chat_works_with_no_forum_configured(self):
        # The whole point of the channel: reachable when nothing else is.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, state_dir=Path(directory)))
            bot.telegram = FakeTelegram()
            self.assertIsNone(bot.forum_chat_id)

            class _Daemon:
                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None):
                    return {"session_id": "concierge", "name": name}
            bot.daemon = _Daemon()
            forwarded = []

            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append(session)
            bot._forward = _forward
            await bot._handle_update({"message": {
                "chat": {"id": 7}, "from": {"id": 7},
                "message_id": 1, "text": "help me set up"}})
            self.assertEqual(forwarded, ["concierge"])

    async def test_a_message_from_anyone_but_the_owner_is_ignored(self):
        # "Which chat" used to answer "who". It no longer will, once the
        # private chat is functional and anyone can open one with a bot.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("alpha", 11)
            forwarded = []

            async def _forward(session, dest, text, prompt_msg=None):
                forwarded.append(session)
            bot._forward = _forward
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_thread_id": 11,
                "message_id": 1, "text": "hello", "from": {"id": 999}}})
            self.assertEqual(forwarded, [])

    async def test_a_pinned_forum_beats_a_learned_one(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)          # pinned to -1001
            bot._learn_forum(-2002)
            self.assertEqual(bot.forum_chat_id, -1001)

    async def test_a_learned_forum_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, state_dir=Path(directory)))
            bot.telegram = FakeTelegram()
            self.assertIsNone(bot.forum_chat_id)  # a fresh deployment has none
            bot._learn_forum(-2002)
            again = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, state_dir=Path(directory)))
            again._load_forum()
            self.assertEqual(again.forum_chat_id, -2002)

    async def test_a_capacity_notice_lands_in_the_evicted_topic(self):
        # A topic that closes under the user must say why, or it reads as the
        # session mysteriously dying rather than the system managing memory.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("evicted", 12)
            await bot._handle_event({
                "type": "notice", "session_id": "evicted", "level": "info",
                "kind": "capacity", "message": "Stopped to free a session slot"})
            self.assertEqual(bot.telegram.messages,
                             [(12, "⏸ Stopped to free a session slot")])

    async def test_ordinary_notices_do_not_reach_the_topic(self):
        # Most notices are internal chatter (auto-allowed tools, re-sent
        # context); only those marked as capacity are for the user.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("alpha", 12)
            await bot._handle_event({
                "type": "notice", "session_id": "alpha",
                "message": "auto-allowed: read_file"})
            self.assertEqual(bot.telegram.messages, [])

    async def test_a_capacity_notice_for_an_untracked_session_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_event({
                "type": "notice", "session_id": "nobody", "level": "info",
                "kind": "capacity", "message": "Stopped"})
            self.assertEqual(bot.telegram.messages, [])

    async def test_a_new_session_gets_a_topic_and_it_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_event({
                "type": "session_added", "session_id": "alpha", "name": "work thing"})
            self.assertEqual(bot.telegram.topics, ["work thing"])
            thread = bot._topics["alpha"]
            # A restart that forgot the map would make a second topic.
            again = self._bot(directory)
            again._load_topics()
            self.assertEqual(again._topics, {"alpha": thread})
            self.assertEqual(again._threads, {thread: "alpha"})

    async def test_a_rename_retitles_the_topic_once(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_event({
                "type": "session_added", "session_id": "alpha", "name": "old"})
            for _ in range(3):
                await bot._handle_event({
                    "type": "session_updated", "session_id": "alpha",
                    "name": "new", "state": "idle"})
            # Only the transition acts: session_updated arrives constantly.
            self.assertEqual(getattr(bot.telegram, "renamed", []),
                             [(bot._topics["alpha"], "new")])

    async def test_stopping_a_session_leaves_its_topic_open(self):
        # Closing would discourage the action that recovers -- `send`
        # auto-resumes -- and its bookkeeping did not survive a bot restart,
        # leaving topics shut for good. The capacity notice says it instead.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_event({
                "type": "session_added", "session_id": "alpha", "name": "work"})
            for state in ("stored", "idle"):
                await bot._handle_event({
                    "type": "session_updated", "session_id": "alpha",
                    "name": "work", "state": state})
            self.assertEqual(getattr(bot.telegram, "closed", []), [])
            self.assertEqual(getattr(bot.telegram, "reopened", []), [])

    async def test_a_deleted_session_takes_its_topic_with_it(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_event({
                "type": "session_added", "session_id": "alpha", "name": "work"})
            thread = bot._topics["alpha"]
            await bot._handle_event({"type": "session_removed", "session_id": "alpha"})
            self.assertEqual(getattr(bot.telegram, "deleted", []), [thread])
            self.assertEqual(bot._topics, {})

    async def test_a_pre_forum_turn_record_is_dropped(self):
        # Records written before the cutover carry a "chat" id that means
        # nothing in a forum, so there is nowhere sensible to deliver them.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._turns_file.parent.mkdir(parents=True, exist_ok=True)
            bot._turns_file.write_text(json.dumps(
                {"session": {"chat": 20, "consumed": 0, "delivered": 0}}))

            class _Daemon:
                async def sessions(self):
                    return [{"session_id": "session", "name": "n",
                             "state": "working", "path": "/tmp"}]
            bot.daemon = _Daemon()
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_dest, {})
            self.assertEqual(bot.telegram.messages, [])


class ShellRunnerTests(unittest.IsolatedAsyncioTestCase):
    """The tmux-backed runner, exercised against real tmux where it exists."""

    def setUp(self):
        if shutil.which("tmux") is None:
            self.skipTest("tmux is not installed")

    async def _run(self, runner, command, cwd):
        job = await runner.start(command, Path(cwd))
        self.addAsyncCleanup(runner.kill, job)
        return job, await runner.wait(job, timeout=30)

    async def test_status_output_and_cwd_come_back(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ShellRunner(Path(directory))
            job, status = await self._run(runner, "pwd; exit 3", "/tmp")
            self.assertEqual(status, 3)
            self.assertIn("/tmp", job.read_output())

    async def test_the_command_is_not_requoted(self):
        # The command is written to a script rather than passed through two
        # shells, so quoting survives verbatim. Re-joining shlex tokens here
        # would break exactly the commands worth running by hand.
        with tempfile.TemporaryDirectory() as directory:
            runner = ShellRunner(Path(directory))
            job, status = await self._run(
                runner, """printf '%s' "a 'b' c" """, "/tmp")
            self.assertEqual(status, 0)
            self.assertEqual(job.read_output(), "a 'b' c")

    async def test_a_slow_command_keeps_running_after_the_wait_gives_up(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ShellRunner(Path(directory))
            job = await runner.start("sleep 30", Path("/tmp"))
            self.addAsyncCleanup(runner.kill, job)
            self.assertIsNone(await runner.wait(job, timeout=1))
            self.assertIn(job.session, await runner.live_sessions())
            self.assertTrue(await runner.kill(job))


class ShellCommandTests(unittest.IsolatedAsyncioTestCase):
    """/sh routing: where it runs, and what it says when it cannot."""

    class FakeRunner:
        def __init__(self):
            self.calls = []
            self.jobs = {}

        def available(self):
            return True

        async def start(self, command, cwd):
            # Records and stops: these tests are about *where* a command is
            # sent, which is decided before tmux is involved at all.
            self.calls.append((command, Path(cwd)))
            raise RuntimeError("stub runner")

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory), default_path=Path("/tmp"),
        ))
        bot.telegram = FakeTelegram()
        bot._shell = self.FakeRunner()
        return bot

    async def test_a_topic_runs_in_its_session_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)

            class Daemon:
                async def session(self, session_id):
                    return {"session_id": session_id, "path": "/srv/work"}

            bot.daemon = Daemon()
            await bot._command(Dest(-1001, 42), "/sh ls -la")
            self.assertEqual(bot._shell.calls, [("ls -la", Path("/srv/work"))])

    async def test_an_unreachable_daemon_falls_back_to_the_default_path(self):
        # A wedged daemon is the case /sh exists for, so failing to resolve a
        # session's directory must not stop the command from running.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)
            await bot._command(Dest(-1001, 42), "/sh whoami")
            self.assertEqual(bot._shell.calls, [("whoami", Path("/tmp"))])

    async def test_bare_sh_explains_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._command(Dest(-1001, None), "/sh   ")
            self.assertEqual(bot._shell.calls, [])
            self.assertIn("Usage: /sh", bot.telegram.messages[0][1])


class RenderingTests(unittest.TestCase):
    def test_common_markdown_becomes_telegram_html(self):
        markdown = (
            "# Title\n\nUse `falconfox list` for **bold** and *italic* moves.\n\n"
            "```python\nprint('a < b')\n```\n\nSee [docs](https://example.com/a?x=1&y=2)."
        )
        messages = render_messages(markdown)
        self.assertEqual(len(messages), 1)
        html = messages[0].html
        self.assertIn("<b>Title</b>", html)
        self.assertIn("<code>falconfox list</code>", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn("<i>italic</i>", html)
        self.assertIn('<pre><code class="language-python">print(\'a &lt; b\')</code></pre>', html)
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2">docs</a>', html)
        self.assertEqual(messages[0].plain, markdown.strip())

    def test_html_in_agent_text_is_escaped(self):
        messages = render_messages("compare a<b> with &c and snake_case_name")
        self.assertEqual(messages[0].html, "compare a&lt;b&gt; with &amp;c and snake_case_name")

    def test_tables_render_monospaced(self):
        messages = render_messages("| id | name |\n|----|------|\n| 1  | foo  |")
        self.assertTrue(messages[0].html.startswith("<pre>"))

    def test_long_turns_split_under_the_limit(self):
        markdown = "\n\n".join(f"paragraph {number} " + "word " * 200
                               for number in range(20))
        messages = render_messages(markdown)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message.html), TELEGRAM_MESSAGE_LIMIT)

    def test_giant_code_block_splits_into_multiple_pre_chunks(self):
        markdown = "```\n" + "\n".join(f"line {number}" for number in range(2000)) + "\n```"
        messages = render_messages(markdown)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message.html), TELEGRAM_MESSAGE_LIMIT)
            self.assertTrue(message.html.startswith("<pre>"))
            self.assertTrue(message.html.endswith("</pre>"))


if __name__ == "__main__":
    unittest.main()
