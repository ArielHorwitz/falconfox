# Package follow-up: the daemon core review fixes

Implementer's report on the three review findings, same Opus 5 session resumed, 2026-09-13. Three commits stacked on the package, then rebased onto the case branch without conflict.

All three fixes landed and the branch is rebased onto `hardening-survey`. Worktree clean, `305 passed in 5.34s`.

## The three fixes

**`9a17280` stop keeps a session that a restart would have found.** The test restores a session from disk and stops it without ever reading it; a second test stops a live auto-named session twice. Both deleted the directory and transcript before the fix. Rather than repeat `_persist_meta`'s clause, "is this session worth keeping on disk" became one answer, `record.keeps_state`: not ephemeral, and either already written, or named, or holding a message. The transcript is consulted last and only to earn an answer not yet earned, since it is the one input a stop drops and a restart starts empty. `_persist_meta`, `_emit` and `_stop_locked` all go through it. A third test pins the other half of the rule: a session that was never named and never spoke is still deleted by stop.

**`d0c7d42` the interrupted-turn notice is owed until it is said.** Test: load an interrupted session, resume, stop, send, and the notice is in that prompt exactly once, ahead of the user's words. It is now derived from the marker when the prompt is built, the way orientation is, so nothing that lives only in `pending_context` can be dropped by `release`. `OpenTurn.interrupted` marks a turn inherited from a previous process; the prompt that carries the news clears the flag but leaves the marker for `turn_started` to replace, so a crash in between owes it again.

**`5009acc` revert gives the session up the way a stop does.** `release()` instead of the hand-rolled teardown. The existing F2 test is the guard and now also asserts nothing the live agent owned is left behind. One intended consequence: a queued session being reverted loses the message it was queued with, which was addressed to the conversation being rewound.

## The rebase

`git rebase hardening-survey` applied all ten commits with **no conflicts**. The two packages turned out to be genuinely disjoint: their 250 changed lines in `tests/test_falconfox_poc.py` are all inside the Telegram test classes, and I checked their side never references `SessionCoordinator`, `make_record`, `_records`, `rename_session`, `set_tags` or `bus.subscribe`, so the surface I changed is untouched by them. The one shared region, the import block, merged both sides cleanly (their `Turn`, my `SessionRecord`/`EventBus`/`create_app`).

One thing I could not do: fold a cosmetic import reordering into its original commit, since `git rebase -i` is unavailable here. I dropped the change rather than leave a `fixup!` commit or add a noise commit. Nothing pushed, nothing merged.