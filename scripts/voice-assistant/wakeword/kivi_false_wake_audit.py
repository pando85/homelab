#!/usr/bin/env python3
"""False-wake truth audit of the "oye Kivi" wake word: are the false wakes we count really false?

Counts only. This tool never plays, prints or saves audio, transcripts or speaker embeddings; it reads tables.

    kivi_false_wake_audit.py table      # one row per live clip and replay event (CSV in $KIVI_HOME/audit/) + summaries
    kivi_false_wake_audit.py sample     # build the BLIND autolabel validation sample (about 40 live clips) + frozen key
    kivi_false_wake_audit.py analyze    # after the owner labelled the sample: confusion matrix, precision / recall of
                                        # auto-false, corrected false wakes per TV and evening with 95 % intervals

Why. Nearly every live "false wake" count rests on `kivi_autolabel.py` (local faster-whisper + ECAPA), whose accuracy was
never measured; most of the other false labels carry no `label_source`. The sample is a stratified, blind measurement of
the autolabel: about 15 auto-false (9 low and 6 high autolabel confidence), 10 auto-true, 10 auto-unclear, 5 auto-not-a-wake clips, both TVs, both day parities.
It is MEASUREMENT ONLY: no training, threshold or selection choice is made from it.

The owner labels the sample as the first group of `kivi_live_wakes.py queue` (the auto label is never shown). The auto
labels are frozen at sampling time in `live/autolabel_validation_key.csv` (+ `.json`), because an owner label then
replaces the auto label in `live_wakes.csv`; `analyze` reads the key, not the live table.

Definitions (as `kivi_collect.py`): evening = 18:00 to 24:00 Europe/Madrid; the time of a live wake is the end of its
wake file (run start + wake_len). Exposure is NOMINAL: the model-era interval intersected with the evenings of the days
covered (both TVs assumed up); the collector's measured evening hours of 2026-11-11 (5.13 to 5.17 h) agree with it.
Model live: v4 `oye_kivi`, v5 `oye_kivi_next`, v6 `oye_kivi_ctx` from V6_LIVE (22:11 to about 22:40 UTC on 2026-11-11);
the eras of the other models are `kivi_live_wakes.ERAS`.

Estimation. Each auto-labelled clip is in a stratum (its auto label and trigger) and a cell (stratum, TV, day parity).
Sampled clips are known; the unsampled clips of a cell get an owner label from a Dirichlet posterior: the cell's
sampled labels + 0.5 per class (Jeffreys) + 2 pseudo-observations at the stratum's pooled label shares (thin cells
borrow from their stratum). 4000 simulated populations give the mean and the 2.5 / 97.5 percentiles; the sampling is
not proportional, the cell structure removes that bias. Rows an owner labelled are taken as labelled, rows
with a label but no `label_source` (older labels, provenance not recorded) are taken as labelled and reported apart.
Poisson variation of the exposure is not in the corrected intervals (the uncorrected rates have exact Poisson ones).
"""
import argparse
import csv
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo(L.TZ)
UTC = timezone.utc
V6_LIVE = (L.V6_PROMOTE, L.V6_ROLLBACK)                          # promote 22:11Z, owner rollback about 22:40Z
MODEL_NAMES = {"oye_kivi": "v4", "oye_kivi_next": "v5", "oye_kivi_ctx": "v6", "oye_kivi_ctx2": "v6.2"}
MODELS = ("v4", "v5", "v6", "v6.2")
EVENING_H = (18, 24)
CLASSES = ("true", "false", "unclear", "not-a-wake")          # the owner's and the autolabel's live labels
CAUSES = ("other-speech", "tv-media", "noise")
AUDIT_FIELDS = ("kind", "table", "id", "device", "madrid_date", "day_role", "evening", "model_live", "trigger", "label",
                "label_source", "label_conf", "speaker", "cause", "near_phrase")
KEY_FIELDS = ("sample_id", "stratum", "cell", "run_id", "device", "madrid_date", "day_role", "evening", "model_live",
              "trigger", "label", "label_source", "label_conf", "speaker")
