from __future__ import annotations

import asyncio
import contextlib
import inspect
from dataclasses import replace
import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import urllib.error

from falconfox import cli as falconfox_cli
from falconfox.cli import CliError, _guard_self_target, build_parser, cmd_daemon
from falconfox import __version__ as falconfox_version
from falconfox import config, get_version
from falconfox import help as ffhelp
from falconfox import state as falconfox_state
from falconfox.coordinator import SessionCoordinator
from falconfox.errors import FalconFoxError
from falconfox.engine.events import CLOSED, EventBus
from falconfox.engine.session import AgentSession, PromptPart
from falconfox.record import SessionRecord
from falconfox.storage import SessionStore
from falconfox.web.server import _send_events
from falconfox.watchdog import StallWatchdog
from falconfox_telegram import bot as bot_module
from falconfox_telegram.api import ApiError, DaemonApi, _json_request
from falconfox_telegram.bot import (QUEUED_FIRST, Dest, DAEMON_DOWN,
                                    PROGRESS_HEADER, PROGRESS_CLOCK_SECONDS,
                                    TURN_ACTION, BotConfig, FalconFoxTelegramBot,
                                    Turn)
from falconfox_telegram.rendering import TELEGRAM_MESSAGE_LIMIT, render_messages
from falconfox_telegram.bot import (COMMANDS, PHOTO_LIMIT_BYTES, SECTIONS,
                                    _final_stamp, _inline_code, _upload_kind,
                                    _write_atomic)
from falconfox_telegram.bot import COMMANDS_HELP
from falconfox_telegram.shell import ShellRunner, tail


# Port 9 is the discard port: nothing listens, so a DaemonApi that is not
# stubbed fails fast instead of quietly reaching a real daemon. Without this,
# tests exercising _ensure_manager fell through to BotConfig's default of
# 127.0.0.1:9721 -- production -- and spawned real sessions there.
UNREACHABLE_DAEMON = "http://127.0.0.1:9"


class FakeAgent:
    """A live agent that spawns nothing. Enough for the coordinator to hold."""

    def __init__(self, session_id="abcd1234"):
        self.session_id = session_id
        self.sent = []
        self.stopped = 0
        self.cancelled = 0
        self.acp_session_id = None
        self.config_options = []

    async def send(self, parts):
        self.sent.append(list(parts))

    async def stop(self):
        self.stopped += 1
        await asyncio.sleep(0)

    async def cancel(self):
        self.cancelled += 1


class QuietAgentSession:
    """An `AgentSession` that spawns nothing, for driving the coordinator's
    own lifecycle. Every step yields, so concurrent activations interleave."""

    def __init__(self, session_id, name, path, backend, emit, request_permission):
        self.session_id = session_id
        self.name = name
        self.emit = emit
        self.acp_session_id = f"acp-{session_id}"
        self.config_options = []
        self.sent = []
        self.stopped = 0

    def _idle(self):
        self.emit({"session_id": self.session_id, "type": "agent_state",
                   "state": "idle"})

    async def start(self):
        await asyncio.sleep(0)
        self._idle()

    async def resume(self, acp_session_id):
        await asyncio.sleep(0)
        self._idle()
        return True

    async def send(self, parts):
        await asyncio.sleep(0)
        self.sent.append(list(parts))

    async def cancel(self):
        await asyncio.sleep(0)

    async def stop(self):
        self.stopped += 1
        await asyncio.sleep(0)


class HangingConn:
    """A connection whose prompt hangs until the connection closes, and then
    fails the way a real one does when its subprocess goes."""

    def __init__(self):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def prompt(self, **_kwargs):
        self.started.set()
        await self.closed.wait()
        raise ConnectionError("connection closed")

    async def cancel(self, **_kwargs):
        self.closed.set()

    async def close(self):
        self.closed.set()


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed = code


