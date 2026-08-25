from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from falconfox.cli import CliError, _guard_self_target
from falconfox.coordinator import SessionCoordinator
from falconfox.engine.session import AgentSession
from falconfox.storage import SessionStore
from falconfox.watchdog import StallWatchdog
from falconfox_telegram.api import ApiError, _json_request
from falconfox_telegram.bot import (BUSY_TURN, DAEMON_DOWN, QUIET_TURN_SECONDS,
                                    TURN_ACTIONS, BotConfig, FalconFoxTelegramBot)
from falconfox_telegram.rendering import TELEGRAM_MESSAGE_LIMIT, render_messages


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
            "state": "idle", "live": True, "created": "1", "last_active": "1",
        }
        self.coordinator._auto_named["focus"] = False
        self.coordinator._transcripts["focus"] = [
            {"type": "message", "role": "user", "text": "switch"}
        ]
        self.coordinator._persist_meta("focus")
        self.assertEqual(self.coordinator.list_sessions(), [])
        self.assertEqual(self.coordinator.list_sessions(include_ephemeral=True)[0]["session_id"],
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


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.message_replies = []
        self.html_messages = []
        self.html_replies = []
        self.edits = []
        self.actions = []
        self.action_error = None
        self._next_id = 100

    async def message(self, chat_id, text, reply_to=None):
        self.messages.append((chat_id, text))
        self.message_replies.append(reply_to)
        self._next_id += 1
        return self._next_id

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None):
        self.html_messages.append((chat_id, html_text, plain_fallback))
        self.html_replies.append(reply_to)

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def chat_action(self, chat_id, action):
        if self.action_error is not None:
            raise self.action_error
        self.actions.append((chat_id, action))


class TelegramEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_signals_each_state_and_sends_one_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            bot._turn_chat["session"] = 20
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
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            sent = []

            class FakeWebSocket:
                async def send(self, payload):
                    sent.append(json.loads(payload))

            bot._ws = FakeWebSocket()
            await bot._forward("session", 20, "do the thing")
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
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            fake = FakeTelegram()
            bot.telegram = fake
            bot._turn_chat["session"] = 20
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
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            bot._turn_chat["session"] = 20
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
            "token", 10, 20, pointer_file=Path(directory, "focus"),
            default_path=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._turn_chat["session"] = 20
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
            await bot._update_progress("session", 20)
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("Working", bot.telegram.messages[0][1])
            self.assertIn("first remark", bot.telegram.messages[0][1])
            message_id = bot._progress_msg["session"]

            await self._stream(bot, "second remark")
            await self._tool_call(bot)
            await bot._update_progress("session", 20)
            # Edited in place: no new message, and the edit carries the tail.
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertEqual(len(bot.telegram.edits), 1)
            self.assertEqual(bot.telegram.edits[0][1], message_id)
            self.assertIn("second remark", bot.telegram.edits[0][2])
            # Nothing dirty, nothing sent: the refresh tick must be a no-op.
            await bot._update_progress("session", 20)
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

    async def test_the_reply_threads_to_the_prompt_message(self):
        # Threading is also the notification story: in a group, a reply (like
        # a mention) cuts through a muted chat, so the progress message can be
        # spam-tolerant while the answer still pings.
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()

            class FakeWebSocket:
                async def send(self, payload):
                    pass

            bot._ws = FakeWebSocket()
            await bot._forward("session", 20, "question", prompt_msg=555)
            await self._stream(bot, "answer")
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 6})
            self.assertEqual(bot.telegram.html_messages[0][2], "answer")
            self.assertEqual(bot.telegram.html_replies, [555])

    async def test_the_idle_from_resuming_a_stored_session_is_not_the_turn_ending(self):
        # Sending to a stored session resumes it, and engine/session.py sets
        # `idle` once the ACP subprocess is up -- before the prompt runs. Taking
        # that for the end of the turn dropped _turn_chat before any chunk
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
            bot._turn_chat.clear()
            bot._reply_parts.clear()
            await bot._forward("session", 20, "do the thing")

            # The resume's idle, before the turn has ever reported working.
            await self._idle(bot)
            self.assertIn("session", bot._turn_chat,
                          "a turn that never began cannot have ended")

            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await self._stream(bot, "the real reply")
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "the real reply")
            self.assertNotIn("session", bot._turn_chat)

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

            await bot._forward("session", 20, "a second message, mid-turn")
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
                async def chat_action(self, chat_id, action):
                    await release.wait()
                    await super().chat_action(chat_id, action)

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
            self.assertNotIn("session", bot._turn_chat)
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
                self.assertNotIn("session", bot._turn_chat)

    async def test_status_reports_the_daemon_and_the_bot_view(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turn_id["session"] = "t123"
            bot._turn_started_at["session"] = time.monotonic() - 5
            bot._last_event_at["session"] = time.monotonic() - 3
            bot._activity_state["session"] = "streaming"
            bot._reply_parts["session"] = ["buffered text"]
            bot._pointer_value = "session"

            class FakeDaemon:
                async def version(self):
                    return {"version": "9.9-test"}

                async def sessions(self):
                    return [{"session_id": "session", "name": "work thing",
                             "state": "working", "path": "/tmp"}]

            bot.daemon = FakeDaemon()
            handled = await bot._command(20, "/status")
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
            self.assertNotIn("session", bot._turn_chat)
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
            self.assertEqual(chat_id, 20)  # the work chat, not the focus chat
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
                async def message(self, chat_id, text):
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
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            bot._turn_chat["session"] = 20
            bot._reply_parts["session"] = ["half an answer"]
            bot._persist_turns()
            bot._reset_connection_state()
            self.assertEqual(bot._turn_chat, {})
            self.assertEqual(bot._reply_parts, {})
            self.assertEqual(bot.telegram.messages, [])
            persisted = json.loads(bot._turns_file.read_text())
            self.assertEqual(persisted["session"]["chat"], 20)

    async def test_a_long_quiet_turn_is_said_once_per_spell(self):
        # "Stuck" cannot be told apart from a long tool call from outside, so
        # the bot states the observable fact -- how long since the daemon last
        # said anything -- once per quiet spell, not once per tick.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._last_event_at["session"] = time.monotonic() - QUIET_TURN_SECONDS - 1
            await bot._check_quiet("session", 20)
            await bot._check_quiet("session", 20)
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("Nothing from the agent", bot.telegram.messages[0][1])
            # An event ends the spell; the next long silence is its own news.
            await self._stream(bot, "sign of life")
            bot._last_event_at["session"] = time.monotonic() - QUIET_TURN_SECONDS - 1
            await bot._check_quiet("session", 20)
            self.assertEqual(len(bot.telegram.messages), 2)
            await self._idle(bot)


class TurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The persisted turn map: a bot restart mid-turn must not orphan the reply.

    Observed live before this existed: a scheduled bot restart landed five
    seconds into a fresh turn, the new process had no idea which chat the
    reply belonged to, and the reply was never delivered -- with the
    silent-turn report unable to fire, since nothing was tracking the turn.
    """

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 10, 20, pointer_file=Path(directory, "focus"),
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
            await bot._forward("session", 20, "do the thing")
            persisted = json.loads(bot._turns_file.read_text())
            self.assertEqual(persisted["session"]["chat"], 20)
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed",
                                     "stop_reason": "end_turn", "output_chars": 0})
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_a_restarted_bot_adopts_a_turn_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_chat["session"] = 20
            # Long-running: without a seeded quiet clock, adoption would fire
            # a spurious "quiet" warning off the inherited start time.
            old._turn_started_at["session"] = time.monotonic() - 600
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon(
                "working", self._transcript("the full", " reply"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_chat, {"session": 20})
            self.assertIn("session", bot._adopted)
            await bot._check_quiet("session", 20)
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
            old._turn_chat["session"] = 20
            old._turn_started_at["session"] = time.monotonic()
            # Six raw characters were already flushed before the restart.
            old._consumed["session"] = 6
            old._delivered["session"] = 6
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript("before", " and after"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_chat, {}, "an ended turn is not adopted")
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("recovered", bot.telegram.messages[0][1].lower())
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "and after")
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_an_ended_turn_with_nothing_undelivered_stays_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_chat["session"] = 20
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
            old._turn_chat["session"] = 20
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
            old._turn_chat["session"] = 20
            old._turn_started_at["session"] = time.monotonic()
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon(state=None)
            await bot._reconcile_persisted_turns()
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("no longer exists", bot.telegram.messages[0][1])
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_focus_chat_turns_die_with_the_bot(self):
        # The focus session is ephemeral and rotated on every connect; its
        # turns are session-management chatter, not work output worth reviving.
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turn_chat["session"] = 10
            old._turn_started_at["session"] = time.monotonic()
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("working")
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turn_chat, {})
            self.assertEqual(bot.telegram.messages, [])
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})


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