REPLAY_TABLES = ("c3_events.csv", "c3_events_r2.csv", "daily_false_wakes.csv")
MAPPINGS = {"c3_events.csv": "c3_events_mapping.json", "c3_events_r2.csv": "c3_events_r2_mapping.json"}
DRAWS = 4000
PRIOR = 0.5            # Dirichlet prior per class (Jeffreys)
POOL_WEIGHT = 2.0      # pseudo-observations at the stratum's pooled owner-label shares: thin cells borrow from the stratum
CI = (2.5, 97.5)

# (stratum, auto label, trigger, default sample size); the sum of the defaults is 40. auto-false is two strata by the
# autolabel's own confidence: 0.95 (no words, almost no speech) and below UNCERTAIN_CONF (unrelated words heard, 0.75),
# the second being the one where a mis-heard "oye Kivi" would hide.
HIGH_CONF = 0.8
STRATA = (
    ("auto-false-low", "false", "wake", 9),
    ("auto-false-high", "false", "wake", 6),
    ("auto-true", "true", "wake", 10),
    ("auto-unclear", "unclear", "wake", 10),
    ("auto-not-a-wake-wake", "not-a-wake", "wake", 2),
    ("auto-not-a-wake-button", "not-a-wake", "button", 3),
)
NOSOURCE_STRATUM = "nosource-false"                          # optional: older false labels without label_source


# ---------------------------------------------------------------------------------------------
# time and model helpers
# ---------------------------------------------------------------------------------------------


def parse_stamp(utc):
    return datetime.strptime(utc, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)


def dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def v6_window():
    return dt(V6_LIVE[0]), dt(V6_LIVE[1])


def model_live(row_model, when):
    """v4 / v5 / v6 live at `when` (aware datetime) on a TV whose live_wakes.csv model column is `row_model`."""
    lo, hi = v6_window()
    if lo <= when < hi:
        return "v6"
    return MODEL_NAMES.get(row_model, row_model or "?")


def madrid(when):
    m = when.astimezone(TZ)
    return m.date().isoformat(), ("test" if m.day % 2 else "mine"), EVENING_H[0] <= m.hour < EVENING_H[1]


def model_intervals(device, until_day):
    """[(start, end, model)] of the live-model eras of one TV from the scope start to the end of the Madrid day
    `until_day` (inclusive): the era table of kivi_live_wakes (`model_of`) with the v6 window cut in."""
    end = datetime.fromisoformat(until_day).replace(tzinfo=TZ) + timedelta(days=1)
    v6a, v6b = v6_window()
    start = dt(L.SCOPE_START)
    cuts = {start, end, v6a, v6b}
    for _m, lo, hi, devs in L.ERAS:
        if devs is None or device in devs:
            cuts |= {dt(lo)} | ({dt(hi)} if hi else set())
    cuts = sorted(c for c in cuts if start <= c <= end)
    out = []
    for lo, hi in zip(cuts, cuts[1:]):
        mid = lo + (hi - lo) / 2
        m = "v6" if v6a <= mid < v6b else MODEL_NAMES.get(L.model_of(device, mid.timestamp()))
        out.append((lo, hi, m))
    return out


def evening_hours(lo, hi):
    """Hours of [lo, hi) that fall in the Madrid evenings."""
    total = 0.0
    day = lo.astimezone(TZ).date()
    while True:
        e0 = datetime(day.year, day.month, day.day, EVENING_H[0], tzinfo=TZ)
        e1 = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(days=1)
        if e0 >= hi:
            break
        a, b = max(lo, e0), min(hi, e1)
        if b > a:
            total += (b - a).total_seconds() / 3600
        day += timedelta(days=1)
    return total


def exposure(device, model, until_day):
    """Nominal evenings (6 h each) of `model` live on `device` up to the end of Madrid day `until_day`."""
    h = sum(evening_hours(lo, hi) for lo, hi, m in model_intervals(device, until_day) if m == model)
    return h / (EVENING_H[1] - EVENING_H[0])


# ---------------------------------------------------------------------------------------------
# audit table
# ---------------------------------------------------------------------------------------------


def evidence(text):
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k, v)
    return out


def source_of(row):
    if not row.get("label"):
        return ""
    s = row.get("label_source", "")
    return s if s else "no-source"


