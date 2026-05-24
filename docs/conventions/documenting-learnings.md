# Documenting Troubleshooting Learnings

When debugging uncovers non-obvious behavior, root causes, or operational patterns that future
agents would benefit from, document them so the knowledge is discoverable.

## Where to Document

1. **`docs/troubleshooting/<topic>.md`** — Detailed runbooks with problem description, diagnosis
   commands, root cause analysis, and fix steps. This is the primary location.
2. **`AGENTS.md` Common Pitfalls** — Add a one-line bullet when the learning is a common mistake
   or non-obvious behavior an agent should watch for. Link to the troubleshooting doc.
3. **`AGENTS.md` subsystem sections** — If the learning is specific to a subsystem already
   documented in AGENTS.md, append it to that section.

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

## When to Document

After resolving a non-trivial investigation that required cross-referencing multiple sources
(metrics, logs, kubectl, git history). If an agent in the future would benefit from knowing
this without re-discovering it, write it down.
