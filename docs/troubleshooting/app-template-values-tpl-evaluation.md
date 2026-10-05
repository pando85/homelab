# app-template evaluates values as Go templates — literal `{{ }}` breaks rendering

## Problem

An app on the bjw-s `app-template` chart suddenly stops deploying. ArgoCD shows the application
`Unknown`/`OutOfSync` with a `ComparisonError`, and **every** sync for that app is blocked — no
unrelated change to the app can roll out until this is fixed. The underlying Helm error names a
template function that doesn't exist:

```
error calling tpl: cannot parse template "...": template: gotpl:23: function "provider" not defined
```

Local `helm template` / `helm lint` may pass, which hides the problem.

Observed on `readest`: a literal `{{provider}}` token sat inside an initContainer `command` (a `sed`
pattern matching a JS i18n string) and silently blocked all readest syncs for ~18 days.

## Root Cause

Since app-template **5.2.x**, string values — including `initContainers.*.command` — are passed
through Helm's `tpl` function (`bjw-s.common.values.evaluateTemplate`). Any `{{ ... }}` inside them
is evaluated as a Go template action. A literal double-brace token that isn't valid Go template
syntax (here `{{provider}}`, a JS i18n interpolation we were trying to match in `sed`) makes `tpl`
try to call a function named `provider`, which is undefined → the whole render fails.

This is a **latent bug activated by a chart bump**, not a bad edit: the value rendered fine under
app-template 5.1.0 (which did not `tpl`-evaluate these fields) and broke when `Chart.yaml` went
5.1.0 → 5.2.1 with no `values.yaml` change.

Why local rendering can miss it: `Chart.lock` and `charts/` are gitignored, so a workstation may
still hold the older chart on disk while ArgoCD runs `helm dependency build` and resolves the newer
version from `Chart.yaml`. See the AGENTS.md pitfall *"Forgetting `helm dependency build` after
updating `Chart.yaml`"*.

## How to Diagnose

```bash
# Render with the SAME chart version ArgoCD resolves — rebuild deps first
helm dependency build apps/<app>/
helm template --include-crds --namespace <ns> <release> apps/<app>/
# A tpl failure names the offending token: function "<token>" not defined

# Find literal double-braces in values that are not meant to be Helm templates
grep -n '{{' apps/<app>/values.yaml

# Confirm what ArgoCD sees
kubectl --context=grigri -n argocd get application <app> \
  -o jsonpath='{.status.sync.status} {.status.conditions[*].message}{"\n"}'
```

## Fix / Workaround

- **Preferred:** don't put literal `{{ }}` in values. For readest the `sed` patterns were rewritten
  to match the label text generically (`label:[A-Za-z0-9_$]+\("[^"]*",...)`) instead of the literal
  `Sign in with {{provider}}`, removing the braces entirely. This also made the patterns resilient
  to minified-identifier churn across releases.
- **If the literal is required**, escape it for `tpl`: writing `{{ "{{provider}}" }}` renders to the
  literal `{{provider}}`.
- After fixing, `helm dependency build` then re-render to confirm the error is gone, and verify the
  rendered manifest still contains the intended literal string. Then commit.

## Prevention

When embedding shell/`sed`/regex that must match a literal `{{ ... }}` (common with JS i18n like
`{{provider}}`, or templated config snippets) inside an app-template `command`/`args` value, escape
it or match around it. Treat any `{{` in `values.yaml` as a Helm template action, because
app-template ≥ 5.2 evaluates it.