def live_audit_rows(live_rows):
    out = []
    for r in live_rows:
        when = parse_stamp(r["utc"]) + timedelta(seconds=float(r["wake_len"] or 0))
        date, role, evening = madrid(when)
        ev = evidence(r.get("auto_evidence"))
        near = int((ev.get("oye") == "1" or ev.get("kivi") == "1") and ev.get("ph") != "1")
        out.append({"kind": "live", "table": "live_wakes.csv", "id": r["run_id"], "device": r["device"],
                    "madrid_date": date, "day_role": role, "evening": "yes" if evening else "no",
                    "model_live": model_live(r["model"], when), "trigger": r["trigger"], "label": r["label"],
                    "label_source": source_of(r), "label_conf": r["label_conf"], "speaker": r["speaker"],
                    "cause": "", "near_phrase": near})
    return out


def read_mapping(kh, table):
    name = MAPPINGS.get(table)
    p = Path(kh) / "v6" / "all" / name if name else None
    if not p or not p.exists():
        return {}
    return {m["id"]: m for m in json.loads(p.read_text(encoding="utf-8"))}


def replay_audit_rows(root, kh):
    out = []
    for name in REPLAY_TABLES:
        p = Path(root) / "live-misses" / name
        mapping = read_mapping(kh, name)
        for r in L.read_miss_table(p):
            when = parse_stamp(r["utc"])
            date, role, evening = madrid(when)
            models = mapping.get(r["id"], {}).get("models")
            trig = "replay:" + "+".join(sorted(models)) if models else "replay:live"
            out.append({"kind": "replay", "table": name, "id": r["id"], "device": r["device"], "madrid_date": date,
                        "day_role": role, "evening": "yes" if evening else "no",
                        "model_live": model_live("oye_kivi_next", when), "trigger": trig, "label": r["label"],
                        "label_source": "owner" if r["label"] else "", "label_conf": "", "speaker": r["speaker"],
                        "cause": r["label"], "near_phrase": ""})
    return out


def write_audit_csv(path, rows, fields=AUDIT_FIELDS):
    L.write_csv_atomic(path, fields, rows)


def poisson_ci(k, exposure_):
    """Exact (Garwood) 95 % interval of a Poisson rate per unit of exposure."""
    from scipy.stats import chi2
    lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
    hi = chi2.ppf(0.975, 2 * (k + 1)) / 2
    return lo / exposure_, hi / exposure_


def fmt_rate(k, expo):
    if expo < 0.25:
        return f"n/a (exposure {expo:.2f})"
    lo, hi = poisson_ci(k, expo)
    return f"{k / expo:.2f} ({lo:.2f}-{hi:.2f})"


def md_table(head, rows):
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def summarize_live(rows, until_day):
    """Markdown: per TV and model, live wake-triggered false wakes split by label provenance, and the rate per evening."""
    wake = [r for r in rows if r["kind"] == "live" and r["trigger"] == "wake"]
    out = []
    head = ("TV", "model", "evenings", "wakes", "false: owner", "false: auto", "false: no source", "false total",
            "unclear", "true", "not-a-wake", "false in evening", "false per evening (95 % CI)")
    tab = []
    for dev in sorted({r["device"] for r in wake}):
        for m in MODELS:
            rs = [r for r in wake if r["device"] == dev and r["model_live"] == m]
            ex = exposure(dev, m, until_day)
            if not rs and ex < 0.01:
                continue
            f = [r for r in rs if r["label"] == "false"]
            src = Counter(r["label_source"] for r in f)
            fe = sum(1 for r in f if r["evening"] == "yes")
            tab.append((dev, m, f"{ex:.2f}", len(rs), src["owner"], src["auto-v1"], src["no-source"], len(f),
                        sum(1 for r in rs if r["label"] == "unclear"), sum(1 for r in rs if r["label"] == "true"),
                        sum(1 for r in rs if r["label"] == "not-a-wake"), fe, fmt_rate(fe, ex)))
    out.append(md_table(head, tab))
    return "\n".join(out)


