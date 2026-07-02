// Command tc-limiter resiliently applies and maintains a tc clsact ingress
// policer on a target pod's eth0, inside its CNI network namespace.
//
// It is intentionally dependency-free (Go standard library only) so it can be
// compiled at container start from source injected via a ConfigMap, running on
// the multi-arch golang image with no separate build pipeline.
//
// The main goal is resilience: detect the target, apply the limit, verify it,
// and keep it applied across pod restarts, Cilium restarts and external
// deletion. Every step is logged (never silent).
//
// Full bandwidth-protection design: docs/conventions/bandwidth-protection.md
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	ciliumSocket = "/var/run/cilium/cilium.sock"
	netnsDir     = "/var/run/netns"
)

type ciliumEndpoint struct {
	Status struct {
		ExternalIdentifiers struct {
			K8sPodName string `json:"k8s-pod-name"`
		} `json:"external-identifiers"`
		Networking struct {
			Addressing []struct {
				IPv4 string `json:"ipv4"`
			} `json:"addressing"`
		} `json:"networking"`
	} `json:"status"`
}

func main() {
	selector := envDefault("TARGET_POD_SELECTOR", "cross-backups-minio")
	rate := envDefault("RATE_LIMIT", "100mbit")
	checkInterval := envDuration("CHECK_INTERVAL", 10*time.Second)
	forceInterval := envDuration("FORCE_REFRESH_INTERVAL", 5*time.Minute)
	burst := burstFor(rate)

	slog.Info("tc-limiter starting",
		"selector", selector, "rate", rate, "burst", burst,
		"check_interval", checkInterval, "force_refresh_interval", forceInterval)

	httpc := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			DialContext: func(_ context.Context, _, _ string) (net.Conn, error) {
				return net.Dial("unix", ciliumSocket)
			},
		},
	}

	lastForce := time.Now()
	for {
		force := time.Since(lastForce) >= forceInterval
		applied, err := reconcile(httpc, selector, rate, burst, force)
		if err != nil {
			slog.Warn("reconcile failed", "error", err)
		}
		if applied && force {
			lastForce = time.Now()
		}
		time.Sleep(checkInterval)
	}
}

// reconcile locates the target pod, verifies the current policer, and re-applies
// it on drift, when missing, or when a forced refresh is due. It returns whether
// an apply happened.
func reconcile(httpc *http.Client, selector, rate string, burst int, force bool) (bool, error) {
	ip, err := targetPodIP(httpc, selector)
	if err != nil {
		return false, fmt.Errorf("query cilium endpoints: %w", err)
	}
	if ip == "" {
		slog.Debug("target pod not found yet", "selector", selector)
		return false, nil
	}

	ns, ok, err := netnsForIP(ip)
	if err != nil {
		return false, fmt.Errorf("locate netns for ip %s: %w", ip, err)
	}
	if !ok {
		slog.Debug("no netns matches pod ip yet", "ip", ip)
		return false, nil
	}

	current, err := currentRate(ns)
	if err != nil {
		slog.Warn("verify read failed, forcing apply", "netns", ns, "error", err)
		if aerr := applyLimit(ns, rate, burst); aerr != nil {
			return false, fmt.Errorf("apply: %w", aerr)
		}
		slog.Info("applied limit (verify unavailable)", "netns", ns, "ip", ip, "rate", rate)
		return true, nil
	}

	if !force && rateMatches(current, rate) {
		slog.Debug("limit intact", "netns", ns, "ip", ip, "rate", current)
		return false, nil
	}

	if err := applyLimit(ns, rate, burst); err != nil {
		return false, fmt.Errorf("apply: %w", err)
	}

	if got, verr := currentRate(ns); verr == nil {
		if rateMatches(got, rate) {
			slog.Info("applied and verified limit", "netns", ns, "ip", ip,
				"rate", rate, "previous", current, "forced", force)
		} else {
			slog.Warn("applied but post-verify mismatch", "netns", ns,
				"ip", ip, "rate", rate, "got", got)
		}
	} else {
		slog.Warn("applied but post-verify read failed", "netns", ns, "error", verr)
	}
	return true, nil
}

// targetPodIP queries the local Cilium agent for the first endpoint whose pod
// name contains the selector and returns its IPv4 address.
func targetPodIP(httpc *http.Client, selector string) (string, error) {
	resp, err := httpc.Get("http://cilium/v1/endpoint")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("cilium /v1/endpoint status %s", resp.Status)
	}
	var eps []ciliumEndpoint
	if err := json.NewDecoder(resp.Body).Decode(&eps); err != nil {
		return "", err
	}
	for _, e := range eps {
		if !strings.Contains(e.Status.ExternalIdentifiers.K8sPodName, selector) {
			continue
		}
		if len(e.Status.Networking.Addressing) > 0 && e.Status.Networking.Addressing[0].IPv4 != "" {
			return e.Status.Networking.Addressing[0].IPv4, nil
		}
	}
	return "", nil
}

