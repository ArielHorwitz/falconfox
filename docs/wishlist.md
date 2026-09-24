# Wishlist

Wanted but not built. This file exists so that closing a case does not lose the
work it deliberately pushed forward — an item here has been **decided against
doing now**, with the reason, rather than forgotten.

Keep entries short and honest about status. When something is picked up, delete
the entry; the reason it was wanted belongs in whatever case takes it on. See
[buglist.md](buglist.md) for things that are broken rather than missing.

## Notice when Telegram rate-limits us

*From the turn-feedback simplification, 2026-09-12.*

`api.py` has no 429 handling. A "Too Many Requests" is just an `ApiError`, and
the callers that matter swallow it: a failed progress edit is logged at
`debug` and retried on the next tick, a failed chat action at `debug` too. The
bot could sit against a limit indefinitely and say nothing anywhere.

That matters more than it looks, because the budget is genuinely unknown.
Telegram documents three limits and all three are about *sending* — one
message a second per chat, 20 a minute to the same group, ~30 a second overall
— and says nothing about whether `editMessageText` or `sendChatAction` count
against them, or whether a forum's topics share one group allowance. Checked
at the source, 2026-09-12. The progress message is edited every few seconds
per live turn, so this is exactly the thing that would find the limit first.

Half of this landed on 2026-09-12 for a different reason: `_json_request` now
logs any call over 10 seconds at WARNING, so a call that is being held rather
than answered leaves a trace. What is still missing is the 429 itself —
parsing `retry_after`, naming the method that hit it, and honouring the
backoff. A week of logs checked on 2026-09-12 held one 429, on `getUpdates`,
none on edits, which is weak evidence that current volumes are nowhere near a
ceiling and no evidence about where the ceiling is.

See [2026-09-12__c716ca46](casebook/2026-09-12__c716ca46/overview.md), which
chose a cadence on how it reads rather than on a number it could not verify.

## Desktop client — delete the dead web UI soon, maybe repair it later

*From the falconfox pivot case, 2026-08-24. Reversed from the phone,
2026-09-09.*

The assets under `src/falconfox/web/static/` were the desktop client, the
counterpart to Telegram as the mobile client. Flattening the session model
(session keyed by id, carrying its path) broke their case- and
project-centric navigation, and nothing has maintained them since.

The pivot case kept them rather than deleting them, reasoning that a working
UI is substrate worth re-earning. That is now reversed. Dead assets in the
tree are a standing tax: a session that reads them takes them for a live
client and for a description of how the system is shaped, and neither is
true. Saying so in [AGENTS.md](../.agents/agents.md) is a patch over the real
fix, which is to delete them, and to do it soon rather than eventually. Git
history keeps them recoverable, so a repair stays possible: pull the files back
and rewire them onto whatever the session model looks like by then. That is
probably the cheapest route to a UI, and it does not need them sitting in the
tree in the meantime.

Removal touches the `static/` directory, the `index` route and the `/static`
mount in `web/server.py`, and `--browser` with its `webbrowser` call in
`cli.py`. It does **not** touch `web/server.py` otherwise: that module is the
daemon's HTTP and websocket API, which every client and the CLI depend on. It
only happens to also mount the static assets.

A desktop client is still wanted, and deleting these files is not a decision
against one. Telegram is *enough* to dogfood falconfox while developing it —
the standard the pivot set for itself and met — so a browser UI stays deferred
on its own merits rather than because there is a stale draft of it lying
around.

## Voice input

*From the falconfox pivot case, 2026-08-24.*

The original motivation: hands-free agent work on long commutes. Architecturally
it is a transcribe step in front of the existing forward path — orthogonal to
the daemon, which is why it was safe to defer at every step while the novel
parts were built.

Wanted, and not a blocker. Text first was the right order; voice is now its own
effort rather than an unfinished corner of the pivot.

## Choose the model when spawning a session

*From the phone, 2026-08-25.*

`falconfox spawn` takes `--path`, `--name`, `--backend` and `--ephemeral` — but
not a model. Today the only way to run a session on a different model is to
declare a **second backend** in `config.toml` with its own `env` or
`config_options`, then `spawn --backend <name>`. That works (verified with
`ANTHROPIC_MODEL=claude-fable-5`), but it means every model is a config edit
plus a daemon config reload, and the choice is baked into a backend name rather
than made per session.

