# Overview

What FalconFox tells a session about itself grew by accretion and is now in
three places, delivered two ways, with no rule about which is which. This case
settles a structure: **orientation is composed from named pieces, all of it
delivered through the prompt channel, and each client owns the text that
describes it.**

Designed in discussion and built the same day. Merged to `dev` on
2026-09-09 and **proven in use from the phone**; closed 2026-09-17. What was
built is recorded at the end, including the three things the design did not
anticipate.

Everything between here and that section is the reasoning as it was settled,
written before the code existed. It is kept in the present tense it was
written in.

## Where it stood before this case

Three texts exist, and a session gets at most two of them.

1. **`SESSION_CONTEXT`** (`src/falconfox/config.py`). Six terse bullets.
   Queued into `_pending_context` at spawn and prepended to the **first**
   prompt the session ever receives, split by `=== the user's message follows
   ===`. Never repeated, invisible to the user. Reaches *every* session.
2. **Manager orientation** (`_prepare_manager_workspace` in
   `src/falconfox_telegram/bot.py`). Written into both `AGENTS.md` and
   `CLAUDE.md` in the manager's working directory, and read by the agent's own
   runtime. Reaches the General topic's session only.
3. **Concierge orientation** (`_prepare_concierge_workspace`, same file, same
   mechanism). Reaches the private chat session only.

A fourth path exists but is not orientation: `_context_prompt` in
`coordinator.py` *replaces* the pending context with the prior transcript when
a session resumes on a backend with no native session loading.

**A work session therefore receives `SESSION_CONTEXT` and nothing else.**
That is the entire surface for teaching it anything.

## The problem, concretely

Tags are the worked example. `falconfox tag` is explained at length in the
*manager* orientation — the replace-not-append trap, and why tag order decides
which topic icon is drawn. `SESSION_CONTEXT` does not mention tags at all. So
a work session does not know that its topic icon comes from its own tags, that
`/tags` works in its topic, or that it can tag itself. The session best placed
to keep its own label current is the one told nothing about labels.

The same hole would swallow the attachment tray, which is the work that
triggered this case: it is work sessions that receive files, and work sessions
read `SESSION_CONTEXT` only.

Second problem, in the manager text: it says "forum" and "topic" without ever
defining them, because the definitions belong to a client and there was
nowhere to put them.

## The design

### Four pieces

1. **Session** (global). What a FalconFox session is: a daemon speaking ACP,
   not a terminal, many sessions at once, turns, `falconfox attach`.
2. **Client** (one per client). What that client's surface is and what follows
   from it: for Telegram, forums, topics, tap-to-copy, the constrained
   typing that makes copyable ids matter, and the commands it offers.
3. **Manager** (role, owned by the daemon). Owning the session lifecycle.
   This is a *builtin* session type, not a Telegram one: managing the daemon's
   sessions through an agent is useful to every client, and the orientation
   for it has no client-specific detail in it. Telegram merely has a natural
   place to put it — the always-on General topic.
4. **Concierge** (role, owned by the Telegram client). Getting the user set
   up. This one genuinely is client-specific: it exists because Telegram
   requires a private chat before a forum can be reached at all.

### One delivery channel

