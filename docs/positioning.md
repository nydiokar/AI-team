Positioning
Purpose
Extend my throughput safely by delegating code work to smart cloud coding agents (Codex/Claude Code) while I’m away. Keep capability in the cloud; keep control on my side.

What it is
A three-layer harness:

Control plane to accept tasks from Telegram and prepare sanitized workpacks.

Isolated runner VM that lets cloud agents do anything they need inside a jail.

Apply-gate that accepts only diffs that pass tests, then merges.

What it is not
Not an autonomous developer. Not a research agent framework. No vendor lock-in to one provider’s sandbox claims.

Why it differs
Most systems chase autonomy and rely on provider sandboxes. This system establishes an external, portable trust boundary (VM + proxy + patch-on-green). Capability (cloud) is separable from control (mine). I can swap backends later without rewriting safety.

Outcome
Work continues while I’m offline. Risk is bounded. Migration path to more autonomy is flipping flags, not re-architecting.