class ReadyConn:
    """A connection that answers, holding each prompt until it is released."""

    def __init__(self):
        self.prompts = []
        self.release = asyncio.Event()

    async def load_session(self, **_kwargs):
        return SimpleNamespace()

    async def prompt(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        await self.release.wait()
        return SimpleNamespace(stop_reason="end_turn", usage=None)


def make_record(coordinator, session_id, *, live=False, **fields):
    """Put a session into a coordinator without spawning anything for it.

    The record is the whole session, so a test that used to assemble a
    metadata dict by hand builds one of these instead. `live=True` gives it a
    `FakeAgent`, since holding an agent is what being live *is*.
    """
    record = SessionRecord(
        session_id=session_id,
        name=fields.pop("name", session_id),
        path=fields.pop("path", "/tmp"),
        backend=fields.pop("backend", "echo"),
        created=fields.pop("created", "1"),
        last_active=fields.pop("last_active", "1"),
    )
    if live:
        record.agent = FakeAgent(session_id)
    for key, value in fields.items():
        if not hasattr(record, key):
            raise AttributeError(f"a session record has no {key!r}")
        setattr(record, key, value)
    coordinator._records[session_id] = record
    return record


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


class MetadataWriteTests(unittest.TestCase):
    """`meta.toml` is the whole session as far as a restart is concerned.

    A session whose metadata will not parse is skipped by the loader, so it
    vanishes from every listing with its transcript orphaned on disk. The
    metadata is rewritten on every replayable event, so a write that can tear
    is a window that is open continuously during use.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root)
        self.meta = {"session_id": "0123abcd", "name": "work", "path": "/tmp",
                     "backend": "echo", "named": True}

    def test_the_live_file_is_replaced_rather_than_written_into(self):
        # The structural claim: nothing ever writes to `meta.toml` itself, so
        # there is no moment at which a reader can see half of one.
        written = []
        original = Path.write_text

        def remember(path, data, *args, **kwargs):
            written.append(Path(path))
            return original(path, data, *args, **kwargs)

        with patch.object(Path, "write_text", remember):
            self.store.write_meta(self.meta)
        self.assertNotIn(self.root.joinpath("0123abcd", "meta.toml"), written)
        self.assertEqual(self.store.load_all_meta(), [self.meta])

    def test_a_write_that_dies_partway_leaves_the_previous_metadata_readable(self):
        self.store.write_meta(self.meta)
        original = Path.write_text

        def die_partway(path, data, *args, **kwargs):
            original(path, data[: len(data) // 2], *args, **kwargs)
            raise OSError("no space left on device")

        with patch.object(Path, "write_text", die_partway):
            with self.assertRaises(OSError):
                self.store.write_meta({**self.meta, "name": "renamed"})
        self.assertEqual(self.store.load_all_meta(), [self.meta],
                         "a torn write must not cost the session its metadata")

    def test_a_torn_file_on_disk_names_its_session_and_the_rest_still_load(self):
        # Belt and braces: a file torn by something other than this process
        # (an older daemon, a hand edit) is still skipped rather than fatal,
        # and the log says which session went missing.
        self.store.write_meta(self.meta)
        torn = self.root.joinpath("beefcafe")
        torn.mkdir()
        torn.joinpath("meta.toml").write_text('name = "half a nam')
        with self.assertLogs("falconfox.storage", level="WARNING") as captured:
            metas = self.store.load_all_meta()
        self.assertEqual([meta["session_id"] for meta in metas], ["0123abcd"])
        self.assertIn("beefcafe", captured.output[0])


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
        record = make_record(self.coordinator, "infra", name="telegram manager",
                             hidden=True, state="idle", live=True, auto_named=False)
        self.coordinator._persist_meta(record)
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
        record = make_record(
            self.coordinator, "focus", name="focus", ephemeral=True, hidden=True,
            state="idle", live=True, auto_named=False,
            transcript=[{"type": "message", "role": "user", "text": "switch"}])
        self.coordinator._persist_meta(record)
        self.assertEqual(self.coordinator.list_sessions(), [])
        self.assertEqual(self.coordinator.list_sessions(include_hidden=True)[0]["session_id"],
                         "focus")
        self.assertFalse(Path(self.temporary.name, "focus").exists())

    async def test_a_turn_that_produced_no_output_is_a_warning(self):
        # The recurring failure shape: a turn ends with nothing to show and
        # nobody notices. The daemon now notices, at the moment it happens.
        make_record(self.coordinator, "s", name="quiet", ephemeral=True,
                    state="working", live=True)
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
        make_record(self.coordinator, "one", name="one",
                    transcript=[{"type": "message", "text": "large"}])
        snapshot = self.coordinator.snapshot()
        self.assertEqual(snapshot["sessions"][0]["session_id"], "one")
        self.assertNotIn("transcripts", snapshot)


class SessionRecordTests(unittest.IsolatedAsyncioTestCase):
    """A session is one record, so ending one is not a list of fields.

    The two teardown paths used to clear slightly different lists by hand,
    which made a forgotten field a leak with nothing to catch it.
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

    def _busy_record(self):
        return make_record(
            self.coordinator, "work", name="work", state="idle", live=True,
            auto_named=False,
            transcript=[{"type": "message", "role": "user", "text": "hi"}],
            config_options=[{"id": "model", "current_value": "opus"}],
            commands=[{"name": "review"}],
            pending_context=[PromptPart(text="owed", system=True)],
            usage={"total_tokens": 12},
            queued=True, queued_text="later",
        )

    async def test_stopping_gives_up_everything_the_live_agent_owned(self):
        record = self._busy_record()
        agent = record.agent
        await self.coordinator.stop_session("work")
        self.assertEqual(agent.stopped, 1)
        self.assertFalse(record.live, "the slot is free the moment the agent goes")
        self.assertEqual(record.state, "stored")
        self.assertIsNone(record.transcript)
        self.assertIsNone(record.config_options)
        self.assertIsNone(record.commands)
        self.assertEqual(record.pending_context, [])
        self.assertFalse(record.queued)
        self.assertIsNone(record.queued_text)
        # What the session is on disk survives, which is the whole point of
        # stop being different from delete.
        self.assertEqual(record.name, "work")
        self.assertTrue(record.persisted)

    async def test_deleting_discards_the_record_rather_than_emptying_it(self):
        record = self._busy_record()
        await self.coordinator.delete_session("work")
        self.assertEqual(self.coordinator._records, {})
        self.assertEqual(record.agent, None)
        self.assertEqual(self.coordinator.snapshot()["usage"], {})
        self.assertFalse(Path(self.temporary.name, "work").exists())

    def _on_disk(self, session_id="work", *, named=False):
        """A session as a restart finds it: on disk, and nothing cached."""
        record = make_record(self.coordinator, session_id, name=session_id,
                             auto_named=not named, persisted=True)
        self.coordinator.store.write_meta(record.stored())
        self.coordinator.store.append_event(
            session_id, {"type": "message", "role": "user", "text": "earlier"})
        record.transcript = None
        return record

    async def test_stopping_a_restored_session_does_not_delete_it(self):
        # `stop` decided keep-versus-delete from the *cached* transcript,
        # which is empty for every session a restart restores. So stopping a
        # session that had never been renamed took its directory and its
        # transcript with it, silently, on the very command the manager
        # orientation tells an agent to run to free a slot.
        self._on_disk()
        restarted = SessionCoordinator(Path(self.temporary.name))
        restarted.load_persisted()
        await restarted.stop_session("work")
        self.assertIn("work", restarted._records, "stop is not delete")
        self.assertTrue(Path(self.temporary.name, "work", "meta.toml").exists())
        self.assertEqual([event["text"] for event in restarted.transcript("work")],
                         ["earlier"])

    async def test_stopping_twice_does_not_delete_what_the_first_stop_kept(self):
        # The same hole from the other side: the first stop drops the
        # transcript cache, so the second one looks at nothing and concludes
        # there was nothing to keep.
        record = self._on_disk()
        record.agent = FakeAgent("work")
        record.transcript = [{"type": "message", "role": "user", "text": "earlier"}]
        await self.coordinator.stop_session("work")
        await self.coordinator.stop_session("work")
        self.assertIn("work", self.coordinator._records)
        self.assertTrue(Path(self.temporary.name, "work", "meta.toml").exists())

    async def test_a_session_with_nothing_to_keep_is_still_deleted_by_stop(self):
        # The other half of the rule, which is why the condition exists:
        # a session that was never named and never spoke has nothing on disk
        # worth waking up again.
        record = make_record(self.coordinator, "blank", live=True)
        await self.coordinator.stop_session("blank")
        self.assertNotIn("blank", self.coordinator._records)
        self.assertFalse(record.persisted)

    async def test_an_event_to_a_stored_session_does_not_replace_its_transcript(self):
        # A stopped session drops its cached transcript to reclaim the memory.
        # An event arriving before anything reads it used to create a
        # one-event history in its place, which the next resume then showed
        # the client and a revert then wrote to disk over the real one.
        record = make_record(self.coordinator, "work", name="work",
                             auto_named=False, persisted=True)
        self.coordinator.store.write_meta(record.stored())
        self.coordinator.store.append_event(
            "work", {"type": "message", "role": "user", "text": "earlier"})
        self.coordinator._emit({"type": "notice", "session_id": "work",
                                "message": "waiting for a free session slot"})
        transcript = self.coordinator.transcript("work")
        self.assertEqual([event["type"] for event in transcript],
                         ["message", "notice"])
        self.assertEqual(transcript[0]["text"], "earlier")


class FileStoreTests(unittest.IsolatedAsyncioTestCase):
    """Per-session file storage: the daemon's half of inbound attachments.

    It stores and it deletes, and it decides nothing about when a file
    reaches the agent. What it does own is the lifetime.
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
        make_record(self.coordinator, "work", name="work", state="idle", live=True)
        self.source = Path(self.temporary.name, "download")
        self.source.write_bytes(b"payload")

    def add(self, name="prod-error.log"):
        return self.coordinator.add_file("work", str(self.source), name)

    async def test_the_name_survives_and_the_id_is_the_directory(self):
        # The point of a directory per file: `prod-error.log` says something
        # that `a1b2c3d4.log` does not, and nothing has to rename it.
        added = self.add()
        stored = Path(added["path"])
        self.assertEqual(stored.name, "prod-error.log")
        self.assertEqual(stored.parent.name, added["file_id"])
        self.assertEqual(stored.read_bytes(), b"payload")

    async def test_the_same_name_twice_does_not_collide(self):
        first, second = self.add(), self.add()
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).exists())

    async def test_adding_copies_rather_than_consuming_the_source(self):
        # `attach` does not consume what it sends, and `add` is its mirror:
        # the caller may still want the file it handed over.
        self.add()
        self.assertTrue(self.source.exists())

    async def test_a_name_from_the_network_becomes_one_path_component(self):
        stored = Path(self.add("../../meta.toml")["path"])
        self.assertEqual(stored.name, "meta.toml")
        self.assertEqual(stored.parent.parent.name, "inbox")

    async def test_a_name_that_survives_nothing_still_yields_a_file(self):
        self.assertEqual(Path(self.add("///")["path"]).name, "file")

    async def test_removing_deletes_the_bytes(self):
        # A file dropped from the tray is never going to reach the agent, so
        # there is nothing left to keep.
        added = self.add()
        self.assertEqual(self.coordinator.remove_file("work", added["file_id"]),
                         {"removed": 1})
        self.assertFalse(Path(added["path"]).exists())
        self.assertEqual(self.coordinator.remove_file("work", added["file_id"]),
                         {"removed": 0})

    async def test_an_id_cannot_address_anything_but_a_stored_file(self):
        self.add()
        meta = Path(self.temporary.name, "work", "meta.toml")
        meta.write_text("keep = true\n")
        self.assertEqual(self.coordinator.remove_file("work", "../.."), {"removed": 0})
        self.assertTrue(meta.exists())

    async def test_clearing_takes_the_lot(self):
        self.add()
        self.add("second.txt")
        self.assertEqual(self.coordinator.clear_files("work"), {"removed": 2})
        self.assertEqual(self.coordinator.clear_files("work"), {"removed": 0})

    async def test_deleting_the_session_takes_its_files(self):
        # The reason the store is the daemon's: no client has to watch for
        # this, get it right, or leak the files of a session deleted while it
        # was not running.
        added = self.add()
        await self.coordinator.delete_session("work")
        self.assertFalse(Path(added["path"]).exists())

    async def test_an_ephemeral_session_has_nowhere_to_put_a_file(self):
        make_record(self.coordinator, "throwaway", state="idle", live=True,
                    ephemeral=True)
        with self.assertRaises(FalconFoxError):
            self.coordinator.add_file("throwaway", str(self.source))

    async def test_an_unknown_session_and_a_missing_file_both_refuse(self):
        with self.assertRaises(FalconFoxError):
            self.coordinator.add_file("nobody", str(self.source))
        with self.assertRaises(FalconFoxError):
            self.coordinator.add_file("work", str(self.source) + ".missing")


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
            self.coordinator._records[session_id].release()
        self.coordinator.stop_session = _stop

    def _live(self, session_id, *, last_active, state="idle", infrastructure=False):
        return make_record(self.coordinator, session_id, live=True, state=state,
                           hidden=infrastructure, last_active=last_active)

    def _limit(self, value):
        self.coordinator.config = replace(self.coordinator.config,
                                          max_live_sessions=value)

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
        # No floor for infrastructure: it queues for a slot and is evicted
        # like anything else, so a small limit degrades rather than
        # deadlocking, and the number is honoured exactly as written.
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

    async def test_infrastructure_waits_for_a_slot_like_anything_else(self):
        # It used to skip the queue outright, so that the manager could be
        # reached with every session busy. That let the daemon exceed its own
        # limit to buy a guarantee the client already gives out of band, so
        # there is no longer a caller that can jump the queue.
        self._limit(3)
        for name in ("a", "b", "c"):
            self._live(name, last_active="1", state="working")
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
        waiting = make_record(self.coordinator, "waiting", name="waiting")
        await self.coordinator.send("waiting", "do the thing")
        self.assertTrue(waiting.queued)
        self.assertEqual(waiting.queued_text, "do the thing")

    async def test_a_queued_session_is_retried_when_one_goes_idle(self):
        self._limit(1)
        self._live("busy", last_active="1", state="working")
        make_record(self.coordinator, "waiting", queued=True)
        drained = []
        self.coordinator._drain_queue = lambda: drained.append(True) or asyncio.sleep(0)
        self.coordinator._emit({"type": "agent_state", "session_id": "busy",
                                "state": "idle"})
        await asyncio.sleep(0)
        self.assertEqual(drained, [True])


class LifecycleSerialisationTests(unittest.IsolatedAsyncioTestCase):
    """One transition at a time per session, one slot handed out at a time.

    Every test here drives real concurrent tasks, because that is how the
    daemon is driven: the websocket dispatches each action as a detached
    task, so overlapping transitions are the ordinary case. A test that
    awaited its calls in order would pass over every race described here.
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
        self.subscription = self.coordinator.bus.subscribe()
        self.queue = self.subscription.__enter__()
        self.addCleanup(self.subscription.__exit__, None, None, None)

    def _limit(self, value):
        self.coordinator.config = replace(self.coordinator.config,
                                          max_live_sessions=value)

    def _emitted(self):
        events = []
        while not self.queue.empty():
            events.append(self.queue.get_nowait())
        return events

    def _hanging_agent(self, record):
        """Give a record a live agent whose turn hangs until its connection
        is closed, which is what a real backend's prompt does."""
        conn = HangingConn()
        agent = AgentSession(
            session_id=record.session_id, name=record.name,
            path=Path(self.temporary.name), backend=None,
            emit=self.coordinator._emit, request_permission=None,
        )
        agent._conn = conn
        agent._acp_session_id = "acp"
        agent._stack.push_async_callback(conn.close)
        record.agent = agent
        return conn

    async def test_two_actions_needing_the_last_slot_do_not_both_take_it(self):
        # Survey F1. Both read the live count, both pick the same idle
        # victim, and both proceed, because freeing a slot spans an await.
        # The cap is there because this daemon has been OOM-killed carrying
        # ten sessions, so exceeding it is the one thing it must not do.
        self._limit(1)
        make_record(self.coordinator, "victim", state="idle", live=True,
                    auto_named=False)
        make_record(self.coordinator, "stored", auto_named=False)
        with patch("falconfox.coordinator.AgentSession", QuietAgentSession):
            await asyncio.gather(
                self.coordinator.add_session(path=self.temporary.name, name="fresh"),
                self.coordinator.resume_session("stored"),
            )
        self.assertEqual(len(self.coordinator.live_session_ids()), 1,
                         "the limit is a number the daemon must not exceed")

    async def test_deleting_during_a_turn_ends_the_turn_out_loud(self):
        # The turn used to be dropped in silence: its reply landed after the
        # metadata was popped and was discarded with nothing logged, so the
        # user's answer vanished and no line said so.
        record = make_record(self.coordinator, "work", name="work",
                             state="working", auto_named=False)
        conn = self._hanging_agent(record)
        turn = asyncio.create_task(self.coordinator.send("work", "think about it"))
        await asyncio.wait_for(conn.started.wait(), 2)
        with self.assertLogs("falconfox.coordinator", level="WARNING") as logged:
            await self.coordinator.delete_session("work")
        await asyncio.wait_for(turn, 2)
        self.assertIn("work", "".join(logged.output))
        events = self._emitted()
        cut = [event for event in events if event.get("type") == "notice"
               and "cut short" in (event.get("message") or "")]
        self.assertEqual(len(cut), 1, "the outcome is said, not just logged")
        ended = [event for event in events if event.get("type") == "turn_ended"]
        self.assertEqual([event["stop_reason"] for event in ended], ["cancelled"])
        # And it is said before the turn is closed, or a client that
        # finalizes on turn_ended has nowhere left to put it.
        self.assertLess(events.index(cut[0]), events.index(ended[0]))

    async def test_a_running_turn_does_not_lock_the_session_out(self):
        # `send` gives both locks up before waiting for the turn. Holding
        # them across it would put `cancel`, `stop` and `delete` out of reach
        # for as long as the agent worked, which is exactly when they are
        # wanted, and `/stop` would hang on the turn it exists to end.
        record = make_record(self.coordinator, "work", name="work",
                             state="working", auto_named=False)
        conn = self._hanging_agent(record)
        turn = asyncio.create_task(self.coordinator.send("work", "a long one"))
        await asyncio.wait_for(conn.started.wait(), 2)
        await asyncio.wait_for(self.coordinator.cancel("work"), 2)
        await asyncio.wait_for(turn, 2)

    async def test_a_revert_cannot_land_the_dying_turns_events_after_the_rewrite(self):
        # Survey F2. Revert stopped the session mid-turn and rewrote the
        # transcript while the dying prompt's error notice was still
        # pending, so the notice was appended to the file it had just
        # rewritten -- a stray line after the revert.
        record = make_record(self.coordinator, "work", name="work",
                             auto_named=False, persisted=True,
                             config_options=[{"id": "model"}],
                             commands=[{"name": "review"}],
                             pending_context=[PromptPart(text="owed")])
        self.coordinator.store.write_meta(record.stored())
        for event in ({"type": "message", "role": "user", "text": "first"},
                      {"type": "message", "role": "agent", "text": "reply"}):
            self.coordinator.store.append_event("work", event)
        conn = self._hanging_agent(record)
        turn = asyncio.create_task(self.coordinator.send("work", "second"))
        await asyncio.wait_for(conn.started.wait(), 2)
        # Index 2 is the message this turn is answering, which is the whole
        # point of reverting mid-turn.
        await self.coordinator.revert_session("work", 2)
        await asyncio.wait_for(turn, 2)
        on_disk = self.coordinator.store.read_transcript("work")
        self.assertEqual([event.get("text") for event in on_disk],
                         ["first", "reply"],
                         "nothing may be appended after the rewrite")
        self.assertEqual(self.coordinator.transcript("work"), on_disk)
        # And the agent that held the rest of this session's state is gone
        # with it, by the same route a stop gives it up rather than by a
        # list of fields written out at the revert.
        self.assertFalse(record.live)
        self.assertEqual(record.state, "stored")
        self.assertIsNone(record.acp_id, "what it would load no longer exists")
        self.assertIsNone(record.config_options)
        self.assertIsNone(record.commands)
        self.assertEqual(record.pending_context, [])

    async def test_a_second_send_to_a_starting_session_waits(self):
        # Survey F6. The second send reached a session that was in the
        # manager but whose connection was not up, raising an AttributeError
        # that the engine reported to the user as "agent error".
        record = make_record(self.coordinator, "work", name="work",
                             auto_named=False, acp_id="acp")
        gate = asyncio.Event()
        conn = ReadyConn()

        async def slow_spawn(agent):
            await gate.wait()
            agent._conn = conn
            agent._supports_load = True

        with patch.object(AgentSession, "_spawn", slow_spawn):
            first = asyncio.create_task(self.coordinator.send("work", "one"))
            second = asyncio.create_task(self.coordinator.send("work", "two"))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(second.done(), "nothing to answer with yet")
            gate.set()
            # The second send returns once it has been through the same door
            # the first went through, which is what "waits" means here.
            await asyncio.wait_for(second, 2)
            self.assertEqual(len(conn.prompts), 1,
                             "and it does not open a second turn on the same "
                             "connection while the first is still running")
            conn.release.set()
            await asyncio.wait_for(first, 2)
        failures = [event for event in self._emitted()
                    if event.get("type") == "notice"
                    and "agent error" in (event.get("message") or "")]
        self.assertEqual(failures, [])


class LiveSessionLimitConfigTests(unittest.TestCase):
    """Reading the cap out of config.toml."""

    def _load(self, body):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        directory = Path(home.name).joinpath("falconfox")
        directory.mkdir()
        directory.joinpath("config.toml").write_text(body)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": home.name}):
            return config.load_config()

    def test_the_current_key_is_read(self):
        self.assertEqual(self._load("max_live_sessions = 2").max_live_sessions, 2)

    def test_an_absent_key_takes_the_default(self):
        self.assertEqual(self._load("").max_live_sessions,
                         config.DEFAULT_MAX_LIVE_SESSIONS)

    def test_a_negative_limit_is_floored_at_zero(self):
        self.assertEqual(self._load("max_live_sessions = -3").max_live_sessions, 0)


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
        await session.send([PromptPart(text="hi")])
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
        await session.send([PromptPart(text="hi")])
        ended = next(event for event in events if event["type"] == "turn_ended")
        self.assertEqual(ended["outcome"], "error")
        self.assertEqual(ended["output_chars"], 0)


class EventBusBoundTests(unittest.IsolatedAsyncioTestCase):
    """A subscriber's queue is bounded, because the alternative is the daemon.

    The queues are per-subscriber and grew without limit, so one client that
    stopped reading cost memory without end on the same 951 MB host the
    live-session cap exists to protect.
    """

    async def test_a_subscriber_that_stops_reading_is_dropped_alone(self):
        bus = EventBus(bound=2)
        with bus.subscribe("slow") as slow, bus.subscribe("quick") as quick:
            with self.assertLogs("falconfox.engine.events", level="WARNING") as logged:
                for number in range(4):
                    bus.publish({"type": "message", "n": number})
                    quick.get_nowait()
            self.assertIn("slow", logged.output[0])
            self.assertEqual(bus.subscribers, 1, "only the slow one goes")
            # What is left for the dropped subscriber is the end of its
            # stream, which is what closes its socket.
            self.assertIs(slow.get_nowait(), CLOSED)
            bus.publish({"type": "message", "n": 99})
            self.assertEqual(quick.get_nowait()["n"], 99,
                             "the others never noticed")
            self.assertTrue(slow.empty())

    async def test_a_closed_stream_closes_the_socket_it_was_feeding(self):
        # Which is the whole point of dropping rather than discarding: the
        # client's reconnect re-subscribes and re-snapshots, so it recovers.
        queue = asyncio.Queue()
        queue.put_nowait({"type": "message", "text": "the last one through"})
        queue.put_nowait(CLOSED)
        websocket = FakeWebSocket()
        await asyncio.wait_for(_send_events(websocket, queue), 2)
        self.assertEqual([event["text"] for event in websocket.sent],
                         ["the last one through"])
        self.assertIsNotNone(websocket.closed)


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


class CliTimeoutTests(unittest.TestCase):
    """The CLI is what an agent inside a session runs, so a wedged daemon
    must cost it an error rather than the rest of its turn."""

    def _timeouts_for(self, call):
        """Every read timeout a command asks for, before it is cut short."""
        seen = []

        def urlopen(request, timeout=None):
            seen.append(timeout)
            raise urllib.error.HTTPError(request.full_url, 500, "no", {}, None)

        with patch("falconfox.cli.urllib.request.urlopen", urlopen), \
                patch("falconfox.cli._base_url", return_value=UNREACHABLE_DAEMON):
            with self.assertRaises(CliError):
                call()
        return seen

    def test_a_request_has_a_finite_timeout(self):
        self.assertEqual(
            self._timeouts_for(lambda: falconfox_cli._request("GET", "/api/sessions")),
            [falconfox_cli.DEFAULT_TIMEOUT])

    def test_the_two_calls_that_legitimately_block_get_longer(self):
        # `send` waits out the whole turn it starts and `attach` waits for a
        # client to take the file, so the default would cut off exactly the
        # two calls that are supposed to take a while.
        send = build_parser().parse_args(["send", "abcd1234", "hello"])
        self.assertEqual(self._timeouts_for(lambda: falconfox_cli.cmd_send(send)),
                         [falconfox_cli.TURN_TIMEOUT])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "note.txt")
            target.write_text("x")
            attach = build_parser().parse_args(["attach", str(target)])
            with patch.dict(os.environ, {"FALCONFOX_SESSION_ID": "abcd1234"}):
                self.assertEqual(
                    self._timeouts_for(lambda: falconfox_cli.cmd_attach(attach)),
                    [falconfox_cli.ATTACH_TIMEOUT])

    def test_a_timeout_says_so_rather_than_looking_like_a_dead_daemon(self):
        def urlopen(_request, timeout=None):
            raise TimeoutError("timed out")

        with patch("falconfox.cli.urllib.request.urlopen", urlopen), \
                patch("falconfox.cli._base_url", return_value=UNREACHABLE_DAEMON):
            with self.assertRaises(CliError) as caught:
                falconfox_cli._request("GET", "/api/sessions")
        self.assertIn("did not answer", str(caught.exception))


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
        self.documents = []
        self.document_error = None
        # Methods that succeed anyway, for testing a fallback.
        self.document_ok = set()
        self._next_id = 100

    async def message(self, chat_id, text, reply_to=None, silent=False, thread=None):
        self.messages.append((thread, text))
        self.message_replies.append(reply_to)
        self.message_silent.append(silent)
        self._next_id += 1
        return self._next_id

    async def send_file(self, chat_id, file_path, method="sendDocument",
                        field="document", caption=None, thread=None):
        error = self.document_error
        if error is not None and method not in self.document_ok:
            raise error
        self.documents.append((chat_id, thread, file_path, caption, method))

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

    async def create_topic(self, chat_id, name, icon=None):
        self.topics = getattr(self, "topics", [])
        self.topics.append((name, icon) if icon else name)
        return 900 + len(self.topics)

    async def set_topic_icon(self, chat_id, thread, icon):
        self.icons = getattr(self, "icons", [])
        self.icons.append((thread, icon))

    async def icon_stickers(self):
        return [{"emoji": "📁", "custom_emoji_id": "5001"},
                {"emoji": "❗️", "custom_emoji_id": "5002"}]

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

    DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

    async def file_path(self, file_id):
        self.described = getattr(self, "described", [])
        self.described.append(file_id)
        return self.remote_paths.get(file_id, "photos/file_12.jpg")

    async def download(self, remote_path, into):
        into.write_bytes(b"bytes of " + remote_path.encode())
        return into

    remote_paths: dict = {}

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
            bot._turns["session"] = Turn("session", Dest(-1001, 20))
            # The drains between events: the indicator rides its own loop task,
            # so give it a tick to land.
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
            self.assertEqual(bot._turns["session"].activity_state, "streaming",
                             "the state is still tracked, for /status")
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            await asyncio.sleep(0)
            # The action is sent by the loop alone now, so a stream of events
            # spends no calls at all on it: what each of them changed used to
            # be a different action, and is now a progress line. One send, from
            # the loop's first tick.
            self.assertEqual(fake.actions, [(20, TURN_ACTION)])
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
            self.assertEqual(fake.actions, [(20, TURN_ACTION)])
            self.assertEqual(sent, [{"action": "send", "session_id": "session",
                                     "text": "do the thing"}])
            # A later event must not start a second loop, nor send an action of
            # its own: the running loop is the only thing that sends.
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            self.assertEqual(fake.actions, [(20, TURN_ACTION)])
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            self.assertEqual(bot._turns, {})


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
            bot._turns["session"] = Turn("session", Dest(-1001, 20))

            fake.action_error = ApiError("Too Many Requests: retry after 1")
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await asyncio.sleep(0)
            self.assertEqual(fake.actions, [])
            self.assertFalse(bot._turns["session"].activity_task.done(),
                             "one failed chat action must not end the loop")

            # Recovered: the loop is still there, and its next tick lands.
            fake.action_error = None
            await bot._send_action(bot._turns["session"])
            self.assertEqual(fake.actions, [(20, TURN_ACTION)])
            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "idle"})
            self.assertEqual(bot._turns, {})

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
            bot._turns["session"] = Turn("session", Dest(-1001, 20))
            await bot._set_activity("session", "working")
            dead = bot._turns["session"].activity_task
            dead.cancel()
            try:
                await dead
            except asyncio.CancelledError:
                pass
            self.assertTrue(dead.done())

            await bot._set_activity("session", "streaming")
            revived = bot._turns["session"].activity_task
            self.assertIsNot(revived, dead)
            self.assertFalse(revived.done())
            revived.cancel()

    def _bot_mid_turn(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
"token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001, state_dir=Path(directory),
            default_path=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        # A real turn always reports `working` before it streams; without that
        # the bot (correctly) refuses to treat `idle` as the turn ending.
        bot._turns["session"] = Turn("session", Dest(-1001, 20), working=True)
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
            self.assertEqual(bot._turns["session"].progress_lines, [
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
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("Working", bot.telegram.messages[0][1])
            self.assertIn("first remark", bot.telegram.messages[0][1])
            message_id = bot._turns["session"].progress_msg

            await self._stream(bot, "second remark")
            await self._tool_call(bot)
            await bot._update_progress(bot._turns["session"])
            # Edited in place: no new message, and the edit carries the tail.
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertEqual(len(bot.telegram.edits), 1)
            self.assertEqual(bot.telegram.edits[0][1], message_id)
            self.assertIn("second remark", bot.telegram.edits[0][2])
            # Nothing dirty, nothing sent: the refresh tick must be a no-op.
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(len(bot.telegram.edits), 1)
            await self._idle(bot)

    async def test_repeated_tool_calls_collapse_into_one_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            for _ in range(3):
                await self._tool_call(bot)
            self.assertEqual(bot._turns["session"].progress_lines,
                             ["⚙️ hidden ×3"])
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
            self.assertEqual(bot.telegram.messages, [(20, "🛠 Working… (0s)")])
            self.assertEqual(bot.telegram.message_silent, [True])
            self.assertIsNotNone(bot._turns["session"].progress_msg)
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
            thought_line = bot._turns["session"].progress_lines[0]
            self.assertTrue(thought_line.startswith("💭 long pondering"))
            self.assertLessEqual(len(thought_line), 290)
            self.assertTrue(thought_line.endswith("…"))
            await self._idle(bot)
            self.assertEqual(bot.telegram.html_messages[0][2], "the answer",
                             "thoughts must never leak into the reply")

    async def test_the_final_stamp_carries_elapsed_time_and_context_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turns["session"].started_at = time.monotonic() - 135
            await bot._handle_event({"type": "usage", "session_id": "session",
                                     "used": 217034, "size": 1000000,
                                     "total_tokens": 877000})
            await bot._handle_event({"type": "tool_call", "session_id": "session",
                                     "tool_call_id": "call-1", "title": "hidden"})
            await self._stream(bot, "the answer")
            await self._idle(bot)
            stamp = bot.telegram.messages[-1][1]
            self.assertIn("✅ Turn finished: 2m15s", stamp)
            self.assertIn("Turn: 877k · 1 tool call\n", stamp)
            self.assertIn("Context window: 217k/1M (22%)", stamp)

    def test_the_stamp_drops_the_lines_it_has_no_figures_for(self):
        """Only the claude backend reports usage; the echo backend and any
        other reports none, and the stamp is then the one line it can stand
        behind rather than a row of zeroes."""
        self.assertEqual(_final_stamp(None, "end_turn", 4.0, 0, {}, {}),
                         "✅ Turn finished: 4s")
        self.assertEqual(_final_stamp(None, "end_turn", 4.0, 1, {}, {}),
                         "✅ Turn finished: 4s\n1 tool call")
        # Cost without a token figure still has a line to sit on, and a cost
        # too small to round to a cent is not worth one.
        self.assertEqual(
            _final_stamp(None, "end_turn", 4.0, 2, {"cost_amount": 1.0}, {}),
            "✅ Turn finished: 4s\n2 tool calls")
        self.assertEqual(
            _final_stamp(None, "end_turn", 4.0, 2,
                         {"cost_amount": 1.42}, {"cost_amount": 1.0}),
            "✅ Turn finished: 4s\nTurn: $0.42 · 2 tool calls")
        self.assertEqual(
            _final_stamp(None, "end_turn", 4.0, 0,
                         {"cost_amount": 1.001}, {"cost_amount": 1.0}),
            "✅ Turn finished: 4s")

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
        # that for the end of the turn dropped the turn before any chunk
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
            bot._turns.clear()
            await bot._forward("session", Dest(-1001, 20), "do the thing")

            # The resume's idle, before the turn has ever reported working.
            await self._idle(bot)
            self.assertIn("session", bot._turns,
                          "a turn that never began cannot have ended")

            await bot._handle_event({"type": "agent_state", "session_id": "session",
                                     "state": "working"})
            await self._stream(bot, "the real reply")
            await self._idle(bot)
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "the real reply")
            self.assertNotIn("session", bot._turns)

    async def test_a_message_arriving_mid_turn_is_refused_not_swallowed(self):
        # Observed live: a message sent while a turn was running was forwarded,
        # the daemon refused it with an *info* notice the client never shows, and
        # the forward itself reset the reply buffer -- destroying the reply in flight.
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
            self.assertEqual(bot._turns["session"].reply_parts,
                             ["half a reply so far"],
                             "the in-flight reply must survive")
            self.assertEqual(bot.telegram.messages, [(20, QUEUED_FIRST)])

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

    async def test_a_hung_progress_edit_does_not_stop_the_typing_indicator(self):
        # Found by dogfooding, 2026-09-12. The two shared one loop, so they
        # shared a stall: a Telegram call from that host hangs until its read
        # timeout, and while the edit hung the chat action queued behind it
        # never went out. "typing…" died for forty seconds at a time.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            release = asyncio.Event()

            class HangingEdits(FakeTelegram):
                async def edit_message(self, chat_id, message_id, text):
                    await release.wait()
                    await super().edit_message(chat_id, message_id, text)

            bot.telegram = HangingEdits()
            bot._turns["session"].progress_msg = 99
            bot._turns["session"].progress_dirty = True
            # Several ticks in a few milliseconds. One action proves nothing --
            # the shared loop sent its action before reaching the edit that hung
            # -- so what is being asserted is that they *keep coming* while the
            # edit is still in flight.
            with patch.object(bot_module, "ACTION_REFRESH_SECONDS", 0.005):
                bot._start_activity(bot._turns["session"])
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(len(bot.telegram.actions), 3,
                                    "the indicator must not wait on the progress edit")
            self.assertEqual(bot.telegram.edits, [], "the edit is still hung")
            release.set()
            for task in bot._turns["session"].take_loops():
                task.cancel()

    async def test_a_hung_chat_action_does_not_stop_the_progress_message(self):
        # The same independence, read the other way round.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            release = asyncio.Event()

            class HangingActions(FakeTelegram):
                async def chat_action(self, chat_id, action, thread=None):
                    await release.wait()
                    await super().chat_action(chat_id, action, thread=thread)

            bot.telegram = HangingActions()
            bot._turns["session"].progress_msg = 99
            bot._turns["session"].progress_dirty = True
            # One edit is enough here: the shared loop sent the action *first*,
            # so a hung one meant the edit never happened at all.
            with patch.object(bot_module, "ACTION_REFRESH_SECONDS", 0.005):
                bot._start_activity(bot._turns["session"])
                await asyncio.sleep(0.05)
            self.assertTrue(bot.telegram.edits,
                            "progress must not wait on the chat action")
            self.assertEqual(bot.telegram.actions, [], "the action is still hung")
            release.set()
            for task in bot._turns["session"].take_loops():
                task.cancel()

    async def test_ending_a_turn_stops_both_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._start_activity(bot._turns["session"])
            activity = bot._turns["session"].activity_task
            progress = bot._turns["session"].progress_task
            await self._idle(bot)
            self.assertEqual(bot._turns, {})
            self.assertTrue(activity.cancelled() or activity.done())
            self.assertTrue(progress.cancelled() or progress.done())

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
            self.assertNotIn("session", bot._turns)
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
                self.assertNotIn("session", bot._turns)

    async def test_status_reports_the_daemon_and_the_bot_view(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            turn = bot._turns["session"]
            turn.turn_id = "t123"
            turn.started_at = time.monotonic() - 5
            turn.last_event_at = time.monotonic() - 3
            turn.activity_state = "streaming"
            turn.reply_parts = ["buffered text"]
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
            bot._turns["session"].working = False
            await self._stream(bot, "output proves the turn began")
            await self._idle(bot)
            self.assertNotIn("session", bot._turns)
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
            bot._turns["session"] = Turn("session", Dest(-1001, 20),
                                         reply_parts=["half an answer"])
            bot._persist_turns()
            bot._reset_connection_state()
            self.assertEqual(bot._turns, {})
            self.assertEqual(bot.telegram.messages, [])
            persisted = json.loads(bot._turns_file.read_text())
            self.assertEqual(persisted["session"]["thread"], 20)

    async def test_a_long_quiet_turn_shows_it_in_the_header_on_its_own_pace(self):
        # What replaced the quiet notice. A turn that has said nothing for a
        # long time is visible as a climbing clock over narration that has
        # stopped changing, and it costs no message of its own -- nor an edit
        # per tick, since a clock that is the only thing to have moved is
        # paced apart from the 4-second tick that carries content.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turns["session"].started_at = time.monotonic() - 601
            bot._turns["session"].progress_msg = 99
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(bot.telegram.edits[-1][2], f"{PROGRESS_HEADER} (10m01s)")
            edits = len(bot.telegram.edits)
            # The clock has moved, but not long enough ago to be worth an edit.
            bot._turns["session"].started_at = time.monotonic() - 605
            await bot._update_progress(bot._turns["session"])
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(len(bot.telegram.edits), edits,
                             "a clock-only tick must not spend an edit")
            # Once the interval has passed, it does.
            header, _ = bot._turns["session"].progress_sent
            bot._turns["session"].progress_sent = (
                header, time.monotonic() - PROGRESS_CLOCK_SECONDS - 1)
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(bot.telegram.edits[-1][2], f"{PROGRESS_HEADER} (10m05s)")
            self.assertEqual(bot.telegram.messages, [],
                             "the clock costs no message of its own")
            await self._idle(bot)

    async def test_narration_is_never_held_back_by_the_clock_s_pace(self):
        # The pace is for a header that moved on its own. Anything with
        # something to show marks itself dirty and goes out on the next tick.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turns["session"].progress_msg = 99
            await bot._update_progress(bot._turns["session"])
            edits = len(bot.telegram.edits)
            await self._stream(bot, "a thing happened")
            bot._close_block(bot._turns["session"])
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(len(bot.telegram.edits), edits + 1)
            self.assertIn("a thing happened", bot.telegram.edits[-1][2])
            await self._idle(bot)

    async def test_a_queued_message_reaches_the_header_without_waiting(self):
        # The depth is the receipt for something the user just did, so it does
        # not sit behind the clock's pace.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot_mid_turn(directory)
            bot._turns["session"].progress_msg = 99
            await bot._update_progress(bot._turns["session"])
            edits = len(bot.telegram.edits)
            await bot._enqueue_message(bot._turns["session"], Dest(-1001, 20),
                                       "and also", 11)
            await bot._update_progress(bot._turns["session"])
            self.assertEqual(len(bot.telegram.edits), edits + 1)
            self.assertIn("📥 1 queued", bot.telegram.edits[-1][2])
            # Dropped rather than flushed: the flush would forward a prompt,
            # and this fixture has no socket to forward it on.
            bot._drop_queue("session")
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
            # Never installed in bot._turns: this turn is over, and a tick
            # still in flight for it finds that out by identity.
            turn = Turn("session", Dest(-1001, 20),
                        progress_lines=["⚙️ did a thing"], progress_dirty=True)
            await bot._update_progress(turn)
            self.assertEqual(bot.telegram.messages, [],
                             "no Working… may appear after the reply")

    async def test_the_final_stamp_is_still_allowed(self):
        # It runs *after* the destination is popped, so it must be exempt.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            turn = Turn("session", Dest(-1001, 20),
                        progress_lines=["⚙️ did a thing"])
            await bot._update_progress(turn, final_note="✅ done in 3s")
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("✅ done in 3s", bot.telegram.messages[0][1])

    async def test_a_stale_chat_action_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._send_action(Turn("session", Dest(-1001, 20)))
            self.assertEqual(bot.telegram.actions, [],
                             "no typing… after the answer has arrived")

    async def test_finish_turn_waits_for_the_activity_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            turn = Turn("session", Dest(-1001, 20))
            bot._turns["session"] = turn
            started = asyncio.Event()

            async def _loop():
                started.set()
                await asyncio.sleep(3600)
            task = asyncio.create_task(_loop())
            turn.activity_task = task
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

        async def session(self, session_id, include_transcript=False):
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
            # Long-running, so the adopted turn inherits a start time rather
            # than beginning its clock again at zero.
            old._turns["session"] = Turn("session", Dest(-1001, 20),
                                         started_at=time.monotonic() - 600)
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon(
                "working", self._transcript("the full", " reply"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turns["session"].dest, Dest(-1001, 20))
            self.assertTrue(bot._turns["session"].adopted)
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
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})
            self.assertEqual(bot._turns, {})

    async def test_a_turn_that_ended_while_the_bot_was_away_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            # Six raw characters were already flushed before the restart.
            old._turns["session"] = Turn("session", Dest(-1001, 20),
                                         consumed=6, delivered=6)
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript("before", " and after"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turns, {}, "an ended turn is not adopted")
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("recovered", bot.telegram.messages[0][1].lower())
            self.assertEqual(len(bot.telegram.html_messages), 1)
            self.assertEqual(bot.telegram.html_messages[0][2], "and after")
            self.assertEqual(json.loads(bot._turns_file.read_text()), {})

    async def test_an_ended_turn_with_nothing_undelivered_stays_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turns["session"] = Turn(
                "session", Dest(-1001, 20), consumed=len("the whole reply"),
                delivered=len("the whole reply"))
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript("the whole reply"))
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot.telegram.messages, [])
            self.assertEqual(bot.telegram.html_messages, [])

    async def test_an_ended_turn_that_produced_nothing_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turns["session"] = Turn("session", Dest(-1001, 20))
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("idle", self._transcript())
            await bot._reconcile_persisted_turns()
            self.assertEqual(len(bot.telegram.messages), 1)
            self.assertIn("without delivering", bot.telegram.messages[0][1])

    async def test_a_vanished_session_is_the_only_true_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self._bot(directory)
            old._turns["session"] = Turn("session", Dest(-1001, 20))
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
            old._turns["session"] = Turn("session", Dest(-1001, None))
            old._persist_turns()

            bot = self._bot(directory)
            bot.daemon = self._Daemon("working")
            await bot._reconcile_persisted_turns()
            self.assertEqual(bot._turns, {})
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
                                hidden=None, roles=None):
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
                async def session(self, session_id, include_transcript=False):
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
                                hidden=None, roles=None):
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
                                hidden=None, roles=None):
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
                                hidden=None, roles=None):
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
                async def session(self, session_id, include_transcript=False):
                    raise ApiError("no such session")

                async def spawn(self, path, name=None, backend=None,
                                ephemeral=False, hidden=None, roles=None):
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
                async def session(self, session_id, include_transcript=False):
                    raise ApiError("none")

                async def spawn(self, path, name=None, backend=None,
                                ephemeral=False, hidden=None, roles=None):
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
                async def session(self, session_id, include_transcript=False):
                    return {"session_id": session_id}

                async def spawn(self, path, name=None, backend=None, ephemeral=False,
                                hidden=None, roles=None):
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
                                hidden=None, roles=None):
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
            self.assertEqual(bot._turns, {})
            self.assertEqual(bot.telegram.messages, [])


