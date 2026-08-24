# Known bugs

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## The focus workspace never prunes stale skills

*Found while widening the focus skill, 2026-08-24.*

`_prepare_focus_workspace` in the Telegram bot writes the packaged
`falconfox-pointer` skill into `<pointer dir>/.agents/skills/` on every start,
but never removes skill directories that are already there. Nothing is broken
today, because the name has never changed.

It breaks the moment one does: renaming or splitting the skill leaves the old
directory in place, and the focus agent discovers **both**, with conflicting
instructions about what it may do. That is a silent failure — the agent behaves
oddly rather than erroring — and it currently blocks a wanted rename.

Fix: treat the skills directory as owned by the bot and reconcile it to exactly
what the package ships, rather than writing into it additively.

## A rebuilt VPS loses the Python 3.12 shim

*From the first phone session, 2026-08-24.*

On Ubuntu 22.04 the bare word `python3` is **3.10**, which cannot import
`tomllib`. Any tool documented as "run it with python3" therefore fails on a
stock host — the casebook skill's CLI did exactly that.

The fix in place is a host-local symlink, `~/.local/bin/python3 ->
/usr/bin/python3.12`, made by hand. It carries **no commit**, so it is not part
of any deployment: a rebuilt or second host silently reverts to 3.10 and the
same failure returns. `setup.sh` does not create it, deliberately — but the
bootstrap checklist has to, or this recurs.

falconfox itself is immune, since uv provisions its own interpreter and the
daemon, bot and deploy scripts all use absolute venv paths. The blast radius is
tools invoked as plain `python3` from inside a session.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident — it is tracked as the desktop-client effort
in [wishlist.md](wishlist.md), and is listed here only so that finding a broken
UI does not read as an undiscovered bug.
