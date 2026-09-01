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