class InterruptedTurnTests(unittest.IsolatedAsyncioTestCase):
    """A turn that was open when the daemon went down leaves a trace.

    The prompt is persisted and the chunks up to the crash are persisted,
    but the fact that a turn was *open* was not, so a restart mid-turn left
    a user message with half an answer and nothing saying why.
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
        record = make_record(self.coordinator, "work", name="work",
                             auto_named=False, persisted=True)
        self.coordinator._persist_meta(record)

    def _stored(self):
        return tomllib.loads(
            Path(self.temporary.name, "work", "meta.toml").read_text())

    def _run_a_turn(self, *, ending: bool):
        emit = self.coordinator._emit
        emit({"type": "message", "session_id": "work", "role": "user",
              "text": "read the failing test"})
        emit({"type": "turn_started", "session_id": "work", "turn_id": "t1",
              "prompt_chars": 24})
        emit({"type": "tool_call", "session_id": "work", "tool_call_id": "c1",
              "title": "read"})
        if ending:
            emit({"type": "turn_ended", "session_id": "work", "turn_id": "t1",
                  "outcome": "completed", "stop_reason": "end_turn",
                  "duration": 1.0, "message_chunks": 1, "output_chars": 9,
                  "thought_chunks": 0, "tool_calls": 1})

    def _restarted(self):
        restarted = SessionCoordinator(Path(self.temporary.name))
        restarted.load_persisted()
        record = restarted._records["work"]
        record.agent = FakeAgent("work")
        return restarted, record

    async def test_an_open_turn_is_on_disk_and_a_closed_one_is_not(self):
        self._run_a_turn(ending=False)
        stored = self._stored()
        self.assertIs(stored.get("turn_open"), True)
        self.assertEqual(stored.get("turn_last_event"), "tool_call")
        self._run_a_turn(ending=True)
        self.assertNotIn("turn_open", self._stored())

    async def test_the_next_prompt_says_the_last_turn_was_cut_off_once(self):
        self._run_a_turn(ending=False)
        restarted, record = self._restarted()

        await restarted.send("work", "are you still there?")
        first = [part.text for part in record.agent.sent[0]]
        interrupted = [text for text in first if "cut off" in text]
        self.assertEqual(len(interrupted), 1)
        self.assertIn("a tool call", interrupted[0], "roughly where it stopped")
        self.assertEqual(first[-1], "are you still there?",
                         "ahead of the user's own words, in its own block")

        await restarted.send("work", "and again")
        self.assertFalse([part for part in record.agent.sent[1]
                          if "cut off" in part.text],
                         "told once, not on every message from here on")

    async def test_a_turn_that_ended_leaves_nothing_to_say(self):
        self._run_a_turn(ending=True)
        restarted, record = self._restarted()
        await restarted.send("work", "next thing")
        self.assertFalse([part for part in record.agent.sent[0]
                          if "cut off" in part.text])

    async def test_a_resume_and_a_stop_in_between_do_not_lose_it(self):
        # A stop gives up everything the live agent owned, and the notice
        # used to be part of that, while the marker it came from was cleared
        # by the next turn: told to nobody and then forgotten. It is owed
        # until it is said, like the orientation beside it.
        self._run_a_turn(ending=False)
        restarted = SessionCoordinator(Path(self.temporary.name))
        restarted.load_persisted()
        with patch("falconfox.coordinator.AgentSession", QuietAgentSession):
            await restarted.resume_session("work")
            await restarted.stop_session("work")
            await restarted.send("work", "are you still there?")
        parts = [part.text for part in restarted._records["work"].agent.sent[0]]
        self.assertEqual(len([text for text in parts if "cut off" in text]), 1)
        self.assertEqual(parts[-1], "are you still there?")


class SessionContextTests(unittest.IsolatedAsyncioTestCase):
    """What a session is told about itself, and when."""

    class FakeSession:
        def __init__(self, session_id="abcd1234"):
            self.session_id = session_id
            self.sent = []

        async def send(self, parts):
            self.sent.append(list(parts))

    def _coordinator(self, directory, session, roles=None):
        coordinator = SessionCoordinator(Path(directory))
        make_record(coordinator, session.session_id, name="fake", path=directory,
                    backend="fake", roles=list(roles or []), agent=session)
        return coordinator

    async def test_the_first_message_carries_orientation_and_the_next_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.FakeSession()
            coordinator = self._coordinator(directory, session)

            await coordinator.send(session.session_id, "hello")
            await coordinator.send(session.session_id, "again")

            first, second = session.sent
            # Its own block, not glued to the front of what the user typed.
            self.assertIn("Running under FalconFox", first[0].text)
            self.assertTrue(first[0].system)
            self.assertEqual(first[-1].text, "hello")
            self.assertFalse(first[-1].system)
            self.assertEqual([part.text for part in second], ["again"])

    async def test_orientation_is_still_owed_after_a_daemon_restart(self):
        # Queued at spawn it would be lost, because the queue is in memory
        # while the session is on disk. What persists instead is the fact that
        # the session has not been told yet.
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(coordinator, "_ensure_slot", return_value=False):
                # Named, so it persists at all: an unnamed session with no
                # messages does not survive a restart in the first place.
                session_id = await coordinator.add_session(
                    path=directory, name="manager", hidden=True,
                    roles=[".manager"])
            self.assertFalse(coordinator._records[session_id].oriented)

            restarted = SessionCoordinator(Path(directory))
            restarted.load_persisted()
            self.assertFalse(restarted._records[session_id].oriented)
            self.assertEqual(restarted._records[session_id].roles, [".manager"])

    async def test_a_role_adds_its_own_piece(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.FakeSession()
            coordinator = self._coordinator(directory, session, roles=[".manager"])
            await coordinator.send(session.session_id, "hello")
            pieces = [part.text for part in session.sent[0]]
            joined = "".join(pieces)
            self.assertIn(config.SESSION_CONTEXT.rstrip(), joined)
            self.assertIn(config.MANAGER_ORIENTATION.rstrip(), joined)
            # Global first, then the role, then the user.
            self.assertLess(joined.index("Running under FalconFox"),
                            joined.index("Session manager"))
            self.assertEqual(pieces[-1], "hello")

    async def test_an_unknown_role_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.FakeSession()
            coordinator = self._coordinator(directory, session,
                                            roles=["nobody.nothing"])
            with self.assertLogs("falconfox.coordinator", level="WARNING") as logged:
                await coordinator.send(session.session_id, "hello")
            self.assertIn("nobody.nothing", "".join(logged.output))
            self.assertEqual(session.sent[0][-1].text, "hello")

    async def test_a_resume_adds_the_transcript_without_displacing_orientation(self):
        # Both want the same queue. Appending rather than replacing is what
        # stops a resumed session losing the explanation of where it is.
        with tempfile.TemporaryDirectory() as directory:
            session = self.FakeSession()
            coordinator = self._coordinator(directory, session)
            record = coordinator._records[session.session_id]
            record.transcript = [
                {"type": "message", "role": "user", "text": "earlier"}]
            record.pending_context = [
                PromptPart(text=coordinator._context_prompt(record),
                           system=True, record=False)]

            await coordinator.send(session.session_id, "hello")
            texts = [part.text for part in session.sent[0]]
            self.assertIn(config.SESSION_CONTEXT.rstrip() + "\n", texts)
            self.assertTrue(any("resuming a previous session" in text
                                for text in texts))

    async def test_a_transcript_replay_stays_out_of_the_transcript(self):
        # Otherwise every resume folds the previous transcript into the next
        # one, and they grow without bound.
        with tempfile.TemporaryDirectory() as directory:
            events = []
            session = AgentSession(
                session_id="s", name="n", path=Path(directory), backend=None,
                emit=events.append, request_permission=None,
            )
            session._acp_session_id = "acp"

            class FakeConn:
                async def prompt(self, **_kwargs):
                    class Response:
                        stop_reason = "end_turn"
                        usage = None
                    return Response()

            session._conn = FakeConn()
            await session.send([
                PromptPart(text="orientation", system=True),
                PromptPart(text="a replay", system=True, record=False),
                PromptPart(text="hello"),
            ])
            recorded = [event["text"] for event in events
                        if event.get("type") == "message"]
            self.assertEqual(recorded, ["orientation", "hello"])

    async def test_the_transcript_replay_includes_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SessionCoordinator(Path(directory))
            record = make_record(coordinator, "s", transcript=[
                {"type": "message", "role": "user", "text": "the orientation",
                 "system": True},
                {"type": "message", "role": "user", "text": "hello"},
            ])
            replayed = coordinator._transcript_text(record)
            self.assertIn("the orientation", replayed)
            self.assertIn("hello", replayed)


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    """`attach` is a request to a client, so the answer comes back from one."""

    def _coordinator(self, directory):
        return SessionCoordinator(Path(directory))

    async def test_no_client_fails_immediately(self):
        # Waiting cannot change the outcome when nothing is subscribed, only
        # how long the agent waits to hear it.
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            target = Path(directory).joinpath("file.txt")
            target.write_text("x")
            with self.assertRaises(FalconFoxError) as caught:
                await coordinator.attach("abcd1234", str(target))
            self.assertIn("no client", str(caught.exception))

    async def test_a_client_result_completes_the_call(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            target = Path(directory).joinpath("file.txt")
            target.write_text("x")
            with coordinator.bus.subscribe() as queue:
                call = asyncio.ensure_future(coordinator.attach("abcd1234", str(target)))
                event = await asyncio.wait_for(queue.get(), 2)
                self.assertEqual(event["type"], "attachment")
                coordinator.resolve_attachment(event["request_id"], True)
                self.assertEqual((await asyncio.wait_for(call, 2))["delivered"], True)

    async def test_a_client_failure_is_raised_to_the_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            target = Path(directory).joinpath("file.txt")
            target.write_text("x")
            with coordinator.bus.subscribe() as queue:
                call = asyncio.ensure_future(coordinator.attach("abcd1234", str(target)))
                event = await asyncio.wait_for(queue.get(), 2)
                coordinator.resolve_attachment(event["request_id"], False, "file is too big")
                with self.assertRaises(FalconFoxError) as caught:
                    await asyncio.wait_for(call, 2)
            self.assertIn("too big", str(caught.exception))

    async def test_waiting_gives_up_rather_than_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            target = Path(directory).joinpath("file.txt")
            target.write_text("x")
            with coordinator.bus.subscribe():
                with self.assertRaises(FalconFoxError) as caught:
                    await coordinator.attach("abcd1234", str(target), timeout=0.05)
            self.assertIn("confirmed", str(caught.exception))

    async def test_no_ack_returns_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            target = Path(directory).joinpath("file.txt")
            target.write_text("x")
            with coordinator.bus.subscribe():
                result = await coordinator.attach("abcd1234", str(target), ack=False)
            self.assertIsNone(result["delivered"])

    async def test_a_missing_file_never_reaches_a_client(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            make_record(coordinator, "abcd1234")
            with coordinator.bus.subscribe() as queue:
                with self.assertRaises(FalconFoxError):
                    await coordinator.attach("abcd1234", f"{directory}/nope.txt")
                self.assertTrue(queue.empty())


class AttachmentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Where the bot sends a session's file, and what it reports back."""

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._ws = self.FakeSocket()
        return bot

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def test_a_file_goes_to_its_session_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)
            target = Path(directory).joinpath("report.txt")
            target.write_text("x")
            await bot._deliver_attachment({
                "session_id": "abcd1234", "path": str(target),
                "caption": "here", "request_id": "req1"})
            self.assertEqual(bot.telegram.documents,
                             [(-1001, 42, target, "here", "sendDocument")])
            self.assertEqual(bot._ws.sent, [{"action": "attachment_result",
                                             "request_id": "req1", "ok": True,
                                             "error": None}])

    async def test_the_manager_sends_to_general(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = "manager1"
            target = Path(directory).joinpath("report.txt")
            target.write_text("x")
            await bot._deliver_attachment({
                "session_id": "manager1", "path": str(target), "request_id": "req2"})
            self.assertEqual(bot.telegram.documents[0][1], None)

    async def test_a_session_with_no_topic_reports_the_failure(self):
        # The agent is waiting on this answer, so "nowhere to send it" has to
        # come back rather than being logged and dropped.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            target = Path(directory).joinpath("report.txt")
            target.write_text("x")
            await bot._deliver_attachment({
                "session_id": "orphan", "path": str(target), "request_id": "req3"})
            self.assertEqual(bot.telegram.documents, [])
            self.assertFalse(bot._ws.sent[0]["ok"])
            self.assertIn("no chat", bot._ws.sent[0]["error"])

    async def test_a_refused_photo_still_arrives_as_a_file(self):
        # A tall screenshot exceeds Telegram's photo dimensions. Reporting a
        # failure would deny the user a file that could have arrived.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)
            target = Path(directory).joinpath("tall.png")
            target.write_text("x")
            bot.telegram.document_error = ApiError("PHOTO_INVALID_DIMENSIONS")
            bot.telegram.document_ok = {"sendDocument"}
            await bot._deliver_attachment({
                "session_id": "abcd1234", "path": str(target), "request_id": "req5"})
            self.assertEqual([row[4] for row in bot.telegram.documents], ["sendDocument"])
            self.assertTrue(bot._ws.sent[0]["ok"])

    async def test_an_upload_failure_is_reported_and_said(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)
            target = Path(directory).joinpath("report.txt")
            target.write_text("x")
            bot.telegram.document_error = ApiError("file is too big")
            await bot._deliver_attachment({
                "session_id": "abcd1234", "path": str(target), "request_id": "req4"})
            self.assertFalse(bot._ws.sent[0]["ok"])
            self.assertIn("too big", bot._ws.sent[0]["error"])
            self.assertIn("Could not send report.txt", bot.telegram.messages[0][1])


class ClientRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """What the bot writes where the daemon reads it, and what it means."""

    def _bot(self, directory):
        return FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, state_dir=Path(directory)))

    def test_orientation_and_roles_are_written_under_the_client_name(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = Path(directory).joinpath("clients")
            clients.mkdir()
            bot = self._bot(directory)
            bot._bot_username = "a_bot"
            with patch.object(bot, "_clients_dir", return_value=clients):
                bot._register_orientation()
            # The directory name is the namespace: nothing inside the files
            # says "telegram", and nothing needs to.
            mine = clients.joinpath("telegram")
            self.assertIn("Talking through Telegram",
                          mine.joinpath("orientation.md").read_text())
            concierge = mine.joinpath("roles", "concierge.md").read_text()
            self.assertIn("You are the session behind the bot's **private chat**",
                          concierge)
            self.assertIn("https://t.me/a_bot?startgroup&admin=manage_topics",
                          concierge)

    def test_a_daemon_with_no_client_directory_is_survivable(self):
        # A client that cannot register should cost its own orientation, not
        # the bot's startup.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            with patch.object(bot, "_clients_dir", return_value=None):
                bot._register_orientation()

    def test_a_partial_write_is_never_visible(self):
        # The daemon reads these on every spawn, so the window matters.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).joinpath("orientation.md")
            target.write_text("the old one")
            seen = []

            original = Path.replace

            def watched(self, other):
                seen.append(Path(other).read_text())
                return original(self, other)

            with patch.object(Path, "replace", watched):
                _write_atomic(target, "the new one")
            self.assertEqual(seen, ["the old one"])
            self.assertEqual(target.read_text(), "the new one")


class ClientOrientationCompositionTests(unittest.IsolatedAsyncioTestCase):
    """How the daemon turns a client directory into a session's orientation."""

    def _write_client(self, clients, name, orientation="", roles=None):
        root = clients.joinpath(name)
        root.joinpath("roles").mkdir(parents=True, exist_ok=True)
        if orientation:
            root.joinpath("orientation.md").write_text(orientation)
        for role, body in (roles or {}).items():
            root.joinpath("roles", f"{role}.md").write_text(body)

    def test_every_client_orientation_reaches_every_session(self):
        # Unconditional on purpose: a session started in one client may be
        # spoken to through another later.
        with tempfile.TemporaryDirectory() as directory:
            clients = Path(directory).joinpath("clients")
            self._write_client(clients, "telegram", orientation="about telegram")
            self._write_client(clients, "web", orientation="about the web")
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir", return_value=clients):
                pieces = coordinator._orientation([])
            joined = "".join(pieces)
            self.assertIn("about telegram", joined)
            self.assertIn("about the web", joined)
            # Deterministic order, by directory name.
            self.assertLess(joined.index("about telegram"),
                            joined.index("about the web"))

    def test_a_role_resolves_through_the_client_that_registered_it(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = Path(directory).joinpath("clients")
            self._write_client(clients, "telegram", roles={"concierge": "tg setup"})
            self._write_client(clients, "web", roles={"concierge": "web setup"})
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir", return_value=clients):
                pieces = coordinator._orientation(["web.concierge"])
            # Two clients can both offer a "concierge" without meeting.
            self.assertIn("web setup", "".join(pieces))
            self.assertNotIn("tg setup", "".join(pieces))

    def test_the_daemons_own_role_takes_the_empty_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = Path(directory).joinpath("clients")
            clients.mkdir()
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir", return_value=clients):
                dotted = coordinator._orientation([".manager"])
                bare = coordinator._orientation(["manager"])
            self.assertIn(config.MANAGER_ORIENTATION.rstrip() + "\n", dotted)
            # A bare name is read as the daemon's, so --role manager works too.
            self.assertEqual(dotted, bare)

    def test_every_piece_ends_with_a_newline(self):
        # Blocks are joined by the backend, so a piece ending mid-line runs
        # into the next one's heading. Seen live as
        # "...Telegram commands# Talking through Telegram".
        with tempfile.TemporaryDirectory() as directory:
            clients = Path(directory).joinpath("clients")
            self._write_client(clients, "telegram", orientation="no trailing newline",
                               roles={"concierge": "nor here"})
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir", return_value=clients):
                pieces = coordinator._orientation(["telegram.concierge", ".manager"])
            self.assertTrue(all(piece.endswith("\n") for piece in pieces))

    def test_a_missing_client_directory_still_yields_the_global_piece(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir",
                              return_value=Path(directory).joinpath("nope")):
                self.assertEqual(coordinator._orientation([]),
                                 [config.SESSION_CONTEXT.rstrip() + "\n"])


class SessionReadTests(unittest.IsolatedAsyncioTestCase):
    """Reading a session should not cost its whole history."""

    async def test_the_client_asks_for_a_transcript_only_when_it_wants_one(self):
        asked = []

        async def fake_request(url, *_args, **_kwargs):
            asked.append(url)
            return {}

        api = DaemonApi("http://daemon")
        with patch("falconfox_telegram.api._json_request", fake_request):
            await api.session("abcd1234")
            await api.session("abcd1234", include_transcript=True)
        self.assertTrue(asked[0].endswith("/api/sessions/abcd1234"))
        self.assertTrue(asked[1].endswith("?include_transcript=true"))

    async def test_the_bot_asks_only_where_it_needs_one(self):
        # The two hot callers want a field -- does this exist, where does it
        # run -- and a transcript grows without bound between clears, so
        # sending one by default made an existence check cost megabytes.
        asked = []

        class Daemon:
            async def session(self, session_id, include_transcript=False):
                asked.append(include_transcript)
                return {"session_id": session_id, "path": "/srv/work",
                        "transcript": []}

        with tempfile.TemporaryDirectory() as directory:
            bot = FalconFoxTelegramBot(BotConfig(
                "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
                state_dir=Path(directory), default_path=Path("/tmp")))
            bot.telegram = FakeTelegram()
            bot.daemon = Daemon()
            bot._bind("abcd1234", 42)
            await bot._still_exists("abcd1234")
            await bot._shell_cwd(Dest(-1001, 42))
            self.assertEqual(asked, [False, False])
            await bot._turn_text_from_transcript("abcd1234")
            self.assertEqual(asked[-1], True)


class HelpModuleTests(unittest.TestCase):
    """The lookup tree behind `falconfox help`."""

    def _run(self, directory, modules):
        run = Path(directory)
        for dotted, body in modules.items():
            namespace, _, rest = dotted.partition(".")
            root = (run.joinpath("help") if not namespace
                    else run.joinpath("clients", namespace, "help"))
            path = root.joinpath(*rest.split(".")).with_suffix(".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return run

    def test_the_directory_decides_the_dotted_path(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(directory, {
                ".lifecycle": "# Daemon startup, restart, and shutdown\n\nx",
                "telegram.commands": "# Telegram commands\n\nx",
                "telegram.commands.new": "# New session command\n\nx",
            })
            self.assertEqual(sorted(ffhelp.discover(run)),
                             [".lifecycle", "telegram.commands",
                              "telegram.commands.new"])

    def test_the_index_takes_its_titles_from_the_first_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(directory, {
                "telegram.commands": "# Telegram commands\n\nbody",
                ".lifecycle": "no heading, just a line\n\nbody",
            })
            listing = ffhelp.index(run)
            self.assertIn("Telegram commands", listing)
            # No frontmatter: a document with no heading still gets a title.
            self.assertIn("no heading, just a line", listing)

    def test_a_module_wins_over_its_own_children(self):
        # Listing instead would hide a document behind the things below it.
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(directory, {
                "telegram.commands": "# Telegram commands\n\nthe body",
                "telegram.commands.new": "# New session command\n\nx",
            })
            found = ffhelp.lookup(run, "telegram.commands")
            self.assertIn("the body", found)
            self.assertIn("More under this topic", found)
            self.assertIn("telegram.commands.new", found)

    def test_a_branch_lists_what_is_under_it(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(directory, {
                "telegram.commands": "# Telegram commands\n\nx"})
            self.assertIn("telegram.commands", ffhelp.lookup(run, "telegram"))

    def test_an_unknown_topic_is_no_answer_rather_than_an_empty_one(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(directory, {"telegram.commands": "# c\n\nx"})
            self.assertIsNone(ffhelp.lookup(run, "nope"))

    def test_no_registrations_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(ffhelp.index(Path(directory)), "")


class HelpInOrientationTests(unittest.IsolatedAsyncioTestCase):
    """The index the daemon composes into the global piece."""

    def test_the_index_is_generated_daemon_side_from_every_namespace(self):
        # A client knows only what it registered; the daemon sees all of them
        # and its own, so the listing cannot be a client's to write.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory).joinpath("run")
            for dotted, root in ((".lifecycle", run.joinpath("help")),
                                 ("telegram.commands",
                                  run.joinpath("clients", "telegram", "help"))):
                root.mkdir(parents=True, exist_ok=True)
                root.joinpath(f"{dotted.split('.')[-1]}.md").write_text(
                    f"# {dotted} title\n\nbody")
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir",
                              return_value=run.joinpath("clients")):
                piece = coordinator._orientation([])[0]
            self.assertIn("## Looking things up", piece)
            self.assertIn(".lifecycle", piece)
            self.assertIn("telegram.commands", piece)

    def test_nothing_registered_means_no_section_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SessionCoordinator(Path(directory))
            with patch.object(falconfox_state, "clients_dir",
                              return_value=Path(directory).joinpath("nope")):
                piece = coordinator._orientation([])[0]
            self.assertNotIn("Looking things up", piece)

    def test_every_command_is_documented_somewhere_in_the_help(self):
        # The one-liners in /help are for a user mid-task; this is what an
        # agent reads when asked what a command does. A new command that
        # reaches neither is one nobody can explain.
        documented = COMMANDS_HELP
        missing = [usage.split()[0] for usage, _, _ in COMMANDS
                   if usage.split()[0] not in documented]
        self.assertEqual(missing, [])


class SessionTagTests(unittest.IsolatedAsyncioTestCase):
    """Tags are opaque labels: FalconFox folds them and stores them, and the
    order is preserved because a client with one slot reads the first one."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.coordinator = SessionCoordinator(Path(self.temporary.name))
        make_record(self.coordinator, "work", name="work", state="idle",
                    live=True, auto_named=False)

    async def test_tags_are_folded_but_not_reordered(self):
        tags = await self.coordinator.set_tags(
            "work", ["Urgent", "  Archived  ", "urgent", ""])
        self.assertEqual(tags, ["urgent", "archived"],
                         "case folds and duplicates drop, but the order is the payload")

    async def test_whitespace_inside_a_tag_is_refused(self):
        # The map matches by string, so a tag has to be one word or the
        # lookup silently never fires.
        with self.assertRaises(FalconFoxError):
            await self.coordinator.set_tags("work", ["needs review"])

    async def test_clearing_is_an_empty_list(self):
        await self.coordinator.set_tags("work", ["archived"])
        self.assertEqual(await self.coordinator.set_tags("work", []), [])

    async def test_tags_survive_a_daemon_restart(self):
        await self.coordinator.set_tags("work", ["archived", "slow"])
        stored = tomllib.loads(
            Path(self.temporary.name, "work", "meta.toml").read_text())
        self.assertEqual(stored.get("tags"), ["archived", "slow"])
        restarted = SessionCoordinator(Path(self.temporary.name))
        restarted.load_persisted()
        self.assertEqual(restarted._records["work"].tags, ["archived", "slow"])

    async def test_a_session_updated_event_carries_the_tags(self):
        events = []
        self.coordinator._emit = lambda event: events.append(event)
        await self.coordinator.set_tags("work", ["archived"])
        self.assertEqual([event["tags"] for event in events
                          if event["type"] == "session_updated"], [["archived"]])


class TopicIconTests(unittest.IsolatedAsyncioTestCase):
    """Tag glyphs in the title, and the one constant topic icon. Every rename
    or icon edit posts a service message, so the tests are mostly about *not*
    making calls."""

    def _bot(self, directory, glyphs=None, default_icon="5009"):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._icon_emoji = dict({"archived": "\U0001f4c1", "urgent": "\u2757\ufe0f"}
                               if glyphs is None else glyphs)
        bot._default_icon = default_icon
        return bot

    # --- the title ------------------------------------------------------

    async def test_every_mapped_tag_is_drawn_in_tag_order(self):
        # The whole point of moving to the title: the icon slot held one, so
        # order had to mean priority. Here it only means order.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            self.assertEqual(
                bot._title_for({"name": "work", "tags": ["urgent", "archived"]}),
                "\u2757\ufe0f\U0001f4c1 work")
            self.assertEqual(
                bot._title_for({"name": "work", "tags": ["archived", "urgent"]}),
                "\U0001f4c1\u2757\ufe0f work")

    async def test_an_unmapped_tag_is_skipped_and_the_rest_still_draw(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            self.assertEqual(
                bot._title_for({"name": "work", "tags": ["golang", "archived"]}),
                "\U0001f4c1 work")
            self.assertEqual(
                bot._title_for({"name": "work", "tags": ["golang"]}), "work",
                "no glyphs means no leading space either")

    async def test_the_length_cap_is_spent_on_the_name(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            title = bot._title_for({"name": "x" * 200, "tags": ["urgent"]})
            self.assertEqual(len(title), 128)
            self.assertTrue(title.startswith("\u2757\ufe0f "),
                            "the glyphs are what the title is for")

    async def test_a_name_that_is_itself_an_emoji_is_not_confused_for_a_glyph(self):
        # The title is always recomputed and never parsed back, which is what
        # keeps /name and the glyphs from fighting.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            self.assertEqual(
                bot._title_for({"name": "\U0001f996 dino", "tags": []}),
                "\U0001f996 dino")

    async def test_a_tag_change_alone_retitles_the_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)
            await bot._mirror_session({"session_id": "work", "name": "work",
                                       "tags": []})
            await bot._mirror_session({"session_id": "work", "name": "work",
                                       "tags": ["urgent"]})
            self.assertEqual(bot.telegram.renamed,
                             [(42, "work"), (42, "\u2757\ufe0f work")])

    # --- the icon -------------------------------------------------------

    async def test_a_new_topic_carries_the_default_icon_without_an_edit(self):
        # Creation takes the icon as an argument; an edit would cost a
        # service message in a topic that is one second old.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._ensure_topic({"session_id": "work", "name": "work",
                                     "tags": ["archived"]})
            self.assertEqual(bot.telegram.topics,
                             [("\U0001f4c1 work", "5009")])
            self.assertEqual(getattr(bot.telegram, "icons", []), [])

    async def test_tags_do_not_touch_the_icon(self):
        # The slot carries no signal yet. Retagging must not write to it.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)
            await bot._apply_icon({"session_id": "work", "tags": ["archived"]}, 42)
            await bot._apply_icon({"session_id": "work", "tags": []}, 42)
            self.assertEqual(bot.telegram.icons, [(42, "5009")],
                             "one icon, applied once, whatever the tags do")

    async def test_an_unchanged_icon_makes_no_call(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            session = {"session_id": "work", "tags": []}
            bot._bind("work", 42)
            self.assertTrue(await bot._apply_icon(session, 42))
            self.assertFalse(await bot._apply_icon(session, 42),
                             "re-applying would stamp a service message")
            self.assertEqual(bot.telegram.icons, [(42, "5009")])

    async def test_with_no_default_configured_the_slot_is_left_alone(self):
        # Not the same as clearing it: a cleared icon draws a badge from the
        # title's first character, which is a question mark once that is a
        # glyph.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory, default_icon="")
            bot._bind("work", 42)
            self.assertFalse(await bot._apply_icon({"session_id": "work"}, 42))
            self.assertEqual(getattr(bot.telegram, "icons", []), [])

    async def test_an_icon_already_in_place_counts_as_applied(self):
        # Telegram calls this a 400, but it means the topic is already how it
        # was asked to be. Read as failure, the bot never records it and asks
        # again on every session_updated for the life of the process.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)

            async def _unchanged(chat_id, thread, icon):
                raise ApiError("Bad Request: TOPIC_NOT_MODIFIED")

            bot.telegram.set_topic_icon = _unchanged
            session = {"session_id": "work", "tags": []}
            await bot._apply_icon(session, 42)
            self.assertEqual(bot._topic_icons.get("work"), "5009")

            calls = []
            async def _count(chat_id, thread, icon):
                calls.append(icon)
            bot.telegram.set_topic_icon = _count
            await bot._apply_icon(session, 42)
            self.assertEqual(calls, [], "the retry loop is what this prevents")

    async def test_a_title_already_in_place_counts_as_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)

            async def _unchanged(chat_id, thread, name):
                raise ApiError("Bad Request: TOPIC_NOT_MODIFIED")

            bot.telegram.rename_topic = _unchanged
            await bot._mirror_session({"session_id": "work", "name": "the work"})
            self.assertEqual(bot._topic_names.get("work"), "the work")

    # --- what survives a restart ----------------------------------------

    async def test_the_applied_icon_survives_a_restart(self):
        # The Bot API cannot report a topic's icon, so the only alternative to
        # remembering is re-applying blindly -- a service message per topic
        # on every start.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)
            await bot._apply_icon({"session_id": "work"}, 42)
            restarted = self._bot(directory)
            restarted._load_topics()
            self.assertEqual(restarted._topics, {"work": 42})
            self.assertFalse(await restarted._apply_icon({"session_id": "work"}, 42))
            self.assertEqual(getattr(restarted.telegram, "icons", []), [])

    async def test_the_composed_title_survives_a_restart(self):
        # Titles are remembered rather than guessed. Guessing costs a call per
        # session per start, and the reconciler now sleeps after every call.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)
            session = {"session_id": "work", "name": "work", "tags": ["urgent"]}
            self.assertTrue(await bot._apply_title(session, 42))
            restarted = self._bot(directory)
            restarted._load_topics()
            self.assertEqual(restarted._topic_names.get("work"),
                             "\u2757\ufe0f work")
            self.assertFalse(await restarted._apply_title(session, 42))
            self.assertEqual(getattr(restarted.telegram, "renamed", []), [])

    async def test_the_old_flat_topic_file_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "topics.json").write_text(json.dumps({"work": 42}))
            bot = self._bot(directory)
            bot._load_topics()
            self.assertEqual(bot._topics, {"work": 42},
                             "dropping the old shape would make a second topic each")
            self.assertEqual(bot._topic_icons, {})
            self.assertEqual(bot._topic_names, {})

    # --- configuration ---------------------------------------------------

    async def test_tag_glyphs_are_taken_as_written(self):
        # They go in the title, which is free text, so there is nothing to
        # validate and no reason to reach for the sticker set.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory, glyphs={})
            with patch.object(config, "tag_icons",
                              return_value={"dino": "\U0001f996"}), \
                 patch.object(config, "default_topic_icon", return_value=""):
                await bot._load_icons()
            self.assertEqual(bot._icon_emoji, {"dino": "\U0001f996"},
                             "an emoji outside the forum set is fine in a title")

    async def test_the_default_icon_is_checked_against_the_forum_set(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory, glyphs={}, default_icon="")
            with patch.object(config, "tag_icons", return_value={}), \
                 patch.object(config, "default_topic_icon",
                              return_value="\U0001f4c1"):
                await bot._load_icons()
            self.assertEqual(bot._default_icon, "5001")

    async def test_a_default_icon_outside_the_forum_set_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory, glyphs={}, default_icon="")
            with patch.object(config, "tag_icons", return_value={}), \
                 patch.object(config, "default_topic_icon",
                              return_value="\U0001f996"):
                await bot._load_icons()
            self.assertEqual(bot._default_icon, "",
                             "sending it would be rejected, and clearing is worse")

    async def test_an_icon_notice_is_left_alone(self):
        # The bot used to delete this notice about 60ms after causing it.
        # Topic icons intermittently failed to reach clients and that notice
        # is the only durable record of the change, so it stays now.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_id": 7, "message_thread_id": 42,
                "forum_topic_edited": {"icon_custom_emoji_id": "5001"}}})
            # `delete_message` is gone from the client entirely, so a bot
            # that still tried to sweep would raise here rather than assert.
            self.assertEqual(bot.telegram.messages, [],
                             "the notice is evidence for clients, not litter")

    # --- reconciling ------------------------------------------------------

    async def test_reconciling_migrates_a_topic_once_and_then_goes_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)

            class _Daemon:
                async def sessions(inner):
                    return [{"session_id": "work", "name": "the work session",
                             "tags": ["urgent"]}]

            bot.daemon = _Daemon()
            with patch.object(bot_module, "RECONCILE_PACE", 0):
                await bot._reconcile_topics()
                self.assertEqual(bot.telegram.renamed,
                                 [(42, "\u2757\ufe0f the work session")])
                self.assertEqual(bot.telegram.icons, [(42, "5009")])
                await bot._reconcile_topics()
            self.assertEqual(len(bot.telegram.renamed), 1,
                             "a second pass has nothing left to do")
            self.assertEqual(len(bot.telegram.icons), 1)

    async def test_a_topic_needing_only_one_call_pauses_only_once(self):
        # The bug this replaced paused per topic, so two calls shared one
        # pause and the forum saw thirty messages a minute against a budget
        # of twenty. Telegram throttled by holding requests for forty seconds.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)
            bot._topic_icons["work"] = "5009"          # icon already right
            session = {"session_id": "work", "name": "work", "tags": []}

            class _Daemon:
                async def sessions(inner):
                    return [dict(session)]

            bot.daemon = _Daemon()
            naps = []

            async def _sleep(seconds):
                naps.append(seconds)

            with patch.object(bot_module.asyncio, "sleep", _sleep):
                await bot._reconcile_topics()
            self.assertEqual(bot.telegram.renamed, [(42, "work")])
            self.assertEqual(getattr(bot.telegram, "icons", []), [])
            self.assertEqual(len(naps), 1, "one call, one pause")

    async def test_one_session_gets_one_topic_under_concurrency(self):
        # Reconciling runs alongside the event loop, so a session_added and a
        # reconcile pass can reach _ensure_topic for the same session at once.
        # The check that it has no topic is separated from the create by an
        # await, and the loser of that race is an unreachable second topic.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            session = {"session_id": "work", "name": "work", "tags": []}
            created = await asyncio.gather(
                bot._ensure_topic(dict(session)),
                bot._ensure_topic(dict(session)),
                bot._ensure_topic(dict(session)),
            )
            self.assertEqual(len(bot.telegram.topics), 1,
                             "three callers, one createForumTopic")
            self.assertEqual(set(created), {bot._topics["work"]},
                             "every caller gets the thread that was made")

    async def test_a_slow_reconcile_does_not_hold_up_the_loops(self):
        # Awaiting reconcile before the loops start left the bot deaf for the
        # length of a paced migration: no Telegram polling, no daemon events.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)

            class _Daemon:
                async def sessions(inner):
                    return [{"session_id": "work", "name": "work", "tags": []}]

            bot.daemon = _Daemon()
            started = asyncio.Event()

            async def _slow(seconds):
                started.set()
                # Never resolves, and does not re-enter the patched sleep.
                await asyncio.get_running_loop().create_future()

            with patch.object(bot_module.asyncio, "sleep", _slow):
                task = asyncio.create_task(bot._reconcile_topics_guarded())
                # If this returns, the paced reconcile is off the hot path.
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertFalse(task.done(), "still mid-pace, as intended")
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_a_failing_reconcile_is_logged_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)

            class _Daemon:
                async def sessions(inner):
                    raise ApiError("the daemon is not there")

            bot.daemon = _Daemon()
            with self.assertLogs("falconfox.telegram", level="WARNING") as caught:
                await bot._reconcile_topics_guarded()
            self.assertTrue(any("reconciliation failed" in line
                                for line in caught.output))

    async def test_reconciling_pauses_after_calls_that_fire_not_topics_seen(self):
        # The forum is one group sharing one message budget across every topic
        # in it, and migration wants two calls per session at once. But once
        # it agrees with the daemon this loop makes no calls, and pacing the
        # iteration would put dead time into every restart forever.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("work", 42)

            class _Daemon:
                async def sessions(inner):
                    return [{"session_id": "work", "name": "work", "tags": []}]

            bot.daemon = _Daemon()
            naps = []

            async def _sleep(seconds):
                naps.append(seconds)

            with patch.object(bot_module.asyncio, "sleep", _sleep):
                await bot._reconcile_topics()
                self.assertEqual(naps, [bot_module.RECONCILE_PACE] * 2,
                                 "a rename and an icon edit are two messages, "
                                 "so they are two pauses")
                await bot._reconcile_topics()
            self.assertEqual(len(naps), 2, "nothing fired, so nothing waited")


class QueueAndStopTests(unittest.IsolatedAsyncioTestCase):
    """A mid-turn message is kept, and a turn can be ended from the chat.

    The rule under test throughout: a queue drains when a turn *ends*. /stop
    flushes nothing itself, it only ends the turn.
    """

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._bind("session", 20)
        bot._turns["session"] = Turn("session", Dest(-1001, 20), working=True)
        self.sent = []
        self.cancelled = []

        class FakeWebSocket:
            async def send(inner, payload):
                self.sent.append(json.loads(payload))

        class FakeDaemon:
            async def cancel(inner, session_id):
                self.cancelled.append(session_id)

        bot._ws = FakeWebSocket()
        bot.daemon = FakeDaemon()
        return bot

    def _update(self, text, thread=20, message_id=1):
        return {"message": {"chat": {"id": -1001}, "text": text,
                            "message_thread_id": thread, "message_id": message_id,
                            "from": {"id": 7}}}

    async def _idle(self, bot):
        await bot._handle_event({"type": "agent_state", "session_id": "session",
                                 "state": "idle"})

    async def test_a_mid_turn_message_is_kept_and_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("more context", message_id=11))
            self.assertEqual(self.sent, [], "nothing reaches the daemon mid-turn")
            self.assertEqual([item["text"] for item in bot._queues["session"]],
                             ["more context"])
            self.assertEqual(bot.telegram.messages, [(20, QUEUED_FIRST)])
            self.assertEqual(bot.telegram.message_replies, [11],
                             "the acknowledgement threads to the message it kept")

    async def test_several_messages_become_one_prompt_in_order(self):
        # Consecutive messages on a phone are one thought split by the send
        # button, so they are joined rather than run as separate turns.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("first", message_id=11))
            await bot._handle_update(self._update("second", message_id=12))
            self.assertEqual(len(bot.telegram.messages), 1,
                             "the ways out are said once, not once per message")
            self.assertEqual(bot._progress_header(bot._turns["session"]),
                             f"{PROGRESS_HEADER} (0s) · 📥 2 queued",
                             "the depth says they were kept, at no message cost")
            await self._idle(bot)
            self.assertEqual([item["text"] for item in self.sent],
                             ["first\n\nsecond"])

    async def test_the_queue_drains_when_the_turn_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("after you", message_id=11))
            self.assertEqual(self.sent, [])
            await self._idle(bot)
            self.assertEqual([item["text"] for item in self.sent], ["after you"])
            self.assertNotIn("session", bot._queues)
            self.assertEqual(bot._turns["session"].prompt_msg, 11,
                             "the new turn answers the last queued message")

    async def test_stop_ends_the_turn_and_does_not_flush_by_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("after you", message_id=11))
            await bot._handle_update(self._update("/stop", message_id=12))
            self.assertEqual(self.cancelled, ["session"])
            self.assertEqual(self.sent, [],
                             "the flush waits for the turn to actually end")
            self.assertIn("session", bot._queues)
            # The daemon ends the turn in its own time; that is what flushes.
            await self._idle(bot)
            self.assertEqual([item["text"] for item in self.sent], ["after you"])

    async def test_unqueue_drops_the_queue_and_leaves_the_turn_running(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("never mind", message_id=11))
            await bot._handle_update(self._update("/unqueue", message_id=12))
            self.assertEqual(self.cancelled, [])
            self.assertNotIn("session", bot._queues)
            self.assertIn("session", bot._turns)
            await self._idle(bot)
            self.assertEqual(self.sent, [])

    async def test_fullstop_does_both_in_one_call(self):
        # Its whole reason to exist: /stop then /unqueue races the flush.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("never mind", message_id=11))
            await bot._handle_update(self._update("/fullstop", message_id=12))
            self.assertEqual(self.cancelled, ["session"])
            await self._idle(bot)
            self.assertEqual(self.sent, [], "nothing queued survives a /fullstop")

    async def test_stop_says_so_when_no_turn_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._turns.pop("session")
            await bot._handle_update(self._update("/stop", message_id=12))
            self.assertEqual(self.cancelled, [],
                             "claiming to stop nothing teaches distrust")
            self.assertIn("No turn is running", bot.telegram.messages[0][1])

    async def test_unqueue_says_so_when_nothing_is_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/unqueue", message_id=12))
            self.assertIn("Nothing was queued", bot.telegram.messages[0][1])

    async def test_the_queue_survives_a_bot_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("after you", message_id=11))
            record = json.loads(Path(directory, "turns.json").read_text())["session"]
            self.assertEqual([item["text"] for item in record["queued"]],
                             ["after you"])
            restarted = self._bot(directory)
            restarted._turns.clear()
            restarted._adopt_turn("session", record)
            self.assertEqual([item["text"] for item in restarted._queues["session"]],
                             ["after you"])


class TurnFeedbackTests(unittest.IsolatedAsyncioTestCase):
    """What a turn tells the chat about itself, now that the reactions on the
    user's own message are gone and the progress message carries all of it."""

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._bind("session", 20)
        self.cancelled = []

        class FakeWebSocket:
            async def send(inner, payload):
                pass

        class FakeDaemon:
            async def cancel(inner, session_id):
                self.cancelled.append(session_id)

        bot._ws = FakeWebSocket()
        bot.daemon = FakeDaemon()
        return bot

    def _update(self, text, message_id=1):
        return {"message": {"chat": {"id": -1001}, "text": text,
                            "message_thread_id": 20, "message_id": message_id,
                            "from": {"id": 7}}}

    async def _run_turn(self, bot, *, outcome="completed", stop=None, deliver=True):
        await bot._handle_event({"type": "turn_started", "session_id": "session",
                                 "turn_id": "t1"})
        if deliver:
            await bot._handle_event({"type": "message", "session_id": "session",
                                     "role": "agent", "text": "the answer"})
        await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                 "turn_id": "t1", "outcome": outcome,
                                 "stop_reason": stop})

    def _stamp(self, bot):
        """The progress message as it was left standing."""
        return bot.telegram.edits[-1][2]

    async def test_a_turn_opens_with_a_progress_message_and_closes_stamped(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("do the thing", message_id=11))
            self.assertEqual(bot.telegram.messages, [(20, f"{PROGRESS_HEADER} (0s)")],
                             "the turn announces itself in one message, silently")
            self.assertEqual(bot.telegram.message_silent, [True])
            await self._run_turn(bot)
            self.assertIn("✅ Turn finished", self._stamp(bot))
            self.assertEqual(bot.telegram.html_messages[0][2], "the answer")
            self.assertEqual(bot.telegram.html_replies[0], 11,
                             "the answer threads to the message that asked")

    async def test_a_cancelled_turn_says_so_in_the_progress_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("never mind", message_id=11))
            await self._run_turn(bot, stop="cancelled", deliver=False)
            self.assertIn("✖️ Turn cancelled", self._stamp(bot))

    async def test_an_errored_turn_says_so_in_the_progress_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("do the thing", message_id=11))
            await self._run_turn(bot, outcome="error", deliver=False)
            self.assertIn("⚠️ Turn ended with an error", self._stamp(bot))

    async def test_a_turn_that_delivered_nothing_says_so_in_words(self):
        # The silent-turn case: it ends "successfully" with nothing to show,
        # which is the failure this client kept producing invisibly. A 😱 on
        # the prompt used to carry it; now only the notice does, so the notice
        # is the whole of the evidence.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("do the thing", message_id=11))
            await self._run_turn(bot, deliver=False)
            self.assertIn("without delivering a reply", bot.telegram.messages[-1][1])
            self.assertEqual(bot.telegram.message_replies[-1], 11)

    async def test_the_header_counts_what_is_queued_behind_the_turn(self):
        # The one thing the reactions did that no message did: say that the
        # second and third message of a mid-turn burst were kept. The first
        # explains the ways out in words; the rest are a count in the header.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("start", message_id=10))
            self.assertEqual(bot._progress_header(bot._turns["session"]),
                             f"{PROGRESS_HEADER} (0s)")
            await bot._handle_update(self._update("and also", message_id=11))
            await bot._handle_update(self._update("and this", message_id=12))
            self.assertEqual(bot._progress_header(bot._turns["session"]),
                             f"{PROGRESS_HEADER} (0s) · 📥 2 queued")
            self.assertEqual([text for _, text in bot.telegram.messages[1:]],
                             [QUEUED_FIRST],
                             "the ways out are said once, not once per message")
            # Flushed, so the count goes with them.
            await bot._handle_event({"type": "turn_ended", "session_id": "session",
                                     "turn_id": "t1", "outcome": "completed"})
            # The flush started a turn of its own, and it carries no queue.
            self.assertEqual(bot._progress_header(bot._turns["session"]),
                             f"{PROGRESS_HEADER} (0s)")

    async def test_unqueueing_reports_the_count_it_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("start", message_id=10))
            await bot._handle_update(self._update("and also", message_id=11))
            await bot._handle_update(self._update("and this", message_id=12))
            await bot._handle_update(self._update("/unqueue", message_id=13))
            self.assertIn("Dropped 2 queued message(s)",
                          bot.telegram.messages[-1][1])
            self.assertEqual(bot._queues.get("session", []), [])


