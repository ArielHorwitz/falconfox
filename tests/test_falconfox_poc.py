from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from falconfox.cli import CliError, _guard_self_target
from falconfox.coordinator import SessionCoordinator
from falconfox.storage import SessionStore
from falconfox_telegram.api import ApiError, _json_request
from falconfox_telegram.bot import (BUSY_TURN, INTERRUPTED_TURN, TURN_ACTIONS,
                                    BotConfig, FalconFoxTelegramBot)
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


class CliSafetyTests(unittest.TestCase):
    def test_session_cannot_delete_itself(self):
        with patch.dict(os.environ, {"FALCONFOX_SESSION_ID": "deadbeef"}):
            with self.assertRaises(CliError):
                _guard_self_target("deadbeef", "delete")
            _guard_self_target("another", "delete")


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.html_messages = []
        self.actions = []
        self.action_error = None

    async def message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def html_message(self, chat_id, html_text, plain_fallback):
        self.html_messages.append((chat_id, html_text, plain_fallback))

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
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "hello "})
            await bot._handle_event({"type": "tool_call", "session_id": "session",
                                     "title": "hidden"})
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "**world**"})
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            # One action per state *change*: a chunk-by-chunk stream must not
            # produce a call per chunk, and the tool call is visible as state
            # without being rendered as content.
            self.assertEqual(fake.actions, [
                (20, TURN_ACTIONS["working"]),
                (20, TURN_ACTIONS["streaming"]),
                (20, TURN_ACTIONS["tool"]),
                (20, TURN_ACTIONS["streaming"]),
            ])
            self.assertEqual(fake.messages, [])
            self.assertEqual(fake.html_messages,
                             [(20, "hello <b>world</b>", "hello **world**")])


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

    async def test_a_tool_call_flushes_what_has_streamed_so_far(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "x" * 300)
            await self._tool_call(bot)
            # Handed over as soon as output stopped, rather than held to the end.
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertIn("x" * 300, bot.telegram.html_messages[0][1])

            await self._stream(bot, "tail")
            await self._idle(bot)
            # The remainder is a second message, and nothing is sent twice.
            self.assertEqual(len(bot.telegram.html_messages), 2)
            self.assertEqual(bot.telegram.html_messages[1][2], "tail")
            bot._activity_tasks and [t.cancel() for t in bot._activity_tasks.values()]

    async def test_a_short_partial_reply_waits_for_the_end_of_the_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "short")
            await self._tool_call(bot)
            self.assertEqual(bot.telegram.html_messages, [])
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "short")

    async def test_a_partial_reply_is_not_cut_inside_a_code_block(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            fence = "```python\n" + "y = 1\n" * 80
            await self._stream(bot, fence)
            await self._tool_call(bot)
            # Long enough to flush, but the fence is still open.
            self.assertGreater(len(fence), 240)
            self.assertEqual(bot.telegram.html_messages, [])
            await self._stream(bot, "```\ndone")
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "thought", "text": ""})
            # Balanced now, so the same guard lets it through.
            self.assertEqual(len(bot.telegram.html_messages), 1)
            await self._idle(bot)

    async def test_partial_flushes_are_rate_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            await self._stream(bot, "a" * 300)
            await self._tool_call(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            # A second burst inside the window is held rather than sent.
            await self._stream(bot, "b" * 300)
            await self._tool_call(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 2)
            self.assertEqual(bot.telegram.html_messages[1][2], "b" * 300)

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

    async def test_read_timeout_is_an_api_error_not_a_teardown(self):
        # urllib wraps a connect failure in URLError but lets a read timeout
        # through as TimeoutError. Escaping as OSError kills the polling task
        # and takes the daemon websocket -- and the in-flight reply -- with it.
        def raise_timeout(*_args, **_kwargs):
            raise TimeoutError("The read operation timed out")

        with patch("urllib.request.urlopen", raise_timeout):
            with self.assertRaises(ApiError):
                await _json_request("http://localhost/nowhere")

    async def test_dropped_connection_reports_the_lost_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            bot._turn_chat["session"] = 20
            bot._reply_parts["session"] = ["half an answer"]
            self.assertEqual(bot._reset_connection_state(), [20])
            # State is cleared, and the chat id is handed back so the caller can
            # tell that chat its reply is never coming.
            self.assertEqual(bot._turn_chat, {})
            self.assertEqual(bot._reply_parts, {})

    async def test_quiet_disconnect_reports_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
                "token", 10, 20, pointer_file=Path(directory, "focus"),
                default_path=Path(directory),
            ))
            bot.telegram = FakeTelegram()
            self.assertEqual(bot._reset_connection_state(), [])
            self.assertTrue(INTERRUPTED_TURN)


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
