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
| `default_model` | string | no | Preferred model, applied at session start when the backend advertises a match (loose match on model id or name). See [Models](#models). |

```toml
[backends.example]
command = ["my-acp-agent", "--flag", "value"]
default_model = "best-model-3"
env = { MY_API_KEY = "sk-...", MY_REGION = "eu" }
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

## Models

Once a session is running, the model dropdown lists exactly the models the backend
**advertises over ACP**; switching models uses ACP under the hood.

Each backend can set a `default_model` — a preferred model applied at session
start, matched case-insensitively against the advertised models' id or name:

```toml
[backends.gemini]
command = ["gemini", "--experimental-acp"]
default_model = "gemini-2.5-pro"
```

If `default_model` is set but can't be applied — it matches nothing the backend
advertises, or the backend exposes no models at all — casebook emits a notice
saying so, rather than silently dropping the preference.

Casebook cannot offer a model the backend doesn't expose, and some backends
advertise only **coarse buckets** rather than exact model ids (see
[Claude](#claude)). For finer control, define **separate backends**, each launched
with that backend's own model-selection flags or env — the vendor-specific value
lives in your config, and casebook stays agnostic:

```toml
[backends.assistant-fast]
command = ["some-acp-agent", "--model", "<fast model the agent understands>"]

[backends.assistant-deep]
command = ["some-acp-agent", "--model", "<deep model the agent understands>"]
```

Then pick the backend you want from the picker (or set `default_backend`).

## Naming

The "name session" button runs a short one-shot query on `naming_backend` (or the
session's own backend if unset), optionally pinned to `naming_model`. The built-in
`echo` backend is **never** used for naming — it has no language model — so if
naming would resolve to `echo`, the app tells you to set `naming_backend`.

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
the initial one with `default_model`:

```toml
[backends.claude]
command = ["claude-agent-acp"]
default_model = "opus"
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

### Gemini

Google's Gemini CLI in its experimental ACP mode:

```toml
[backends.gemini]
command = ["gemini", "--experimental-acp"]
default_model = "gemini-2.5-pro"
env = { GEMINI_API_KEY = "..." }
```

### Any other ACP agent

Point `command` at the program and whatever flags put it in ACP mode:

```toml
[backends.custom]
command = ["/opt/agents/my-agent", "serve", "--acp"]
env = { MY_AGENT_TOKEN = "..." }
```