Wanted: `falconfox spawn --model <id>`, so a session can be started on a
different model without touching config — most usefully from the focus chat,
which is where sessions actually get spawned.

The design constraint is the case's own layer boundary: the daemon knows
nothing about models, and shouldn't start. The model is a **backend concern**,
already expressed two ways per backend (`env` for vendor-specific selection,
`config_options` for ACP-advertised options). A `--model` flag would have to
resolve to one of those rather than becoming a daemon-level concept — likely by
setting the ACP `model` config option at session start, with the env-var route
staying the escape hatch for values a backend does not advertise.

## Make use of Telegram message streaming

*From the phone, 2026-08-28.*

Bot API 9.3 (2025-12-31) added `sendMessageDraft`, "allowing partial messages
to be streamed to a user while being generated" — a message that fills in as
it is produced, rather than one that is sent whole or edited in place. It
takes `can_stop` / `keep_on_stop`, so the user can halt a generation from the
chat.

Wanted; **how is deliberately open**. Everything the bot shows today is built
from whole messages — a progress message created up front and edited as work
happens, a reply sent once the turn ends. Streaming is a different primitive
underneath both of those, and it postdates the design that chose them, so the
right question is not "where do we bolt this on" but "what would the turn look
like if this had existed". Whether it carries the reply, replaces the progress
message, does both, or neither, is exactly what has not been decided.

Worth reading the turn-feedback case
([2026-08-24__165f0606](casebook/2026-08-24__165f0606/overview.md)) first:
it settled the two-message turn against the constraints of whole messages,
and it records why each of those choices was made — which is what tells you
whether streaming actually improves on them or just moves them.

## Let the private-chat session actually diagnose, not just advise

*From the forum rework, 2026-08-30. Still open after the orientation rewrite,
2026-09-08.*

The private chat cannot **look**. There is no Telegram surface anywhere it can
reach: the `falconfox` CLI reports sessions and nothing about chats, so the
session cannot answer "is my forum working?" - the single most likely question
in the channel that exists for when the forum is not. The bot already has
`check_forum`, which reports which of the three conditions failed. The session
simply cannot call it.

Observed doing real damage: asked exactly that, it invented a probe (spawning a
session and telling the user to look for its topic), got a false negative from
an `--ephemeral` session that was never going to produce one, and sent the user
hunting through a group that was fine.

What the orientation rewrite changed is permission, not capability. It may now
run commands other than `falconfox`, so it *could* read the bot token out of
`~/.config/falconfox/telegram.env` and call the Bot API by hand. That is a
workaround available to a determined agent, not a surface, and nothing points
it there.

Two shapes, not exclusive:

- **A surface it can call** - a `/check` command, or `falconfox` growing a
  Telegram-side report. Small, and it is the part that removes the guessing.
- **Richer instructions** covering what it may run into: common failure modes,
  what each looks like, what to do about them.

**Deferred on purpose, and the reason is the second one.** Troubleshooting text
is mostly *descriptions of current state*, the category that goes stale
fastest, and stale instructions are not merely useless but actively harmful,
since the agent finds them and follows them. Pick it up when the shape has
settled. The surface half can land earlier and independently, since it adds a
capability rather than a description, so it does not rot.

## Tell a session when its turn was interrupted

*From the session-context discussion, 2026-09-04.*

A turn killed mid-flight by a daemon restart or an eviction leaves no trace
the agent can see. Its next turn opens on the user's next message as if
nothing happened, so it cannot tell whether the work it was doing finished,
half-finished, or never started, and it will often assert one of those
confidently.

FalconFox knows what the session cannot: it had a turn in flight when it
stopped, and roughly how far in. The fix is to say so on the next send, in the
same hidden-context channel that already re-sends a transcript to a backend
without native resume.

It was deferred behind the FalconFox session context, which needed the same
channel and landed first (2026-09-07). That reason is spent: the channel
exists, and this is a second producer for it.

## Say something when an infrastructure session is tagged

*From the /help environments case, 2026-09-08.*

