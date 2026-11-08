#!/usr/bin/env python3
"""Static coverage check for the Firecrawl egress NetworkPolicies.

Reads a rendered Firecrawl chart on stdin and fails if the policies selecting the
URL-fetching pods do not line up with the endpoints those pods are configured to
use, or if they let a private / link-local / non-web destination through.

    helm template --namespace firecrawl firecrawl apps/firecrawl \
      | python3 test/mcp-gateway/firecrawl-egress-test.py

Nothing here touches the cluster. ipBlock rules are read as plain IPv4 CIDR math,
which is the conservative side: Cilium also ignores in-cluster pod and node IPs in
CIDR rules unless policyCIDRMatchMode is set, so a destination denied here is
denied twice over, never less. Stdlib + PyYAML only.
"""

import ipaddress
import sys
import urllib.parse

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("PyYAML is required")

# Pods that fetch caller-chosen URLs, so their egress has to be restricted.
FETCHERS = ["api", "worker", "nuq-worker", "playwright"]
APP_FETCHERS = ["api", "worker", "nuq-worker"]  # same, minus the browser

# Destinations no fetcher may ever reach.
DENIED = [
    "0.0.0.1",
    "10.0.4.110",  # pod address
    "10.43.0.10",  # kube-dns service address
    "127.0.0.1",
    "100.64.0.1",  # CGNAT / tailnet
    "169.254.169.254",  # cloud metadata
    "172.16.0.1",
    "192.0.2.1",
    "192.168.192.11",  # node address
    "198.18.0.1",
    "224.0.0.5",
    "240.0.0.1",
]
PUBLIC = ["8.8.8.8", "93.184.216.34"]

WEB_PORTS = {("TCP", 80), ("TCP", 443)}
DNS_PORTS = {("UDP", 53), ("TCP", 53)}
DNS_APPS = ("node-local-dns", "kube-dns")

failures = []


def fail(message):
    failures.append(message)


def selector_matches(selector, labels):
    """Kubernetes LabelSelector semantics against a pod's label set."""
    for key, value in (selector or {}).get("matchLabels", {}).items():
        if labels.get(key) != value:
            return False
    for expr in (selector or {}).get("matchExpressions", []):
        actual, values, op = labels.get(expr["key"]), expr.get("values", []), expr["operator"]
        if op == "In" and actual not in values:
            return False
        if op == "NotIn" and actual in values:
            return False
        if op == "Exists" and actual is None:
            return False
        if op == "DoesNotExist" and actual is not None:
            return False
    return True


def ports_of(rule):
    out = set()
    for entry in rule.get("ports") or []:
        if entry.get("port") is None or "portRange" in entry:
            out.add(("ANY", "ANY"))
        else:
            out.add((entry.get("protocol", "TCP"), int(entry["port"])))
    return out


def cidr_allows(peer, ip):
    """True when a peer's ipBlock permits the address."""
    block = peer.get("ipBlock")
    if not block:
        return False
    addr = ipaddress.ip_address(ip)
    if addr not in ipaddress.ip_network(block["cidr"], strict=False):
        return False
    return not any(addr in ipaddress.ip_network(x, strict=False) for x in block.get("except") or [])


def dns_peer(peer):
    """A DNS destination: a kube-system pod labelled node-local-dns or kube-dns."""
    namespace = ((peer.get("namespaceSelector") or {}).get("matchLabels") or {})
    pod = ((peer.get("podSelector") or {}).get("matchLabels") or {})
    return (namespace.get("kubernetes.io/metadata.name") == "kube-system"
            and pod.get("k8s-app") in DNS_APPS)


