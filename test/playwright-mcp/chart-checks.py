#!/usr/bin/env python3
"""Static assertions on the rendered Playwright MCP chart.

Each check is an invariant a live change could break silently, asserted on
`helm template` output rather than on the source YAML. The chart renders a
single pod with an MCP server + headless Chromium, reachable only via
`kubectl port-forward`. The invariants cover:

  * no HTTPRoute, Ingress, SecurityPolicy, Kanidm resources (Stage 1 is
    unexposed by design);
  * automountServiceAccountToken is false (the pod holds no cluster identity);
  * the CNP egress except list contains the cluster pod and service CIDRs,
    and the ingress is host-only on 8931 with no world rule;
  * exec probes are used and no HTTP probe port is opened;
  * /dev/shm is an emptyDir with medium Memory;
  * the image ref carries a Renovate hint;
  * readOnlyRootFilesystem is enabled (best-effort, verified by the live
    acceptance script, not by construction).

Run from the repository root:

    python3 test/playwright-mcp/chart-checks.py
"""
import json
import re
import subprocess
import sys

import yaml

results = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    results.append(bool(ok))


def helm(sets=()):
    cmd = ["helm", "template", "--include-crds", "--namespace", "playwright-mcp",
           "playwright-mcp", "apps/playwright-mcp"]
    for kv in sets:
        cmd += ["--set", kv]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"helm template: {out.stderr[-900:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs, kind):
    return next((d for d in docs if d.get("kind") == kind), None)


