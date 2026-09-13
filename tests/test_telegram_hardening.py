"""Hardening the Telegram client: the failure paths the surveys found.

Kept apart from `test_falconfox_poc.py` deliberately. What is exercised here
is what happens when Telegram refuses, when two tasks reach the same turn at
once, and when a topic the bot believes in is no longer there -- the paths the
existing suite never drives, because its fakes never fail and its handlers run
one at a time.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed

from falconfox_telegram import bot as bot_module
from falconfox_telegram.api import ApiError
from falconfox_telegram.bot import BotConfig, DAEMON_DOWN, Dest, FalconFoxTelegramBot

from test_falconfox_poc import UNREACHABLE_DAEMON, FakeTelegram


def _bot(directory: str) -> FalconFoxTelegramBot:
    bot = FalconFoxTelegramBot(BotConfig(
        "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
        state_dir=Path(directory), default_path=Path(directory),
    ))
    bot.telegram = FakeTelegram()
    return bot


class _EventStream:
    """The daemon's websocket: a fixed list of events, then the socket ends."""

    def __init__(self, *events: dict) -> None:
        self._frames = [json.dumps(event) for event in events]
        self.sent: list[dict] = []

    def __aiter__(self) -> "_EventStream":
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _Sockets:
    """`connect(url)` as `run()` consumes it: an async iterator of sockets."""

    def __init__(self, count: int) -> None:
        self._left = count
        self.opened = 0

    def __call__(self, url: str) -> "_Sockets":
        return self

    def __aiter__(self) -> "_Sockets":
        return self

    async def __anext__(self) -> object:
        if self._left <= 0:
            raise StopAsyncIteration
        self._left -= 1
        self.opened += 1
        return object()


class RefusingTelegram(FakeTelegram):
    """Telegram that will not take the reply. Not the deleted-topic error:
    this is the rate limit or the read timeout, which is permanent enough to
    matter and must never be read as the daemon being down."""

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        raise ApiError("Too Many Requests: retry after 30")


class TelegramFailureIsNotDaemonFailureTests(unittest.IsolatedAsyncioTestCase):
    """Survey F1: a send that fails propagated out of the event loop, and the
    reconnect handler read it as the daemon being gone -- announcing an outage
    of a healthy daemon, then resending the recovered turn to the same dead
    destination, forever."""

    async def test_a_failing_reply_send_does_not_end_the_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            bot.telegram = RefusingTelegram()
            bot._turn_dest["session"] = Dest(-1001, 20)
            bot._reply_parts["session"] = ["the answer nobody will see"]
            bot._turn_working.add("session")
            bot._ws = _EventStream(
                {"type": "turn_ended", "session_id": "session", "turn_id": "t1",
                 "outcome": "completed", "stop_reason": "end_turn",
                 "output_chars": 25},
                {"type": "session_added", "session_id": "later", "name": "later"},
            )
            with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                # Returns because the socket ended, not because a send failed.
                await bot._receive_events()
            self.assertIn("later", bot._topics,
                          "the event after the failure is still handled")
            self.assertTrue(
                any("session" in line and "turn_ended" in line
                    for line in logs.output),
                f"the dropped event must name the session and its type: {logs.output}")

    async def test_an_api_error_is_never_announced_as_a_daemon_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            sockets = _Sockets(2)
            bot._register_orientation = lambda: None

            async def refuse(websocket):
                raise ApiError("Too Many Requests: retry after 30")

            bot._run_connected = refuse
            with patch.object(bot_module, "connect", sockets), \
                    patch.object(bot_module, "StallWatchdog", _NoWatchdog):
                with self.assertRaises(ApiError):
                    await bot.run()
            self.assertEqual(sockets.opened, 1, "a Telegram failure is not a reconnect")
            self.assertEqual(bot.telegram.messages, [],
                             "a healthy daemon is never announced as lost")

    async def test_a_broken_socket_is_still_announced_and_reconnected(self):
        # The other half: narrowing the catch must not cost the real one.
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            sockets = _Sockets(2)
            bot._register_orientation = lambda: None

            async def drop(websocket):
                raise ConnectionClosed(None, None)

            bot._run_connected = drop
            with patch.object(bot_module, "connect", sockets), \
                    patch.object(bot_module, "StallWatchdog", _NoWatchdog), \
                    patch.object(asyncio, "sleep", _no_wait):
                await bot.run()
            self.assertEqual(sockets.opened, 2)
            self.assertEqual([text for _thread, text in bot.telegram.messages],
                             [DAEMON_DOWN, DAEMON_DOWN])


class _NoWatchdog:
    def __init__(self, logger=None) -> None:
        pass

    def start(self) -> None:
        pass


async def _no_wait(seconds: float) -> None:
    """asyncio.sleep, without the wait: the reconnect backoff is not what any
    of these tests are about."""
    return None
