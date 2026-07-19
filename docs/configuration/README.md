# Configuration

Casebook is configured with a single TOML file. Everything is optional — with no
config at all, the app runs on the built-in `echo` backend. A real agent like
`claude` is declared explicitly (see [backends.md](backends.md)).

## Where the config lives

Casebook reads, in order, merging later over earlier:

1. **Global:** `$XDG_CONFIG_HOME/casebook/config.toml`, or
   `~/.config/casebook/config.toml` if `$XDG_CONFIG_HOME` is unset.
2. **Project override:** `<project-root>/.casebook/config.toml` — handy for
   per-checkout settings. (`.casebook/` is git-ignored by default.)

Merge rules: top-level keys in the project file replace the global ones; the
`[backends.*]` tables are merged **per backend name** (so a project file can add
a backend without redefining the global ones).

## All keys at a glance

| Key | Type | Default | What it does |
|---|---|---|---|
| `default_backend` | string | first declared backend, else `"echo"` | Which backend new sessions use unless one is picked in the UI. |
| `naming_prompt` | string | (built-in) | Instructions handed to the model by the "name session" button. |
| `naming_backend` | string | — | Which backend names sessions. **Required** for the "name session" button — if unset, naming is disabled. Its model comes from its own `config_options`. See [backends.md](backends.md#naming). |
| `[backends.<name>]` | table | built-in `echo` only | Define a launchable ACP agent. Full detail: **[backends.md](backends.md)**. |
| `[hotkeys]` | table | (built-in) | Rebind keyboard shortcuts. Full detail: **[hotkeys.md](hotkeys.md)**. |
| `[ui]` | table | `50%`/`320px`/`none` | Session-column sizing — see [UI sizing](#ui-sizing). |

## A complete example

```toml
# ~/.config/casebook/config.toml

default_backend = "claude"

# The "name session" button (✨). Required to enable naming — its model comes
# from that backend's own config_options (point it at a small, cheap model).
naming_prompt = "Reply with a concise title of at most six words for this session."
naming_backend = "claude"

# Claude via npx (needs Claude Code installed + signed in). To skip npx, install
# claude-agent-acp and use command = ["claude-agent-acp"]. See backends.md.
[backends.claude]
command = ["npx", "-y", "@agentclientprotocol/claude-agent-acp"]
# Default values for the options this backend advertises (read exact keys/values
# from the session-options ⚙ popover). The model is just one of them.
[backends.claude.config_options]
model = "opus"

[backends.gemini]
command = ["gemini", "--experimental-acp"]
env = { GEMINI_API_KEY = "..." }
[backends.gemini.config_options]
model = "gemini-2.5-pro"

[hotkeys]
new_session = "n"
focus_next = ["ArrowRight", "ArrowDown"]
```

## UI sizing

Each open session is a column (pane) in the case page's main area. Size them with
a `[ui]` table — values are **CSS lengths**, so any unit works: `vw`/`%` for a
fraction of the screen, `px`/`em`/`rem` for fixed sizes, `none` for no maximum.

| Key | Default | What it does |
|---|---|---|
| `session_width` | `"50%"` | The basis width of each session column (default: two columns fill the width). |
| `session_min_width` | `"320px"` | Never shrink a column below this. |
| `session_max_width` | `"none"` | Never grow a column beyond this. |
| `session_widths` | `["20%","33%","50%","66%","75%","100%"]` | Widths the resize hotkey (`cycle_width`, default `w`) cycles through. Your last choice is remembered per browser. |

```toml
[ui]
session_width = "33vw"      # each column is a third of the viewport…
session_min_width = "28em"  # …but at least this wide…
session_max_width = "720px" # …and never wider than this.
session_widths = ["33%", "50%", "100%"]  # the `w` hotkey cycles these
```

Columns don't grow or shrink to fit; when they overflow the window the main area
scrolls horizontally.

## See also

- **[backends.md](backends.md)** — what a backend is, the schema, the built-in
  `echo`, session options (model, effort, mode) and their defaults, and copy-paste
  quick-setup recipes (Claude, Codex, Gemini, and more).
- **[hotkeys.md](hotkeys.md)** — every bindable action, the default keys, and the
  key-name syntax. (The app also lists the *active* bindings live — press `?` or
  click the ⌨ button.)