class TagCommandTests(unittest.IsolatedAsyncioTestCase):
    """Tagging from the topic itself, without going through the manager."""

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._bind("session", 20)
        bot._icon_emoji = {"archived": "📁", "urgent": "❗️"}
        self.tagged = []
        outer = self

        class FakeDaemon:
            tags = ["urgent"]

            async def sessions(inner, include_hidden=False):
                return [{"session_id": "session", "tags": list(inner.tags)}]

            async def tag(inner, session_id, tags):
                outer.tagged.append((session_id, tags))
                inner.tags = list(tags)
                return {"session_id": session_id, "tags": list(tags)}

        bot.daemon = FakeDaemon()
        return bot

    def _update(self, text):
        return {"message": {"chat": {"id": -1001}, "text": text,
                            "message_thread_id": 20, "message_id": 1,
                            "from": {"id": 7}}}

    async def test_a_bare_tag_shows_rather_than_clears(self):
        # Clearing by accident is not recoverable from the chat.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag"))
            self.assertEqual(self.tagged, [])
            self.assertIn("urgent", bot.telegram.messages[0][1])

    async def test_the_report_glyphs_every_tag_that_draws(self):
        # One slot became many, so there is no longer a winner to name.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag urgent archived"))
            body = bot.telegram.messages[0][1]
            self.assertIn("\u2757\ufe0furgent", body)
            self.assertIn("\U0001f4c1archived", body)
            self.assertNotIn("Configured glyphs:", body,
                             "setting tags does not need the whole vocabulary")

    async def test_tags_that_draw_nothing_say_so(self):
        # Silence here reads as "it worked" while the topic quietly keeps
        # the title does not change.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag notes review"))
            body = bot.telegram.messages[0][1]
            self.assertIn("⚠️ No glyphs for these.", body)
            self.assertIn("Send /tag", body, "say what to do about it")

    async def test_the_bare_form_does_not_point_at_itself(self):
        # The vocabulary is already in this message, so pointing at /tag
        # would point at the message it is in.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon.tags = ["notes"]
            await bot._handle_update(self._update("/tag"))
            body = bot.telegram.messages[0][1]
            self.assertIn("⚠️ No glyphs for these.", body)
            self.assertNotIn("Send /tag", body)
            self.assertIn("Configured glyphs:", body)

    async def test_no_tags_at_all_is_not_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag -"))
            self.assertNotIn("⚠️", bot.telegram.messages[0][1])

    async def test_the_vocabulary_is_offered_on_a_bare_tag_only(self):
        # It is the longest part of the message and answers "what can I set?",
        # which is the bare form's question and not the setting form's.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag"))
            body = bot.telegram.messages[0][1]
            self.assertIn("\n📁 archived", body, "one per line, to read down")
            self.assertIn("\n❗️ urgent", body)

    async def test_tags_replace_the_whole_list(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag archived slow"))
            self.assertEqual(self.tagged, [("session", ["archived", "slow"])])

    async def test_a_lone_hyphen_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._update("/tag -"))
            self.assertEqual(self.tagged, [("session", [])])
            self.assertIn("No tags", bot.telegram.messages[0][1])

    async def test_a_refused_tag_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)

            async def _refuse(session_id, tags):
                raise ApiError("tags must not contain whitespace")

            bot.daemon.tag = _refuse
            await bot._handle_update(self._update("/tag 'needs review'"))
            self.assertIn("Could not set tags", bot.telegram.messages[0][1])