// netnsForIP scans the CNI named netns for one whose eth0 IPv4 matches ip.
func netnsForIP(ip string) (string, bool, error) {
	matches, err := filepath.Glob(filepath.Join(netnsDir, "cni-*"))
	if err != nil {
		return "", false, err
	}
	for _, m := range matches {
		ns := filepath.Base(m)
		out, err := exec.Command("ip", "netns", "exec", ns, "ip", "-o", "-4", "addr", "show", "eth0").CombinedOutput()
		if err != nil {
			continue // transient netns; try the next
		}
		if ipv4FromAddrOut(out) == ip {
			return ns, true, nil
		}
	}
	return "", false, nil
}

func ipv4FromAddrOut(b []byte) string {
	fields := strings.Fields(string(b))
	for i, f := range fields {
		if f == "inet" && i+1 < len(fields) {
			return strings.Split(fields[i+1], "/")[0]
		}
	}
	return ""
}

// currentRate returns the policer rate currently on eth0 ingress (e.g. "100Mbit"),
// or "" if none is configured.
func currentRate(ns string) (string, error) {
	out, err := exec.Command("ip", "netns", "exec", ns, "tc", "filter", "show", "dev", "eth0", "ingress").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("tc filter show: %w: %s", err, strings.TrimSpace(string(out)))
	}
	s := string(out)
	i := strings.Index(s, "rate ")
	if i < 0 {
		return "", nil
	}
	rest := s[i+len("rate "):]
	if j := strings.IndexByte(rest, ' '); j >= 0 {
		rest = rest[:j]
	}
	return strings.TrimSpace(rest), nil
}

func rateMatches(current, desired string) bool {
	if current == "" {
		return false
	}
	return strings.EqualFold(current, desired)
}

func applyLimit(ns, rate string, burst int) error {
	if err := ensureClsact(ns); err != nil {
		return err
	}
	out, err := exec.Command("ip", "netns", "exec", ns, "tc", "filter", "replace", "dev", "eth0",
		"ingress", "pref", "1", "proto", "ip", "matchall",
		"action", "police", "rate", rate, "burst", strconv.Itoa(burst), "conform-exceed", "drop",
	).CombinedOutput()
	if err != nil {
		return fmt.Errorf("tc filter replace: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func ensureClsact(ns string) error {
	out, err := exec.Command("ip", "netns", "exec", ns, "tc", "qdisc", "add", "dev", "eth0", "clsact").CombinedOutput()
	if err != nil {
		msg := string(out)
		if strings.Contains(msg, "exists") || strings.Contains(msg, "File exists") {
			return nil // already present
		}
		return fmt.Errorf("tc qdisc add clsact: %w: %s", err, strings.TrimSpace(msg))
	}
	return nil
}

// burstFor picks a policer burst of ~4 ms of traffic (min 4 MTUs), which is far
// smoother than the single-packet burst (1540) the old version used.
func burstFor(rate string) int {
	const mtu = 1500
	bps := parseBits(rate)
	if bps <= 0 {
		return 4 * mtu
	}
	burst := int(float64(bps) / 8 * 0.004)
	if burst < 4*mtu {
		burst = 4 * mtu
	}
	return burst
}

func parseBits(rate string) int64 {
	s := strings.ToLower(strings.TrimSpace(rate))
	var mult int64 = 1
	num := s
	switch {
	case strings.HasSuffix(s, "gbit"):
		mult, num = 1_000_000_000, strings.TrimSuffix(s, "gbit")
	case strings.HasSuffix(s, "mbit"):
		mult, num = 1_000_000, strings.TrimSuffix(s, "mbit")
	case strings.HasSuffix(s, "kbit"):
		mult, num = 1_000, strings.TrimSuffix(s, "kbit")
	case strings.HasSuffix(s, "g"):
		mult, num = 1_000_000_000, strings.TrimSuffix(s, "g")
	case strings.HasSuffix(s, "m"):
		mult, num = 1_000_000, strings.TrimSuffix(s, "m")
	case strings.HasSuffix(s, "k"):
		mult, num = 1_000, strings.TrimSuffix(s, "k")
	}
	n, err := strconv.ParseInt(strings.TrimSpace(num), 10, 64)
	if err != nil {
		return 0
	}
	return n * mult
}

func envDefault(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envDuration(k string, d time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if dur, err := time.ParseDuration(v); err == nil {
			return dur
		}
		if n, err := strconv.Atoi(v); err == nil {
			return time.Duration(n) * time.Second
		}
	}
	return d
}