def summarize_device_evenings(rows, until_day):
    """Markdown: per TV and Madrid day, the wake-triggered clips of the evening (18-24 h) by label provenance."""
    wake = [r for r in rows if r["kind"] == "live" and r["trigger"] == "wake"]
    tab = []
    for dev, day in sorted({(r["device"], r["madrid_date"]) for r in wake}):
        rs = [r for r in wake if r["device"] == dev and r["madrid_date"] == day]
        ev = [r for r in rs if r["evening"] == "yes"]
        f = Counter(r["label_source"] for r in ev if r["label"] == "false")
        models = "+".join(sorted({r["model_live"] for r in rs}))
        tab.append((dev, day, rs[0]["day_role"], models, len(rs), len(ev), f["owner"], f["auto-v1"], f["no-source"],
                    sum(f.values()), sum(1 for r in ev if r["label"] == "unclear"),
                    sum(1 for r in ev if r["label"] == "true")))
    return md_table(("TV", "Madrid day", "role", "model(s)", "wake clips (day)", "wake clips (evening)",
                     "false evening: owner", "auto", "no source", "false evening total", "unclear evening",
                     "true evening"), tab)


def summarize_trust(rows):
    wake = [r for r in rows if r["kind"] == "live" and r["trigger"] == "wake"]
    c = Counter((r["label"] or "(none)", r["label_source"] or "-") for r in wake)
    tab = [(lab, src, n) for (lab, src), n in sorted(c.items())]
    return md_table(("label", "source", "wake-triggered clips"), tab)


def summarize_replay(rows):
    rep = [r for r in rows if r["kind"] == "replay"]
    out = []
    tab = []
    for t in REPLAY_TABLES:
        rs = [r for r in rep if r["table"] == t]
        lab = Counter(r["label"] or "(unlabelled)" for r in rs)
        osp = Counter(r["speaker"] or "-" for r in rs if r["label"] == "other-speech")
        att = Counter(r["speaker"] or "-" for r in rs if r["label"] == "attempt")
        tab.append((t, len(rs), lab["attempt"], lab["other-speech"], dict(sorted(osp.items())) or "-", lab["tv-media"],
                    lab["noise"], lab["unclear"], lab["(unlabelled)"], sum(1 for r in rs if r["evening"] == "yes")))
    out.append(md_table(("table", "events", "attempt", "other-speech", "other-speech by speaker", "tv-media", "noise",
                         "unclear", "unlabelled", "in the evening"), tab))
    v5 = [r for r in rep if "v5" in r["trigger"].replace("replay:", "").split("+") or r["trigger"] == "replay:live"]
    lab = Counter(r["label"] or "(unlabelled)" for r in v5)
    out.append(f"\nEvents the live model v5 fired on in the replay (the rest fired only on a candidate model: v6, v6.2 "
               f"or the ablation): {len(v5)}; labels {dict(sorted(lab.items()))}.")
    return "\n".join(out)


def summarize_causes(rows):
    """Per cause, where labelled (owner): replay events by device and the live clips an owner labelled."""
    rep = [r for r in rows if r["kind"] == "replay" and r["label"]]
    tab = []
    for dev in sorted({r["device"] for r in rep}):
        c = Counter(r["label"] for r in rep if r["device"] == dev)
        tab.append((dev, c["attempt"], c["other-speech"], c["tv-media"], c["noise"], c["unclear"], "not recorded"))
    out = [md_table(("TV (replay events, owner labels)", "attempt", "other-speech", "tv-media", "noise", "unclear",
                     "near-phrase"), tab)]
    live = [r for r in rows if r["kind"] == "live" and r["trigger"] == "wake"]
    near = [r for r in live if r["near_phrase"] == 1]
    nlab = Counter(r["label"] for r in near)
    out.append("")
    out.append(f"Live wake-triggered clips whose ASR heard an oye-like or kivi-like word without the full phrase "
               f"(autolabel evidence, a proxy for near-phrase, not a label): {len(near)} of {len(live)} "
               f"(labels: {dict(sorted(nlab.items()))}). Live clips have no recorded cause; the validation sample adds it.")
    return "\n".join(out)


def build_table(root, kh, until=None):
    live_rows = L.read_table(Path(root) / "live" / "live_wakes.csv")
    audit = live_audit_rows(live_rows) + replay_audit_rows(root, kh)
    if until is None:
        until = max((r["madrid_date"] for r in audit if r["kind"] == "live"), default=datetime.now(TZ).date().isoformat())
    return audit, until