def find_all(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def main():
    docs = helm()
    vals = open("apps/playwright-mcp/values.yaml").read()

    check("chart renders", bool(docs))

    # ---- rendered kinds: no HTTPRoute, Ingress, SecurityPolicy, Kanidm
    kinds = {d.get("kind") for d in docs}
    check("Deployment is rendered", "Deployment" in kinds)
    check("Service is rendered", "Service" in kinds)
    check("CiliumNetworkPolicy is rendered", "CiliumNetworkPolicy" in kinds)
    forbidden = {"HTTPRoute", "Ingress", "SecurityPolicy",
                 "KanidmOAuth2Client", "KanidmGroup"}
    leaked = kinds & forbidden
    check("no HTTPRoute/Ingress/SecurityPolicy/Kanidm resources rendered",
          not leaked, f"found: {leaked}")

    # ---- Deployment
    dep = find(docs, "Deployment")
    check("Deployment exists", dep is not None)
    if dep:
        spec = dep["spec"]["template"]["spec"]
        containers = spec.get("containers", [])
        check("exactly one container", len(containers) == 1,
              f"found {len(containers)}")
        if containers:
            c = containers[0]
            # automountServiceAccountToken
            check("automountServiceAccountToken is false",
                  spec.get("automountServiceAccountToken") is False,
                  f"got {spec.get('automountServiceAccountToken')}")
            # image tag
            img = c.get("image", "")
            check("image tag is v0.0.85", ":v0.0.85" in img, f"image={img}")
            # args (no command override)
            check("no command override (image ENTRYPOINT preserved)",
                  "command" not in c, f"command={c.get('command')}")
            args = c.get("args", [])
            check("args include --port 8931",
                  "--port" in args and "8931" in args, f"args={args}")
            check("args include --host 0.0.0.0",
                  "--host" in args and "0.0.0.0" in args, f"args={args}")
            # Guards the exact combination that crash-looped v0.0.85 on the
            # first deploy: --user-data-dir together with isolated mode makes
            # the server exit 1 ("Browser userDataDir is not supported in
            # isolated mode"). This asserts ABSENCE, not presence.
            check("args omit --user-data-dir (incompatible with isolated mode)",
                  "--user-data-dir" not in args
                  and not any("browser-profile" in a for a in args),
                  f"args={args}")
            # env HOME=/tmp
            env = {e["name"]: e.get("value") for e in c.get("env", [])}
            check("HOME=/tmp env var", env.get("HOME") == "/tmp",
                  f"HOME={env.get('HOME')}")
            check("PLAYWRIGHT_MCP_ISOLATED=true env var",
                  env.get("PLAYWRIGHT_MCP_ISOLATED") == "true",
                  f"PLAYWRIGHT_MCP_ISOLATED={env.get('PLAYWRIGHT_MCP_ISOLATED')}")
            # The env form of the same flag breaks startup identically, so
            # guarding only the CLI arg would leave the failure reachable.
            check("no PLAYWRIGHT_MCP_USER_DATA_DIR env (same incompatibility)",
                  "PLAYWRIGHT_MCP_USER_DATA_DIR" not in env,
                  f"env keys={sorted(env)}")
            # probes
            for probe_name in ("livenessProbe", "readinessProbe", "startupProbe"):
                probe = c.get(probe_name)
                check(f"{probe_name} is exec type",
                      probe and "exec" in probe and "httpGet" not in probe,
                      f"probe={probe}")
                if probe and "exec" in probe:
                    cmd = probe["exec"]["command"]
                    check(f"{probe_name} uses node -e",
                          cmd[0] == "node" and cmd[1] == "-e",
                          f"cmd={cmd[:2]}")
                    script = cmd[2]
                    check(f"{probe_name} targets 127.0.0.1:8931",
                          "127.0.0.1" in script and "8931" in script,
                          f"script snippet: {script[:80]}")
                    check(f"{probe_name} accepts status <500",
                          "statusCode<500" in script,
                          f"script snippet: {script[:80]}")
                    check(f"{probe_name} exits 1 on error",
                          "process.exit(1)" in script,
                          f"script snippet: {script[:80]}")
            # securityContext
            sc = c.get("securityContext", {})
            check("readOnlyRootFilesystem is true",
                  sc.get("readOnlyRootFilesystem") is True,
                  f"got {sc.get('readOnlyRootFilesystem')}")
            check("allowPrivilegeEscalation is false",
                  sc.get("allowPrivilegeEscalation") is False,
                  f"got {sc.get('allowPrivilegeEscalation')}")
            caps = sc.get("capabilities", {})
            check("capabilities drop ALL",
                  caps.get("drop") == ["ALL"], f"caps={caps}")
            # resources
            res = c.get("resources", {})
            req = res.get("requests", {})
            lim = res.get("limits", {})
            check("memory request is 250Mi", req.get("memory") == "250Mi",
                  f"got {req.get('memory')}")
            check("memory limit is 2Gi", lim.get("memory") == "2Gi",
                  f"got {lim.get('memory')}")
            check("cpu request is 100m", req.get("cpu") == "100m",
                  f"got {req.get('cpu')}")
            check("cpu limit is 2", lim.get("cpu") in ("2", "2000m"),
                  f"got {lim.get('cpu')}")
        # pod securityContext
        pod_sc = spec.get("securityContext", {})
        check("runAsNonRoot is true", pod_sc.get("runAsNonRoot") is True,
              f"got {pod_sc.get('runAsNonRoot')}")
        check("runAsUser is 1000", pod_sc.get("runAsUser") == 1000,
              f"got {pod_sc.get('runAsUser')}")
        check("runAsGroup is 1000", pod_sc.get("runAsGroup") == 1000,
              f"got {pod_sc.get('runAsGroup')}")
        # nodeSelector
        ns = spec.get("nodeSelector", {})
        check("nodeSelector has kubernetes.io/arch=amd64",
              ns.get("kubernetes.io/arch") == "amd64", f"nodeSelector={ns}")
        # volumes
        volumes = spec.get("volumes", [])
        shm_vol = next((v for v in volumes if v.get("name") == "shm"), None)
        check("/dev/shm volume exists", shm_vol is not None)
        if shm_vol:
            ed = shm_vol.get("emptyDir", {})
            check("/dev/shm is emptyDir medium Memory",
                  ed.get("medium") == "Memory", f"medium={ed.get('medium')}")
            check("/dev/shm sizeLimit is 1Gi",
                  ed.get("sizeLimit") == "1Gi", f"sizeLimit={ed.get('sizeLimit')}")
        # volumeMounts
        if containers:
            mounts = containers[0].get("volumeMounts", [])
            shm_mount = next((m for m in mounts if m.get("mountPath") == "/dev/shm"), None)
            check("/dev/shm is mounted at /dev/shm", shm_mount is not None,
                  f"mounts={[m.get('mountPath') for m in mounts]}")
            tmp_mount = next((m for m in mounts if m.get("mountPath") == "/tmp"), None)
            check("/tmp is mounted at /tmp", tmp_mount is not None,
                  f"mounts={[m.get('mountPath') for m in mounts]}")

    # ---- Service
    svc = find(docs, "Service")
    check("Service exists", svc is not None)
    if svc:
        check("Service is ClusterIP", svc["spec"]["type"] == "ClusterIP",
              f"type={svc['spec']['type']}")
        ports = svc["spec"]["ports"]
        check("Service has port 8931",
              any(p.get("port") == 8931 for p in ports),
              f"ports={[p.get('port') for p in ports]}")
        check("Service has no NodePort",
              not any("nodePort" in p for p in ports),
              f"ports={ports}")

    # ---- CiliumNetworkPolicy
    cnps = find_all(docs, "CiliumNetworkPolicy")
    check("exactly one CiliumNetworkPolicy", len(cnps) == 1,
          f"found {len(cnps)}")
    if cnps:
        cnp = cnps[0]
        spec = cnp.get("spec", {})
        # endpointSelector
        sel = spec.get("endpointSelector", {}).get("matchLabels", {})
        check("CNP endpointSelector has app.kubernetes.io/name=playwright-mcp",
              sel.get("app.kubernetes.io/name") == "playwright-mcp",
              f"selector={sel}")
        check("CNP endpointSelector has app.kubernetes.io/instance=playwright-mcp",
              sel.get("app.kubernetes.io/instance") == "playwright-mcp",
              f"selector={sel}")
        # ingress
        ingress = spec.get("ingress", [])
        check("CNP has exactly one ingress rule", len(ingress) == 1,
              f"found {len(ingress)}")
        if ingress:
            rule = ingress[0]
            check("CNP ingress fromEntities is [host]",
                  rule.get("fromEntities") == ["host"],
                  f"fromEntities={rule.get('fromEntities')}")
            check("CNP ingress has no world",
                  "world" not in rule.get("fromEntities", []),
                  f"fromEntities={rule.get('fromEntities')}")
            ports = rule.get("toPorts", [])
            check("CNP ingress has toPorts", bool(ports))
            if ports:
                port_list = ports[0].get("ports", [])
                check("CNP ingress port is 8931",
                      any(p.get("port") == "8931" for p in port_list),
                      f"ports={port_list}")
                check("CNP ingress protocol is TCP",
                      all(p.get("protocol") == "TCP" for p in port_list),
                      f"ports={port_list}")
        # egress
        egress = spec.get("egress", [])
        check("CNP has egress rules", bool(egress))
        # DNS rule
        dns_rules = [r for r in egress if "toEndpoints" in r]
        check("CNP has DNS toEndpoints rule", bool(dns_rules))
        if dns_rules:
            dns_rule = dns_rules[0]
            endpoints = dns_rule.get("toEndpoints", [])
            check("CNP DNS rule selects node-local-dns pods",
                  any(ep.get("matchLabels", {}).get("k8s-app") == "node-local-dns"
                      for ep in endpoints),
                  f"endpoints={endpoints}")
            check("CNP DNS rule selects kube-dns pods",
                  any(ep.get("matchLabels", {}).get("k8s-app") == "kube-dns"
                      for ep in endpoints),
                  f"endpoints={endpoints}")
            check("CNP DNS rule is in kube-system namespace",
                  all(ep.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "kube-system"
                      for ep in endpoints),
                  f"endpoints={endpoints}")
            dns_ports = dns_rule.get("toPorts", [])
            if dns_ports:
                port_list = dns_ports[0].get("ports", [])
                check("CNP DNS rule allows port 53 UDP",
                      any(p.get("port") == "53" and p.get("protocol") == "UDP" for p in port_list),
                      f"ports={port_list}")
                check("CNP DNS rule allows port 53 TCP",
                      any(p.get("port") == "53" and p.get("protocol") == "TCP" for p in port_list),
                      f"ports={port_list}")
        # internet rule
        cidr_rules = [r for r in egress if "toCIDRSet" in r]
        check("CNP has toCIDRSet egress rule", bool(cidr_rules))
        if cidr_rules:
            cidr_rule = cidr_rules[0]
            cidr_set = cidr_rule.get("toCIDRSet", [])
            check("CNP toCIDRSet has cidr 0.0.0.0/0",
                  any(c.get("cidr") == "0.0.0.0/0" for c in cidr_set),
                  f"cidrSet={cidr_set}")
            if cidr_set:
                excepts = cidr_set[0].get("except", [])
                check("CNP except list contains 10.0.0.0/8 (pod CIDR)",
                      "10.0.0.0/8" in excepts, f"excepts={excepts}")
                check("CNP except list contains 10.43.0.0/16 (service CIDR)",
                      "10.43.0.0/16" in excepts, f"excepts={excepts}")
                check("CNP except list contains 172.16.0.0/12",
                      "172.16.0.0/12" in excepts, f"excepts={excepts}")
                check("CNP except list contains 192.168.0.0/16",
                      "192.168.0.0/16" in excepts, f"excepts={excepts}")
                check("CNP except list contains 169.254.0.0/16",
                      "169.254.0.0/16" in excepts, f"excepts={excepts}")
                check("CNP except list contains 100.64.0.0/10",
                      "100.64.0.0/10" in excepts, f"excepts={excepts}")
        # no world anywhere in CNP
        cnp_text = json.dumps(cnp)
        check("CNP has no 'world' entity anywhere",
              '"world"' not in cnp_text and "'world'" not in cnp_text,
              "world entity found in CNP")

    # ---- networkIsolation=false renders no CNP
    docs_off = helm(["networkIsolation=false"])
    cnps_off = find_all(docs_off, "CiliumNetworkPolicy")
    check("networkIsolation=false renders no CiliumNetworkPolicy",
          len(cnps_off) == 0, f"found {len(cnps_off)}")

    # ---- Renovate hint
    check("Renovate hint present above image ref in values.yaml",
          "renovate: datasource=docker depName=mcr.microsoft.com/playwright/mcp" in vals,
          "missing renovate hint")

    # ---- README documents key points
    readme = open("apps/playwright-mcp/README.md").read()
    check("README quotes 'not a security boundary'",
          "not a security boundary" in readme.lower())
    check("README documents port-forward command",
          "kubectl" in readme and "port-forward" in readme)
    check("README documents Stage 2 unknowns",
          "Stage 2" in readme and "claimMap" in readme)
    check("README documents ephemeral profile decision",
          "ephemeral" in readme.lower() and "PVC" in readme)

    # ---- determinism
    a = yaml.dump_all(helm(), sort_keys=True)
    b = yaml.dump_all(helm(), sort_keys=True)
    check("repeated renders are byte-identical (no randomness/lookup)", a == b)

    print()
    print(f"=== {sum(results)} passed, {results.count(False)} failed ===")
    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
