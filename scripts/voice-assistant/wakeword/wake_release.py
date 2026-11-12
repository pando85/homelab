#!/usr/bin/env python3
"""Release driver for a custom wake word: from real takes to a trial on one TV, almost automatic.

    python3 wake_release.py <step> --version <version> [options]

Steps (each idempotent; each prints what it will do and records its result in
$KIVI_HOME/releases/<version>/state.json (~/kivi-models/data); `--plan` prints and stops):

    init            create the release state: model id, phrase, candidate directory
    record-check    prerequisites, and the exact owner commands for a PC session and c3-b button takes
    extract         kivi_extract.py for one PC session or one button-take window (records the session id),
                    or --adopt-session to record sessions an earlier release already extracted
    pick-threshold  v4_select.py on the room SELECTION part only (or an owner-given threshold)
    calibrate       export the calibrated TFLite/ONNX pair, calibration sidecar and SHA256SUMS
    score           kivi_score.py per session (recall per speaker, source, condition) and v3_room.py on
                    the room TEST part (false wakes per hour)
    gate            recall >= 80 % TV silent, >= 60 % TV on, <= 0.25 false wakes/h, with intervals
    package         version directory, SHA256SUMS, model card (template) and models.lock in kivi-models
    release         OWNER GATE: commit, push, push the one version tag, watch CI, digest, anonymous-pull check
    deploy          homelab values bump, push, Argo CD rollout, in-pod sha256 and Wyoming describe check
    enable          OWNER GATE: trial pipeline (copy of the base pipeline, wake word changed) on one TV
    rollback        that TV back to its previous pipeline (always allowed)
    promote         OWNER GATE: the model becomes the wake word of the household (preferred) pipeline; TVs on
                    the trial pipeline go back to it; open runs restarted
    demote          undo promote: the household pipeline's wake word back to the previous one (always allowed)
    status          what is live and where: lock, image pin, files in the pod, pipelines, TV selects

The existing tools are called, not re-implemented. Runbook: kivi-models docs/runbook/wake-word-release.md.
Data rules: aggregates only; no audio is played, printed or transcribed; speakers are spk01, spk02.
Run with the system python3 (it has websocket-client); the tools run in $WORK/.venv inside a light
systemd scope (MemoryMax 6G, CPUQuota 400 %, nice 10).
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOMELAB = HERE.parents[2]


def _home():
    """Workspace: $KIVI_HOME, else $WORK (older), else ~/kivi-models/data, else the legacy /work path (same rule as
    kivi_real_common.kivi_home; this file runs on the system python, which has no numpy)."""
    for c in (os.environ.get("KIVI_HOME"), os.environ.get("WORK"), "~/kivi-models/data", "/work/workspaces/kivi-wakeword"):
        if c and os.path.isdir(os.path.expanduser(c)):
            return Path(os.path.expanduser(c))
    return Path("/work/workspaces/kivi-wakeword")


def _models_repo(home):
    for c in (os.environ.get("KIVI_MODELS"), "~/kivi-models", str(home / "repo/kivi-models")):
        if c and (Path(c).expanduser() / "models.lock").exists():
            return Path(c).expanduser()
    return home / "repo/kivi-models"


WORK = _home()
RELEASES = Path(os.environ.get("WAKE_RELEASES", WORK / "releases"))
KIVI_MODELS = _models_repo(WORK)
VALUES = HOMELAB / "apps/home-assistant/values.yaml"
PY = os.environ.get("WAKE_PY", str(WORK / ".venv/bin/python"))
PY_TF = os.environ.get("WAKE_PY_TF", str(WORK / ".venv-tf/bin/python"))
DATASET = Path(os.environ.get("KIVI_DATASET", WORK / "datasets/real"))
ROOM_SPLIT = WORK / "v3/room_split.json"
ROOM_SEL = [WORK / "v3/feat/room_c3-a_sel.npz", WORK / "v3/feat/room_c3-b_sel.npz"]

KUBE = ["kubectl", "--context", "grigri"]
NS = "home-assistant"
OWW_DEPLOY = "home-assistant-openwakeword"
ARGO_APP = ("argocd", "home-assistant")
HA_URL = os.environ.get("HA_URL", "https://hass.grigri.cloud")
FORGEJO = "https://git.grigri.cloud"
REPO = "agil/kivi-models"
IMAGE = "git.grigri.cloud/agil/kivi-models"
SSH_REMOTE = "ssh://git@git.grigri.cloud:29518/agil/kivi-models.git"
FJ_KEYS = Path.home() / ".local/share/forgejo-cli/keys.json"
NAME = "cand"                                   # the model's name inside kivi_score / v3_room / v4_select
GRID = [0.5, 0.7, 0.8, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99]
GATE = {"tv-off": 0.80, "tv-on": 0.60, "fa_per_h": 0.25, "min_takes_per_condition": 10,
        "min_speakers": 2, "min_room_devices": 2}
ACCEPT = ",".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                   "application/vnd.docker.distribution.manifest.v2+json",
                   "application/vnd.docker.distribution.manifest.list.v2+json"])
VERSION_RE = re.compile(r"^v[0-9]+(-[a-z0-9]+)*-[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ID_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

PLAN = False                                    # --plan: print the actions, do none


class Refused(SystemExit):
    """A step refused to run (owner gate, missing input, guard)."""


def say(msg):
    print(msg, flush=True)


def will(msg):
    """Announce an action. Returns False under --plan (the caller then skips the action)."""
    say(("PLAN  " if PLAN else "will  ") + msg)
    return not PLAN


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- state

def release_dir(version):
    check_version(version)
    return RELEASES / version


def check_version(version):
    if not VERSION_RE.match(version or ""):
        raise Refused(f"bad version {version!r}: v<N>-<slug>-<YYYY-MM-DD>, e.g. v5-oye-kivi-2026-11-20")


def load_state(version, must_exist=True):
    p = release_dir(version) / "state.json"
    if not p.exists():
        if must_exist:
            raise Refused(f"no release state {p}: run `init` first")
        return {"version": version, "config": {}, "steps": {}, "sessions": [], "log": []}
    return json.loads(p.read_text())


def save_state(state):
    if PLAN:
        return
    d = release_dir(state["version"])
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, d / "state.json")


def record(state, step, **fields):
    entry = dict(fields, at=now_utc())
    state["steps"][step] = entry
    state["log"].append({"step": step, "at": entry["at"], "summary": fields.get("summary", "")})
    save_state(state)
    return entry


# ------------------------------------------------------------------------------------------ helpers

def run(cmd, check=True, capture=False, cwd=None, input_text=None, env=None):
    """Run a command (list). Never through a shell."""
    r = subprocess.run(cmd, check=False, text=True, cwd=cwd, input=input_text, env=env,
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if check and r.returncode != 0:
        err = (r.stderr or "").strip()[-2000:] if capture else ""
        raise Refused(f"command failed ({r.returncode}): {shlex.join(cmd)}\n{err}")
    return r


def light(cmd):
    """Wrap a tool run in the shared-PC limits (unless WAKE_RELEASE_NO_SCOPE is set)."""
    if os.environ.get("WAKE_RELEASE_NO_SCOPE"):
        return ["nice", "-n", "10"] + cmd
    return ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=6G", "-p", "CPUQuota=400%",
            "nice", "-n", "10"] + cmd


def tool(script, *args):
    return light([PY, "-I", str(HERE / script), *map(str, args)])


def thr_key(d, t):
    """The key of threshold `t` in a tool's JSON dict (keys are formatted floats)."""
    for k in d:
        try:
            if abs(float(k) - float(t)) < 1e-9:
                return k
        except ValueError:
            continue
    return None


def wilson(k, n, z=1.959964):
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def _poisson_cdf(k, lam):
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    s = term
    for i in range(1, k + 1):
        term *= lam / i
        s += term
    return min(1.0, s)


def poisson_ci(k, alpha=0.05):
    """Exact (Garwood) 95 % interval of a Poisson count, by bisection (no scipy)."""
    def solve(f, lo, hi):
        for _ in range(200):
            mid = (lo + hi) / 2
            if f(mid):
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2
    upper = solve(lambda lam: _poisson_cdf(k, lam) <= alpha / 2, 0.0, 10.0 * k + 20)
    lower = 0.0 if k == 0 else solve(lambda lam: 1 - _poisson_cdf(k - 1, lam) >= alpha / 2, 0.0, 10.0 * k + 20)
    return [lower, upper]


def device_class_of(source):
    """tv / pc-desk / pc-webcam / phone / other, derived from a dataset source label (old score summaries
    have no device_class field). Same rule as kivi_real_common.device_class."""
    s = source or ""
    return "tv" if s.startswith("tv-") else {"pc-mic": "pc-desk", "webcam-mic": "pc-webcam", "phone": "phone"}.get(s, "other")


def row_class(r):
    return r.get("device_class") or device_class_of(r.get("source"))


def condition_of(text):
    t = (text or "").lower()
    if "tv-off" in t or "tv_off" in t or "tvoff" in t:
        return "tv-off"
    if "tv-on" in t or "tv_on" in t or "tvon" in t:
        return "tv-on"
    return "other"


# ------------------------------------------------------------------------------- kivi-models helpers

def read_lock(path):
    """models.lock: {id: (version, sha256)} in file order."""
    out = {}
    for line in Path(path).read_text().splitlines():
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        if len(f) != 3 or not ID_RE.match(f[0]) or not VERSION_RE.match(f[1]) or not re.fullmatch(r"[0-9a-f]{64}", f[2]):
            raise Refused(f"malformed models.lock line: {line!r}")
        if f[0] in out:
            raise Refused(f"duplicate id {f[0]} in models.lock")
        out[f[0]] = (f[1], f[2])
    return out


def update_lock_text(text, model_id, version, sha):
    """Set the line of `model_id` (replace it, or append it). Comments and order are kept."""
    lines = text.splitlines()
    new = f"{model_id:<9} {version:<23} {sha}"
    for i, line in enumerate(lines):
        f = line.split()
        if f and not f[0].startswith("#") and f[0] == model_id:
            lines[i] = new
            break
    else:
        lines.append(new)
    return "\n".join(lines) + "\n"