def cmd_table(a, out=print):
    root, kh = Path(a.dataset_root).expanduser(), C.kivi_home()
    audit, until = build_table(root, kh, a.until)
    dest = Path(a.out).expanduser() if a.out else kh / "audit" / "false_wake_audit.csv"
    if a.out:
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        C.private_mkdir(dest.parent, kh)
    write_audit_csv(dest, audit)
    live = [r for r in audit if r["kind"] == "live"]
    out(f"audit table: {len(audit)} rows ({len(live)} live clips, {len(audit) - len(live)} replay events), "
        f"live days to {until} (Madrid); written to {dest}")
    out("\n## Live wake-triggered clips: label provenance\n\n" + summarize_trust(audit))
    out("\n## Live false wakes per TV and model (counts; per evening = false in the evening / nominal evenings)\n\n"
        + summarize_live(audit, until))
    out("\n## Live wake-triggered clips per TV and day (evening columns: 18-24 h Madrid)\n\n"
        + summarize_device_evenings(audit, until))
    out("\n## Replay events (owner labels)\n\n" + summarize_replay(audit))
    out("\n## Causes where labelled\n\n" + summarize_causes(audit))
    return audit


# ---------------------------------------------------------------------------------------------
# blind validation sample
# ---------------------------------------------------------------------------------------------


def stratum_of(row):
    """Stratum of an audit/key row, or None when it is not sampled (owner label, no-source, other triggers)."""
    if row["label_source"] != "auto-v1":
        return NOSOURCE_STRATUM if (row["label_source"] == "no-source" and row["label"] == "false"
                                    and row["trigger"] == "wake") else None
    for name, label, trigger, _n in STRATA:
        if row["label"] == label and row["trigger"] == trigger:
            if label == "false" and (float(row["label_conf"] or 0) >= HIGH_CONF) != name.endswith("high"):
                continue
            return name
    return None


def cell_of(row):
    return f"{row['device']}/{row['day_role']}"


def allocate(cells, n, rng):
    """Equal share per cell (round robin in a random order) with the cell sizes as caps: {cell: k}, sum = min(n, total)."""
    left = dict(cells)
    got = dict.fromkeys(cells, 0)
    while n > 0 and any(left.values()):
        order = [c for c in left if left[c] > 0]
        rng.shuffle(order)
        for c in order:
            if n == 0:
                break
            got[c] += 1
            left[c] -= 1
            n -= 1
    return got


def draw_sample(key_rows, sizes, seed):
    """key_rows: audit rows (live, with run id in `id`). Returns {run_id: sample_id}, assigned in a shuffled order."""
    rng = random.Random(seed)
    chosen = []
    for name, n in sizes.items():
        pool = sorted((r for r in key_rows if stratum_of(r) == name), key=lambda r: r["id"])
        cells = defaultdict(list)
        for r in pool:
            cells[cell_of(r)].append(r)
        for cell, k in allocate({c: len(v) for c, v in cells.items()}, n, rng).items():
            chosen += rng.sample(cells[cell], k)
    rng.shuffle(chosen)
    return {r["id"]: f"v{i:02d}" for i, r in enumerate(chosen, 1)}


