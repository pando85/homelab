# Ansible Conventions

Rules for Ansible code in `metal/`.

- **Profile:** `safety` (ansible-lint)
- **Task name prefix:** `{stem} | ` — where `{stem}` is the task file's basename. A role's
  `tasks/main.yml` therefore takes *no* prefix, since `main | ` is not what `name[casing]` expects.
- **Variable naming:** `^[a-z_][a-z0-9_]*$`
- Use `become: true` for privilege escalation
- Keep roles in `roles/` with standard structure
- **Toolchain floor:** `ansible-core~=2.20` / `ansible~=13.0`. The control node runs Python 3.14,
  which `ansible-lint` refuses to pair with an older core, and Ubuntu 26.04 managed nodes ship
  Python 3.14. See `docs/troubleshooting/ansible-ubuntu-2604-compat.md`
- **Galaxy roles are gitignored** (`metal/roles/.gitignore`) and must stay byte-identical to
  upstream. Never patch them — a patch does not survive a clone. Wrap them in a repo role and pass
  overrides as `include_role` params; static `roles:` params lose to the inner role's `include_vars`.
- **Ubuntu 26.04 nodes** need `ansible_become_exe: /usr/bin/sudo.ws` (sudo-rs rejects the `-H` flag
  Ansible passes) and chrony instead of `ntp`.
