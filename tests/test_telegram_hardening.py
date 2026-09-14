"""Hardening the Telegram client: the failure paths the surveys found.

Kept apart from `test_falconfox_poc.py` deliberately. What is exercised here
is what happens when Telegram refuses, when two tasks reach the same turn at
once, and when a topic the bot believes in is no longer there -- the paths the
existing suite never drives, because its fakes never fail and its handlers run
one at a time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed

from falconfox_telegram import bot as bot_module
from falconfox_telegram.api import ApiError, TelegramApi
from falconfox_telegram.bot import (BotConfig, DAEMON_DOWN, Dest,
                                    FalconFoxTelegramBot, Turn)

from test_falconfox_poc import UNREACHABLE_DAEMON, FakeTelegram


def _bot(directory: str) -> FalconFoxTelegramBot:
    bot = FalconFoxTelegramBot(BotConfig(
        "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
        state_dir=Path(directory), default_path=Path(directory),
    ))
    bot.telegram = FakeTelegram()
    return bot


def _instant_retries():
    """The send-retry policy with the waiting taken out of it. What is under
    test is what happens across the attempts, never how long they take."""
    return patch.multiple(bot_module, SEND_BACKOFF_SECONDS=(0.0, 0.0),
                          SEND_PAUSE_CEILING=0.0)


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


class RefusingNoticeTelegram(FakeTelegram):
    """Telegram that will not take a plain message."""

    async def message(self, chat_id, text, reply_to=None, silent=False, thread=None):
        raise ApiError("Too Many Requests: retry after 30")


class TelegramFailureIsNotDaemonFailureTests(unittest.IsolatedAsyncioTestCase):
    """Survey F1: a send that fails propagated out of the event loop, and the
    reconnect handler read it as the daemon being gone -- announcing an outage
    of a healthy daemon, then resending the recovered turn to the same dead
    destination, forever."""

    async def test_an_event_that_cannot_be_handled_does_not_end_the_loop(self):
        # The capacity notice is the shortest real path to an event that
        # raises: it has nowhere of its own to put a send failure. The reply
        # send used to be that path, and is now absorbed further down (see
        # `RefusedReplyTests`), which is why this drives a different event.
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            bot.telegram = RefusingNoticeTelegram()
            bot._bind("session", 20)
            bot._ws = _EventStream(
                {"type": "notice", "kind": "capacity", "session_id": "session",
                 "message": "paused for capacity"},
                {"type": "session_added", "session_id": "later", "name": "later"},
            )
            with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                # Returns because the socket ended, not because a send failed.
                await bot._receive_events()
            self.assertIn("later", bot._topics,
                          "the event after the failure is still handled")
            self.assertTrue(
                any("session" in line and "notice" in line
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


class DeletedTopicTelegram(FakeTelegram):
    """A forum with one topic deleted by hand: sends there are refused with the
    Bot API's answer for a thread that is not there, and everything else works."""

    def __init__(self, gone: int) -> None:
        super().__init__()
        self._gone = gone

    def _check(self, thread) -> None:
        if thread == self._gone:
            raise ApiError("Bad Request: message thread not found")

    async def message(self, chat_id, text, reply_to=None, silent=False, thread=None):
        self._check(thread)
        return await super().message(chat_id, text, reply_to=reply_to,
                                     silent=silent, thread=thread)

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        self._check(thread)
        await super().html_message(chat_id, html_text, plain_fallback,
                                   reply_to=reply_to, thread=thread)


class _Daemon:
    """Enough of the daemon for a session to be looked up by id."""

    def __init__(self, name: str = "work thing") -> None:
        self._name = name

    async def session(self, session_id, include_transcript=False):
        return {"session_id": session_id, "name": self._name, "path": "/tmp"}