def read_sums(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        f = line.split()
        if len(f) == 2:
            out[f[1].lstrip("*")] = f[0]
    return out


# ---------------------------------------------------------------------------------- homelab values

PIN_RE = re.compile(r"(repository: git\.grigri\.cloud/agil/kivi-models\n\s+tag: )(\S+)")


def current_pin(values_text):
    m = PIN_RE.search(values_text)
    if not m:
        raise Refused("no kivi-models image pin found in values.yaml")
    return m.group(2)


def bump_pin(values_text, release, digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise Refused(f"bad digest {digest!r}")
    if "cp /models/*.tflite /custom/" not in values_text:
        raise Refused("the init container does not copy /models/*.tflite (multi-model command missing)")
    new = f"{release}@{digest}"
    return PIN_RE.sub(lambda m: m.group(1) + new, values_text, count=1), new


# ------------------------------------------------------------------------------------- HA (websocket)

class HA:
    """Home Assistant over REST/websocket. Token: --ha-token-file / HA_TOKEN_FILE, else the documented
    headless login (pass `web/hass.grigri.cloud`, homelab docs/troubleshooting/home-assistant-api-hacs-deploy.md);
    the login's refresh token is revoked on exit. Never prints a token. Never writes on the HA pod."""

    def __init__(self, token_file=None, url=HA_URL):
        self.url = url.rstrip("/")
        self.token_file = token_file or os.environ.get("HA_TOKEN_FILE")
        self.refresh = None
        self.ws = None
        self.i = 0

    def _post(self, path, data, form=False):
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        ctype = "application/x-www-form-urlencoded" if form else "application/json"
        req = urllib.request.Request(self.url + path, body, {"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    def __enter__(self):
        import websocket  # websocket-client; only needed for HA steps
        if self.token_file:
            access = Path(self.token_file).read_text().strip()
        else:
            pw = run(["pass", "show", "web/hass.grigri.cloud"], capture=True).stdout.splitlines()[0]
            cid = self.url + "/"
            flow = self._post("/auth/login_flow", {"client_id": cid, "handler": ["homeassistant", None],
                                                   "redirect_uri": cid + "?auth_callback=1"})
            res = self._post(f"/auth/login_flow/{flow['flow_id']}", {"username": "agil", "password": pw, "client_id": cid})
            del pw
            if "result" not in res:
                raise Refused("Home Assistant login failed (no MFA expected; check the pass entry)")
            tok = self._post("/auth/token", {"grant_type": "authorization_code", "code": res["result"], "client_id": cid},
                             form=True)
            access, self.refresh = tok["access_token"], tok.get("refresh_token")
        self.ws = websocket.create_connection(self.url.replace("http", "ws", 1) + "/api/websocket", timeout=30)
        json.loads(self.ws.recv())
        self.ws.send(json.dumps({"type": "auth", "access_token": access}))
        if json.loads(self.ws.recv()).get("type") != "auth_ok":
            raise Refused("Home Assistant websocket auth failed")
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(Exception):
            self.ws.close()
        if self.refresh:
            try:
                self._post("/auth/revoke", {"token": self.refresh}, form=True)
                say("HA: login session revoked")
            except Exception as e:  # noqa: BLE001
                say(f"HA: WARNING could not revoke the login session ({type(e).__name__}); revoke it in the profile")

    def call(self, msg_type, **kw):
        self.i += 1
        self.ws.send(json.dumps(dict(kw, id=self.i, type=msg_type)))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.i and r.get("type") == "result":
                if not r.get("success"):
                    raise Refused(f"HA {msg_type} failed: {r.get('error')}")
                return r.get("result")

    # higher level
    def pipelines(self):
        return self.call("assist_pipeline/pipeline/list")

    def states(self):
        return {s["entity_id"]: s for s in self.call("get_states")}

    def wake_words(self, entity="wake_word.openwakeword"):
        return [w["id"] for w in self.call("wake_word/info", entity_id=entity)["wake_words"]]

    def runs(self, pipeline_id):
        """The debug run list of a pipeline: [{pipeline_run_id, timestamp}]."""
        return self.call("assist_pipeline/pipeline_debug/list", pipeline_id=pipeline_id)["pipeline_runs"]

    def run_events(self, pipeline_id, run_id):
        return self.call("assist_pipeline/pipeline_debug/get", pipeline_id=pipeline_id,
                         pipeline_run_id=run_id)["events"]

    def kick(self, pipeline):
        """A pipeline update with every field unchanged: Home Assistant invalidates the open runs of that
        pipeline, so the satellites start new runs (and re-read their pipeline select)."""
        fields = {k: v for k, v in pipeline.items() if k != "id"}
        self.call("assist_pipeline/pipeline/update", pipeline_id=pipeline["id"], **fields)
        after = next(p for p in self.pipelines()["pipelines"] if p["id"] == pipeline["id"])
        if after != pipeline:
            raise Refused(f"pipeline {pipeline['name']} changed on a no-op update: {pipeline} -> {after}")


PIPELINE_FIELDS = ("conversation_engine", "conversation_language", "language", "stt_engine", "stt_language",
                   "tts_engine", "tts_language", "tts_voice", "wake_word_entity", "wake_word_id", "prefer_local_intents")


def trial_pipeline_fields(base, name, wake_word_id):
    """A copy of `base` with only the name and the wake word id changed."""
    out = {k: base.get(k) for k in PIPELINE_FIELDS if k in base}
    out["name"] = name
    out["wake_word_id"] = wake_word_id
    return out


def find_pipeline(plist, name_or_id):
    for p in plist["pipelines"]:
        if name_or_id in (p["name"], p["id"]):
            return p
    return None


def resolve_option(plist, option):
    """The pipeline a satellite select option means ('preferred' or a pipeline name)."""
    if option == "preferred":
        return find_pipeline(plist, plist["preferred_pipeline"])
    return find_pipeline(plist, option)


def devices_map():
    return {k: v["device_id_prefix"] for k, v in json.loads((HERE / "kivi_devices.json").read_text()).items()
            if not k.startswith("_")}


def tv_entities(registry, tv, devices=None):
    """The pipeline select and the microphone-muted sensor of a satellite, from the entity registry."""
    prefix = (devices or devices_map()).get(tv)
    if not prefix:
        raise Refused(f"unknown TV {tv!r} (kivi_devices.json)")
    sel = [e for e in registry if e.get("platform") == "esphome" and e["entity_id"].startswith("select.")
           and (e.get("device_id") or "").startswith(prefix) and e.get("unique_id", "").endswith("-pipeline")]
    if len(sel) != 1:
        raise Refused(f"{tv}: expected one pipeline select, found {[e['entity_id'] for e in sel]}")
    mute = [e["entity_id"] for e in registry if e["entity_id"].startswith("binary_sensor.")
            and (e.get("device_id") or "") == sel[0]["device_id"] and e["entity_id"].endswith("microphone_muted")]
    sat = [e["entity_id"] for e in registry if e["entity_id"].startswith("assist_satellite.")
           and (e.get("device_id") or "") == sel[0]["device_id"]]
    return {"select": sel[0]["entity_id"], "muted": mute[0] if mute else None, "device_id": sel[0]["device_id"],
            "satellite": sat[0] if sat else None}


def preflight_pipeline(pipeline, states, served, force=False):
    """Refuse to point a satellite at a pipeline that cannot run: a conversation/stt/tts engine that is an
    entity but `unavailable` (or missing), or a wake word id the wake word service does not serve.
    `pipeline` is a pipeline dict (or the fields of one), `states` the HA states by entity id, `served` the
    served wake word ids. --force skips the refusal, not the message."""
    problems = []
    for key in ("conversation_engine", "stt_engine", "tts_engine"):
        eng = pipeline.get(key)
        if not eng or "." not in eng:          # unset, or a legacy engine id that is not an entity
            continue
        state = (states.get(eng) or {}).get("state")
        if state is None:
            problems.append(f"{key} {eng} does not exist in Home Assistant")
        elif state == "unavailable":
            problems.append(f"{key} {eng} is unavailable")
    ww = pipeline.get("wake_word_id")
    if ww and ww not in served:
        problems.append(f"wake_word_id {ww!r} is not served (served: {', '.join(served)})")
    if problems:
        msg = f"pipeline {pipeline.get('name')!r} cannot run: " + "; ".join(problems)
        if not force:
            raise Refused(msg + " (--force to switch anyway)")
        say(f"WARNING {msg}; --force given")
    return problems


def check_run(ha, pipeline_id, satellite, since, timeout_s=30, poll_s=2):
    """The newest run of `satellite` on the pipeline that started after `since`: ok only when it contains
    wake_word-start and no error event. Retries for `timeout_s`. Returns (ok, detail); detail is the error
    code when the run errored, else a short reason."""
    end = time.time() + timeout_s
    detail = f"no run of {satellite or 'any satellite'} on the pipeline"
    while True:
        runs = [r for r in ha.runs(pipeline_id) if dt.datetime.fromisoformat(r["timestamp"]) > since]
        for r in sorted(runs, key=lambda r: r["timestamp"], reverse=True):
            events = ha.run_events(pipeline_id, r["pipeline_run_id"])
            start = next((e for e in events if e.get("type") == "run-start"), None)
            if satellite and (start or {}).get("data", {}).get("satellite_id") != satellite:
                continue                     # another satellite's run
            err = next((e for e in events if e.get("type") == "error"), None)
            if err:
                detail = (err.get("data") or {}).get("code") or "error"
            elif any(e.get("type") == "wake_word-start" for e in events):
                return True, "ok"
            else:
                detail = "newest run has no wake_word-start yet"
            break                            # only the newest run of that satellite counts
        if time.time() >= end:
            return False, detail
        time.sleep(poll_s)


def served_for(ha, pipeline):
    ent = pipeline.get("wake_word_entity")
    return ha.wake_words(ent) if ent else ha.wake_words()


def verify_run(ha, dest, tv, ent, t0, rollback_hint, timeout_s=30):
    """check_run for one TV; prints the result. Returns True when the run is ok. On failure prints
    `FAILED: <code>` and the command that puts the TV back (the select is left as it is)."""
    ok, detail = check_run(ha, dest["id"], ent.get("satellite"), t0, timeout_s)
    if ok:
        say(f"check: {tv}: newest run on {dest['name']!r} has wake_word-start and no error: OK")
        return True
    say(f"FAILED: {detail}")
    say(f"check: {tv}: no healthy run on {dest['name']!r} within {timeout_s} s; the select was left as it is")
    say(f"Rollback: {rollback_hint}")
    return False


# ------------------------------------------------------------------------------ Forgejo and registry

def forgejo_token():
    f = os.environ.get("FORGEJO_TOKEN_FILE")
    if f:
        return Path(f).read_text().strip()
    try:
        return json.loads(FJ_KEYS.read_text())["hosts"]["git.grigri.cloud"]["token"]
    except (OSError, KeyError, ValueError):
        raise Refused(f"no Forgejo token: log in with `fj auth login` or set FORGEJO_TOKEN_FILE ({FJ_KEYS})")


def http(url, headers=None, method="GET", auth=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def api(path, token):
    code, _, body = http(f"{FORGEJO}/api/v1/repos/{REPO}{path}", {"Authorization": f"token {token}"})
    if code != 200:
        raise Refused(f"Forgejo API {path}: HTTP {code}")
    return json.loads(body)


def registry_digest(tag, token):
    _, _, body = http(f"https://git.grigri.cloud/v2/token?service=container_registry&scope=repository:{REPO}:pull",
                      auth=("agil", token))
    bearer = json.loads(body).get("token")
    code, headers, _ = http(f"https://git.grigri.cloud/v2/{REPO}/manifests/{tag}",
                            {"Accept": ACCEPT, "Authorization": f"Bearer {bearer}"}, method="HEAD")
    if code != 200:
        return None
    return {k.lower(): v for k, v in headers.items()}.get("docker-content-digest")


def anonymous_refused(tag):
    """True when the registry refuses an anonymous manifest request (with and without an anonymous token)."""
    code1, _, _ = http(f"https://git.grigri.cloud/v2/{REPO}/manifests/{tag}", {"Accept": ACCEPT}, method="HEAD")
    _, _, body = http(f"https://git.grigri.cloud/v2/token?scope=repository:{REPO}:pull")
    try:
        anon = json.loads(body).get("token")
    except ValueError:
        anon = None
    code2 = None
    if anon:
        code2, _, _ = http(f"https://git.grigri.cloud/v2/{REPO}/manifests/{tag}",
                           {"Accept": ACCEPT, "Authorization": f"Bearer {anon}"}, method="HEAD")
    return code1 != 200 and code2 != 200, (code1, code2)


def watch_ci(version, token, timeout_s=900):
    """Wait for the tag-push run of `version`; return (status, run id, job log lines of interest)."""
    end = time.time() + timeout_s
    run_ = None
    while time.time() < end:
        runs = api("/actions/runs?limit=30", token)["workflow_runs"]
        cands = [r for r in runs if r.get("prettyref") == version and r.get("trigger_event") == "push"]
        if cands:
            run_ = max(cands, key=lambda r: r["id"])
            if run_["status"] in ("success", "failure", "cancelled", "skipped"):
                break
        time.sleep(10)
    if not run_:
        return "not-found", None, []
    jobs = api(f"/actions/runs/{run_['id']}/jobs", token)
    lines = []
    if jobs:
        code, _, body = http(f"{FORGEJO}/api/v1/repos/{REPO}/actions/jobs/{jobs[0]['id']}/logs",
                             {"Authorization": f"token {token}"})
        if code == 200:
            lines = [ln.split(" ", 1)[-1] for ln in body.decode(errors="replace").splitlines()
                     if re.search(r"pushed |anonymous manifest|PUBLIC|refusing|model \S+ v|staged|: OK$", ln)]
    return run_["status"], run_["index_in_repo"], lines


# --------------------------------------------------------------------------------------- the cluster

def kube_json(*args):
    return json.loads(run(KUBE + list(args) + ["-o", "json"], capture=True).stdout)


def pod_model_sums():
    out = run(KUBE + ["-n", NS, "exec", f"deploy/{OWW_DEPLOY}", "-c", "openwakeword", "--",
                      "sh", "-c", "sha256sum /custom/*.tflite"], capture=True).stdout
    return {Path(f[1]).stem: f[0] for f in (ln.split() for ln in out.splitlines()) if len(f) == 2}


DESCRIBE = """
import asyncio
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
async def m():
    async with AsyncTcpClient("127.0.0.1", 10400) as c:
        await c.write_event(Describe().event())
        while True:
            e = await c.read_event()
            if Info.is_type(e.type):
                print(" ".join(sorted(x.name for w in Info.from_event(e).wake for x in w.models))); return
asyncio.run(m())
"""


def wyoming_ids():
    out = run(KUBE + ["-n", NS, "exec", f"deploy/{OWW_DEPLOY}", "-c", "openwakeword", "--",
                      "/usr/src/.venv/bin/python3", "-I", "-c", DESCRIBE], capture=True).stdout
    return out.split()


# ============================================================================================== steps

def cmd_init(a):
    check_version(a.version)
    if not ID_RE.match(a.model_id):
        raise Refused(f"bad model id {a.model_id!r}: lower case words joined by _, it becomes the file stem")
    if re.fullmatch(r"[^_]+_v[0-9.]+", a.model_id):
        raise Refused("ids like name_v1 lose their suffix in wyoming-openwakeword 2.1.0; choose another id")
    cand = Path(a.candidate).resolve()
    if not cand.is_dir():
        raise Refused(f"candidate directory {cand} does not exist")
    cfg = {"model_id": a.model_id, "phrase": a.phrase, "phrase_norm": " ".join(a.phrase.lower().split()),
           "candidate": str(cand), "pipeline_name": a.pipeline_name or a.phrase,
           "base_pipeline": a.base_pipeline, "recipe": a.recipe or ""}
    st = load_state(a.version, must_exist=False)
    if st["config"] and st["config"] != cfg:
        if not a.update:
            raise Refused(f"release {a.version} exists with another config; pass --update to change it:\n"
                          f"  now {st['config']}\n  new {cfg}")
    if st["config"] == cfg:
        say(f"init: {a.version} already set up, nothing to do")
        return
    if will(f"create release state {release_dir(a.version)}/state.json for {a.model_id} (\"{a.phrase}\")"):
        st["config"] = cfg
        record(st, "init", summary=f"{a.model_id} from {cand}")


def cmd_record_check(a):
    st = load_state(a.version)
    cfg = st["config"]
    problems = []
    for what, ok in [(f"tools venv {PY}", Path(PY).exists()), (f"dataset {DATASET}", DATASET.is_dir()),
                     ("kivi_session.py", (HERE / "kivi_session.py").exists())]:
        say(f"check {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            problems.append(what)
    if DATASET.is_dir() and (DATASET.stat().st_mode & 0o077):
        problems.append(f"{DATASET} is readable by others (mode {oct(DATASET.stat().st_mode & 0o777)}, want 700)")
    devs = devices_map()
    for d in (a.pc_device, a.button_device):
        if d not in devs:
            problems.append(f"device {d} not in kivi_devices.json")
    r = run(KUBE + ["-n", NS, "get", "pod", "home-assistant-hass-0", "home-assistant-postgres-0", "-o", "name"],
            check=False, capture=True)
    say(f"check {'ok  ' if r.returncode == 0 else 'FAIL'} kubectl read access to the HA and Postgres pods")
    if r.returncode != 0:
        problems.append("kubectl access")
    live = {}
    if not a.no_ha:
        with HA(a.ha_token_file) as ha:
            plist, states, reg = ha.pipelines(), ha.states(), ha.call("config/entity_registry/list")
            for tv in (a.pc_device, a.button_device):
                ent = tv_entities(reg, tv, devs)
                opt = states.get(ent["select"], {}).get("state")
                p = resolve_option(plist, opt) if opt else None
                muted = states.get(ent["muted"], {}).get("state") if ent["muted"] else None
                live[tv] = {"pipeline": p and p["name"], "wake_word": p and p["wake_word_id"], "muted": muted}
                say(f"check live {tv}: pipeline {live[tv]['pipeline']!r}, wake word {live[tv]['wake_word']!r}, "
                    f"microphone muted: {muted}")
            if "input_button.kivi_take" not in states:
                problems.append("input_button.kivi_take missing in Home Assistant")
    mid, phrase = cfg["model_id"], cfg["phrase_norm"]
    if live.get(a.pc_device, {}).get("wake_word") == mid:
        say(f"\nNOTE {a.pc_device} runs {mid} live (trial): every \"{cfg['phrase']}\" will start the assistant. For a\n"
            f"     clean session run `wake_release.py rollback --version {a.version} --tv {a.pc_device}` first and\n"
            f"     `enable ... --yes-owner-approved` after; or keep it (the extractor counts live detections as run ends).")
    if live.get(a.button_device, {}).get("wake_word") == mid:
        say(f"NOTE {a.button_device} runs {mid} live: button takes would start the assistant; roll it back first.")
    H = "~/homelab/scripts/voice-assistant/wakeword"
    v = a.version
    say(f"""
=== Owner commands ({'prerequisites OK' if not problems else 'FIX FIRST: ' + '; '.join(problems)}) ===
Rules: consent of every speaker first; ids spk01/spk02 only, never names; nothing is listened to or transcribed.

# A. PC session, {a.pc_speaker} on {a.pc_device} (about 12 min). One session with --split train, another day one with --split test.
cd {H}
# 1. Unmute {a.pc_device} in Home Assistant (dashboard /voice-assistant).
python3 kivi_session.py --list-devices      # pick the hardware inputs (alsa_input.*)
python3 kivi_session.py --phrase "{phrase}" --model-name {mid} --device {a.pc_device} --speaker {a.pc_speaker} --split test \\
    --desk-mic {a.desk_mic} --webcam-mic {a.webcam_mic} --tag volume=normal
# 2. Right after (HA keeps debug recordings 90 days); then mute {a.pc_device} again:
python3 wake_release.py extract --version {v} --session-dir ~/kivi-sessions/<session id> --dry-run
python3 wake_release.py extract --version {v} --session-dir ~/kivi-sessions/<session id>

# B. Button takes, {a.button_speaker} on {a.button_device} (about 6 min), two windows: TV off, then TV on.
# Unmute {a.button_device}. Note the start time (local). Say "{cfg['phrase']}" once, THEN press "Kivi take"
# (dashboard /voice-assistant), wait about 5 s; 15 times with the TV off. Note the end time.
# Then TV on (normal volume, own content): note the start, 15 takes the same way, note the end. Mute {a.button_device}.
python3 wake_release.py extract --version {v} --button-takes --device {a.button_device} --speaker {a.button_speaker} \\
    --split test --condition tv-off --since 2026-MM-DDTHH:MM --until 2026-MM-DDTHH:MM --dry-run
python3 wake_release.py extract --version {v} --button-takes --device {a.button_device} --speaker {a.button_speaker} \\
    --split test --condition tv-off --since 2026-MM-DDTHH:MM --until 2026-MM-DDTHH:MM
python3 wake_release.py extract --version {v} --button-takes --device {a.button_device} --speaker {a.button_speaker} \\
    --split test --condition tv-on --since 2026-MM-DDTHH:MM --until 2026-MM-DDTHH:MM

# C. Then: python3 wake_release.py score --version {v} && python3 wake_release.py gate --version {v}
""")
    record(st, "record-check", problems=problems, live=live, summary="ok" if not problems else "; ".join(problems))
    if problems:
        raise Refused("prerequisites missing: " + "; ".join(problems))


def dataset_sessions():
    return {p.name for p in DATASET.iterdir() if p.is_dir()} if DATASET.is_dir() else set()


def cmd_adopt(a, st, cfg):
    """Record dataset sessions extracted by an earlier release into this release's state.

    Releases reuse the same real recordings: re-running `kivi_extract.py` would only make another revision
    of audio that is already aligned and marked. `score` reads the stored clips, so adopting is enough.
    """
    sys.path.insert(0, str(HERE))
    import kivi_real_common as C
    ids = []
    for sid in a.adopt_session:
        if not (DATASET / sid).is_dir():
            raise Refused(f"--adopt-session: {DATASET / sid} is not an extracted dataset session")
        rows = [r for r in C.read_manifest(DATASET)
                if r.get("session") == sid and not r.get("superseded") and C.row_phrase(r) == cfg["phrase_norm"]]
        if not rows:
            raise Refused(f"--adopt-session: {sid} has no rows for the phrase {cfg['phrase_norm']!r}")
        entry = {"id": sid, "kind": "session", "condition": None}
        if session_tv_channel(sid):
            entry["tv_channel"] = session_tv_channel(sid)
        st["sessions"] = [s for s in st["sessions"] if s["id"] != sid] + [entry]
        ids.append(sid)
        say(f"extract: adopted session {sid} ({len(rows)} rows of {cfg['phrase_norm']!r}, no re-extraction)")
    if will(f"record {len(ids)} already-extracted session(s): {', '.join(ids)}"):
        record(st, "extract", summary=f"adopted {', '.join(ids)}", sessions=ids)


def session_tv_channel(sid):
    """"missing" when the extraction found no satellite audio (the PC-only path), from the dataset session record."""
    p = DATASET / sid / "session.json"
    try:
        return (json.loads(p.read_text()).get("tv_channel") or {}).get("status")
    except (OSError, ValueError):
        return None


def cmd_extract(a):
    st = load_state(a.version)
    cfg = st["config"]
    if a.adopt_session:
        return cmd_adopt(a, st, cfg)
    if a.button_takes:
        for k in ("device", "speaker", "since", "until", "split", "condition"):
            if not getattr(a, k):
                raise Refused(f"--button-takes needs --{k}")
        args = ["--button-takes", "--device", a.device, "--speaker", a.speaker, "--since", a.since, "--until", a.until,
                "--split", a.split, "--phrase", cfg["phrase_norm"]]
    elif a.session_dir:
        args = ["--session-dir", os.path.expanduser(a.session_dir)]
    else:
        raise Refused("give --session-dir (PC session) or --button-takes")
    args += ["--model-name", cfg["model_id"]] + (["--dry-run"] if a.dry_run else []) + list(a.extra or [])
    before = dataset_sessions()
    if not will("kivi_extract.py " + shlex.join(args)):
        return
    run(tool("kivi_extract.py", *args))
    if a.dry_run:
        return
    new = sorted(dataset_sessions() - before)
    if not new and a.session_dir:
        sid = Path(os.path.expanduser(a.session_dir)).name
        new = [sid] if (DATASET / sid).is_dir() else []
    if not new:
        say("extract: no new session directory in the dataset (already extracted?); nothing recorded")
        return
    for sid in new:
        kind = "button" if a.button_takes else "session"
        entry = {"id": sid, "kind": kind, "condition": a.condition if a.button_takes else None}
        tvc = session_tv_channel(sid)
        if tvc:
            entry["tv_channel"] = tvc
        st["sessions"] = [s for s in st["sessions"] if s["id"] != sid] + [entry]
        say(f"extract: recorded {kind} {sid}" + (f" ({a.condition})" if a.button_takes else "")
            + ("  [TV channel MISSING: PC sources only, no gate input]" if tvc == "missing" else ""))
    record(st, "extract", summary=", ".join(new), sessions=new)


def candidate_files(cfg):
    c = Path(cfg["candidate"])
    mid = cfg["model_id"]
    onnx = next((p for p in [c / f"{mid}_uncalibrated.onnx", c / "kivi.onnx"] if p.exists()), None)
    tfl = c / f"{mid}_uncalibrated.tflite"
    return onnx, tfl if tfl.exists() else None


def cmd_pick_threshold(a):
    st = load_state(a.version)
    cfg = st["config"]
    if a.threshold is not None:
        if not a.reason:
            raise Refused("an owner-given --threshold needs --reason (who decided it and why)")
        if will(f"record threshold {a.threshold} (owner-given: {a.reason})"):
            record(st, "pick-threshold", threshold=a.threshold, source="owner", reason=a.reason,
                   summary=f"{a.threshold} (owner: {a.reason})")
        return
    rooms = [Path(r) for r in (a.room_sel or ROOM_SEL)]
    for r in rooms:
        if re.search(r"test", r.name):
            raise Refused(f"{r} looks like a TEST part: thresholds are picked on the selection part only")
        if not r.exists():
            raise Refused(f"missing room selection features {r}")
    onnx, _ = candidate_files(cfg)
    if not onnx:
        raise Refused(f"no uncalibrated ONNX in {cfg['candidate']}")
    out = release_dir(a.version) / "selection.json"
    args = ["--model", f"{NAME}={onnx}", "--max-fa", a.max_fa, "--json", out]
    for r in rooms:
        args += ["--room", r]
    args += list(a.extra or [])
    prev = st["steps"].get("pick-threshold")
    if prev and prev.get("source") == "selection" and prev.get("onnx_sha256") == sha256_file(onnx) and out.exists() \
            and prev.get("max_fa") == a.max_fa and not a.force:
        say(f"pick-threshold: already done ({prev['threshold']}); --force to redo")
        return
    if not will("v4_select.py " + shlex.join(map(str, args))):
        return
    run(tool("v4_select.py", *args))
    sel = json.loads(out.read_text())[NAME]
    t = round(float(sel["threshold"]), 4)
    say(f"pick-threshold: raw {t}, {sel['sel_fa']} false accepts in {sel['sel_hours']} h of the selection part")
    record(st, "pick-threshold", threshold=t, source="selection", max_fa=a.max_fa, onnx_sha256=sha256_file(onnx),
           sel_fa=sel["sel_fa"], sel_hours=sel["sel_hours"], summary=f"raw {t} at <= {a.max_fa} FA on the selection part")


CALIBRATE_ONNX = """
import math, sys, onnx
from onnx import numpy_helper as nh
src, dst, t = sys.argv[1], sys.argv[2], float(sys.argv[3])
m = onnx.load(src)
n = 0
for i in m.graph.initializer:
    if i.name == "last_layer.bias":
        b = nh.to_array(i) - math.log(t / (1 - t)); i.CopyFrom(nh.from_array(b.astype("float32"), i.name)); n += 1
assert n == 1, "no last_layer.bias"
onnx.save(m, dst)
"""


def cmd_calibrate(a):
    st = load_state(a.version)
    cfg = st["config"]
    pick = st["steps"].get("pick-threshold")
    if not pick:
        raise Refused("run pick-threshold first")
    t = pick["threshold"]
    mid = cfg["model_id"]
    onnx, _ = candidate_files(cfg)
    if not onnx:
        raise Refused(f"no uncalibrated ONNX in {cfg['candidate']}")
    out = release_dir(a.version) / "model"
    files = [f"{mid}.tflite", f"{mid}.onnx", f"{mid}_uncalibrated.tflite", f"{mid}_uncalibrated.onnx",
             f"{mid}_uncalibrated.calibration.json"]
    sidecar = out / f"{mid}_uncalibrated.calibration.json"
    if (out / "SHA256SUMS").exists() and sidecar.exists() and json.loads(sidecar.read_text()).get("server_0_5_raw") == t \
            and all((out / f).exists() for f in files) and not a.force:
        say(f"calibrate: {out} already calibrated at raw {t}")
        return
    check = ["--check-features", a.check_features] if a.check_features else []
    steps = [
        ("copy " + str(onnx), None),
        (f"bias shift -logit({t}) -> {mid}.onnx", None),
        (f"export_tflite.py uncalibrated and --calibrate-threshold {t}", None),
    ]
    for s, _ in steps:
        if not will(s):
            return
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(onnx, out / f"{mid}_uncalibrated.onnx")
    run(light([PY, "-I", "-c", CALIBRATE_ONNX, str(out / f"{mid}_uncalibrated.onnx"), str(out / f"{mid}.onnx"), str(t)]))
    exp = str(HERE / "export_tflite.py")
    run(light([PY_TF, exp, str(out / f"{mid}_uncalibrated.onnx"), str(out / f"{mid}_uncalibrated.tflite"), *check]))
    run(light([PY_TF, exp, str(out / f"{mid}_uncalibrated.onnx"), str(out / f"{mid}.tflite"),
               "--calibrate-threshold", str(t), *check]))
    sidecar.write_text(json.dumps({"server_0_5_raw": t}) + "\n")
    sums = "".join(f"{sha256_file(out / f)}  {f}\n" for f in files)
    (out / "SHA256SUMS").write_text(sums)
    say(sums.strip())
    record(st, "calibrate", threshold=t, dir=str(out), sha256=sha256_file(out / f"{mid}.tflite"),
           summary=f"{mid}.tflite at raw {t}")


def model_paths(st):
    """(uncalibrated tflite, threshold or None) for scoring: the calibrated release copy, else the candidate."""
    cfg = st["config"]
    mid = cfg["model_id"]
    cal = st["steps"].get("calibrate")
    if cal:
        return Path(cal["dir"]) / f"{mid}_uncalibrated.tflite", cal["threshold"]
    _, tfl = candidate_files(cfg)
    side = Path(cfg["candidate"]) / f"{mid}_uncalibrated.calibration.json"
    t = st["steps"].get("pick-threshold", {}).get("threshold")
    if t is None and side.exists():
        t = json.loads(side.read_text()).get("server_0_5_raw")
    return tfl, t


def manifest_info(session_id):
    """speaker, split and eval sources of a session, from the dataset manifest with the marks applied."""
    sys.path.insert(0, str(HERE))
    import kivi_real_common as C
    rows = [r for r in C.read_manifest(DATASET) if r.get("session") == session_id and not r.get("superseded")]
    info = {}
    for r in rows:
        s = info.setdefault(r.get("source"), {"speakers": set(), "splits": set()})
        s["speakers"].add(r.get("speaker"))
        s["splits"].add(r.get("split"))
    out = {}
    for src, v in info.items():
        splits = sorted(x for x in v["splits"] if x)
        # eval_channel is derived (a satellite always; any other source when its split is test), not read
        # from the stored field: rows written before the 2026-11-11 rule change say False for PC sources
        out[src] = {"speaker": "/".join(sorted(x for x in v["speakers"] if x)), "split": "/".join(splits),
                    "device_class": C.device_class(src), "eval_channel": C.is_eval_channel(src, splits[0] if len(splits) == 1 else None)}
    return out


def summarize_session(js, threshold, info):
    """Rows {source, speaker, split, condition, eval_channel, k, n} from a kivi_score.py session JSON (list)."""
    rows = []
    for e in js if isinstance(js, list) else [js]:
        src = e.get("source")
        meta = info.get(src, {})
        for cond, v in e["overall"]["kivi_by_condition"].items():
            if cond == "ALL":
                continue
            hits = v["cue_hits"][NAME]
            k = thr_key(hits, threshold)
            if k is None:
                raise Refused(f"threshold {threshold} missing in the score of {e.get('session')}; run score again")
            rows.append({"session": e.get("session"), "source": src, "speaker": meta.get("speaker") or e.get("speaker"),
                         "split": meta.get("split") or e.get("split"), "eval_channel": meta.get("eval_channel", True),
                         "device_class": meta.get("device_class") or device_class_of(src),
                         "condition": condition_of(cond), "condition_raw": cond,
                         "k": hits[k]["k"], "n": hits[k]["n"]})
    return rows


def summarize_button(js, threshold, info, condition):
    rec = js["recall"][NAME]
    k = thr_key(rec, threshold)
    if k is None:
        raise Refused(f"threshold {threshold} missing in the score of {js.get('session')}; run score again")
    src, meta = next(iter(info.items())) if info else (None, {})
    return [{"session": js["session"], "source": src, "speaker": meta.get("speaker"), "split": meta.get("split"),
             "eval_channel": meta.get("eval_channel", True), "device_class": meta.get("device_class") or device_class_of(src),
             "condition": condition or "other",
             "condition_raw": condition, "k": rec[k]["all_takes"]["k"], "n": rec[k]["all_takes"]["n"]}]


def summarize_room(js, threshold):
    out = {}
    for scope, v in js["scopes"].items():
        fa = v["false_accepts"][NAME]
        k = thr_key(fa, threshold)
        if k is None:
            raise Refused(f"threshold {threshold} missing in the room score; run score again")
        out[scope] = {"hours": v["hours"], "k": fa[k]["k"], "per_hour": round(fa[k]["k"] / v["hours"], 3) if v["hours"] else None,
                      "ci95": [round(x / v["hours"], 3) for x in poisson_ci(fa[k]["k"])] if v["hours"] else None}
    return out


def cmd_score(a):
    st = load_state(a.version)
    tfl, t = model_paths(st)
    if not tfl or not tfl.exists():
        raise Refused("no uncalibrated TFLite (candidate or calibrate output)")
    if t is None:
        raise Refused("no threshold yet: run pick-threshold (or give the candidate a calibration sidecar)")
    grid = sorted(set(GRID + [t]))
    d = release_dir(a.version) / "scores"
    sessions = [s for s in st["sessions"] if not a.session or s["id"] in a.session]
    if not sessions and a.no_room:
        raise Refused("nothing to score: no extracted sessions in the release state")
    rows = []
    for s in sessions:
        out = d / f"{s['id']}.json"
        args = ["--model", f"{NAME}={tfl}", "--calibration", f"{NAME}={t}", "--label", a.version,
                "--thresholds", ",".join(map(str, grid)), "--json", out, "--md", out.with_suffix(".md")]
        args = (["--button-takes", DATASET / s["id"]] if s["kind"] == "button" else ["--session", DATASET / s["id"], "--also-pc"]) + args
        if out.exists() and not a.force:
            say(f"score: {s['id']} already scored ({out.name}); --force to redo")
        elif will("kivi_score.py " + shlex.join(map(str, args))):
            d.mkdir(parents=True, exist_ok=True)
            run(tool("kivi_score.py", *args))
        if out.exists():
            js = json.loads(out.read_text())
            info = manifest_info(s["id"])
            rows += summarize_button(js, t, info, s.get("condition")) if s["kind"] == "button" else summarize_session(js, t, info)
    room = None
    if not a.no_room:
        out = d / "room_test.json"
        args = ["score", "--split", a.room_split, "--part", "test", "--model", f"{NAME}={tfl}",
                "--thresholds", ",".join(map(str, grid)), "--jobs", a.jobs, "--json", out]
        if out.exists() and not a.force:
            say("score: room test part already scored; --force to redo")
        elif will("v3_room.py " + shlex.join(map(str, args))):
            d.mkdir(parents=True, exist_ok=True)
            run(tool("v3_room.py", *args))
        if out.exists():
            room = summarize_room(json.loads(out.read_text()), t)
    if PLAN:
        return
    summary = {"threshold": t, "model": str(tfl), "model_sha256": sha256_file(tfl), "recall": rows, "room_test": room}
    (release_dir(a.version) / "score_summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    say(f"\nRecall at raw {t} (per source and device class, never pooled across classes; the gate reads the satellite "
        "rows only; a PC, webcam or phone source is an evaluation channel in a test-split session; aggregates only):")
    for r in rows:
        say(f"  {r['session']:<45} {r['source'] or '?':<11} {row_class(r):<10} {r['speaker'] or '?':<6} {r['split'] or '?':<10} "
            f"{r['condition']:<7} {r['k']}/{r['n']}{'' if r['eval_channel'] else '  (not an eval channel)'}")
    if room:
        for scope, v in room.items():
            say(f"  room test {scope:<7} {v['k']} in {v['hours']} h = {v['per_hour']}/h {v['ci95']}")
    record(st, "score", threshold=t, sessions=[s["id"] for s in sessions], room=bool(room),
           summary=f"{len(rows)} recall rows, room {'yes' if room else 'no'}")


def row_is_eval(r, split="test"):
    """Is a score row an evaluation channel: a satellite (tv) row by its stored flag; any other device class
    (PC desk, webcam, phone) when its split is test (owner decision 2026-11-11), whatever the stored flag of
    an older summary says."""
    if row_class(r) == "tv":
        return bool(r.get("eval_channel"))
    return r.get("split") == split


def other_devices(summary, split="test"):
    """INFORMATIONAL (no gate yet): held-out recall of the non-satellite sources (PC desk mic, webcam, phone)
    per device class, source, speaker and TV condition, with Wilson 95 % intervals. Never pooled with the
    satellite rows that the gate reads."""
    groups = {}
    for r in summary["recall"]:
        if row_class(r) == "tv" or not row_is_eval(r, split) or r.get("split") != split:
            continue
        g = groups.setdefault((row_class(r), r.get("source"), r.get("speaker") or "?", r["condition"]), [0, 0])
        g[0] += r["k"]
        g[1] += r["n"]
    return [{"device_class": c, "source": src, "speaker": spk, "condition": cond, "k": k, "n": n,
             "value": round(k / n, 4) if n else None, "ci95": wilson(k, n)}
            for (c, src, spk, cond), (k, n) in sorted(groups.items())]


def gate_eval(summary, targets=GATE, split="test"):
    """Household gate: real held-out recall for every speaker/TV condition plus independent room negatives.

    A pooled good score cannot compensate for an untested speaker, an untested TV condition or one
    noisy device. This gate checks point estimates; its 'confident' field separately records whether
    the whole 95% interval meets every target. Owner overrides are handled by the release driver.
    """
    rows = [r for r in summary["recall"] if r["eval_channel"] and row_class(r) == "tv" and r.get("split") == split
            and r["condition"] in ("tv-off", "tv-on")]          # satellite rows only: other devices never enter the gate
    speakers = sorted({r["speaker"] for r in rows if r.get("speaker")})
    checks, missing = [], []
    min_takes = targets.get("min_takes_per_condition", 10)
    if len(speakers) < targets.get("min_speakers", 2):
        missing.append(f"at least {targets.get('min_speakers', 2)} distinct test speakers required")
    if any(not r.get("speaker") for r in rows):
        missing.append("test takes without a speaker id")

    def add(name, k, n, target, ci):
        rate = k / n if n else None
        ok = rate is not None and rate >= target
        checks.append({"check": name, "k": k, "n": n, "value": None if rate is None else round(rate, 4),
                       "ci95": ci, "target": target, "pass": bool(ok), "confident": bool(ok and ci[0] >= target)})

    for cond in ("tv-off", "tv-on"):
        sel = [r for r in rows if r["condition"] == cond]
        k, n = sum(r["k"] for r in sel), sum(r["n"] for r in sel)
        add(f"recall {cond}, pooled", k, n, targets[cond], wilson(k, n))
        if not n:
            missing.append(f"no real test takes with the TV {'silent' if cond == 'tv-off' else 'on'}")
        for spk in speakers:
            group = [r for r in sel if r["speaker"] == spk]
            ks, ns = sum(r["k"] for r in group), sum(r["n"] for r in group)
            add(f"recall {cond}, {spk}", ks, ns, targets[cond], wilson(ks, ns))
            if ns < min_takes:
                missing.append(f"{spk}: {cond} has {ns} test takes, need >= {min_takes}")

    room = summary.get("room_test") or {}
    pooled = room.get("pooled")
    if not pooled or not pooled.get("hours"):
        missing.append("no room test part score")
    else:
        scopes = [(name, v) for name, v in sorted(room.items()) if name != "pooled" and v.get("hours")]
        if len(scopes) < targets.get("min_room_devices", 2):
            missing.append(f"room test requires >= {targets.get('min_room_devices', 2)} distinct devices")
        for name, v in [("pooled", pooled), *scopes]:
            if v.get("per_hour") is None or not v.get("ci95"):
                missing.append(f"room test {name}: missing rate or confidence interval")
                continue
            checks.append({"check": f"room false wakes/h, test part, {name}", "k": v["k"],
                           "hours": v["hours"], "value": v["per_hour"], "ci95": v["ci95"],
                           "target": targets["fa_per_h"], "pass": v["per_hour"] <= targets["fa_per_h"],
                           "confident": v["ci95"][1] <= targets["fa_per_h"]})
    verdict = "incomplete" if missing else ("pass" if all(c["pass"] for c in checks) else "fail")
    return {"verdict": verdict, "confident": verdict == "pass" and all(c["confident"] for c in checks),
            "missing": missing, "checks": checks, "threshold": summary["threshold"], "targets": targets,
            "other_devices": other_devices(summary, split)}

def gate_text(g):
    lines = [f"| Check | Measured | 95 % interval | Target | Result |", "| --- | --- | --- | --- | --- |"]
    for c in g["checks"]:
        if "hours" in c:
            m = f"{c['k']} in {c['hours']} h = {c['value']}/h"
            ci = f"[{c['ci95'][0]}, {c['ci95'][1]}]/h"
            tgt = f"<= {c['target']}/h"
        else:
            m = f"{c['k']}/{c['n']}" + (f" = {c['value'] * 100:.1f} %" if c["value"] is not None else "")
            ci = f"[{c['ci95'][0] * 100:.1f}, {c['ci95'][1] * 100:.1f}] %"
            tgt = f">= {c['target'] * 100:.0f} %"
        res = ("pass" + (" (whole interval)" if c["confident"] else " (point only)")) if c["pass"] else "FAIL"
        lines.append(f"| {c['check']} | {m} | {ci} | {tgt} | {res} |")
    for m in g["missing"]:
        lines.append(f"| {m} | not measured | | | MISSING |")
    lines.append("")
    lines.append(f"Verdict at raw {g['threshold']}: **{g['verdict'].upper()}**"
                 + (" (every interval inside the target)" if g["confident"] else "")
                 + " (satellite rows only)")
    other = g.get("other_devices") or []
    if other:
        lines += ["", "Other devices, test split (informational, not part of the verdict; no gate yet):", "",
                  "| Device class | Source | Speaker | Condition | Recall | 95 % interval |", "| --- | --- | --- | --- | --- | --- |"]
        for o in other:
            v = "n/a" if not o["n"] else f"{o['k']}/{o['n']} = {o['value'] * 100:.1f} %"
            lines.append(f"| {o['device_class']} | {o['source']} | {o['speaker']} | {o['condition']} | {v} | "
                         f"[{o['ci95'][0] * 100:.1f}, {o['ci95'][1] * 100:.1f}] % |")
    return "\n".join(lines)


def cmd_gate(a):
    st = load_state(a.version)
    p = release_dir(a.version) / "score_summary.json"
    if not p.exists():
        raise Refused("run score first")
    targets = {"tv-off": a.min_recall_tv_off, "tv-on": a.min_recall_tv_on, "fa_per_h": a.max_fa_per_h}
    g = gate_eval(json.loads(p.read_text()), targets)
    say(gate_text(g))
    if not PLAN:
        (release_dir(a.version) / "gate.json").write_text(json.dumps(g, indent=1) + "\n")
        record(st, "gate", verdict=g["verdict"], confident=g["confident"], threshold=g["threshold"],
               summary=f"{g['verdict']} at raw {g['threshold']}")


def evidence(st):
    """The evidence summary printed before an owner-gated step."""
    s = st["steps"]
    lines = [f"Release {st['version']}: {st['config'].get('model_id')} (\"{st['config'].get('phrase')}\")"]
    for step in ("pick-threshold", "calibrate", "score", "gate", "package", "release", "deploy"):
        if step in s:
            lines.append(f"  {step:<15} {s[step].get('summary', '')} ({s[step]['at']})")
        else:
            lines.append(f"  {step:<15} not done")
    gp = release_dir(st["version"]) / "gate.json"
    if gp.exists():
        lines.append(gate_text(json.loads(gp.read_text())))
    for n in st.get("notes", []):
        lines.append(f"  note: {n}")
    return "\n".join(lines)


def render_card(tmpl, values):
    class D(dict):
        def __missing__(self, k):
            return "not measured"
    return tmpl.format_map(D(values))


def card_values(st, model_dir):
    cfg = st["config"]
    mid = cfg["model_id"]
    rd = release_dir(st["version"])
    pick = st["steps"].get("pick-threshold", {})
    t = pick.get("threshold")
    v = {"model_id": mid, "version": st["version"], "phrase": cfg["phrase"], "date": dt.date.today().isoformat(),
         "threshold": t, "served_bytes": f"{(model_dir / f'{mid}.tflite').stat().st_size:,}".replace(",", " "),
         "sha256": sha256_file(model_dir / f"{mid}.tflite"),
         "recipe": cfg.get("recipe") or "Not given (`init --recipe`); see the homelab experiment note.",
         "threshold_reason": (f"owner: {pick.get('reason')}" if pick.get("source") == "owner" else
                              f"lowest raw threshold with at most {pick.get('max_fa')} false accepts on the room "
                              f"selection part ({pick.get('sel_fa')} in {pick.get('sel_hours')} h)")}
    v["selection"] = v["threshold_reason"] + "."
    sp = rd / "score_summary.json"
    if sp.exists():
        s = json.loads(sp.read_text())
        rows = [r for r in s["recall"] if r["eval_channel"]]
        tbl = ["| Session | Source | Speaker | Split | Condition | Recall | 95 % |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            ci = wilson(r["k"], r["n"])
            tbl.append(f"| {r['session']} | {r['source']} | {r['speaker']} | {r['split']} | {r['condition']} | "
                       f"{r['k']}/{r['n']} | [{ci[0] * 100:.1f}, {ci[1] * 100:.1f}] % |")
        v["real_recall"] = "\n".join(tbl) if rows else "**Untested: no real takes of the phrase.**"
        if s.get("room_test"):
            v["room_fa"] = "\n".join(f"- {k}: {x['k']} in {x['hours']} h = {x['per_hour']}/h [{x['ci95'][0]}, {x['ci95'][1]}]"
                                     for k, x in s["room_test"].items())
    else:
        v["real_recall"] = "**Untested: no real takes of the phrase.**"
    gp = rd / "gate.json"
    if gp.exists():
        g = json.loads(gp.read_text())
        v["gate_table"] = gate_text(g)
        v["gate_verdict"] = g["verdict"]
    else:
        v["gate_table"] = "Not run."
        v["gate_verdict"] = "not run"
    notes = rd / "notes.md"
    v["notes"] = notes.read_text().strip() if notes.exists() else "To be written by the owner (release notes.md)."
    return v


def cmd_package(a):
    st = load_state(a.version)
    cfg = st["config"]
    mid = cfg["model_id"]
    cal = st["steps"].get("calibrate")
    src = Path(cal["dir"]) if cal else Path(cfg["candidate"])
    need = [f"{mid}.tflite", f"{mid}.onnx", f"{mid}_uncalibrated.tflite", f"{mid}_uncalibrated.onnx"]
    miss = [f for f in need if not (src / f).exists()]
    if miss:
        raise Refused(f"missing in {src}: {miss} (run calibrate)")
    dst = KIVI_MODELS / "wakeword" / mid / a.version
    sha = sha256_file(src / f"{mid}.tflite")
    if dst.exists() and (dst / f"{mid}.tflite").exists() and sha256_file(dst / f"{mid}.tflite") != sha:
        raise Refused(f"{dst} exists with another {mid}.tflite: version directories are never edited")
    lic = Path(a.license_notes) if a.license_notes else None
    if not lic:
        prev = sorted((KIVI_MODELS / "wakeword" / mid).glob("*/LICENSE-NOTES.md"))
        prev = [p for p in prev if p.parent != dst]
        lic = prev[-1] if prev else None
    if not (dst / "LICENSE-NOTES.md").exists() and not lic:
        raise Refused("no LICENSE-NOTES.md for this model: pass --license-notes FILE (write the licence reasoning first)")
    files = need + [f for f in [f"{mid}_uncalibrated.calibration.json"] if (src / f).exists()]
    if will(f"copy {files} to {dst}"):
        dst.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not (dst / f).exists() or sha256_file(dst / f) != sha256_file(src / f):
                shutil.copyfile(src / f, dst / f)
        (dst / "SHA256SUMS").write_text("".join(f"{sha256_file(dst / f)}  {f}\n" for f in files))
        if not (dst / "LICENSE-NOTES.md").exists():
            text = lic.read_text()
            if lic.parent.parent == dst.parent:
                text = text.replace(lic.parent.name, a.version)
            (dst / "LICENSE-NOTES.md").write_text(text)
            say(f"package: LICENSE-NOTES.md copied from {lic}: REVIEW it for this version's data")
    card = dst / "MODEL_CARD.md"
    marker = "Generated by the homelab `scripts/voice-assistant/wakeword/wake_release.py package`"
    if card.exists() and marker not in card.read_text() and not a.force:
        say(f"package: {card} was written by hand; kept (--force to regenerate)")
    elif will(f"write {card} from templates/MODEL_CARD.md.tmpl"):
        tmpl = (KIVI_MODELS / "templates/MODEL_CARD.md.tmpl").read_text()
        card.write_text(render_card(tmpl, card_values(st, dst)))
    lock = KIVI_MODELS / "models.lock"
    old = lock.read_text()
    new = update_lock_text(old, mid, a.version, sha)
    if new != old and will(f"models.lock: {mid} -> {a.version} {sha[:12]}"):
        lock.write_text(new)
    read_lock(lock)
    if not PLAN:
        say(run(["git", "-C", str(KIVI_MODELS), "status", "--short"], capture=True).stdout)
        record(st, "package", dir=str(dst), sha256=sha, summary=f"{dst.relative_to(KIVI_MODELS)} ({sha[:12]})")


def require_owner(a, st, what):
    say(evidence(st))
    if not a.yes_owner_approved:
        raise Refused(f"\n{what} needs the owner's approval: re-run with --yes-owner-approved after the owner said yes "
                      f"to exactly this ({st['version']}).")


def git(*args, capture=True, check=True):
    return run(["git", "-C", str(KIVI_MODELS), *args], capture=capture, check=check)


def cmd_release(a):
    st = load_state(a.version)
    cfg = st["config"]
    require_owner(a, st, "release (pushing a version tag publishes an image)")
    g = st["steps"].get("gate", {})
    if g.get("verdict") != "pass" and not a.accept_gate_fail:
        raise Refused(f"gate verdict is {g.get('verdict', 'not run')!r}: pass --accept-gate-fail \"<owner's reason>\" "
                      "if the owner releases anyway (for example a trial)")
    v = a.version
    lock = read_lock(KIVI_MODELS / "models.lock")
    if lock.get(cfg["model_id"], (None,))[0] != v:
        raise Refused(f"models.lock does not serve {cfg['model_id']} {v}: run package")
    url = git("remote", "get-url", "origin").stdout.strip()
    if url != SSH_REMOTE and will(f"set origin to {SSH_REMOTE} (the https remote asks for a password)"):
        git("remote", "set-url", "origin", SSH_REMOTE)
    git("fetch", "-q", "origin", "--tags")
    paths = [f"wakeword/{cfg['model_id']}/{v}", "models.lock"]
    if git("status", "--porcelain", "--", *paths).stdout.strip():
        msg = f"Add {cfg['model_id']} {v} and serve it in the next image release"
        if will(f"commit {paths}: {msg}"):
            git("add", "--", *paths)
            git("commit", "-q", "-m", msg, "--", *paths)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    ahead = int(git("rev-list", "--count", f"origin/{branch}..HEAD").stdout)
    behind = int(git("rev-list", "--count", f"HEAD..origin/{branch}").stdout)
    if behind:
        raise Refused(f"kivi-models is {behind} commit(s) behind origin/{branch}: pull first")
    if ahead and will(f"push {branch} ({ahead} commit(s))"):
        git("push", "-q", "origin", branch)
    remote_tags = git("ls-remote", "--tags", "origin", f"refs/tags/{v}").stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    if remote_tags:
        tag_commit = git("rev-parse", f"refs/tags/{v}^{{commit}}", check=False).stdout.strip()
        say(f"release: tag {v} already on the remote (commit {tag_commit[:9]}); not pushed again")
    else:
        if git("tag", "-l", v).stdout.strip():
            if git("rev-parse", f"refs/tags/{v}^{{commit}}").stdout.strip() != head:
                raise Refused(f"local tag {v} points elsewhere than HEAD; fix by hand")
        elif will(f"tag {v} at {head[:9]}"):
            git("tag", "-a", v, "-m", f"Release {v}: models.lock at this commit (owner-approved {dt.date.today()})")
        if will(f"push ONLY refs/tags/{v}"):
            git("push", "origin", f"refs/tags/{v}")
    if PLAN or a.no_watch:
        return
    token = forgejo_token()
    say("release: waiting for the CI run of the tag ...")
    status, run_no, lines = watch_ci(v, token)
    for ln in lines:
        say("  ci: " + ln.strip())
    if status != "success":
        raise Refused(f"CI run of {v}: {status}")
    digest = registry_digest(v, token)
    if not digest:
        raise Refused(f"no manifest for {v} in the registry")
    refused, codes = anonymous_refused(v)
    say(f"release: {IMAGE}:{v}@{digest}; anonymous pull refused: {refused} (HTTP {codes})")
    if not refused:
        raise Refused("ANONYMOUS PULL WORKS: make the package owner agil limited again before anything else")
    record(st, "release", tag=v, commit=head, ci_run=run_no, digest=digest, anonymous_refused=True,
           gate_override=a.accept_gate_fail, summary=f"{v}@{digest[:19]} (CI run {run_no})")


def homelab_git(*args, capture=True, check=True):
    return run(["git", "-C", str(HOMELAB), *args], capture=capture, check=check)


def cmd_deploy(a):
    st = load_state(a.version)
    rel = st["steps"].get("release")
    if not rel:
        raise Refused("run release first (it records the digest)")
    v, digest = a.version, rel["digest"]
    lock_text = git("show", f"refs/tags/{v}:models.lock").stdout
    tmp = release_dir(v) / "models.lock.at-tag"
    if not PLAN:
        tmp.write_text(lock_text)
    want = {k: s for k, (_, s) in read_lock(tmp).items()} if tmp.exists() else {}
    text = VALUES.read_text()
    old = current_pin(text)
    new_text, new = bump_pin(text, v, digest)
    if old == new:
        say(f"deploy: values.yaml already pins {new}")
    else:
        in_use = changed_ids_in_use(a, old, want)
        if in_use and not a.yes_owner_approved:
            raise Refused(f"the release changes model(s) in live use ({in_use}): needs --yes-owner-approved")
        if will(f"values.yaml: {old} -> {new}"):
            VALUES.write_text(new_text)
            rel_path = str(VALUES.relative_to(HOMELAB))
            homelab_git("commit", "-q", "-m", f"home-assistant: Serve kivi-models release {v}", "--", rel_path)
    homelab_git("fetch", "-q", "origin")
    branch = homelab_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    behind = int(homelab_git("rev-list", "--count", f"HEAD..origin/{branch}").stdout)
    ahead = int(homelab_git("rev-list", "--count", f"origin/{branch}..HEAD").stdout)
    if behind:
        raise Refused(f"homelab is {behind} commit(s) behind origin/{branch}: pull first")
    if ahead and will(f"push homelab {branch} ({ahead} commit(s))"):
        homelab_git("push", "-q", "origin", branch)
    if PLAN or a.no_watch:
        return
    commit = homelab_git("rev-parse", "HEAD").stdout.strip()
    say(f"deploy: waiting for Argo CD to sync {commit[:9]} and the rollout ...")
    end = time.time() + a.timeout
    while time.time() < end:
        app = kube_json("-n", ARGO_APP[0], "get", "application", ARGO_APP[1])
        dep = kube_json("-n", NS, "get", "deploy", OWW_DEPLOY)
        img = dep["spec"]["template"]["spec"]["initContainers"][0]["image"]
        if app["status"]["sync"].get("revision") == commit and img.endswith(new.split("@")[1]):
            if run(KUBE + ["-n", NS, "rollout", "status", f"deploy/{OWW_DEPLOY}", "--timeout=120s"], check=False,
                   capture=True).returncode == 0:
                break
        time.sleep(10)
    else:
        raise Refused("rollout not done in time; check Argo CD")
    sums = pod_model_sums()
    ok = all(sums.get(k) == s for k, s in want.items())
    ids = wyoming_ids()
    say(f"deploy: /custom in the pod: {sums}\ndeploy: Wyoming describe: {ids}")
    if not ok:
        raise Refused(f"in-pod sha256 differs from models.lock at {v}: {want}")
    if not set(want) <= set(ids):
        raise Refused(f"the server does not list {set(want) - set(ids)}")
    app = kube_json("-n", ARGO_APP[0], "get", "application", ARGO_APP[1])
    record(st, "deploy", pin=new, commit=commit, pod_sha256=sums, wyoming=ids,
           argo=f"{app['status']['sync'].get('status')}/{app['status']['health'].get('status')}",
           summary=f"{new.split('@')[0]} live, in-pod sha256 OK, ids {sorted(want)}")
    say("deploy: done. Update docs/voice-assistant/model-registry.md (Deployed? and storage) by hand.")


def changed_ids_in_use(a, old_pin, want):
    """Model ids whose served file changes with this release and that a pipeline uses (None if unknown)."""
    try:
        old_tag = old_pin.split("@")[0]
        old_lock = {k: s for k, (_, s) in _lock_from_text(git("show", f"refs/tags/{old_tag}:models.lock",
                                                              check=False).stdout).items()}
    except Refused:
        old_lock = {}
    changed = {k for k, s in want.items() if old_lock.get(k) != s}
    if not changed:
        return []
    try:
        with HA(a.ha_token_file) as ha:
            used = {p["wake_word_id"] for p in ha.pipelines()["pipelines"]}
    except Exception as e:  # noqa: BLE001
        say(f"deploy: could not read the pipelines ({type(e).__name__}); assuming the changed models are in use")
        return sorted(changed)
    return sorted(changed & used)


def _lock_from_text(text):
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and not f[0].startswith("#"):
            out[f[0]] = (f[1], f[2])
    return out


def cmd_enable(a):
    st = load_state(a.version)
    cfg = st["config"]
    require_owner(a, st, f"enable on {a.tv} (it changes which phrase wakes that TV)")
    mid, name = cfg["model_id"], cfg["pipeline_name"]
    failed = False
    with HA(a.ha_token_file) as ha:
        if mid not in ha.wake_words():
            raise Refused(f"{mid} is not served (wake_word/info): deploy first")
        plist = ha.pipelines()
        base = resolve_option(plist, a.base_pipeline or cfg.get("base_pipeline") or "preferred")
        if not base:
            raise Refused("base pipeline not found")
        fields = trial_pipeline_fields(base, name, mid)
        trial = find_pipeline(plist, name)
        if trial is None:
            if will(f"create pipeline {name!r}: copy of {base['name']!r} with wake_word_id {mid}"):
                trial = ha.call("assist_pipeline/pipeline/create", **fields)
        elif {k: trial.get(k) for k in fields} != fields:
            if will(f"update pipeline {name!r} to {fields}"):
                ha.call("assist_pipeline/pipeline/update", pipeline_id=trial["id"], **fields)
        else:
            say(f"enable: pipeline {name!r} already a copy of {base['name']!r} with {mid}")
        if PLAN:
            return
        plist = ha.pipelines()
        trial = find_pipeline(plist, name)
        ent = tv_entities(ha.call("config/entity_registry/list"), a.tv)
        states = ha.states()
        cur = states[ent["select"]]["state"]
        prev_entry = st.setdefault("tvs", {}).get(a.tv, {})
        if cur == name:
            say(f"enable: {a.tv} already on {name!r}")
        else:
            on = resolve_option(plist, cur)
            preflight_pipeline(trial, states, served_for(ha, trial), a.force)
            if will(f"{ent['select']}: {cur!r} -> {name!r}, then restart the open run of {on['name']!r} (no-op update)"):
                t0 = dt.datetime.now(dt.timezone.utc)
                ha.call("call_service", domain="select", service="select_option",
                        service_data={"option": name}, target={"entity_id": ent["select"]})
                ha.kick(on)
                prev_entry = {"previous": cur, "select": ent["select"], "enabled_at": now_utc()}
                st["tvs"][a.tv] = prev_entry
                save_state(st)
                muted = states.get(ent["muted"], {}).get("state") if ent["muted"] else None
                back = f"python3 wake_release.py rollback --version {a.version} --tv {a.tv}"
                if not verify_run(ha, trial, a.tv, ent, t0, back):
                    failed = True
                    say(f"enable: {a.tv} did not start a healthy {name!r} run (microphone muted: {muted})")
        record(st, f"enable-{a.tv}", pipeline=name, pipeline_id=trial["id"], previous=prev_entry.get("previous", "preferred"),
               select=ent["select"], summary=f"{a.tv} on {name!r} ({mid}); rollback to {prev_entry.get('previous', 'preferred')!r}"
               + ("; RUN CHECK FAILED" if failed else ""))
    say(f"\nRollback: python3 wake_release.py rollback --version {a.version} --tv {a.tv}")
    if failed:
        raise Refused("enable: the run check FAILED (see above)")


def cmd_rollback(a):
    st = load_state(a.version)
    cfg = st["config"]
    name = cfg["pipeline_name"]
    target = a.to or st.get("tvs", {}).get(a.tv, {}).get("previous") or "preferred"
    failed = False
    with HA(a.ha_token_file) as ha:
        plist = ha.pipelines()
        ent = tv_entities(ha.call("config/entity_registry/list"), a.tv)
        states = ha.states()
        cur = states[ent["select"]]["state"]
        dest = resolve_option(plist, target)
        if dest is None:
            raise Refused(f"rollback target {target!r} is not a pipeline")
        if cur == target:
            say(f"rollback: {a.tv} already on {target!r}")
        else:
            preflight_pipeline(dest, states, served_for(ha, dest), a.force)
            if will(f"{ent['select']}: {cur!r} -> {target!r}, then restart the open run of {cur!r} (no-op update)"):
                on = resolve_option(plist, cur)
                t0 = dt.datetime.now(dt.timezone.utc)
                ha.call("call_service", domain="select", service="select_option",
                        service_data={"option": target}, target={"entity_id": ent["select"]})
                if on:
                    ha.kick(on)
                muted = states.get(ent["muted"], {}).get("state") if ent["muted"] else None
                say(f"rollback: {a.tv} on {target!r} ({dest['name']}, wake word {dest['wake_word_id']}; "
                    f"microphone muted: {muted})")
                back = f"python3 wake_release.py rollback --version {a.version} --tv {a.tv} --to {shlex.quote(cur)} --force"
                failed = not verify_run(ha, dest, a.tv, ent, t0, back)
        if a.delete_pipeline:
            trial = find_pipeline(ha.pipelines(), name)
            users = [s for s, v in ha.states().items() if s.startswith("select.") and v.get("state") == name]
            if trial and users:
                raise Refused(f"pipeline {name!r} still selected by {users}")
            if trial and will(f"delete pipeline {name!r}"):
                ha.call("assist_pipeline/pipeline/delete", pipeline_id=trial["id"])
        if not PLAN:
            st.setdefault("tvs", {}).pop(a.tv, None)
            record(st, f"rollback-{a.tv}", to=target,
                   summary=f"{a.tv} back on {target!r}" + ("; RUN CHECK FAILED" if failed else ""))
    if failed:
        raise Refused("rollback: the run check FAILED (see above)")


def promoted_fields(pipeline, wake_word_id):
    """The update of `pipeline` that changes only its wake word id."""
    out = {k: pipeline[k] for k in pipeline if k != "id"}
    out["wake_word_id"] = wake_word_id
    return out


def tvs_on_pipeline(selects, plist, pipeline_name):
    """TVs whose select points at `pipeline_name` (by name, or 'preferred' when it is the preferred one)."""
    p = find_pipeline(plist, pipeline_name)
    out = []
    for tv, option in selects.items():
        q = resolve_option(plist, option) if option else None
        if p and q and q["id"] == p["id"]:
            out.append(tv)
    return out


def cmd_promote(a):
    """Make the model the household wake word: the preferred (or --pipeline) pipeline gets its wake word id;
    TVs on the trial pipeline go back to that pipeline. Every other pipeline field stays unchanged."""
    st = load_state(a.version)
    cfg = st["config"]
    require_owner(a, st, "promote (it changes the wake word of every TV on the household pipeline)")
    mid, trial_name = cfg["model_id"], cfg["pipeline_name"]
    with HA(a.ha_token_file) as ha:
        if mid not in ha.wake_words():
            raise Refused(f"{mid} is not served (wake_word/info): deploy first")
        plist = ha.pipelines()
        target = resolve_option(plist, a.pipeline)
        if not target:
            raise Refused(f"pipeline {a.pipeline!r} not found")
        if target["name"] == trial_name:
            raise Refused("the target is the trial pipeline itself; promote into the household pipeline")
        reg, states = ha.call("config/entity_registry/list"), ha.states()
        ents = {tv: tv_entities(reg, tv) for tv in devices_map()}
        selects = {tv: states.get(e["select"], {}).get("state") for tv, e in ents.items()}
        muted = {tv: states.get(e["muted"], {}).get("state") if e["muted"] else None for tv, e in ents.items()}
        preflight_pipeline(dict(target, wake_word_id=mid), states, served_for(ha, target), a.force)
        t0 = dt.datetime.now(dt.timezone.utc)
        prev = a.previous_wake_word or st.get("promotion", {}).get("previous_wake_word") or (
            target["wake_word_id"] if target["wake_word_id"] != mid else "alexa")
        if prev == mid:
            raise Refused(f"previous wake word {prev!r} is the model itself; pass --previous-wake-word")
        changed = False
        if target["wake_word_id"] == mid:
            say(f"promote: {target['name']!r} already uses {mid}")
            if a.previous_wake_word and not (st.get("promotion") or {}).get("previous_wake_word"):
                # found live without a driver record (changed outside the driver): record it, change nothing
                if will(f"record the promotion of {mid} into {target['name']!r} (previous {prev}); no Home Assistant change"):
                    st["promotion"] = {"pipeline_id": target["id"], "pipeline": target["name"], "previous_wake_word": prev,
                                       "observed_at": now_utc(), "recorded_after_the_fact": True}
                    save_state(st)
        elif will(f"pipeline {target['name']!r}: wake_word_id {target['wake_word_id']} -> {mid} (all else unchanged); "
                  "Home Assistant ends its open runs"):
            ha.call("assist_pipeline/pipeline/update", pipeline_id=target["id"], **promoted_fields(target, mid))
            after = find_pipeline(ha.pipelines(), target["id"])
            if {k: v for k, v in after.items() if k != "wake_word_id"} != {k: v for k, v in target.items() if k != "wake_word_id"}:
                raise Refused(f"other fields changed: {target} -> {after}")
            st["promotion"] = {"pipeline_id": target["id"], "pipeline": target["name"], "previous_wake_word": prev,
                               "promoted_at": now_utc()}
            save_state(st)
            changed = True
        if a.owner_override and not PLAN and "promotion" in st:
            st["promotion"]["owner_override"] = a.owner_override
            save_state(st)
        back = "preferred" if target["id"] == plist["preferred_pipeline"] else target["name"]
        trial = find_pipeline(plist, trial_name)
        for tv in (tvs_on_pipeline(selects, plist, trial_name) if trial else []):
            if will(f"{ents[tv]['select']}: {selects[tv]!r} -> {back!r}, then end the open {trial_name!r} run (no-op update)"):
                ha.call("call_service", domain="select", service="select_option",
                        service_data={"option": back}, target={"entity_id": ents[tv]["select"]})
                ha.kick(trial)
                st.setdefault("tvs", {}).pop(tv, None)
                save_state(st)
                changed = True
        if trial is None and not PLAN:
            # the trial pipeline is gone: its recorded TV entries are stale (nothing can select it)
            st.pop("tvs", None)
            save_state(st)
        if PLAN:
            return
        states = ha.states()
        plist = ha.pipelines()
        live = {}
        on_target = []
        for tv, e in ents.items():
            p = resolve_option(plist, states.get(e["select"], {}).get("state"))
            live[tv] = {"select": states.get(e["select"], {}).get("state"), "wake_word": p and p["wake_word_id"],
                        "muted": muted[tv]}
            if p and p["id"] == target["id"]:
                on_target.append(tv)
            say(f"promote: {tv}: select {live[tv]['select']!r} -> {live[tv]['wake_word']} (microphone muted: {muted[tv]})")
        back = f"python3 wake_release.py demote --version {a.version}"
        seen = None
        if changed:
            seen = all([verify_run(ha, target, tv, ents[tv], t0, back) for tv in on_target]) if on_target else None
        say(f"promote: healthy new {target['name']!r} run seen on every TV using it: {seen}")
        record(st, "promote", pipeline=target["name"], previous_wake_word=prev, live=live, new_run_seen=seen,
               summary=f"{target['name']!r} on {mid} (was {prev}); TVs {live}")
    say(f"\nRollback: {back}   (wake word back to {prev})")
    if seen is False:
        raise Refused("promote: the run check FAILED (see above)")


def cmd_demote(a):
    """Undo promote: the household pipeline's wake word back to the recorded previous one (always allowed)."""
    st = load_state(a.version)
    promo = st.get("promotion") or {}
    to = a.to or promo.get("previous_wake_word") or "alexa"
    failed = False
    with HA(a.ha_token_file) as ha:
        if to not in ha.wake_words():
            raise Refused(f"{to} is not served (wake_word/info)")
        plist = ha.pipelines()
        target = find_pipeline(plist, promo.get("pipeline_id") or "") or resolve_option(plist, a.pipeline)
        if not target:
            raise Refused("household pipeline not found")
        if target["wake_word_id"] == to:
            say(f"demote: {target['name']!r} already uses {to}")
        elif will(f"pipeline {target['name']!r}: wake_word_id {target['wake_word_id']} -> {to} (all else unchanged)"):
            t0 = dt.datetime.now(dt.timezone.utc)
            ha.call("assist_pipeline/pipeline/update", pipeline_id=target["id"], **promoted_fields(target, to))
            plist, states = ha.pipelines(), ha.states()
            reg = ha.call("config/entity_registry/list")
            for tv in devices_map():
                e = tv_entities(reg, tv)
                p = resolve_option(plist, states.get(e["select"], {}).get("state"))
                if p and p["id"] == target["id"]:
                    ok = verify_run(ha, target, tv, e, t0, f"python3 wake_release.py demote --version {a.version} --to {to}")
                    failed = failed or not ok
        if not PLAN:
            st.pop("promotion", None)
            record(st, "demote", pipeline=target["name"], to=to,
                   summary=f"{target['name']!r} back on {to}" + ("; RUN CHECK FAILED" if failed else ""))
    if failed:
        raise Refused("demote: the run check FAILED (see above)")


def cmd_status(a):
    say("== kivi-models")
    try:
        lock = read_lock(KIVI_MODELS / "models.lock")
        for k, (ver, s) in lock.items():
            say(f"  models.lock  {k:<10} {ver:<24} {s[:12]}")
    except (OSError, Refused) as e:
        say(f"  models.lock: {e}")
    say("== homelab values")
    pin = current_pin(VALUES.read_text())
    say(f"  image pin    {IMAGE}:{pin}")
    say("== cluster")
    try:
        for k, s in sorted(pod_model_sums().items()):
            say(f"  /custom      {k + '.tflite':<20} {s[:12]}")
        say(f"  Wyoming ids  {' '.join(wyoming_ids())}")
        app = kube_json("-n", ARGO_APP[0], "get", "application", ARGO_APP[1])
        say(f"  Argo CD      {app['status']['sync'].get('status')}/{app['status']['health'].get('status')} "
            f"at {app['status']['sync'].get('revision', '')[:9]}")
    except Refused as e:
        say(f"  cluster read failed: {e}")
    if not a.no_ha:
        say("== Home Assistant")
        with HA(a.ha_token_file) as ha:
            plist, states, reg = ha.pipelines(), ha.states(), ha.call("config/entity_registry/list")
            for p in plist["pipelines"]:
                pref = " (preferred)" if p["id"] == plist["preferred_pipeline"] else ""
                say(f"  pipeline     {p['name']!r:<14} wake word {p['wake_word_id']}{pref}")
            for tv in devices_map():
                ent = tv_entities(reg, tv)
                opt = states.get(ent["select"], {}).get("state")
                p = resolve_option(plist, opt) if opt else None
                muted = states.get(ent["muted"], {}).get("state") if ent["muted"] else None
                say(f"  TV {tv:<9} select {opt!r} -> wake word {p and p['wake_word_id']} (microphone muted: {muted})")
    say("== release states")
    for p in sorted(RELEASES.glob("*/state.json")) if RELEASES.is_dir() else []:
        s = json.loads(p.read_text())
        last = s["log"][-1] if s.get("log") else {}
        say(f"  {s['version']:<26} {s['config'].get('model_id', '?'):<10} last: {last.get('step')} {last.get('at', '')}"
            + (f"  TVs: {s.get('tvs')}" if s.get("tvs") else ""))


# =============================================================================================== CLI

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Runbook: kivi-models docs/runbook/wake-word-release.md")
    ap.add_argument("--plan", action="store_true", help="print what each step would do, do nothing")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def sub(name, fn, version=True, ha=False, owner=False):
        p = sp.add_parser(name)
        p.set_defaults(fn=fn)
        if version:
            p.add_argument("--version", required=True, help="release / version directory, e.g. v5-oye-kivi-2026-11-20")
        if ha:
            p.add_argument("--ha-token-file", help="a file with an HA access token (default: HA_TOKEN_FILE, else the "
                                                   "documented login from pass, revoked afterwards)")
        if owner:
            p.add_argument("--yes-owner-approved", action="store_true",
                           help="the owner approved exactly this step for this version")
        return p

    p = sub("init", cmd_init)
    p.add_argument("--model-id", required=True, help="wake word id = file stem, e.g. oye_kivi")
    p.add_argument("--phrase", required=True, help='the phrase as people say it, e.g. "oye Kivi"')
    p.add_argument("--candidate", required=True, help="directory with <id>_uncalibrated.onnx/.tflite (or a run's kivi.onnx)")
    p.add_argument("--pipeline-name", help="trial pipeline name (default: the phrase)")
    p.add_argument("--base-pipeline", default="preferred", help="pipeline the trial copies (default: the preferred one)")
    p.add_argument("--recipe", help="recipe pointer text for the model card")
    p.add_argument("--update", action="store_true")

    p = sub("record-check", cmd_record_check, ha=True)
    p.add_argument("--pc-device", default="c3-a")
    p.add_argument("--pc-speaker", default="spk01")
    p.add_argument("--button-device", default="c3-b")
    p.add_argument("--button-speaker", default="spk02")
    p.add_argument("--desk-mic", default="Samson")
    p.add_argument("--webcam-mic", default="C920")
    p.add_argument("--no-ha", action="store_true", help="skip the Home Assistant read")

    p = sub("extract", cmd_extract)
    p.add_argument("--adopt-session", action="append", metavar="SESSION",
                   help="record an already-extracted dataset session (by its directory name) without "
                        "re-running kivi_extract.py")
    p.add_argument("--session-dir")
    p.add_argument("--button-takes", action="store_true")
    p.add_argument("--device")
    p.add_argument("--speaker")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--split")
    p.add_argument("--condition", choices=["tv-off", "tv-on"], help="(--button-takes) the TV state of the window")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("extra", nargs="*", help="after --: more kivi_extract.py options")

    p = sub("pick-threshold", cmd_pick_threshold)
    p.add_argument("--max-fa", type=int, default=2, help="false accepts allowed on the room selection part")
    p.add_argument("--room-sel", action="append", help=f"selection-part features (default {[str(x) for x in ROOM_SEL]})")
    p.add_argument("--threshold", type=float, help="an owner-given raw threshold instead of the selection")
    p.add_argument("--reason")
    p.add_argument("--force", action="store_true")
    p.add_argument("extra", nargs="*", help="after --: more v4_select.py options (validation windows, clips)")

    p = sub("calibrate", cmd_calibrate)
    p.add_argument("--check-features", help="feature windows for export_tflite.py's output check")
    p.add_argument("--force", action="store_true")

    p = sub("score", cmd_score)
    p.add_argument("--session", action="append", help="only these session ids")
    p.add_argument("--no-room", action="store_true")
    p.add_argument("--room-split", default=str(ROOM_SPLIT))
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--force", action="store_true")

    p = sub("gate", cmd_gate)
    p.add_argument("--min-recall-tv-off", type=float, default=GATE["tv-off"])
    p.add_argument("--min-recall-tv-on", type=float, default=GATE["tv-on"])
    p.add_argument("--max-fa-per-h", type=float, default=GATE["fa_per_h"])

    p = sub("package", cmd_package)
    p.add_argument("--license-notes", help="LICENSE-NOTES.md to use (default: the previous version's, to review)")
    p.add_argument("--force", action="store_true")

    p = sub("release", cmd_release, owner=True)
    p.add_argument("--accept-gate-fail", metavar="REASON", help="the owner releases although the gate did not pass")
    p.add_argument("--no-watch", action="store_true")

    p = sub("deploy", cmd_deploy, ha=True, owner=True)
    p.add_argument("--no-watch", action="store_true")
    p.add_argument("--timeout", type=int, default=900)

    p = sub("enable", cmd_enable, ha=True, owner=True)
    p.add_argument("--force", action="store_true", help="switch even if the pipeline preflight refuses")
    p.add_argument("--tv", required=True)
    p.add_argument("--base-pipeline")

    p = sub("rollback", cmd_rollback, ha=True)
    p.add_argument("--force", action="store_true", help="switch even if the pipeline preflight refuses")
    p.add_argument("--tv", required=True)
    p.add_argument("--to", help="select option to go back to (default: the one recorded by enable, else preferred)")
    p.add_argument("--delete-pipeline", action="store_true", help="also delete the trial pipeline if nothing selects it")

    p = sub("promote", cmd_promote, ha=True, owner=True)
    p.add_argument("--force", action="store_true", help="promote even if the pipeline preflight refuses")
    p.add_argument("--pipeline", default="preferred", help="household pipeline to switch (default: the preferred one)")
    p.add_argument("--previous-wake-word", help="the wake word id demote goes back to (default: the pipeline's current one; "
                   "needed when the pipeline already uses the model, e.g. changed outside the driver)")
    p.add_argument("--owner-override", metavar="REASON",
                   help="record the owner's reason when the gate is not 'pass' (the driver never refuses on the gate; the owner does)")

    p = sub("demote", cmd_demote, ha=True)
    p.add_argument("--pipeline", default="preferred")
    p.add_argument("--to", help="wake word id to go back to (default: the one promote replaced, else alexa)")

    p = sub("status", cmd_status, version=False, ha=True)
    p.add_argument("--no-ha", action="store_true")
    return ap


def main(argv=None):
    global PLAN
    a = build_parser().parse_args(argv)
    PLAN = a.plan
    try:
        a.fn(a)
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