def cmd_sample(a, out=print):
    root, kh = Path(a.dataset_root).expanduser(), C.kivi_home()
    live = root / "live"
    val_path, key_path = live / L.VALIDATION_TABLE, live / L.VALIDATION_KEY
    if val_path.exists() or key_path.exists():
        raise SystemExit(f"{val_path.name} or the key exists: not overwritten (owner labels would be lost); "
                         "move both away to build a new sample")
    audit, until = build_table(root, kh, a.until)
    rows = [r for r in audit if r["kind"] == "live"]
    sizes = {name: n for name, _l, _t, n in STRATA}
    if a.n_nosource:
        sizes[NOSOURCE_STRATUM] = a.n_nosource
    ids = draw_sample(rows, sizes, a.seed)
    by_id = {r["id"]: r for r in rows}
    live_rows = {r["run_id"]: r for r in L.read_table(live / "live_wakes.csv")}
    key = []
    for r in rows:
        key.append({"sample_id": ids.get(r["id"], ""), "stratum": stratum_of(r) or "", "cell": cell_of(r),
                    "run_id": r["id"], **{k: r[k] for k in ("device", "madrid_date", "day_role", "evening", "model_live",
                                                            "trigger", "label", "label_source", "label_conf", "speaker")}})
    val = [{"sample_id": sid, "run_id": rid, "device": by_id[rid]["device"], "utc": live_rows[rid]["utc"], "label": "",
            "cause": "", "speaker": ""} for rid, sid in sorted(ids.items(), key=lambda kv: kv[1])]
    meta = {"seed": a.seed, "until": until, "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sizes": sizes,
            "strata": {n: {"population": sum(1 for r in rows if stratum_of(r) == n),
                           "sampled": sum(1 for k in key if k["stratum"] == n and k["sample_id"])}
                       for n in sizes}}
    L.write_csv_atomic(key_path, KEY_FIELDS, key)
    key_path.with_suffix(".json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(key_path.with_suffix(".json"), 0o600)
    L.write_csv_atomic(val_path, L.VALIDATION_FIELDS, val)
    out(f"sample: {len(val)} clips in {val_path}; key (auto labels, frozen) in {key_path}")
    for n, m in meta["strata"].items():
        cells = Counter(k["cell"] for k in key if k["stratum"] == n and k["sample_id"])
        out(f"  {n}: {m['sampled']} of {m['population']}  {dict(sorted(cells.items()))}")
    play_s = sum(min(L.TAIL_S, float(live_rows[r["run_id"]]["wake_len"] or 0)) + float(live_rows[r["run_id"]]["stt_len"] or 0)
                 for r in val)
    out(f"playing time of the sample: {play_s / 60:.1f} min once, plus the owner's keystrokes")
    out("owner command: " + owner_command())
    return val


def owner_command():
    return "~/kivi-models/data/.venv/bin/python ~/homelab/scripts/voice-assistant/wakeword/kivi_live_wakes.py queue --minutes 15"


# ---------------------------------------------------------------------------------------------
# analysis of the owner's labels against the frozen key
# ---------------------------------------------------------------------------------------------


def read_csv_rows(path, fields):
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in fields} for row in csv.DictReader(f)]


def simulate(key, owner, draws=DRAWS, seed=20261112):
    """Population class draws. key: rows of the key; owner: {sample_id: label}. Returns (units, cls) where cls is an
    int array [draws, len(units)] with the CLASSES index of each unit (the owner's truth), -1 for units that are not
    classified (a trigger outside the strata). Sampled units are fixed at the owner's label; owner / no-source labels
    are taken as given; the other auto units come from the partly pooled Dirichlet posterior of their cell."""
    import numpy as np
    rng = np.random.default_rng(seed)
    idx = {c: i for i, c in enumerate(CLASSES)}
    units = list(key)
    cls = np.full((draws, len(units)), -1, dtype=np.int8)
    cells = defaultdict(lambda: {"sampled": [], "rest": []})
    for j, u in enumerate(units):
        st = stratum_of(u)
        if u["sample_id"] and owner.get(u["sample_id"]):
            cls[:, j] = idx[owner[u["sample_id"]]]
            if st:
                cells[(st, u["cell"])]["sampled"].append(j)
        elif st and st != NOSOURCE_STRATUM:
            cells[(st, u["cell"])]["rest"].append(j)
        elif u["label"] in idx:
            cls[:, j] = idx[u["label"]]                        # an owner label, or a no-source label, taken as given
    pooled = defaultdict(lambda: np.zeros(len(CLASSES)))
    for (st, _cell), g in cells.items():
        for j in g["sampled"]:
            pooled[st][cls[0, j]] += 1
    for (st, _cell), g in cells.items():
        rest = g["rest"]
        if not rest:
            continue
        counts = np.zeros(len(CLASSES))
        for j in g["sampled"]:
            counts[cls[0, j]] += 1
        prop = pooled[st] / pooled[st].sum() if pooled[st].sum() else np.full(len(CLASSES), 1 / len(CLASSES))
        theta = rng.dirichlet(counts + PRIOR + POOL_WEIGHT * prop, size=draws)          # [draws, 4]
        for d in range(draws):
            cls[d, rest] = rng.choice(len(CLASSES), size=len(rest), p=theta[d])
    return units, cls


