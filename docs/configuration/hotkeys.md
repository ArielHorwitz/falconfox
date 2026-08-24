# Keyboard shortcuts

FalconFox is fully keyboard-drivable. Bindings live under a `[hotkeys]` table in
your [config](README.md); any subset overrides the defaults. The app also shows
the **active** bindings live — press `?` or click the ⌨ button.

```toml
[hotkeys]
new_session = "n"
focus_next = ["ArrowRight", "ArrowDown"]
delete_session = "D"   # require shift, to make deletes deliberate
```

## Bindable actions

A binding is **one key or a list of keys**. Session actions act on the currently
**focused** session; navigation differs between the home page (cases) and a case
page (sessions).

| Action | Default key(s) | Where | What it does |
|---|---|---|---|
| `new_case` | `c` | anywhere | Prompt for a title, create a case, open its page. |
| `new_session` | `n` | home / case | Case page: start a new session. Home page: create a case (so "new" is one key everywhere). |
| `home` | `h` | anywhere | Go to the cases home page. |
| `scratch` | `s` | anywhere | Go to the scratch page (caseless one-off sessions). |
| `cycle_width` | `w` | case page | Step the session-column width through the configured list (see [ui sizing](README.md#ui-sizing)). |
| `focus_next` | `ArrowRight` `ArrowDown` | home / case | Focus the next case (home) or session (case page). |
| `focus_prev` | `ArrowLeft` `ArrowUp` | home / case | Focus the previous case / session. |
| `open_focused` | `Enter` | home / case | Home: open the focused case. Case page: focus the open session's input box, or open (resume) the focused session if it's closed. |
| `rename_session` | `r` | case page | Rename the focused session (prompt). |
| `name_session` | `g` | case page | Ask the model to name the focused session. |
| `close_session` | `x` | case page | Close the focused session if it's open (collapses it to the sidebar). |
| `delete_session` | `d` | home / case | Case page: delete the focused session. Home page: delete the focused case and all its sessions. Both ask to confirm. |
| `toggle_allow` | `a` | case page | Toggle "always allow" permissions on the focused session. |
| `cancel_turn` | `S` | case page | Stop the focused session's running turn (e.g. to abort a long tool call). |
| `help` | `?` | anywhere | Toggle the shortcuts overlay. |

### Always-on keys (not configurable)

- **Enter** / **Shift+Enter** in a session's input box — send the message / insert
  a newline.
- **Escape** — leave the input box back to navigation; or close the file preview /
  shortcuts overlay when one is open.

## Key-name syntax

Keys are matched against the browser's [`KeyboardEvent.key`](https://developer.mozilla.org/en-US/docs/Web/API/KeyboardEvent/key/Key_Values)
value:

- **Printable keys** are the literal character: `"c"`, `"/"`, `" "` (space).
  They are **case-sensitive**, and the shifted form is the shifted character — so
  `"?"` is Shift+/ and `"X"` is Shift+x. Use that to make destructive actions
  deliberate (e.g. bind `delete_session` to `"D"`).
- **Named keys** use their event name: `"Enter"`, `"Escape"`, `"Tab"`,
  `"ArrowUp"`, `"ArrowDown"`, `"ArrowLeft"`, `"ArrowRight"`.

Notes:

- **Modifier combos aren't supported.** A keypress with Ctrl/Cmd/Alt held is
  ignored by FalconFox (left to the browser), so you can't bind `Ctrl+S`.
- **Shortcuts don't fire while you're typing** in an input, textarea, or select —
  so they never collide with prompting. The one exception is Escape, which blurs
  the input.
- If two actions share a key, the last one loaded wins.