class TrayTests(unittest.IsolatedAsyncioTestCase):
    """Inbound files: a chat has no compose step, so they wait in a tray.

    The tray is this client's own state, and the daemon holds the bytes.
    """

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        bot._bind("session", 20)
        self.store = {}
        self.removed = []
        outer = self

        class FakeDaemon:
            async def add_file(inner, session_id, path, name=None):
                file_id = f"f{len(outer.store)}"
                outer.store[file_id] = {"session": session_id, "name": name,
                                        "body": Path(path).read_bytes()}
                return {"file_id": file_id, "name": name,
                        "path": f"/state/{session_id}/inbox/{file_id}/{name}"}

            async def remove_file(inner, session_id, file_id):
                outer.removed.append(file_id)
                outer.store.pop(file_id, None)
                return {"removed": 1}

        bot.daemon = FakeDaemon()
        self.sent = []

        class FakeWebSocket:
            async def send(inner, payload):
                action = json.loads(payload)
                if action.get("action") == "send":
                    outer.sent.append((action["session_id"], action["text"]))

        bot._ws = FakeWebSocket()
        return bot

    @staticmethod
    def _photo(message_id=1, caption=None, size=1000):
        message = {"chat": {"id": -1001}, "message_thread_id": 20,
                   "message_id": message_id, "from": {"id": 7},
                   "photo": [{"file_id": "small", "file_size": 10},
                             {"file_id": "big", "file_size": size}]}
        if caption is not None:
            message["caption"] = caption
        return {"message": message}

    @staticmethod
    def _text(body, message_id=9):
        return {"message": {"chat": {"id": -1001}, "message_thread_id": 20,
                            "message_id": message_id, "from": {"id": 7},
                            "text": body}}

    async def test_a_photo_waits_instead_of_prompting(self):
        # The decision the rest follows from: one message can be about five
        # photos, and an album has nothing marking its last part.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo())
            self.assertEqual(self.sent, [], "a file must not start a turn")
            self.assertEqual(len(bot._trays["session"]), 1)
            self.assertIn("Filed", bot.telegram.html_messages[0][2],
                          "the receipt is the whole acknowledgement now")

    async def test_the_receipt_carries_a_tappable_id_and_says_when(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo())
            rich = bot.telegram.html_messages[0][1]
            self.assertIn("<code>f0</code>", rich, "the id has to be tappable")
            self.assertIn("next message", rich,
                          "a file that waits silently reads as one ignored")

    async def test_a_photo_is_stored_under_a_name_nothing_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.telegram.remote_paths = {"big": "photos/file_12.jpg"}
            await bot._handle_update(self._photo())
            self.assertEqual(self.store["f0"]["name"], "photo.jpg")

    async def test_a_document_keeps_the_name_it_arrived_with(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_thread_id": 20, "message_id": 2,
                "from": {"id": 7},
                "document": {"file_id": "d1", "file_name": "prod-error.log",
                             "file_size": 40}}})
            self.assertEqual(self.store["f0"]["name"], "prod-error.log")

    async def test_the_largest_rendition_of_a_photo_is_the_one_taken(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo())
            self.assertEqual(bot.telegram.described, ["big"])

    async def test_the_next_message_carries_the_tray_and_empties_it(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            await bot._handle_update(self._photo(2, caption="the error"))
            await bot._handle_update(self._text("what do you make of these?"))
            (_session, prompt), = self.sent
            self.assertIn("attached: /state/session/inbox/f0/photo.jpg", prompt)
            self.assertIn("attached: /state/session/inbox/f1/photo.jpg (the error)",
                          prompt)
            self.assertTrue(prompt.endswith("what do you make of these?"),
                            "the files lead, as context for the message")
            self.assertNotIn("session", bot._trays)

    async def test_a_caption_alone_never_prompts(self):
        # The design's one surprise, and it is deliberate: an album's caption
        # rides one of its parts, so this would fire mid-album.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(caption="what is this?"))
            self.assertEqual(self.sent, [])

    async def test_a_command_leaves_the_tray_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo())
            await bot._handle_update(self._text("/id"))
            self.assertEqual(len(bot._trays["session"]), 1)

    async def test_a_file_over_the_download_limit_is_refused_with_the_reason(self):
        # Checked from the message: attempting it would fail slower and say
        # less, and the limit is Telegram's rather than ours.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(size=30 * 1024 * 1024))
            self.assertEqual(self.store, {})
            self.assertIn("20MB", bot.telegram.messages[0][1])
            self.assertEqual(bot.telegram.message_replies[0], 1,
                             "the refusal threads to the file it refused")

    async def test_a_message_carrying_nothing_we_take_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_thread_id": 20, "message_id": 3,
                "from": {"id": 7}, "sticker": {"file_id": "s1"}}})
            self.assertIn("not that", bot.telegram.messages[0][1])
            self.assertEqual(self.store, {})

    async def test_a_topic_service_message_is_still_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update({"message": {
                "chat": {"id": -1001}, "message_thread_id": 20, "message_id": 4,
                "from": {"id": 7}, "forum_topic_created": {"name": "x"}}})
            self.assertEqual(bot.telegram.messages, [])

    async def test_bare_tray_lists_what_is_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1, caption="left knee"))
            await bot._handle_update(self._text("/tray"))
            rich = bot.telegram.html_messages[-1][1]
            self.assertIn("<code>f0</code>", rich)
            self.assertIn("left knee", rich)

    async def test_tray_arguments_remove_rather_than_replace(self):
        # The inversion against /tag, which is why the help line says
        # "remove" plainly.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            await bot._handle_update(self._photo(2))
            await bot._handle_update(self._text("/tray f0"))
            self.assertEqual(self.removed, ["f0"])
            self.assertEqual([item["file_id"] for item in bot._trays["session"]],
                             ["f1"])
            self.assertIn("Removed 1 file(s)", bot.telegram.messages[-1][1])

    async def test_removing_deletes_rather_than_unlists(self):
        # It was never going to reach the agent, so nothing is left to keep.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            await bot._handle_update(self._text("/tray -"))
            self.assertEqual(self.store, {})
            self.assertNotIn("session", bot._trays)

    async def test_an_unknown_id_removes_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            await bot._handle_update(self._text("/tray nope"))
            self.assertEqual(self.removed, [])
            self.assertEqual(len(bot._trays["session"]), 1)
            self.assertIn("Nothing in the tray", bot.telegram.messages[-1][1])

    async def test_an_empty_tray_says_it_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._text("/tray"))
            self.assertIn("empty", bot.telegram.messages[-1][1])

    async def test_the_tray_survives_a_restart(self):
        # It has to: the files are already in the daemon's store, and a tray
        # that forgot them would leave them waiting for nothing.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            revived = self._bot(directory)
            revived._load_tray()
            self.assertEqual([item["file_id"] for item in revived._trays["session"]],
                             ["f0"])

    async def test_a_deleted_session_takes_its_tray(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._handle_update(self._photo(1))
            await bot._handle_event({"type": "session_removed",
                                     "session_id": "session"})
            self.assertNotIn("session", bot._trays)


class SessionListingTests(unittest.IsolatedAsyncioTestCase):
    """/list: what it says, in what order, and what it drops when it cannot
    say all of it."""

    def _bot(self, directory):
        bot = FalconFoxTelegramBot(BotConfig(
            "token", 7, daemon_url=UNREACHABLE_DAEMON, forum_chat_id=-1001,
            state_dir=Path(directory),
        ))
        bot.telegram = FakeTelegram()
        return bot

    class ListDaemon:
        """Sessions in the order the daemon returns them: by creation, which
        is exactly the order /list is not supposed to keep."""

        def __init__(self, sessions):
            self._sessions = sessions

        async def sessions(self, include_hidden=False):
            return list(self._sessions)

    @staticmethod
    def _session(session_id, last_active, name="work", path="/tmp"):
        return {"session_id": session_id, "name": name, "state": "idle",
                "path": path, "last_active": last_active}

    async def test_list_marks_up_the_ids_so_they_can_be_tapped_to_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ListDaemon([self._session("abcd1234", "2026-09-08T10:00:00")])
            await bot._command(Dest(-1001, 42), "/list")
            self.assertEqual(bot.telegram.messages, [],
                             "a plain send would print the markup literally")
            _, html_text, plain = bot.telegram.html_messages[0]
            # The id alone is the code span: a whole-line block would copy the
            # name and the path with it.
            self.assertIn("<code>abcd1234</code> work", html_text)
            self.assertNotIn("<code>", plain)
            self.assertIn("abcd1234", plain)

    async def test_list_escapes_the_names_it_did_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ListDaemon(
                [self._session("abcd1234", "2026-09-08T10:00:00", name="a < b & c")])
            await bot._command(Dest(-1001, 42), "/list")
            html_text = bot.telegram.html_messages[0][1]
            self.assertIn("a &lt; b &amp; c", html_text)

    async def test_list_orders_by_activity_not_by_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ListDaemon([
                self._session("oldest01", "2026-09-01T10:00:00", name="stale"),
                self._session("newest01", "2026-09-08T10:00:00", name="live"),
                self._session("middle01", "2026-09-05T10:00:00", name="warm"),
            ])
            await bot._command(Dest(-1001, 42), "/list")
            plain = bot.telegram.html_messages[0][2]
            self.assertEqual([line.split()[0] for line in plain.splitlines()],
                             ["newest01", "middle01", "oldest01"])

    async def test_list_without_an_activity_stamp_sorts_last_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            stamped = self._session("stamped1", "2026-09-08T10:00:00")
            unstamped = self._session("unknown1", None)
            del unstamped["last_active"]
            bot.daemon = self.ListDaemon([unstamped, stamped])
            await bot._command(Dest(-1001, 42), "/list")
            plain = bot.telegram.html_messages[0][2]
            self.assertEqual([line.split()[0] for line in plain.splitlines()],
                             ["stamped1", "unknown1"])

    async def test_a_long_list_drops_whole_sessions_and_says_how_many(self):
        # Telegram counts a message's rendered length, so the budget is the
        # plain text's. Cutting to fit it mid-entry would split a tag and the
        # whole message would be rejected.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ListDaemon([
                self._session(f"sess{index:04d}",
                              f"2026-09-08T10:{59 - index // 60:02d}:00",
                              path="/home/ariel/projects/" + "x" * 60)
                for index in range(200)
            ])
            await bot._command(Dest(-1001, 42), "/list")
            _, html_text, plain = bot.telegram.html_messages[0]
            self.assertLessEqual(len(plain), 4096)
            lines = plain.splitlines()
            self.assertRegex(lines[-1], r"^…and \d+ more")
            self.assertEqual(len(lines) - 1 + int(lines[-1].split()[1]), 200)
            # Every kept line is whole: the tags survived the cut.
            self.assertEqual(html_text.count("<code>"), html_text.count("</code>"))
            self.assertEqual(html_text.count("<code>"), len(lines) - 1)

    async def test_list_says_so_when_there_is_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ListDaemon([])
            await bot._command(Dest(-1001, 42), "/list")
            self.assertEqual(bot.telegram.html_messages[0][2], "No sessions.")


