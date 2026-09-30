---
id: operator-identity
type: rule
created: "2026-09-29T21:30:00Z"
consequence: 8
locus: output
uses: 0
summary: Operator email-by-context table (global gmail vs ghobz admin@ghobz.com) and gh account split (Roukh vs gh0bz).
scope: global
status: active
---
# Operator identity

| Context | Email |
|---|---|
| Global (default everywhere) | gueeve.ghobady@gmail.com |
| ghobz projects only (`~/Projects/ghobz/**`, gh account `gh0bz`) | admin@ghobz.com |

- Don't use admin@ghobz.com outside ghobz work.
- Git commit identity (as of 2026-09-29): the global `user.email` is the GitHub noreply address `99585672+Roukh@users.noreply.github.com`. ghobz repos get admin@ghobz.com through `includeIf gitdir:~/Projects/ghobz/ghobz_projects/` → `~/.gitconfig-ghobz`. Changing the global git email to the gmail address would publish it in every public commit, so that's the operator's call. It hasn't been changed.
- gh accounts: `Roukh` for roukh-* repos, `gh0bz` for ghobz. See [[placement]] for global-vs-local notes.