class FlakyReplyTelegram(FakeTelegram):
    """A reply send that refuses a few times and then works, which is what a
    rate limit or one of this host's read timeouts actually looks like."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.attempts = 0
        self._left = failures

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        self.attempts += 1
        if self._left:
            self._left -= 1
            raise ApiError("Too Many Requests: retry after 1")
        await super().html_message(chat_id, html_text, plain_fallback,
                                   reply_to=reply_to, thread=thread)


class RefusedReplyTests(unittest.IsolatedAsyncioTestCase):
    """The reply is the one message of a turn that cannot be read off the
    screen some other way, so a refusal that may pass is waited out -- and one
    that does not pass is said out loud rather than dropped in a debug log."""

    def _bot_mid_turn(self, directory, telegram):
        bot = _bot(directory)
        bot.telegram = telegram
        bot.daemon = _Daemon()
        bot._bind("session", 20)
        bot._turns["session"] = Turn("session", Dest(-1001, 20),
                                     reply_parts=["the answer"], working=True)
        return bot

    async def _end_turn(self, bot):
        await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                 "turn_id": "t1", "outcome": "completed",
                                 "stop_reason": "end_turn", "output_chars": 10})

    async def test_a_reply_refused_twice_is_still_delivered_once(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = FlakyReplyTelegram(failures=2)
            bot = self._bot_mid_turn(directory, telegram)
            with _instant_retries():
                await self._end_turn(bot)
            self.assertEqual(telegram.attempts, 3)
            self.assertEqual([entry[2] for entry in telegram.html_messages],
                             ["the answer"], "delivered once, not once per attempt")
            self.assertEqual([text for _thread, text in telegram.messages
                              if "without delivering" in text], [],
                             "a reply that arrived is not a silent turn")

    async def test_a_reply_that_never_lands_says_so_rather_than_vanishing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory, RefusingTelegram())
            with _instant_retries():
                with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                    # Must not raise: the turn is already torn down, so an
                    # exception here is a turn nobody ever reports on.
                    await self._end_turn(bot)
            self.assertTrue(
                any("session" in line and "10" in line for line in logs.output),
                f"the session and the size of what was lost: {logs.output}")
            notice = [text for _thread, text in bot.telegram.messages
                      if "without delivering" in text]
            self.assertEqual(len(notice), 1, f"the chat is told: {bot.telegram.messages}")
            self.assertIn("refused", notice[0])
            self.assertIn("10-character", notice[0])

    def test_the_pause_honours_telegrams_own_retry_after_within_reason(self):
        # Telegram puts the number in the description, which is all `ApiError`
        # carries. Honoured, but capped: this runs on the event pipeline, so a
        # 429 that wants half a minute is better answered with the notice.
        self.assertEqual(
            bot_module._retry_pause(ApiError("Too Many Requests: retry after 2"), 1),
            2.0)
        self.assertEqual(
            bot_module._retry_pause(ApiError("Too Many Requests: retry after 30"), 1),
            bot_module.SEND_PAUSE_CEILING)
        self.assertEqual(bot_module._retry_pause(ApiError("TimeoutError"), 1),
                         bot_module.SEND_BACKOFF_SECONDS[0])
        self.assertEqual(bot_module._retry_pause(ApiError("TimeoutError"), 2),
                         bot_module.SEND_BACKOFF_SECONDS[1])


class ThreadRefusingTelegram(FakeTelegram):
    """Telegram that refuses every send to one thread, with a rate limit --
    nothing to do with the topic, and permanent for as long as it lasts."""

    def __init__(self, refused: int) -> None:
        super().__init__()
        self._refused = refused

    def _check(self, thread) -> None:
        if thread == self._refused:
            raise ApiError("Too Many Requests: retry after 30")

    async def message(self, chat_id, text, reply_to=None, silent=False, thread=None):
        self._check(thread)
        return await super().message(chat_id, text, reply_to=reply_to,
                                     silent=silent, thread=thread)

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        self._check(thread)
        await super().html_message(chat_id, html_text, plain_fallback,
                                   reply_to=reply_to, thread=thread)


class _RecoveryDaemon:
    """Sessions that all went idle while the bot was away, each with a
    transcript holding the reply nobody delivered."""

    def __init__(self, session_ids) -> None:
        self._session_ids = list(session_ids)

    async def sessions(self):
        return [{"session_id": session_id, "name": session_id,
                 "state": "idle", "path": "/tmp"}
                for session_id in self._session_ids]

    async def session(self, session_id, include_transcript=False):
        return {"session_id": session_id, "name": session_id, "path": "/tmp",
                "transcript": [
                    {"type": "message", "role": "user", "text": "ask"},
                    {"type": "message", "role": "agent",
                     "text": f"answer for {session_id}"}]}


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Settling the persisted turn map is a batch, and one entry's Telegram
    failure used to end it: everything after it in iteration order was skipped
    and the cleanup never ran, so the offending entry stayed on disk and
    aborted the next reconnect too, and the one after that."""

    def _bot(self, directory, session_ids, refused):
        bot = _bot(directory)
        bot.telegram = ThreadRefusingTelegram(refused=refused)
        bot.daemon = _RecoveryDaemon(session_ids)
        return bot

    async def test_one_poisoned_entry_does_not_strand_the_others(self):
        with tempfile.TemporaryDirectory() as directory:
            persisted = (("one", 21), ("two", 22), ("three", 23))
            old = _bot(directory)
            for session_id, thread in persisted:
                old._turns[session_id] = Turn(session_id, Dest(-1001, thread))
            old._persist_turns()

            names = [session_id for session_id, _thread in persisted]
            bot = self._bot(directory, names, refused=22)
            with _instant_retries(), \
                    self.assertLogs("falconfox.telegram", "WARNING") as logs:
                await bot._reconcile_persisted_turns()
            self.assertEqual([entry[0] for entry in bot.telegram.html_messages],
                             [21, 23],
                             "the entries after the failing one are still settled")
            self.assertTrue(any("two" in line for line in logs.output),
                            f"the entry that failed is named: {logs.output}")
            self.assertEqual(json.loads(bot._turns_file.read_text()), {},
                             "nothing is left on disk to abort the next connection")

            # And the next connection has nothing left to re-abort on.
            again = self._bot(directory, names, refused=22)
            await again._reconcile_persisted_turns()
            self.assertEqual(again.telegram.html_messages, [])
            self.assertEqual(again.telegram.messages, [])


