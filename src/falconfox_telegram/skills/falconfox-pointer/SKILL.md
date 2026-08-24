---
name: falconfox-pointer
description: Resolve a FalconFox session request and move the Telegram focus pointer, spawning the session first if needed.
---

For every request:

1. Run `falconfox list` and resolve which session the user means — by id, name,
   or path. References are often spoken/fuzzy; pick the closest match and say
   which one you chose.
2. If no session matches and the user names a path or project, create one:
   `falconfox spawn --path <path> [--name <name>]`, then use the printed id.
3. Write only the chosen session id plus a newline to the pointer file named in
   `AGENTS.md`. The bot watches that file and confirms every switch in the work
   chat, so a wrong resolution is immediately visible.

Never do project work in this session: no editing, no code analysis, no
commands beyond `falconfox` and writing the pointer file. If asked for anything
else, point the user to the work chat. When greeting or unsure, ask which
session to focus.
