# reuse-before-build

**Intent:** prefer reusing an existing capability, session, or pattern over building a new one;
spend the minimum change the task requires.

Ground the work in the project's actual code, git state, and prior work before adding anything.
Investigate the existing patterns and tailor the change to them rather than introducing a
generic pattern that conflicts with the repo. A new construct re-pays a full cost that a warm,
existing one does not — reuse the one you have when the work fits its context; open a fresh one
only when the work is genuinely unrelated, needs a clean context, or must run in parallel.

Change only what the task requires. Preserve the existing structure and formatting; make changes
in small, verifiable steps; choose the solution that needs the least new code. No drive-by
refactors and no unrelated improvements. If a larger change is genuinely unavoidable, state why
the minimal change is impossible and get approval before taking it — do not expand scope silently.
