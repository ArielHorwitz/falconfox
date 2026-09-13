# Package follow-up: the Telegram client review fixes

Implementer's report on the three review findings, same Opus 5 session resumed, 2026-09-13. Three commits stacked on the package.

All three fixes landed, worktree clean, suite green: **280 passed in 5.18s** (277 before these three, 5 new tests).

## Commits

| commit | what |
| --- | --- |
| `f84920f` | `fix: settling the persisted turn map is per entry, not all or nothing` |
| `0e6ff2c` | `fix: a recovered reply recreates a dead topic like a live one` |
| `47acc50` | `fix: a refused reply is waited out, and then said out loud` |

## What each test proves

**1.** Three persisted turns, the middle one's topic rate-limited: the first and third are still recovered and delivered, the failure is logged at WARNING naming `two`, and `turns.json` ends empty, so a second reconnect on the same state dir sends nothing and re-aborts nowhere. Before the fix the second entry's `ApiError` escaped the loop and the assertion never ran.

**2.** A persisted turn whose bound thread answers "message thread not found" ends with exactly one new topic, the recovered reply in it, `TOPIC_REPLACED` then `RECOVERED_TURN` in that order, and `topics.json` rebound. `_send_reply` and `_deliver_recovered_turn` now share `_send_rendered`, so there is one path from agent text to the chat rather than two that drifted.

**3.** A reply refused twice then accepted: three attempts, one message delivered, no silent-turn notice. A reply always refused: no raise out of `_finish_turn`, a WARNING carrying the session and the 10 characters lost, and one chat notice reading "Telegram refused the 10-character reply 3 times". Plus a pure-function test that `retry after 2` is honoured, `retry after 30` is capped, and the hintless schedule is 1s then 3s.

## Decisions inside the fixes worth your eye

- **The retry lives in `_send_for_turn`, per send.** So a multi-part reply can never redeliver a part, and the recovery path gets the same treatment. The dead-topic branch does not consume attempts.
- **`SEND_PAUSE_CEILING = 5.0`, three attempts.** `_finish_turn` runs on the `_receive_events` task, so every second waiting is a second in which no session's events are handled. Honouring a bare `retry after 30` would stall the whole connection; the notice is the better answer. Worst case is ~10s of pause. This is the one place I read the brief's "honour `retry_after`" more narrowly than written, and the reason is in the constant's comment.
- **The silent-turn notice is now best-effort.** Without that, a 429 taking both the reply and the notice would still raise out of `_finish_turn` and skip the queue flush, which is what the requirement "does not raise" is actually protecting.
- **`turn.undelivered` is a new `Turn` field**, so the notice can say a reply was written and refused rather than reusing `output_chars` and reporting "the agent produced no output".

## Where I disagree with the reviewer

Nowhere on the findings — all three were real, and I confirmed each by watching the test fail first. Two notes on framing rather than substance:

- Finding 3 says "the old code eventually redelivered it via reconnect". Only by way of the storm F1 removed: the redelivery happened because the exception faked a daemon outage, and it went to the same destination that had just refused it. It was not a recovery path, it was the bug. The fix is worth doing on its own merits, which is why I did it.
- Finding 3 is filed against `_finish_turn`, and the brief follows it. I put the retry one level down in `_send_for_turn` instead, because at `_finish_turn`'s level the buffer is already drained and a retry there would resend the wrong text or duplicate parts.

One thing left deliberately: after a topic is replaced, the redelivery into the new topic is a single attempt, not a retried one. Retrying there would need care against creating a third topic, and the failure is now reported rather than silent.