def pct(x):
    import numpy as np
    return float(np.mean(x)), float(np.percentile(x, CI[0])), float(np.percentile(x, CI[1]))


def analyze(key, validation, until, draws=DRAWS, seed=20261112):
    """key: key rows; validation: validation rows (owner labels). Returns a dict of numbers (see cmd_analyze)."""
    import numpy as np
    owner = {v["sample_id"]: v["label"] for v in validation if v["label"]}
    for v in validation:
        if v["label"] and v["label"] not in CLASSES:
            raise ValueError(f"unknown label {v['label']!r} for {v['sample_id']}")
        if v["label"] == "false" and v["cause"] and v["cause"] not in CAUSES:
            raise ValueError(f"unknown cause {v['cause']!r} for {v['sample_id']}")
    units, cls = simulate(key, owner, draws, seed)
    idx = {c: i for i, c in enumerate(CLASSES)}
    by = lambda f: np.array([bool(f(u)) for u in units])        # noqa: E731
    auto = by(lambda u: u["label_source"] == "auto-v1" and stratum_of(u) in {s[0] for s in STRATA})
    auto_false = by(lambda u: u["label_source"] == "auto-v1" and (stratum_of(u) or "").startswith("auto-false"))
    res = {"labelled": len(owner), "sampled": sum(1 for u in units if u["sample_id"])}
    isf = cls == idx["false"]
    n_af = auto_false.sum()
    prec = isf[:, auto_false].sum(1) / n_af if n_af else np.zeros(draws)
    tot_false_auto = isf[:, auto].sum(1)
    rec = np.divide(isf[:, auto_false].sum(1), tot_false_auto, out=np.zeros(draws), where=tot_false_auto > 0)
    res["precision_auto_false"] = pct(prec)
    res["p_precision_ge_90"] = float(np.mean(prec >= 0.9))
    res["recall_auto_false"] = pct(rec)
    res["auto_false_owner_true"] = pct((cls[:, auto_false] == idx["true"]).sum(1) / max(n_af, 1))
    res["est_owner_false_among_auto"] = pct(tot_false_auto)
    # sample confusion matrix: auto label x owner label (counts); and the estimated population matrix (means)
    conf = {a: dict.fromkeys(CLASSES, 0) for a in CLASSES}
    for u in units:
        if u["sample_id"] and owner.get(u["sample_id"]):
            conf[u["label"]][owner[u["sample_id"]]] += 1
    res["confusion_sample"] = conf
    pop = {}
    for a_ in CLASSES:
        m = by(lambda u, a_=a_: u["label_source"] == "auto-v1" and u["label"] == a_ and stratum_of(u))
        pop[a_] = {c: float((cls[:, m] == idx[c]).sum(1).mean()) for c in CLASSES}
    res["confusion_population"] = pop
    res["cause_sample"] = dict(Counter((v["cause"] or "-") for v in validation if v["label"] == "false"))
    res["speaker_sample"] = dict(Counter((v["speaker"] or "-") for v in validation if v["label"] in ("true", "unclear")
                                         or v["cause"] == "other-speech"))
    # corrected false wakes per evening, wake-triggered clips, per TV and model, plus all models together
    wake = by(lambda u: u["trigger"] == "wake")
    rows = []
    for dev in sorted({u["device"] for u in units}):
        for m in MODELS + ("all",):
            sel = wake & by(lambda u, dev=dev, m=m: u["device"] == dev and (m == "all" or u["model_live"] == m))
            if not sel.any():
                continue
            ev = sel & by(lambda u: u["evening"] == "yes")
            expo = exposure_all(dev, until) if m == "all" else exposure(dev, m, until)
            naive = int(sum(1 for j in np.flatnonzero(ev) if units[j]["label"] == "false"))
            corrected = isf[:, ev].sum(1)
            rows.append({"device": dev, "model": m, "exposure": expo, "wakes": int(sel.sum()),
                         "naive_false_evening": naive, "corrected_false_evening": pct(corrected),
                         "corrected_per_evening": pct(corrected / expo) if expo >= 0.25 else None})
    res["rates"] = rows
    return res


def exposure_all(device, until_day):
    return sum(exposure(device, m, until_day) for m in MODELS)