def main():
    docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict) and d.get("kind")]
    if not docs:
        sys.exit("no Kubernetes documents on stdin - did helm template succeed?")

    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    policies = [p for p in docs if p["kind"] == "NetworkPolicy"]
    if not policies:
        sys.exit("no NetworkPolicy rendered - egress isolation is missing entirely")
    # A peer without a namespaceSelector means the policy's own namespace, so the test
    # has to know it to judge the peers that do name one.
    namespace_name = policies[0]["metadata"].get("namespace") or "firecrawl"

    def labels_of(component):
        dep = deployments.get("firecrawl-" + component)
        if dep is None:
            fail("deployment firecrawl-%s is not rendered" % component)
            return {}
        return dep["spec"]["template"]["metadata"].get("labels") or {}

    labels = {c: labels_of(c) for c in FETCHERS + ["redis"]}

    def egress_for(component):
        """Rules and policy names of every Egress policy selecting this component."""
        rules, names = [], []
        for pol in policies:
            spec = pol["spec"]
            if "Egress" in (spec.get("policyTypes") or []) and selector_matches(
                spec.get("podSelector"), labels[component]
            ):
                names.append(pol["metadata"]["name"])
                rules.extend(spec.get("egress") or [])
        return rules, names

    def envs_of(component):
        out = {}
        dep = deployments.get("firecrawl-" + component) or {}
        containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            for entry in container.get("env") or []:
                out[entry["name"]] = entry.get("value")
        return out

    def opens(component, port, pod_labels, what):
        """Some rule must open TCP port for pods of this release matching those labels.

        A peer that also carries a namespaceSelector only counts when it names this
        release's namespace: a cross-namespace or all-namespace peer is not the pinned
        backend and would silently stop working (or reach a same-labelled pod elsewhere).
        """
        for rule in egress_for(component)[0]:
            if ("TCP", port) not in ports_of(rule):
                continue
            for peer in rule.get("to") or []:
                if "ipBlock" in peer:
                    continue
                namespace = (peer.get("namespaceSelector") or {}).get("matchLabels")
                if namespace is not None and namespace.get(
                    "kubernetes.io/metadata.name") != namespace_name:
                    continue
                if not selector_matches(peer.get("podSelector"), pod_labels):
                    continue
                # The peer has to name this backend and nothing else: a selector that
                # also covers another component would let a compromised fetcher talk to
                # redis or the API on a port it has no business using.
                broad = [c for c in labels if selector_matches(peer.get("podSelector"), labels[c])]
                if len(broad) > 1:
                    fail("%s: TCP %s peer for %s also selects %s"
                         % (component, port, what, ", ".join(sorted(broad))))
                return
        fail("%s: nothing opens TCP %s for %s in namespace %r"
             % (component, port, what, namespace_name))

    # 1. Egress only: an Ingress policyType without a matching rule denies all traffic
    #    into the pods, and cluster-wide ingress isolation is deliberately off here. If
    #    ingress isolation is introduced on purpose, update this check with it.
    for pol in policies:
        types = pol["spec"].get("policyTypes") or []
        if types != ["Egress"]:
            fail("%s: policyTypes is %s; this chart isolates egress only"
                 % (pol["metadata"]["name"], types))
        if pol["spec"].get("ingress"):
            fail("%s: carries ingress rules; this chart isolates egress only"
                 % pol["metadata"]["name"])

    # 2. Every fetcher is covered by an Egress policy, or default-deny is not in force.
    #    The redis pod is deliberately not a fetcher: an unscoped selector that starts
    #    selecting it (a matchExpressions typo, a dropped label) silently isolates the
    #    queue backing store, which is not what this chart claims to do.
    covered = {c: egress_for(c)[1] for c in FETCHERS}
    for component in FETCHERS:
        if not covered[component]:
            fail("%s: no Egress NetworkPolicy selects its pods" % component)
    for pol in policies:
        if selector_matches(pol["spec"].get("podSelector"), labels["redis"]):
            fail("%s: selects the redis pod, which is not a fetcher" % pol["metadata"]["name"])

    # 3. Rules stay narrow: a destination and an explicit port on each of them. A rule
    #    without 'to' or without ports allows everything in that direction.
    for component in FETCHERS:
        for index, rule in enumerate(egress_for(component)[0]):
            where = "%s rule #%d of %s" % (component, index, covered[component])
            peers = rule.get("to")
            if peers is None:
                fail("%s: no 'to', every destination is allowed" % where)
            elif not rule.get("ports"):
                fail("%s: no ports, every port is allowed" % where)
            for peer in peers or []:
                if not peer:
                    fail("%s: allows all destinations" % where)
                elif "ipBlock" in peer and len(peer) > 1:
                    fail("%s: mixes ipBlock with selectors %s" % (where, sorted(peer)))
                elif any(peer.get(k) is not None for k in ("podSelector", "namespaceSelector")) \
                        and not any((peer.get(k) or {}).get(m)
                                    for k in ("podSelector", "namespaceSelector")
                                    for m in ("matchLabels", "matchExpressions")):
                    # podSelector: {} means every pod in the namespace, namespaceSelector: {}
                    # every namespace - both silently reopen what the policy just closed.
                    fail("%s: an empty selector peer allows every pod: %s" % (where, peer))

    # 4. Private, link-local, loopback and reserved addresses stay denied; public hosts
    #    are reachable on the web ports only.
    for component in FETCHERS:
        rules = egress_for(component)[0]
        cidrs = [p for r in rules for p in (r.get("to") or []) if "ipBlock" in p]
        if not cidrs:
            fail("%s: no ipBlock rule, the fetchers cannot reach the internet" % component)
        for peer in cidrs:
            if ipaddress.ip_network(peer["ipBlock"]["cidr"]).version == 6:
                fail("%s: IPv6 ipBlock %s - this chart keeps all IPv6 egress denied"
                     % (component, peer["ipBlock"]["cidr"]))
        for ip in DENIED:
            for peer in cidrs:
                if cidr_allows(peer, ip):
                    fail("%s: %s permitted by ipBlock %s" % (component, ip, peer["ipBlock"]["cidr"]))
        for ip in PUBLIC:
            allowed = set()
            for rule in rules:
                if any(cidr_allows(p, ip) for p in rule.get("to") or []):
                    allowed |= ports_of(rule)
            if not allowed:
                fail("%s: public host %s is unreachable" % (component, ip))
            elif not allowed <= WEB_PORTS:
                fail("%s: %s also reachable on %s" % (component, ip, sorted(allowed - WEB_PORTS)))

    # 5. DNS on port 53 to the node-local cache and kube-dns pods in kube-system. Cilium
    #    checks egress after the LocalRedirectPolicy DNAT, so the backend pod is what a
    #    policy has to name - never a cluster IP or the 169.254.25.10 literal.
    for component in FETCHERS:
        if not any(
            ports_of(rule) and ports_of(rule) <= DNS_PORTS
            and rule.get("to") and all(dns_peer(p) for p in rule["to"])
            for rule in egress_for(component)[0]
        ):
            fail("%s: no DNS rule to kube-system %s, every URL lookup would fail"
                 % (component, "/".join(DNS_APPS)))

    # 6. The allow list must match the endpoints the containers are configured with,
    #    otherwise the next values bump silently breaks scraping.
    postgres = {d["metadata"]["name"] for d in docs if d["kind"] == "postgresql"}
    for component in APP_FETCHERS:
        env = envs_of(component)
        for name, service, port, pod_labels, what in [
            ("REDIS_URL", "firecrawl-redis", 6379, labels["redis"], "redis"),
            ("PLAYWRIGHT_MICROSERVICE_URL", "firecrawl-playwright", 3000,
             labels["playwright"], "the browser"),
        ]:
            host = urllib.parse.urlparse(env.get(name) or "").hostname
            if host != service:
                fail("%s: %s host %r is not the chart service %r" % (component, name, host, service))
            else:
                opens(component, port, pod_labels, what)
        if env.get("DATABASE_HOST") not in postgres:
            fail("%s: DATABASE_HOST %r has no postgresql resource in this chart"
                 % (component, env.get("DATABASE_HOST")))
        else:
            opens(component, int(env.get("DATABASE_PORT", 5432)),
                  {"cluster-name": env["DATABASE_HOST"]}, "postgres")

    # SearXNG backs web_search, and only the API process calls it: executeSearch is
    # reached from controllers/v2/search.ts, never from a job worker. The FQDN in the
    # env is <service>.<namespace>.svc.cluster.local, which pins the named namespace.
    searxng = urllib.parse.urlparse(envs_of("api").get("SEARXNG_ENDPOINT") or "")
    if not (searxng.hostname or "").endswith(".svc.cluster.local") or not searxng.port:
        fail("api: SEARXNG_ENDPOINT %r is not a cluster service URL" % searxng.hostname)
    else:
        namespace = searxng.hostname.split(".")[1]
        if not any(
            ("TCP", searxng.port) in ports_of(rule)
            and any(
                ((p.get("namespaceSelector") or {}).get("matchLabels") or {}).get(
                    "kubernetes.io/metadata.name") == namespace
                for p in rule.get("to") or []
            )
            for rule in egress_for("api")[0]
        ):
            fail("api: nothing opens TCP %d for searxng in namespace %r"
                 % (searxng.port, namespace))
        for component in ("worker", "nuq-worker"):
            if any(("TCP", searxng.port) in ports_of(r) for r in egress_for(component)[0]):
                fail("%s: opens TCP %d although only the API calls SearXNG"
                     % (component, searxng.port))

    if failures:
        print("FAIL firecrawl egress policies")
        for message in failures:
            print(" -", message)
        sys.exit(1)

    print("PASS firecrawl egress policies: %d policies; %s"
          % (len(policies), ", ".join("%s=%s" % (c, covered[c]) for c in FETCHERS)))


if __name__ == "__main__":
    main()