`/tag` acts on whichever session speaks in the chat it is typed in, so in
General it tags the **session manager** and in the private chat it tags the
concierge. Both are accepted in full: the tags are stored, reported back, and
then never drawn, because a tag is rendered into a *topic title* and neither
of those sessions has a topic. Nothing is broken and nothing says so either.

It reaches there three ways — the user typing `/tag` in General, `falconfox
tag` naming an infrastructure session, and an agent tagging itself — and the
last is the one that matters, since an agent that gets a success back has no
way to learn that the label went nowhere.

Wanted: one decision, applied to all three. Either refuse the tag with the
reason, or keep accepting it and say plainly that nothing will draw it. Not
done now because it is a question about what tags *mean* on a session with no
topic, and answering it in passing while splitting `/help` would have been
guessing.

## Typed prompt content blocks, and the producers still concatenating

*From the inbound attachments case, 2026-09-08. The array half was built by the
orientation case ([2026-09-09__1071993a](casebook/2026-09-09__1071993a/overview.md)),
2026-09-09.*

An ACP prompt is an **array** of content blocks and FalconFox sent exactly one,
so everything that was not the user's own words got concatenated into the same
string they typed, and read to the agent as though the user had said it.

**The array is built.** Orientation composes as a list of pieces, each its own
`PromptPart` and its own block, and producers append to that list rather than
overwriting a single slot. Two things remain that an array on its own does not
answer.

**Typed blocks beyond text.** `acp.schema` carries `ImageContentBlock`,
`ResourceLink`, `EmbeddedResourceContentBlock` and `AudioContentBlock`, and the
initialize response carries `promptCapabilities` saying which of them a backend
accepts. `_spawn` reads only `load_session` off that object and drops the rest,
so there is capability negotiation to write, with a text fallback for every
backend that says no. The motivating case is an attached image arriving as an
`ImageContentBlock` the model sees directly, rather than a path it must open.

**The producers still gluing text together.** Orientation was moved
deliberately; these were not, and each is a string where structure would do:
the interrupted-turn notice, the `attached: <path>` lines the tray adds, and
the re-sent transcript, which is a whole prior conversation flattened into one
block. The tray composes its lines in the client rather than the daemon
([2026-09-09__33198985](casebook/2026-09-09__33198985/overview.md)), so its
half of this lands when the `send` action grows an array a client can fill.

## Telegram forum desync reconciliation

*From the phone, 2026-09-11. Recorded as stated, not worked out.*

Reconcile the forum against the sessions it is meant to reflect: fix topic
names and icons, handle deleted topics, and the rest of what drifts.

## Deliver orientation as system instructions, not as user-voice context

*From the phone, 2026-09-11.*

Orientation reaches a session as ordinary prompt content blocks. `PromptPart`
carries a `system` flag, but the flag is ours rather than ACP's, which has no
notion of a system turn: it tells clients to hide the text, and the agent
receives it in the user's voice like anything else.

Wanted: a better mechanism than that. Ideally a set-or-append system
instructions lever on the ACP surface itself, so what a session is told about
itself arrives as what it is instead of as something the user appears to have
said.

ACP is not ours, so the likely shape is a protocol proposal first, with
whatever a given backend already offers as the interim.

## Show the full narration of every turn, not just the last

*From the phone, 2026-09-11.*

Wanted: rework the progress message so it shows the full messages of all
turns rather than only the most recent one.

Which of two things this is aimed at is not settled, and it may be both.
`PROGRESS_LIMIT` caps a progress message at 3500 characters by trimming its
oldest lines, and `THOUGHT_PREVIEW_CHARS` cuts each thought to its opening 280
— so a talkative turn loses its own beginning and the detail of its thinking.
Separately, a progress message belongs to one turn: `_forward` makes a new one
per turn and leaves the old one standing, so earlier turns survive only as
however far up the chat they have scrolled, never gathered anywhere.

## Deliberately not planned

**Off-loopback remote access + bearer token.** Listed in the pivot case as the
daemon's feature 2 and "the genuinely new capability", then made unnecessary by
the architecture: the Telegram bot is co-located with the daemon, so the daemon
binds `127.0.0.1` and Telegram *is* the remote access. Exposing it would add an
auth surface nothing needs. Recorded here so it is not re-raised as an
oversight — it was deleted by the design, not skipped.
