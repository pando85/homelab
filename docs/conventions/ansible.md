# Ansible Conventions

Rules for Ansible code in `metal/`.

- **Profile:** `safety` (ansible-lint)
- **Task name prefix:** `{stem} | `
- **Variable naming:** `^[a-z_][a-z0-9_]*$`
- Use `become: true` for privilege escalation
- Keep roles in `roles/` with standard structure
