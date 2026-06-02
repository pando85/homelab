# Documenting Troubleshooting Learnings

When debugging uncovers non-obvious behavior, root causes, or operational patterns that future
agents would benefit from, document them so the knowledge is discoverable without making always-loaded
agent instructions noisy.

## Where to Document

1. **`docs/troubleshooting/<topic>.md`** — Detailed runbooks with problem description, diagnosis
   commands, root cause analysis, and fix steps. This is the primary location.
2. **`AGENTS.md` Common Pitfalls** — Add only a one-line trigger when the learning is a common
   mistake or non-obvious behavior an agent should watch for. Link to the troubleshooting doc.
3. **Skills** — Add workflow detail only when it applies to a narrowly triggered skill. Keep skill
   descriptions explicit about when not to use the skill.
4. **`AGENTS.md` subsystem sections** — If the learning is specific to a subsystem already
   documented in AGENTS.md, append a concise link or reminder.

## What to Document

- Non-obvious root causes (e.g., exit code 255 = node reboot, not app crash)
- Operational patterns (e.g., released PVs accumulate with Retain policy)
- Workarounds for known limitations (e.g., Cilium TCX bypasses tc qdiscs)
- Recommended values or configurations discovered through investigation

## Format

Use the existing troubleshooting doc pattern:

```markdown
# Topic

## Problem
What happened, symptoms observed.

## Root Cause
Why it happened.

## How to Diagnose
Commands to identify the issue.

## Fix / Workaround
How to resolve it, or why it's expected behavior.
```

## Instruction Hygiene

- Prefer docs for durable knowledge and runbooks.
- Prefer `AGENTS.md` for short, high-signal constraints that should always be loaded.
- Prefer skills for task-specific workflows with clear trigger phrases and boundaries.
- Do not duplicate full procedures across docs, skills, and agent instructions.

## When to Document

After resolving a non-trivial investigation that required cross-referencing multiple sources
(metrics, logs, kubectl, git history). If an agent in the future would benefit from knowing
this without re-discovering it, write it down.
