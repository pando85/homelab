# Planning

Implementation specs, TODOs, and design documents for pending work.

## Purpose

This directory contains technical specifications and implementation plans that:
- Require execution by developers or LLMs
- Contain diagnostic context and step-by-step TODOs
- Are distinct from user documentation in `docs/`

After implementation is complete, specs can be:
- Moved to `docs/` as reference documentation
- Archived or removed
- Kept as implementation history

## Structure

```
planning/
├── README.md           # This file
├── ipv6-implementation.md  # IPv6 enablement spec
└── ...                 # Future specs
```

## Naming Convention

Files should follow pattern: `<topic>-implementation.md` or `<topic>-spec.md`

Examples:
- `ipv6-implementation.md` - Full implementation spec with TODOs
- `cilium-dual-stack-spec.md` - Technical design spec
- `vault-migration-todo.md` - Migration checklist

## Spec Template

Each spec should include:

1. **Overview** - Summary and status
2. **Context & Current State** - Diagnostic findings, current config
3. **Implementation Plan** - Phased TODOs (TODO-1.1, TODO-2.1, etc.)
4. **Verification Checklist** - Checkboxes for validation
5. **Troubleshooting** - Common issues
6. **Reference Information** - IPs, config values, file paths
7. **Files to Modify** - Table of all affected files
8. **Execution Order** - Phase dependencies

## Related

- `docs/` - User documentation and reference
- `AGENTS.md` - Repository guidelines for coding agents
- `metal/` - Ansible playbooks (infrastructure)
- `system/` - Cluster system components
