# AGENTS.md

## Branches

`dev` is the development branch: everything lands there first, including
documentation and one-line fixes. `master` is the stable production branch,
and it lags behind `dev` at a commit that has been proven in use.

The invariant is that **`master` never diverges from `dev`**: it is always a
direct ancestor of it. That means nothing is ever committed to `master`
directly, and `master` moves only by fast-forward to a commit that is already
on `dev`. Branch new work off `dev`, and merge it back into `dev`.

Ignoring this is cheap to do and expensive to find: the divergence surfaces
much later as a merge conflict at deploy time. See the "Branches" section of
[deploy/README.md](../deploy/README.md) for how the release step uses this.

## Checkouts

`~/projects/falconfox` is the development checkout, and it is on `dev`. It is
where you are, and it is what the dev instance runs. Work in a worktree under
`.worktrees/`, branched from `dev`, and merge back into `dev`.

`~/projects/falconfox-prod` is the production checkout, on `master`, and is
the only thing a deployment runs. Do not develop in it and do not commit
there: it moves by fast-forward alone.

Both checkouts must stay clean, so never edit either one in place.

One operational warning: restarting the daemon kills every agent session it
is running, including your own turn, whether you restart it with `update.sh`
or by hand. Detach the restart (`update.sh --detach-restart`, or
`systemd-run --user --on-active=5 …`) and end your turn.
