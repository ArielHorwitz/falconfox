# Backends

A **backend** is the agent casebook launches for a session. Casebook is
vendor-agnostic: a backend is just *a command to run* (plus optional environment)
that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP)
over stdio. Any ACP-speaking agent works; casebook knows nothing about which
vendor or model is behind it.

> **Just want it running?** Jump to [Quick setup](#quick-setup) for copy-paste
> config — start with [Claude](#claude).

## How a backend runs

When you start a session on a backend, casebook:

1. launches the backend's `command` as a subprocess, with the **project root** as
   its working directory;
2. gives it the **full inherited environment**, overlaid with the backend's `env`;
3. speaks ACP to it over stdin/stdout (initialize → new session → prompts).

Because the agent is your own trusted tool, it gets your real environment (PATH,
ambient credentials, etc.), not a trimmed one.

## Schema

Each backend is a table under `[backends.<name>]`. `<name>` is what you'll see in
the backend picker and can set as `default_backend`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `command` | array of strings | yes | The program and its arguments, e.g. `["claude-agent-acp"]` or `["gemini", "--experimental-acp"]`. The first element is resolved on `PATH`. |
| `env` | table of strings | no | Extra environment variables for the subprocess, overlaid on the inherited environment. Use it for an API key, a model override, or a gateway URL. |
| `[backends.<name>.config_options]` | table | no | Default values for the backend's ACP config options (model, reasoning effort, mode, …), applied at session start. Keys are option **ids** and values are choice **values** the backend advertises — read the exact ones from the session-options (⚙) popover. See [Session options](#session-options). |

```toml
[backends.example]
command = ["my-acp-agent", "--flag", "value"]
env = { MY_API_KEY = "sk-...", MY_REGION = "eu" }

[backends.example.config_options]
model = "best-model-3"
reasoning_effort = "high"
```

## The built-in backend

**`echo`** is the only built-in backend — a tiny in-tree ACP agent
(`python -m casebook.echo_backend`) that reflects your messages back. It's always
available, so the app runs with zero setup, but it has no language model (see
[Naming](#naming)).

Every other backend, Claude included, is one you declare under `[backends.*]` —
see [Quick setup](#quick-setup). Declaring a `[backends.echo]` overrides the
built-in one.

## Choosing the default

`default_backend` selects the backend new sessions use unless you pick another in
the UI. If you don't set it, casebook uses the **first backend you declare**, or
`echo` if you declared none.

```toml
default_backend = "claude"
```

## Session options

Backends advertise **config options** over ACP — the model, reasoning effort,
approval mode, feature toggles, whatever that agent exposes. Casebook treats them
all the same: a running session shows them in the **session-options (⚙) popover**,
and changes are applied over ACP. The model is just one of these options, not a
special case.

**Setting defaults.** Give a backend a `[backends.<name>.config_options]` table to
apply values at session start:

```toml
[backends.example.config_options]
model = "opus"
reasoning_effort = "high"
```

The **keys are option ids** and the **values are choice values** — both defined by
the backend, not by casebook. Don't guess them: open the ⚙ popover on a running
session and each row shows a copy-ready snippet (e.g. `reasoning_effort = "high"`)
for exactly what to paste here. Set the option in the UI to see the value you want,
then copy the line.

If a default can't be applied — the backend advertises no such option id, or the
value matches none of its choices — casebook emits a notice saying so (and lists
what's available), rather than silently dropping it.

Casebook cannot offer an option a backend doesn't expose, and some backends
advertise only **coarse model buckets** rather than exact ids (see
[Claude](#claude)). For finer control, define **separate backends**, each launched
with that backend's own selection flags or env — the vendor-specific value lives in
your config, and casebook stays agnostic:

```toml
[backends.assistant-fast]
command = ["some-acp-agent", "--model", "<fast model the agent understands>"]

[backends.assistant-deep]
command = ["some-acp-agent", "--model", "<deep model the agent understands>"]
```

Then pick the backend you want from the picker (or set `default_backend`).

## Naming

The "name session" button (✨) runs a short one-shot query on the backend named by
the top-level `naming_backend` key. **It's required for the feature**: if
`naming_backend` is unset, session naming is disabled (there is no fallback to the
session's own backend). The naming backend's model — and any other option — comes
from its own `[backends.<naming_backend>.config_options]`, so point it at a small,
cheap model:

```toml
naming_backend = "namer"

[backends.namer]
command = ["some-acp-agent"]
[backends.namer.config_options]
model = "<a small, fast model>"
```

(The built-in `echo` backend only reflects your prompt back, so it can't produce
useful names.)

## Verifying

Start `casebook` and open a case: configured backends appear in the
**+ session** backend picker. If a backend fails to launch, the failure surfaces
as a toast/notice (e.g. a wrong command or missing binary).

## Quick setup

Copy-paste recipes for common backends. Each block goes in your config file
(`~/.config/casebook/config.toml`, or the per-project `.casebook/config.toml`).

### Claude

The `claude` backend is the [`claude-agent-acp`](https://www.npmjs.com/package/@agentclientprotocol/claude-agent-acp)
adapter — a wrapper around the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
that speaks ACP.

**Authenticate first.** The adapter signs in through Claude Code, so install
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview) and log in
(run `claude` once and follow the prompt) before using this backend — then no
separate API key is needed. Alternatively, put an `ANTHROPIC_API_KEY` in `env`
(see below).

**Get going with `npx`** — fetched and cached on first use, nothing to install:

```toml
[backends.claude]
command = ["npx", "-y", "@agentclientprotocol/claude-agent-acp"]
```

**Or install it and call the binary directly** — no npx overhead on each launch.
Install it, then point `command` at `claude-agent-acp` (the `[backends.claude]`
block is still required):

```bash
npm install -g @agentclientprotocol/claude-agent-acp
# add `sudo` if `npm prefix -g` is root-owned (e.g. /usr), or install without
# root: npm install -g --prefix ~/.local @agentclientprotocol/claude-agent-acp
```

```toml
[backends.claude]
command = ["claude-agent-acp"]
```

**Pick a model.** The adapter exposes coarse buckets — `default`, `sonnet`,
`opus`, `haiku` — not exact ids, with `opus` (the latest Opus) as the default. Set
the initial one (and any other advertised option, e.g. reasoning effort) under
`config_options`:

```toml
[backends.claude]
command = ["claude-agent-acp"]
[backends.claude.config_options]
model = "opus"
```

To pin an **exact** model id instead of a bucket, set `ANTHROPIC_MODEL` (a
`--model` argument on the command line is ignored in ACP mode):

```toml
[backends.claude]
command = ["claude-agent-acp"]
env = { ANTHROPIC_MODEL = "claude-opus-4-8" }
```

The adapter honors the same environment variables as the `claude` CLI —
`ANTHROPIC_API_KEY` (use a key instead of your login), `ANTHROPIC_BASE_URL` (route
through a proxy/gateway), and more. Full list: the Claude Code
[environment variables reference](https://docs.claude.com/en/docs/claude-code/settings#environment-variables).
Put any of them in `env`.

### Codex

OpenAI's Codex via the [`codex-acp`](https://www.npmjs.com/package/@agentclientprotocol/codex-acp)
adapter. Install `codex` and `codex-acp`, then authenticate once (`codex login`
for ChatGPT sign-in, or `printenv OPENAI_API_KEY | codex login --with-api-key`) —
until then a session errors with "Authentication required".

```toml
[backends.codex]
command = ["codex-acp"]
```

Codex advertises its **model**, **reasoning effort**, and approval **mode** as
session options (visible in the ⚙ popover). Set defaults with:

```toml
[backends.codex.config_options]
model = "gpt-5.5"
reasoning_effort = "high"
mode = "read-only"
```

### Gemini

Google's Gemini CLI in its experimental ACP mode:

```toml
[backends.gemini]
command = ["gemini", "--experimental-acp"]
env = { GEMINI_API_KEY = "..." }

[backends.gemini.config_options]
model = "gemini-2.5-pro"
```

### Any other ACP agent

Point `command` at the program and whatever flags put it in ACP mode:

```toml
[backends.custom]
command = ["/opt/agents/my-agent", "serve", "--acp"]
env = { MY_AGENT_TOKEN = "..." }
```