All of it goes through the prompt channel that landed in `8b9c6fb` ("tell a
session what it is running inside", 2026-09-04). **The `AGENTS.md` /
`CLAUDE.md` workspace mechanism is removed.**

Writing both filenames only ever existed because FalconFox cannot know which
runtime will read them. A prompt needs no such guess, so this is *more*
portable, not less. It also collapses two mechanisms into one and treats work
sessions and infrastructure sessions the same way.

### The daemon composes, not the client

A session may be started in one client and resumed from another, so **every**
client's orientation must always be present. A client cannot know that at
spawn time, so the daemon holds all of them and composes.

This reverses an earlier suggestion in the discussion that each client pass
its own text at spawn. The multi-client argument defeats it.

### Roles are named at spawn, and they compose

The daemon cannot tell that a session is the manager. Spawn grows roles, and
they **persist in `meta.toml`**, or a resume and a `/clear` will not recompose
the same orientation.

**Roles are a set, not a choice.** An earlier draft had one mutually exclusive
`--role manager|concierge`, which bakes in an assumption the daemon has no
business making: that a client-specific role cannot compose with the manager
role, or with another client's role. Nothing about the manager conflicts with
being something else as well. So a session carries zero or more roles and gets
the orientation for each.

This also fixes where the two roles live. The **manager** is the daemon's own,
so the daemon carries its text. The **concierge** belongs to Telegram, so
Telegram carries it, by the same registration route as the client
orientation.

### The manager text becomes client-agnostic

It should describe what is constant — the daemon, the CLI, the lifecycle — and
*refer* to the client for the rest. Not "use `/id`", but closer to "every
session knows its own id from the environment, but the client usually has a
cheaper way to hand you one". The client orientation carries the specifics,
and the manager is a session too, so it receives both and the reference
resolves.

### The concierge text stays as it is

It mixes daemon and Telegram detail deliberately and that duplication is
correct: it is the channel that has to work *before* a forum exists. It is the
most special session in the deployment — more so than the manager, which is
better understood as a default session that happens to be used often.

## Tradeoffs accepted

**Orientation changes do not reach existing sessions.** Accepted, and not
globally solvable. `/clear` exists precisely so a new session can pick up new
orientation, including for infrastructure sessions.

**A prompt prefix cannot be re-read after compaction; a file could.** Accepted.
It bites the manager most, whose job description is long and consulted
repeatedly. A future command that simply re-sends orientation would answer it.
Not a present concern: sessions are rotated frequently enough that compaction
has not yet caused trouble.

**Token cost.** Judged negligible, so orientation may be thorough as well as
concise. Where something is better explained by running a CLI command, it
*may* be left to the CLI, but that is an option rather than a rule.

## How clients supply their orientation: a file, not a connection

The daemon composes, and clients hand it their text by **writing it to a
shared runtime directory when they initialise**. The daemon reads what is
there. No protocol action, no connection involved.

The alternative considered was registering over the websocket the Telegram bot
already holds. It was rejected for making orientation depend on process start
order: a session spawned before a client had connected would silently get an
incomplete orientation. That is not a hypothetical about today's single
client so much as the shape of a bug that appears rarely and reads as
inexplicable when it does — and a feature to enable and disable clients, which
is plausible, would make it ordinary. A file removes the ordering question
instead of managing it.

Secondary virtues: it survives a client restart, it survives transient
connection trouble, and it can be read with `cat` when something looks wrong.

**The daemon reads at spawn, from `XDG_RUNTIME_DIR`.** Both halves were argued
the other way first and both were changed by the same fact.

Reading once at daemon startup is simpler and has the appealing property that
the files cannot shift under a long-running process. It does not work here:
`falconfox-telegram` is `After=falconfox-daemon`, correctly, since the bot is
a client that connects *to* the daemon. So the daemon always starts first, and
on a tmpfs that is empty every boot it would read nothing and describe no
client at all until someone restarted it by hand. Pairing startup reads with a
durable directory fixes that but leaves the daemon permanently one restart
behind whatever the client last wrote.

Reading per spawn costs a directory listing and removes both problems.

With per-spawn reads, tmpfs beats a durable directory on **accuracy**: the
orientation then describes the clients that are actually running, so a session
spawned while the bot is down is not told it has a `/tray` and topics. A
durable directory would keep describing a stopped client indefinitely, and
wrong information is worse than missing information here, because an agent
acts on an affordance it is told it has. The tmpfs failure mode is also the
milder one — a spawn in the gap between daemon start and client start, which
self-corrects on the next spawn and is close to unreachable for a
Telegram-originated spawn, since that requires the bot to be up already.

**The directory is per daemon run**, and that is what removes staleness
entirely rather than managing it.

This was first dismissed as circular — clients would have to know the run id
before the daemon creates it — which is wrong: the daemon *publishes* the
path. `server.json` already exists for exactly this kind of discovery, written
at daemon startup with pid, port and start time, with read helpers in
`state.py`. So the daemon creates
`$XDG_RUNTIME_DIR/falconfox/run-<pid>/clients/`, names it in `server.json`,
and clients write there.

A new run is a new directory, so a client that has been removed or disabled
leaves its file behind in a directory nothing reads again. No periodic
cleanup, no clients rewriting on a timer, no question of whether a file is
current. Old run directories are a few kilobytes of text on tmpfs and go on
reboot; the daemon may also remove sibling run directories at startup, which
is safe because those runs are over.

**This requires clients to write on every connect**, not only at their own
startup. Otherwise a daemon restart strands a still-running client's
orientation in the previous run's directory. The Telegram bot already
reconnects, so it is a write on a path that exists.

The case for this over hardcoding daemon-side is not mainly decoupling. It is
that the manager and concierge texts **already** live in `bot.py`, next to the
commands they describe. Hardcoding client text daemon-side would move them
away from that code, which is a regression from the current state rather than
a neutral choice.

## Resume: the orientation was being lost

`_pending_context` is a single slot holding what gets prepended to the next
prompt. At spawn it holds the orientation. On resume, when the backend cannot
load a session natively and a transcript exists, `_context_prompt` **replaces**
it with the prior conversation.

The comment justifying that replacement says the transcript "contains it
already if it was ever delivered". It does not. `send` passes `display_text`
so orientation stays out of the visible transcript, and `_transcript_text`
reads exactly those visible messages. Orientation is delivered once, never
recorded, and then replaced. The daemon's own notice — "Context re-sent from
saved transcript imperfectly" — has fired twice on the dev instance, so this
is a path that runs.

It matters more after this case than before it. Today the manager and
concierge read their orientation from `AGENTS.md`, which a resume cannot lose,
because the runtime reads the file again. Removing the file mechanism removes
that. A resumed manager would lose its entire job description rather than six
generic bullets, and silently.

**Decided.**

1. **Pending context becomes a list**, delivered as an array of content
   blocks. Producers add rather than overwrite, so no future producer can
   silently displace another.
2. **Orientation goes into the transcript.** It is text the session receives
   in the user's voice, and everything a session sees belongs in its
   transcript. That makes the resume path correct for free: re-sending the
   transcript now genuinely does carry the orientation.
3. **Orientation is not re-sent on every resume.** Cheap, but a session may be
   stopped and resumed between every message, which would make it constant
   noise.

**The marker already exists and is unused.** `AgentSession.send` accepts a
`system` flag and stamps it onto the message event, and nothing in the
codebase ever passes it. The web UI already renders such events as a distinct
bubble; `_transcript_text` already skips them. Both are dead code guarding
against events nothing produces.

So orientation adopts `system: true` rather than inventing a marker, and
`_transcript_text` stops excluding it — an exclusion that was harmless while
nothing produced these events and would be exactly backwards once orientation
does.

Worth being clear that `system` is **ours, not ACP's**. ACP's `Role` enum is
`user` and `assistant` only, and `PromptRequest` carries no role or system
marker: a prompt simply is the user turn. The flag governs how our own clients
display an event and how we rebuild transcripts, and changes nothing about
what the agent receives. Clients hide it for now; showing it is a later
choice, not a constraint.

## Client orientation and client roles are different things

They arrive by the same route and are delivered on opposite terms, so the
distinction is worth stating plainly.

**A client orientation is unconditional.** Every session gets every client's,
always, because a session may be started in one client and spoken to through
another later. It describes a surface: what a forum and a topic are, that ids
are worth making tappable because typing on a phone is expensive, what the
commands do.

**A role orientation is conditional.** Only a session carrying that role gets
it. It describes a job: owning the session lifecycle, or walking a user
through setup.

The two also have different owners. **`manager` is the daemon's own role** —
managing the daemon's sessions through an agent is useful to every client, and
its text has no client-specific detail in it. **`concierge` is Telegram's**,
and exists only because Telegram requires a private chat before a forum can be
reached. A client registers its orientation and its roles in the same file.

### Roles are namespaced, and the namespace comes from the directory

A second client wanting its own "concierge" must not collide with Telegram's.
Rather than detect that and error, the layout makes it impossible.

A client writes a directory named after itself:

```
$XDG_RUNTIME_DIR/falconfox/run-<pid>/clients/
    telegram/
        orientation.md
        roles/
            concierge.md
```

The daemon derives the namespace from the path, so Telegram's role is
`telegram.concierge` and a second client's is `newclient.concierge`. The
daemon's own roles take the empty namespace and are written `.manager`. Spawn
accepts them as they read: `--role telegram.concierge`.

Deriving rather than declaring is what makes this structural. There is no
prefix field a client could get wrong, no arbitration for the daemon to
perform, and no rule to enforce beyond the layout itself — a client cannot
shadow `.manager`, because it has nowhere to write it. The only requirement
left is that client names are unique, which they must be anyway.

It also separates the two kinds of text cleanly, which a single file would
have had to encode some other way: `orientation.md` is the unconditional
piece, everything under `roles/` is conditional on a session carrying it.

Two details for the build. Client files should be written to a temporary name
and renamed into place, since the daemon reads per spawn and could otherwise
catch a partial write. And client orientations need a deterministic order in
the composed text — by directory name, for want of any better reason.

## Help: the other half of orientation

Orientation is what a session cannot work without, told once and unasked. It
is the wrong shape for detail that only sometimes matters, and the worked
example is the Telegram commands: a session cannot run `/help`, because the
bot handles those before anything reaches a session, so an agent invited to
answer questions about commands had no way to know what they were.

`falconfox help <topic>` reads nested markdown registered in the same per-run
directory as orientation, so registering both is one write:

```
<run>/help/<name>.md                    ->  .<name>
<run>/clients/<client>/help/<name>.md   ->  <client>.<name>
<run>/clients/<client>/help/<a>/<b>.md  ->  <client>.<a>.<b>
```

The empty namespace is the daemon's own, exactly as it is for roles, so
`.lifecycle` reads like `.manager`. Bare `falconfox help` lists everything,
one dotted path per line with its title.

**Titles come from the first heading**, not from frontmatter. A metadata block
is a second place for a document's name to live, and the two drift; the
heading a reader already sees cannot.

**The listing is generated by the daemon**, not written by a client. It is the
one part that spans every namespace: a client knows what it registered and
nothing about anyone else's, while the daemon sees all of them and its own. It
is generated per spawn and composed into the global piece, so it describes
what is actually registered.

**Listing and lookup answer different questions.** The index walks files, so a
directory with no `.md` of its own contributes nothing: writing only
`commands/new.md` lists `telegram.commands.new` and no parent. Lookup is where
the two can collide, and there a module wins over its own children --
`falconfox help telegram.commands` prints the document and appends a pointer
to what is below it, rather than listing children and hiding the document
behind them. With no document, lookup falls through to that listing.

**Help text is written by hand, not generated from docstrings.** An earlier
draft would have split the dispatcher into one method per command and lifted
their docstrings. Rejected: a docstring serves the code reader, and the two
audiences pull the text in different directions. The `/help` one-liners are
already hand-written prose, so this is consistent rather than a compromise.
What guards it is a test that every command in `COMMANDS` appears somewhere in
the help text -- not that each has a file of its own, since one `commands.md`
covers them all until something earns its own page.

One consequence accepted: `server.json` is removed when the daemon stops, so
`falconfox help` needs the daemon running, which for an agent inside a session
is true by construction. Every other shortfall answers as "no help found" --
a daemon that publishes no directory is a daemon with no help, and naming a
narrower reason for it would be a special case that stops being true shortly
and says less than the general one everywhere else.

## Implementation plan

In dependency order. Steps 1 and 2 are independent of each other.

1. **Content blocks.** `AgentSession.send` takes a list of blocks instead of
   one string. `_pending_context` becomes a list per session, appended to and
   never overwritten. Orientation is emitted as its own message event with
   `role: user` and `system: true`, and `_transcript_text` stops excluding
   system events.
2. **Registration.** The daemon creates
   `$XDG_RUNTIME_DIR/falconfox/run-<pid>/clients/` at startup, publishes the
   path in `server.json`, and removes sibling run directories. It reads that
   directory per spawn. The Telegram bot writes its file at startup and on
   every connect.
3. **Composition.** Spawn grows roles as a set, persisted in `meta.toml`.
   Orientation is global + every client orientation present + the text for
   each of the session's roles.
4. **The texts.** Today's `SESSION_CONTEXT` becomes the global piece. A new
   Telegram client piece covers forums, topics, tap-to-copy, the commands,
   tags, and the photo re-encoding note. The manager piece is rewritten
   client-agnostic and moves into the daemon. The concierge piece moves as it
   stands into Telegram's registration file.
5. **Remove the file mechanism.** `_prepare_workspace` and its two callers go.
   The workspaces remain as working directories.
6. **Resume.** `_context_prompt` appends rather than replaces. Orientation
   rides the transcript, so nothing extra is sent on resume.
7. **Help.** `falconfox/help.py` walks the tree, `falconfox help [topic]`
   reads it, the Telegram client registers `help/commands.md` beside its
   orientation, and the daemon composes the index into the global piece.
8. **Tests, README, and the wishlist entry** for content blocks, which is
   narrowed rather than deleted: this case takes the array, not the typed
   blocks or the capability negotiation.

Two consequences to expect. Existing sessions keep their old orientation until
`/clear`, which is accepted. And `system: true`, dead until now, goes live and
changes how the web UI renders those events.

## What was built

All eight steps, on `dev`, in two commits plus follow-ups: `b81b1c4` for
orientation and `c051b0a` for help.

- `src/falconfox/help.py` is new. `falconfox help [module]` walks the tree,
  prints a module, or lists a branch.
- `PromptPart` in `engine/session.py` carries `text`, `system` and `record`.
  `AgentSession.send` takes a sequence of them and prompts with one block
  each. `display_text` is gone, along with the concatenation it served.
- `state.py` grew `run_dir`, `clients_dir` and `prepare_clients_dir`, and
  `server.json` publishes the path.
- The coordinator composes: `_orientation`, `_orientation_parts`,
  `_role_orientation`, `_client_registrations`, `_help_index`.
- `config.py` holds the global piece and `MANAGER_ORIENTATION` under
  `ROLE_ORIENTATIONS`. The Telegram client holds `CLIENT_ORIENTATION`,
  `CONCIERGE_ORIENTATION` and `COMMANDS_HELP`, and writes all three at
  registration.
- `_prepare_workspace` and its two callers are gone; no `AGENTS.md` or
  `CLAUDE.md` is written anywhere.
- `spawn` takes repeatable `--role`, and roles persist in `meta.toml`.

Names have moved since: the hardening case
([2026-09-12__07b4eb32](../2026-09-12__07b4eb32/overview.md)) folded per-session
dictionaries into a `SessionRecord`, so `_pending_context[session_id]` is now
`record.pending_context`. The design is unchanged -- producers still append,
and a transcript replay is still the one part not recorded.

## Three things the design did not anticipate

**A piece that ends mid-line collides with the next one.** Blocks reach a
backend as an array and are joined by it. The generated help index ended the
global piece without a trailing newline, so a live session read
`telegram.commands  Telegram commands# Talking through Telegram`. Fixed by
normalising at composition rather than asking each author, since registered
files are read stripped and their authors are other people's clients
(`fcce64a`).

**A listing is not an invitation.** `falconfox help telegram` printed bare
module paths, which reads as output rather than as something to call again.
Every listing now leads with how to open an entry (`769a84e`). The reader is
usually an agent deciding whether a second turn is worth spending.

**`system: true` was already there, and dead.** The flag existed on the
message event, the web UI already styled it, `_transcript_text` already
skipped it, and nothing had ever set it. Orientation adopted it rather than
inventing a marker, and the skip -- harmless while nothing produced these
events -- became exactly backwards and was removed.

## Proven in use

The user tested from the phone on the day it merged, and the strongest check
was behavioural rather than recall: **tag a session that already has a tag**.
Only orientation says a session may tag itself and that the call replaces the
whole list, so an unoriented session silently drops the existing tag. It
carried it forward.

Also confirmed live: `falconfox help` and `falconfox help telegram.commands`
from a session, and a work session answering a question about stopping a turn
by reaching for the help module -- the whole chain, since a session cannot run
`/help` itself.

## Related work

- **`ed379c4`** wishlists using more than one prompt content block. **Pulled
  into this case**: orientation is a list of pieces, and a list of pieces
  wants a list of blocks. Concatenating them into one `text_block` would work,
  but the whole difficulty below is what happens when one producer overwrites
  another's single slot, and an array is what stops that being possible.
- **`_context_prompt` replaces pending context on resume.** Settled below; it
  turned out to be a live fault rather than a future risk.
- The **attachment tray** was paused pending this case. It became its own
  case ([2026-09-09__33198985](../2026-09-09__33198985/overview.md)), which is
  built and closed; its wishlist entry went with it.
- **Wishlisted afterwards**: delivering orientation as *system instructions*
  rather than as user-voice content blocks. The `system` flag this case made
  live is ours and not ACP's, so it hides the text from clients without
  changing what the agent receives. The successor wants a lever on the ACP
  surface itself, which is an upstream ask rather than a local change.