class DeadTopicTests(unittest.IsolatedAsyncioTestCase):
    """Buglist: deleting a topic by hand stranded its session. There is no
    `forum_topic_deleted` service message and the Bot API cannot enumerate
    topics, so the first refused send is the only notice the bot ever gets."""

    def _bot_mid_turn(self, directory, telegram):
        bot = _bot(directory)
        bot.telegram = telegram
        bot.daemon = _Daemon()
        bot._bind("session", 20)
        bot._turns["session"] = Turn("session", Dest(-1001, 20),
                                     reply_parts=["the answer"], working=True)
        return bot

    async def _end_turn(self, bot):
        await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                 "turn_id": "t1", "outcome": "completed",
                                 "stop_reason": "end_turn", "output_chars": 10})

    async def test_a_send_to_a_deleted_topic_makes_exactly_one_new_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory, DeletedTopicTelegram(gone=20))
            await self._end_turn(bot)

            self.assertEqual(len(getattr(bot.telegram, "topics", [])), 1,
                             "one topic replaces the dead one, not one per send")
            fresh = bot._topics["session"]
            self.assertNotEqual(fresh, 20)
            self.assertEqual(bot._threads[fresh], "session")
            self.assertEqual(bot.telegram.html_messages[-1][0], fresh,
                             "the reply lands in the topic that exists")
            self.assertEqual(bot.telegram.html_messages[-1][2], "the answer")
            said = [text for thread, text in bot.telegram.messages if thread == fresh]
            self.assertEqual(len(said), 1, f"one line about it, not several: {said}")
            self.assertIn("new one", said[0])
            persisted = json.loads(Path(directory, "topics.json").read_text())
            self.assertEqual(persisted["topics"], {"session": fresh})

    async def test_a_recovered_reply_makes_a_new_topic_too(self):
        # The survey's own scenario: a turn that ended while the bot was away,
        # to a topic deleted in the meantime. The live path recreates the
        # topic; recovery sent straight through the API client and lost the
        # reply -- the half of the buglist that the reconnect path kept open.
        with tempfile.TemporaryDirectory() as directory:
            old = _bot(directory)
            old._turns["session"] = Turn("session", Dest(-1001, 20))
            old._persist_turns()

            bot = _bot(directory)
            bot.telegram = DeletedTopicTelegram(gone=20)
            bot.daemon = _RecoveryDaemon(["session"])
            bot._bind("session", 20)
            await bot._reconcile_persisted_turns()

            self.assertEqual(len(getattr(bot.telegram, "topics", [])), 1)
            fresh = bot._topics["session"]
            self.assertNotEqual(fresh, 20)
            self.assertEqual(bot.telegram.html_messages[-1][0], fresh)
            self.assertEqual(bot.telegram.html_messages[-1][2], "answer for session")
            said = [text for thread, text in bot.telegram.messages if thread == fresh]
            self.assertEqual(len(said), 2, f"the note, then the recovery line: {said}")
            self.assertIn("new one", said[0])
            self.assertIn("recovered", said[1].lower())
            persisted = json.loads(Path(directory, "topics.json").read_text())
            self.assertEqual(persisted["topics"], {"session": fresh})

    async def test_an_ordinary_send_failure_never_unbinds_the_topic(self):
        # A rate limit or a read timeout says nothing about the topic. Reading
        # one as "the topic is gone" would throw away a live topic and leave a
        # second one beside it. It is retried and then reported instead.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory, RefusingTelegram())
            with _instant_retries():
                await self._end_turn(bot)
            self.assertEqual(bot._topics["session"], 20, "the binding stands")
            self.assertEqual(getattr(bot.telegram, "topics", []), [])