def render_analysis(res):
    f = lambda t: f"{t[0] * 100:.0f} % ({t[1] * 100:.0f}-{t[2] * 100:.0f})"      # noqa: E731
    out = [f"labelled {res['labelled']} of {res['sampled']} sampled clips"]
    out.append("\n### Confusion matrix of the sample (rows: autolabel, columns: owner)\n")
    out.append(md_table(("auto \\ owner",) + CLASSES + ("n",),
                        [(a, *[res["confusion_sample"][a][c] for c in CLASSES], sum(res["confusion_sample"][a].values()))
                         for a in CLASSES]))
    out.append("\n### Estimated population matrix (owner truth among auto-labelled wake and not-a-wake clips, mean counts)\n")
    out.append(md_table(("auto \\ owner",) + CLASSES,
                        [(a, *[f"{res['confusion_population'][a][c]:.1f}" for c in CLASSES]) for a in CLASSES]))
    out.append("\n### Autolabel quality (mean, 95 % interval)\n")
    out.append(md_table(("measure", "value"), [
        ("precision of auto-false (owner agrees it is false)", f(res["precision_auto_false"])),
        ("P(precision >= 90 %)", f"{res['p_precision_ge_90'] * 100:.0f} %"),
        ("auto-false the owner calls true (missed attempts counted as false wakes)", f(res["auto_false_owner_true"])),
        ("recall of auto-false (share of owner-false clips the autolabel found)", f(res["recall_auto_false"])),
        ("estimated owner-false clips among the auto-labelled (count)",
         f"{res['est_owner_false_among_auto'][0]:.1f} ({res['est_owner_false_among_auto'][1]:.0f}-"
         f"{res['est_owner_false_among_auto'][2]:.0f})")]))
    out.append(f"\ncauses of owner-false clips in the sample: {res['cause_sample']}; "
               f"speakers (true, unclear, other-speech): {res['speaker_sample']}")
    out.append("\n### Corrected live false wakes in the evening, per TV and model\n")
    tab = []
    for r in res["rates"]:
        c = r["corrected_false_evening"]
        pe = r["corrected_per_evening"]
        tab.append((r["device"], r["model"], f"{r['exposure']:.2f}", r["wakes"], r["naive_false_evening"],
                    f"{c[0]:.1f} ({c[1]:.0f}-{c[2]:.0f})",
                    f"{pe[0]:.2f} ({pe[1]:.2f}-{pe[2]:.2f})" if pe else "n/a (exposure < 0.25)"))
    out.append(md_table(("TV", "model", "evenings", "wake clips", "false in evening: as labelled",
                         "corrected (95 %)", "corrected per evening (95 %)"), tab))
    return "\n".join(out)


def cmd_analyze(a, out=print):
    root = Path(a.dataset_root).expanduser()
    live = root / "live"
    key_path = Path(a.key).expanduser() if a.key else live / L.VALIDATION_KEY
    val_path = Path(a.validation).expanduser() if a.validation else live / L.VALIDATION_TABLE
    meta = json.loads(key_path.with_suffix(".json").read_text(encoding="utf-8"))
    key = read_csv_rows(key_path, KEY_FIELDS)
    val = L.read_validation(val_path)
    res = analyze(key, val, meta["until"], a.draws, a.seed)
    out(render_analysis(res))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["table", "sample", "analyze"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--until", help="last Madrid day covered (inclusive), default: the day of the latest live clip")
    ap.add_argument("--out", help="(table) CSV path, default $KIVI_HOME/audit/false_wake_audit.csv")
    ap.add_argument("--seed", type=int, default=20261112)
    ap.add_argument("--n-nosource", type=int, default=0,
                    help="(sample) also sample this many older false labels that carry no label_source (default 0)")
    ap.add_argument("--key", help="(analyze) key CSV, default live/autolabel_validation_key.csv")
    ap.add_argument("--validation", help="(analyze) validation CSV, default live/autolabel_validation.csv")
    ap.add_argument("--draws", type=int, default=DRAWS, help="(analyze) simulated populations")
    a = ap.parse_args(argv)
    {"table": cmd_table, "sample": cmd_sample, "analyze": cmd_analyze}[a.what](a)


if __name__ == "__main__":
    main()
