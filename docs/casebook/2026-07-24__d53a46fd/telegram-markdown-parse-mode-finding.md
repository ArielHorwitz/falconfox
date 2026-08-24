# Telegram replies are not rendered: missing parse_mode

Filed 2026-08-24 during the live acceptance run. Not being handled yet — a
finding for later.

## Symptom

Agent replies arrive in Telegram as raw text: markdown syntax (`**bold**`,
backticks, tables, headings) is visible instead of rendered.

## Cause

`falconfox_telegram/api.py` sends messages with a bare
`sendMessage {chat_id, text}` — no `parse_mode`. Telegram renders formatting
only when `parse_mode` is set (`MarkdownV2` or `HTML`); plain sends are shown
verbatim.

## Notes for the fix (when picked up)

- Agent output is ordinary markdown, which is **not** valid Telegram
  `MarkdownV2` — that dialect requires escaping a long list of characters
  (`. - ( ) ! #` …) and rejects the whole message on any parse error, so naive
  `parse_mode=MarkdownV2` would make sends *fail*, not just render badly.
- The robust approach is converting agent markdown → Telegram **HTML**
  (`parse_mode=HTML`, escape `& < >`, map bold/italic/code/pre; drop or
  degrade what Telegram HTML lacks — headings, tables, lists become text).
- Whatever the conversion, keep a fallback: if a send fails with a parse
  error, resend as plain text rather than dropping the reply.
- Also relevant: Telegram's 4096-char message limit — long agent turns need
  splitting; worth handling in the same pass since the renderer must split on
  entity boundaries.
