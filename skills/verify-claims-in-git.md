# verify-claims-in-git

**Intent:** ground every claim in what the repository actually contains; the committed diff is
the unit of evidence, never the prose around it.

The commit is your unit of evidence. Work on your own tree, commit with a clear message, and
reference commit SHAs in your hand-back — the reviewer reads your committed diff in git
(`git show` / `git log` / `grep` / reading the diff), not your summary. Never trust dispatch
prose or a self-reported report over what the repository actually contains; if they conflict,
surface it.

When reviewing, verify the committed diff in git before accepting anything — a summary is not
proof and a completion claim is not a diff. A green test on one layer does not prove the goal
holds end-to-end: another layer can render a correct-looking change inert, so trace the value
from where it was changed to where the objective is actually observed, and state which seams
were verified and which were not.