class VersionTests(unittest.TestCase):
    """Which answer wins when the build-time stamp and git disagree."""

    def setUp(self):
        get_version.cache_clear()
        self.addCleanup(get_version.cache_clear)

    def test_live_git_beats_a_stale_stamp(self):
        # The editable-install case: the stamp is written once and the source
        # moves afterwards, which is what installing that way is for.
        with patch("falconfox._git_commit", return_value="abc1234"), \
                patch("falconfox._git_dirty", return_value=False), \
                patch("falconfox._baked_version", return_value="0.1.0-old0000-dirty"):
            self.assertEqual(get_version(), "0.1.0-abc1234")

    def test_the_stamp_is_used_when_there_is_no_repository(self):
        with patch("falconfox._git_commit", return_value=None), \
                patch("falconfox._baked_version", return_value="0.1.0-abc1234"):
            self.assertEqual(get_version(), "0.1.0-abc1234")

    def test_neither_leaves_the_bare_version(self):
        with patch("falconfox._git_commit", return_value=None), \
                patch("falconfox._baked_version", return_value=None):
            self.assertEqual(get_version(), falconfox_version)


class UploadKindTests(unittest.TestCase):
    """Displayed in the chat, or exactly as it is."""

    def test_images_and_video_are_sent_to_be_seen(self):
        self.assertEqual(_upload_kind(Path("shot.png"), False), ("sendPhoto", "photo"))
        self.assertEqual(_upload_kind(Path("a.jpg"), False), ("sendPhoto", "photo"))
        self.assertEqual(_upload_kind(Path("loop.gif"), False),
                         ("sendAnimation", "animation"))
        self.assertEqual(_upload_kind(Path("clip.mp4"), False), ("sendVideo", "video"))

    def test_anything_else_is_sent_as_it_is(self):
        self.assertEqual(_upload_kind(Path("notes.txt"), False),
                         ("sendDocument", "document"))
        self.assertEqual(_upload_kind(Path("archive.tar.gz"), False),
                         ("sendDocument", "document"))

    def test_raw_overrides_the_type(self):
        # Telegram re-encodes photos, which is invisible on a photograph and
        # very visible on a screenshot of text.
        self.assertEqual(_upload_kind(Path("shot.png"), True),
                         ("sendDocument", "document"))

    def test_an_image_past_the_photo_limit_is_sent_as_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            big = Path(directory, "big.png")
            big.write_bytes(b"0" * (PHOTO_LIMIT_BYTES + 1))
            self.assertEqual(_upload_kind(big, False), ("sendDocument", "document"))


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
                async def session(self, session_id, include_transcript=False):
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

    class ClearDaemon:
        """Records the delete and hands back a new session on spawn."""

        def __init__(self):
            self.deleted = []
            self.spawned = []

        async def delete(self, session_id):
            self.deleted.append(session_id)

        async def spawn(self, **kwargs):
            self.spawned.append(kwargs)
            return {"session_id": f"new{len(self.spawned)}"}

        async def session(self, session_id, include_transcript=False):
            raise ApiError("no such session")

    async def test_a_hidden_session_never_gets_a_topic(self):
        # The bug this fixes: /clear spawned a manager, the session_added event
        # beat the spawn's HTTP response, and the id it was compared against
        # was still the old one -- so every clear left a "telegram manager"
        # topic that nothing owned.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = None
            thread = await bot._ensure_topic(
                {"session_id": "new00001", "name": "telegram manager", "hidden": True})
            self.assertIsNone(thread)
            self.assertEqual(getattr(bot.telegram, "topics", []), [])
            self.assertEqual(bot._topics, {})

    async def test_a_visible_session_still_gets_one(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            thread = await bot._ensure_topic(
                {"session_id": "abcd1234", "name": "work", "hidden": False})
            self.assertIsNotNone(thread)
            self.assertEqual(bot._topics, {"abcd1234": thread})

    async def test_clear_replaces_the_manager_session(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ClearDaemon()
            bot.manager_session_id = "mgr00001"

            await bot._command(Dest(-1001, None), "/clear")

            self.assertEqual(bot.daemon.deleted, ["mgr00001"])
            self.assertEqual(bot.manager_session_id, "new1")
            # Hidden, so the replacement does not appear as a work session.
            self.assertTrue(bot.daemon.spawned[0]["hidden"])
            self.assertIn("this is a new session (new1)", bot.telegram.messages[0][1])

    async def test_clear_replaces_the_private_chat_session(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ClearDaemon()
            bot._bot_username = "a_bot"
            bot.concierge_session_id = "con00001"

            await bot._command(Dest(7, None), "/clear")

            self.assertEqual(bot.daemon.deleted, ["con00001"])
            self.assertEqual(bot.concierge_session_id, "new1")

    async def test_clear_is_refused_in_a_work_session_topic(self):
        # A work session's conversation is the work: clearing one from the
        # chat would be a delete with a gentler name.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ClearDaemon()
            bot._bind("abcd1234", 42)

            await bot._command(Dest(-1001, 42), "/clear")

            self.assertEqual(bot.daemon.deleted, [])
            self.assertIn("only for General", bot.telegram.messages[0][1])

    async def test_the_new_id_is_remembered_across_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.daemon = self.ClearDaemon()
            bot.manager_session_id = "mgr00001"
            await bot._command(Dest(-1001, None), "/clear")

            restarted = self._bot(directory)
            restarted._load_infra()
            self.assertEqual(restarted.manager_session_id, "new1")

    async def test_help_lists_every_command_that_is_dispatched(self):
        # The list and the dispatcher are separate, so this reads the command
        # literals back out of `_command` itself: a command added without a
        # line in COMMANDS is invisible, which is the whole failure mode of
        # keeping help by hand.
        source = inspect.getsource(FalconFoxTelegramBot._command)
        dispatched = set(re.findall(r'command (?:==|in \(?)\s*"(/[a-z]+)"', source))
        dispatched |= set(re.findall(r'"(/[a-z]+)"', source.split("if command", 1)[1]))
        listed = {usage.split()[0] for usage, _, _ in COMMANDS}
        self.assertTrue(dispatched, "found no commands to check against")
        self.assertEqual(dispatched - listed, set())

    async def test_help_is_html_but_leaves_the_commands_bare(self):
        # HTML now, for the bold section headers. Telegram still parses a bare
        # /command into a tappable entity inside HTML -- checked against the
        # live API -- but a <code> or <pre> span would swallow it, so the
        # usages must stay unmarked.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._command(Dest(-1001, None), "/help")
            self.assertEqual(bot.telegram.messages, [])
            _, html_text, plain = bot.telegram.html_messages[0]
            self.assertIn("<b>Management</b>", html_text)
            self.assertIn("<i>For specific sessions.</i>", html_text)
            self.assertIn("/sh &lt;command&gt;", html_text)
            self.assertNotIn("<pre>", html_text)
            for line in html_text.splitlines():
                if line.startswith("/"):
                    self.assertNotIn("<code>" + line[:2], line)
            self.assertIn("/sh <command>", plain)
            self.assertNotIn("<b>", plain)

    async def test_help_is_one_text_wherever_it_is_asked_for(self):
        # The whole vocabulary, in every chat. A per-chat /help made a command
        # appear only where you had already thought to look for it.
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            for dest in (Dest(-1001, 42), Dest(-1001, None), Dest(7, None)):
                await bot._command(dest, "/help")
            said = {message[1] for message in bot.telegram.html_messages}
            self.assertEqual(len(said), 1)
            listed = {line.split()[0] for line in said.pop().splitlines()
                      if line.startswith("/")}
            self.assertEqual(listed, {usage.split()[0] for usage, _, _ in COMMANDS})

    async def test_help_keeps_each_command_under_its_own_section(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._command(Dest(-1001, 42), "/help")
            plain = bot.telegram.html_messages[0][2]
            section, seen = None, {}
            for line in plain.splitlines():
                if line in dict(SECTIONS):
                    section = line
                elif line.startswith("/"):
                    seen[line.split()[0]] = section
            self.assertEqual(seen["/help"], None, "the preamble has no section")
            self.assertEqual(seen["/clear"], "Session")
            self.assertEqual(seen["/kill"], "Execution")
            self.assertEqual(seen["/new"], "Management")
            # Section order is the reading order, not COMMANDS order.
            self.assertLess(plain.index("Management"), plain.index("Session"))
            self.assertLess(plain.index("Session"), plain.index("Execution"))

    async def test_id_answers_with_the_topic_s_session(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot._bind("abcd1234", 42)
            bot._topic_names["abcd1234"] = "the work session"
            await bot._command(Dest(-1001, 42), "/id")
            _, html_text, plain = bot.telegram.html_messages[0]
            # Inline, not a block: the id is tap-to-copy on its own while the
            # name it belongs to stays ordinary text on the same line.
            self.assertIn("<code>abcd1234</code>", html_text)
            self.assertNotIn("<pre>", html_text)
            self.assertIn("the work session", plain)
            self.assertIn("abcd1234", plain)

    async def test_id_in_general_answers_with_the_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            bot.manager_session_id = "mgr00001"
            await bot._command(Dest(-1001, None), "/id")
            self.assertIn("<code>mgr00001</code>", bot.telegram.html_messages[0][1])

    async def test_id_says_so_when_nothing_owns_the_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._command(Dest(-1001, 99), "/id")
            self.assertIn("No FalconFox session owns", bot.telegram.messages[0][1])

    async def test_output_comes_back_as_a_code_block(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._say_block(Dest(-1001, None), "header", "a < b & c")
            _, html_text, plain = bot.telegram.html_messages[0]
            self.assertIn("<pre>a &lt; b &amp; c</pre>", html_text)
            self.assertEqual(plain, "header\na < b & c")

    def test_the_tail_budget_counts_escaped_length(self):
        # Raw length would fit and the escaped message would then be rejected
        # by Telegram, which is how output full of markup loses the whole
        # message rather than its head.
        body, clipped = tail("<" * 50, 40, measure=lambda value: len(value) * 4)
        self.assertTrue(clipped)
        self.assertEqual(len(body), 10)

    async def test_bare_sh_explains_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self._bot(directory)
            await bot._command(Dest(-1001, None), "/sh   ")
            self.assertEqual(bot._shell.calls, [])
            self.assertIn("Usage: /sh", bot.telegram.messages[0][1])


class InlineCodeTests(unittest.TestCase):
    """The backtick-to-<code> conversion the help descriptions rely on."""

    def test_a_backticked_run_becomes_a_code_span(self):
        self.assertEqual(_inline_code("show or set tags (`-` clears)"),
                         "show or set tags (<code>-</code> clears)")

    def test_markup_in_the_text_is_escaped_not_trusted(self):
        self.assertEqual(_inline_code("/name <name>"), "/name &lt;name&gt;")

    def test_an_unbalanced_backtick_is_left_as_it_is(self):
        # Guessing where the span ends would swallow the rest of the line,
        # which is a worse outcome than one visible stray character.
        self.assertEqual(_inline_code("a ` b"), "a ` b")


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