class BlockingReplyTelegram(FakeTelegram):
    """Telegram with a reply send that can be held open, which is the whole of
    the F2 race: `_finish_turn` is inside this await while a new message for
    the same session arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def html_message(self, chat_id, html_text, plain_fallback, reply_to=None,
                           thread=None):
        self.entered.set()
        await self.release.wait()
        await super().html_message(chat_id, html_text, plain_fallback,
                                   reply_to=reply_to, thread=thread)


class TurnRecordTests(unittest.IsolatedAsyncioTestCase):
    """Survey F2 and F3: per-turn state was twenty loose dicts, so the teardown
    of a finished turn erased the state of the turn that replaced it, and
    figures from one turn were still there to be reported by the next."""

    def _bot(self, directory, telegram=None):
        bot = _bot(directory)
        if telegram is not None:
            bot.telegram = telegram
        bot._ws = _EventStream()
        return bot

    async def _stream(self, bot, text):
        await bot._handle_event({"type": "message", "session_id": "session",
                                 "role": "agent", "text": text})

    async def _end_turn(self, bot):
        await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                 "turn_id": "t1", "outcome": "completed",
                                 "stop_reason": "end_turn", "output_chars": 10})

    async def test_a_new_turn_during_the_reply_send_keeps_its_own_progress_message(self):
        # The buglist's extra "Working…" after the reply, as an interleaving:
        # `_finish_turn` awaits the reply send, a fresh message for the same
        # session starts a new turn in that window, and the teardown that
        # follows erases the new turn's state -- including the id of the
        # progress message it had just created, which is then orphaned and a
        # second one made beside it.
        with tempfile.TemporaryDirectory() as directory:
            telegram = BlockingReplyTelegram()
            bot = self._bot(directory, telegram)
            with patch.object(bot_module, "ACTION_REFRESH_SECONDS", 0.005):
                await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
                await self._stream(bot, "the answer")
                ending = asyncio.create_task(self._end_turn(bot))
                await asyncio.wait_for(telegram.entered.wait(), timeout=1)

                # The race: a second message, while the first turn's reply is
                # still in flight.
                await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
                telegram.release.set()
                await asyncio.wait_for(ending, timeout=1)

                # The new turn narrates, so its progress message has something
                # to say and its loop has a reason to speak.
                await self._stream(bot, "working on it")
                await bot._handle_event({"type": "tool_call", "session_id": "session",
                                         "title": "grep"})
                await asyncio.sleep(0.05)

            working = [text for _thread, text in telegram.messages
                       if "Working" in text]
            self.assertEqual(len(working), 2,
                             f"one progress message per turn, not one orphaned "
                             f"and one live: {telegram.messages}")
            stamped = [text for _chat, message_id, text in telegram.edits
                       if message_id == 101]
            self.assertTrue(any("Turn finished" in text for text in stamped),
                            "the first turn is still finalised")
            live = {message_id for _chat, message_id, text in telegram.edits
                    if "working on it" in text}
            self.assertEqual(live, {102},
                             "the second turn edits the message it created")
            await self._end_turn(bot)

    async def test_a_turn_without_usage_does_not_report_the_last_one_s_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000})
            await self._stream(bot, "the answer")
            await self._end_turn(bot)
            self.assertIn("Context window: 217k/1M", bot.telegram.edits[-1][2])

            await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
            await self._stream(bot, "another answer")
            await self._end_turn(bot)
            self.assertNotIn("Context window", bot.telegram.edits[-1][2],
                             "a turn that emitted no usage has no figures to give")

    async def test_the_turn_line_measures_growth_from_where_the_last_one_ended(self):
        """Context and cost both accumulate across a session, so what the turn
        spent is a subtraction -- against figures that outlive the turn and so
        cannot live on it."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000,
                                     "cost_amount": 1.5, "cost_currency": "USD"})
            await self._stream(bot, "the answer")
            await self._end_turn(bot)
            first = bot.telegram.edits[-1][2]
            self.assertIn("Context window: 217k/1M (22%)", first)
            self.assertNotIn("$", first,
                             "the first turn has nothing to measure against")

            await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 249034, "size": 1000000,
                                     "cost_amount": 1.92, "cost_currency": "USD"})
            await self._stream(bot, "another answer")
            await self._end_turn(bot)
            self.assertIn("Turn: +$0.42", bot.telegram.edits[-1][2])
            self.assertIn("Context window: 249k/1M (25%, +32k)",
                          bot.telegram.edits[-1][2])

    async def test_a_respawned_session_reports_the_negative_it_was_given(self):
        """Cost is cumulative per agent process and starts over when one is
        replaced, so the turn that straddles a respawn reports a large
        negative. Shown as it came: it is the backend saying something
        surprising, and hiding it would leave the next figure unexplained."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000,
                                     "cost_amount": 22.27})
            await self._stream(bot, "the answer")
            await self._end_turn(bot)

            await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 219402, "size": 1000000,
                                     "cost_amount": 0.2})
            await self._stream(bot, "another answer")
            await self._end_turn(bot)
            stamp = bot.telegram.edits[-1][2]
            self.assertIn("Turn: -$22.07", stamp)
            self.assertIn("Context window: 219k/1M (22%, +2k)", stamp,
                          "the context survived the respawn, so its growth stands")

    async def test_a_compacted_context_shows_the_ground_it_gave_back(self):
        """The other direction, and the one worth having: a backend that
        compacts mid-turn ends it holding far less than it started with."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 332000, "size": 1000000})
            await self._stream(bot, "the answer")
            await self._end_turn(bot)

            await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 44000, "size": 1000000})
            await self._stream(bot, "another answer")
            await self._end_turn(bot)
            self.assertIn("Context window: 44k/1M (4%, -288k)",
                          bot.telegram.edits[-1][2])

    async def test_a_window_reported_as_empty_is_not_reported_at_all(self):
        """The backend does occasionally report `used` as zero mid-session.
        Believing it would claim an empty context and then charge the whole
        window to the next turn as growth."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._forward("session", Dest(-1001, 20), "first", prompt_msg=1)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000})
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 0, "size": 1000000})
            await self._stream(bot, "the answer")
            await self._end_turn(bot)
            self.assertIn("Context window: 217k/1M", bot.telegram.edits[-1][2])

            await bot._forward("session", Dest(-1001, 20), "second", prompt_msg=2)
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 220034, "size": 1000000})
            await self._stream(bot, "another answer")
            await self._end_turn(bot)
            self.assertIn("(22%, +3k)", bot.telegram.edits[-1][2],
                          "growth is measured from the last figure worth having")


class SuspendingTopics(FakeTelegram):
    """Topic edits that suspend, which is what a real one does and what a fake
    that never awaits cannot: without a suspension the two callers of an apply
    never overlap, and the double-apply is invisible."""

    async def rename_topic(self, chat_id, thread, name):
        await asyncio.sleep(0)
        await super().rename_topic(chat_id, thread, name)

    async def set_topic_icon(self, chat_id, thread, icon):
        await asyncio.sleep(0)
        await super().set_topic_icon(chat_id, thread, icon)


class TopicApplyTests(unittest.IsolatedAsyncioTestCase):
    """Survey F6: `_mirror_session` and the reconciler both check, both await,
    and both record, so a `session_updated` event and a reconcile pass could
    both decide a title had changed and both send the edit -- two service
    messages in the topic, and a burst of icon edits is the one aggravator the
    buglist names for clients showing a stale icon."""

    def _bot(self, directory):
        bot = _bot(directory)
        bot.telegram = SuspendingTopics()
        bot._bind("session", 20)
        return bot

    async def test_a_concurrent_title_apply_makes_exactly_one_call(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            session = {"session_id": "session", "name": "work thing"}
            await asyncio.gather(bot._apply_title(session, 20),
                                 bot._apply_title(session, 20))
            self.assertEqual(getattr(bot.telegram, "renamed", []),
                             [(20, "work thing")])

    async def test_a_concurrent_icon_apply_makes_exactly_one_call(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._default_icon = "5001"
            session = {"session_id": "session", "name": "work thing"}
            await asyncio.gather(bot._apply_icon(session, 20),
                                 bot._apply_icon(session, 20))
            self.assertEqual(getattr(bot.telegram, "icons", []), [(20, "5001")])


class TurnPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Survey F5: the whole turn map was serialised and written synchronously
    on every narration block and every thought close, on the loop that carries
    every session's events."""

    async def test_many_narration_blocks_in_one_tick_write_once(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            bot._ws = _EventStream()
            writes = []
            written = bot_module._write_atomic

            def counting(path, text):
                writes.append(path)
                written(path, text)

            with patch.object(bot_module, "_write_atomic", counting), \
                    patch.object(bot_module, "PERSIST_TURNS_SECONDS", 0.01):
                await bot._forward("session", Dest(-1001, 20), "go", prompt_msg=1)
                writes.clear()
                for index in range(5):
                    await bot._handle_event({
                        "type": "message", "session_id": "session",
                        "role": "agent", "text": f"narration {index}"})
                    await bot._handle_event({
                        "type": "tool_call", "session_id": "session",
                        "title": "grep", "tool_call_id": str(index)})
                self.assertEqual(writes, [],
                                 "the event path does not touch the disk")
                await asyncio.sleep(0.05)
                self.assertEqual(len(writes), 1, "one write for the lot")

                # A turn boundary is not debounced: the map has to be right on
                # disk before the process can be told to stop.
                writes.clear()
                await bot._handle_event({
                    "type": "turn_ended", "session_id": "session",
                    "turn_id": "t1", "outcome": "completed",
                    "stop_reason": "end_turn", "output_chars": 9})
                self.assertTrue(writes, "the end of a turn is written at once")
                self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_a_dropped_connection_flushes_what_was_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            bot._ws = _EventStream()
            with patch.object(bot_module, "PERSIST_TURNS_SECONDS", 3600):
                await bot._forward("session", Dest(-1001, 20), "go", prompt_msg=1)
                await bot._handle_event({"type": "message", "session_id": "session",
                                         "role": "agent", "text": "narration"})
                await bot._handle_event({"type": "tool_call",
                                         "session_id": "session", "title": "grep"})
                bot._reset_connection_state()
            record = json.loads(bot._turns_file.read_text())["session"]
            self.assertEqual(record["progress"], ["narration", "⚙️ grep"],
                             "what the timer had not reached is on disk anyway")


class WebsocketSendTests(unittest.IsolatedAsyncioTestCase):
    """Every write to the daemon socket goes through one function, so the
    question of whether it needs a lock has one answer rather than two. It had
    two: `_forward` held `_ws_lock` and `_report_attachment` did not."""

    def test_nothing_writes_to_the_socket_behind_the_helper(self):
        source = inspect.getsource(bot_module)
        self.assertEqual(source.count("self._ws.send("), 1,
                         "a second writer is a second discipline")
        self.assertIn("self._ws.send(",
                      inspect.getsource(FalconFoxTelegramBot._ws_send))

    async def test_a_prompt_and_an_attachment_report_both_reach_the_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            bot._ws = _EventStream()
            await asyncio.gather(
                bot._forward("session", Dest(-1001, 20), "go", prompt_msg=1),
                bot._report_attachment("request-1", None))
            self.assertEqual([action["action"] for action in bot._ws.sent],
                             ["send", "attachment_result"])


class OrientationWaitTests(unittest.TestCase):
    """Buglist: the bot warned about orientation it went on to register.

    Both units restart together, so on an ordinary restart the bot looks for
    the daemon's client directory before the daemon has published it. That
    warned, at a level that says something needs attention, about a consequence
    that did not happen: the next connection registered a few seconds later.
    """

    def test_the_first_miss_says_it_is_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            with patch.object(bot_module.falconfox_state, "read_server_info",
                              return_value=None):
                with self.assertLogs("falconfox.telegram", "INFO") as logs:
                    bot._register_orientation()
            self.assertEqual([record.levelno for record in logs.records],
                             [logging.INFO])
            self.assertIn("waiting for the daemon", logs.output[0])

    def test_a_miss_after_it_worked_is_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            published = Path(directory).joinpath("run-1/clients")
            published.mkdir(parents=True)
            with patch.object(bot_module.falconfox_state, "read_server_info",
                              return_value=SimpleNamespace(
                                  pid=1, port=9725, started="now",
                                  clients_dir=str(published))):
                bot._register_orientation()
            self.assertTrue(published.joinpath("telegram/orientation.md").is_file())
            with patch.object(bot_module.falconfox_state, "read_server_info",
                              return_value=None):
                with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                    bot._register_orientation()
            self.assertIn("without Telegram orientation", logs.output[0])


class _SnapshotSocket:
    """A daemon socket that delivers one snapshot and then ends."""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    async def recv(self) -> str:
        return json.dumps(self._snapshot)

    def __aiter__(self) -> "_SnapshotSocket":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def send(self, _payload: str) -> None:
        pass


async def _nothing(*_args, **_kwargs) -> None:
    pass


async def _forever(*_args, **_kwargs) -> None:
    await asyncio.sleep(3600)


class ProtocolVersionTests(unittest.IsolatedAsyncioTestCase):
    """The daemon announces the wire version it speaks in its snapshot. It was
    produced and never checked, so a client left behind by a wire change would
    have found out as an action that did nothing."""

    async def _connect(self, directory: str, protocol) -> None:
        bot = _bot(directory)
        # Everything a connection does besides read the snapshot has a test of
        # its own; what is under test here is the first thing it reads.
        bot._register_orientation = lambda: None
        for step in ("_announce_daemon_up", "_reconcile_persisted_turns",
                     "_ensure_manager", "_load_icons",
                     "_reconcile_topics_guarded"):
            setattr(bot, step, _nothing)
        bot._poll_telegram = _forever
        await bot._run_connected(
            _SnapshotSocket({"type": "snapshot", "sessions": [],
                             "protocol": protocol}))

    async def test_a_mismatch_is_said_and_carried_on_from(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                await self._connect(directory, bot_module.DAEMON_PROTOCOL + 1)
            self.assertTrue(any("protocol" in line for line in logs.output),
                            f"the skew is named: {logs.output}")

    async def test_the_version_it_was_built_for_says_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertNoLogs("falconfox.telegram", "WARNING"):
                await self._connect(directory, bot_module.DAEMON_PROTOCOL)


class RefusedActionTests(unittest.IsolatedAsyncioTestCase):
    """The daemon answers an action it will not run with an event, which used
    to be a log line in the daemon and silence here."""

    async def test_a_refusal_is_logged_and_nothing_else_happens(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            with self.assertLogs("falconfox.telegram", "WARNING") as logs:
                # No session id, because a refused action need not have named
                # one -- and an event without one is dropped unread.
                await bot._handle_event({"type": "action_error",
                                         "action": "wibble",
                                         "error": "unknown action: wibble"})
            self.assertIn("refused", logs.output[0])
            self.assertIn("wibble", logs.output[0])

    async def test_an_event_type_this_client_does_not_know_is_ignored(self):
        # What the daemon relies on when it adds an event type: a client built
        # before it carries on working.
        with tempfile.TemporaryDirectory() as directory:
            bot = _bot(directory)
            await bot._handle_event({"type": "invented_later",
                                     "session_id": "abcd1234"})


class FakeDriftTests(unittest.TestCase):
    """`FakeTelegram` stands in for `TelegramApi` in most of the suite, so a
    method it has and the real one does not is a test passing against a client
    that does not exist. It kept `set_reaction` for months after reactions were
    removed."""

    def test_every_fake_method_exists_on_the_real_client(self):
        fake = {name for name, value in vars(FakeTelegram).items()
                if not name.startswith("_") and callable(value)}
        missing = sorted(name for name in fake if not hasattr(TelegramApi, name))
        self.assertEqual(missing, [],
                         "the fake answers calls the Bot API client cannot make")


class _NoWatchdog:
    def __init__(self, logger=None) -> None:
        pass

    def start(self) -> None:
        pass


async def _no_wait(seconds: float) -> None:
    """asyncio.sleep, without the wait: the reconnect backoff is not what any
    of these tests are about."""
    return None
