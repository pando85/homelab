#!/usr/bin/env python3
"""Owner-run prompter plus dual PC recorder for real-voice "kivi" measurements and training data.

The OWNER runs this and speaks. Nothing here plays sound: the cues are text on the terminal.
It records two PC microphones (the desk mic and the webcam mic) at 16 kHz mono into separate WAVs
from one process, and writes `session.json` (session id, speaker, UTC start with monotonic offsets,
every cue with its time and label, the block conditions, the device names). The satellite's own
audio is taken afterwards from Home Assistant by `kivi_extract.py`.

    kivi_session.py --list-devices
    kivi_session.py --device c3-a --speaker spk01 --desk-mic Samson --webcam-mic C920
    kivi_session.py --print-protocol > my_blocks.json      # edit, then: --blocks my_blocks.json
    kivi_session.py --dry-run                              # fake tones, no microphone, fast

Default protocol (one session, about 12 min): at 1.5 m and at 3 m, each with the TV silent (on, with nothing
playing: the satellite runs on the TV, so it must stay on) and with the TV playing, 10 x "kivi" (a cue about every 5 s, +-1 s); then the near-words kiwi, tivi, kili, kiri,
Pipi and "aquí vi", 2 each; then 60 s of silence with you present. "Enter when ready" separates
the blocks; the recording runs through the pauses (they are logged and never used as negatives).
Use `--repeat N`, `--only NAME`, `--tag volume=loud` and a `--blocks` file for the 4-6 training
sessions with varied volume, speed and position.

Privacy: the owner records, the owner consents for every voice; the speaker is a pseudonymous id
(spk01...), never a name. `--dry-run` and `--fake-source` never open a microphone.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
CHUNK_BYTES = 3200            # 100 ms of S16 mono
NEAR_WORDS = ["kiwi", "tivi", "kili", "kiri", "Pipi", "aquí vi"]
MIN_GAP_S = 3.0               # smallest allowed time between two cues (interval - jitter)
LEAD_IN_S = 3.0
TAIL_S = 4.0


# ---------------------------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------------------------


def default_blocks():
    blocks = []
    for pos in (1.5, 3):
        for tv in ("off", "on"):
            blocks.append({"name": f"kivi-{pos}m-tv{tv}", "kind": "kivi", "n": 10, "interval_s": 5.0,
                           "jitter_s": 1.0, "conditions": {"position_m": pos, "tv": tv}})
    blocks.append({"name": "near-words", "kind": "near", "words": NEAR_WORDS, "each": 2, "interval_s": 5.0,
                   "jitter_s": 1.0, "conditions": {"position_m": 1.5, "tv": "off"}})
    blocks.append({"name": "silence", "kind": "silence", "seconds": 60,
                   "conditions": {"position_m": 1.5, "tv": "off"}})
    return blocks


def load_blocks(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["blocks"] if isinstance(data, dict) else data


def validate_blocks(blocks):
    names = set()
    for b in blocks:
        if b.get("kind") not in ("kivi", "near", "silence"):
            raise SystemExit(f"block {b.get('name')!r}: kind must be kivi, near or silence")
        if not b.get("name") or b["name"] in names:
            raise SystemExit(f"block names must be unique and non-empty (got {b.get('name')!r})")
        names.add(b["name"])
        if b["kind"] == "silence":
            if float(b.get("seconds", 0)) <= 0:
                raise SystemExit(f"block {b['name']!r}: seconds must be > 0")
            continue
        if float(b.get("interval_s", 5)) - float(b.get("jitter_s", 1)) < MIN_GAP_S:
            raise SystemExit(f"block {b['name']!r}: interval_s - jitter_s must be >= {MIN_GAP_S} s "
                             "(clips of consecutive cues must not overlap)")
        if b["kind"] == "near" and not b.get("words"):
            raise SystemExit(f"block {b['name']!r}: near blocks need a words list")
    return blocks


def expand_blocks(blocks, only=None, repeat=1):
    if only:
        wanted = set(only)
        missing = wanted - {b["name"] for b in blocks}
        if missing:
            raise SystemExit(f"--only: unknown block(s) {sorted(missing)}; have {[b['name'] for b in blocks]}")
        blocks = [b for b in blocks if b["name"] in wanted]
    out = []
    for r in range(1, repeat + 1):
        for b in blocks:
            b = json.loads(json.dumps(b))
            if repeat > 1:
                b["name"] = f"{b['name']}-r{r}"
            out.append(b)
    return out


def instruction(b):
    c = b.get("conditions", {})
    where = f"{c['position_m']} m from the TV" if "position_m" in c else "your usual place"
    tv = {"off": "TV SILENT: keep the TV on, with nothing playing (no sound)",
          "on": "TV ON at normal volume (content of your choice)"}.get(c.get("tv"), "")
    extra = ", ".join(f"{k}={v}" for k, v in c.items() if k not in ("position_m", "tv"))
    if b["kind"] == "kivi":
        what = f"say KIVI {b['n']} times, a cue about every {b.get('interval_s', 5):g} s"
    elif b["kind"] == "near":
        what = (f"say each word {b.get('each', 2)} times in the order shown ("
                + ", ".join(b["words"]) + ")")
    else:
        what = f"stay where you are and do NOT speak for {b['seconds']:g} s"
    return b.get("instruction") or f"{where}, {tv}{', ' + extra if extra else ''}: {what}."


# ---------------------------------------------------------------------------------------------
# clock and sources
# ---------------------------------------------------------------------------------------------


class Clock:
    """Session time in seconds since t0 (monotonic). `scale` < 1 compresses waits for dry runs."""

    def __init__(self, scale=1.0):
        self.scale = scale
        self.t0_utc = datetime.now(timezone.utc)
        self.t0_mono_ns = time.monotonic_ns()

    def now(self):
        return (time.monotonic_ns() - self.t0_mono_ns) / 1e9 / self.scale

    def sleep_until(self, t):
        d = (t - self.now()) * self.scale
        if d > 0:
            time.sleep(d)

    def utc(self, offset_s):
        return (self.t0_utc + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Source:
    """Common part of a recorded source: one WAV, first-sample time, a progress series."""

    def __init__(self, label, device, path, clock):
        self.label, self.device, self.path, self.clock = label, device, path, clock
        self.wav = C.WavWriter(path)
        self.first_sample_offset_s = None
        self.progress = []       # (session offset s, samples written), every 10 s of audio
        self.error = None
        self._thread = None
        self._stop = threading.Event()

    def _note(self, n_new_bytes):
        n = self.wav.n // 2
        if not self.progress or n - self.progress[-1][1] >= 10 * RATE:
            self.progress.append((round(self.clock.now(), 3), n))

    def wait_first(self, timeout=8.0):
        t = time.monotonic()
        while self.first_sample_offset_s is None and time.monotonic() - t < timeout and not self.error:
            time.sleep(0.02)
        return self.first_sample_offset_s is not None

    def finish(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.wav.close()

    def info(self):
        return {"device_name": self.device, "file": Path(self.path).name, "rate": RATE, "channels": 1,
                "samples": self.wav.n // 2, "first_sample_offset_s": self.first_sample_offset_s,
                "progress": self.progress, "error": self.error}


class ParecSource(_Source):
    """A PipeWire/PulseAudio source through `parec` (resampled and downmixed to 16 kHz mono by the
    sound server). Only the owner's real run opens it."""

    def start(self):
        cmd = ["parec", f"--device={self.device}", f"--rate={RATE}", "--channels=1", "--format=s16le",
               "--raw", "--latency-msec=50"]
        # its own session and process group: Ctrl-C goes to the prompter only, which stops the recorders cleanly
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        carry = b""
        while not self._stop.is_set():
            chunk = self.proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                if not self._stop.is_set():
                    self.error = f"{self.label}: recorder ended early (device gone or busy?)"
                break
            if self.first_sample_offset_s is None:  # the chunk just completed: it began one chunk ago
                self.first_sample_offset_s = round(self.clock.now() - len(chunk) / 2 / RATE, 4)
            chunk = carry + chunk
            carry = chunk[len(chunk) // 2 * 2:]
            chunk = chunk[:len(chunk) // 2 * 2]
            self.wav.write(chunk)
            self._note(len(chunk))
            if self.wav.n % (RATE * 2 * 2) < CHUNK_BYTES:
                self.wav.flush_header()

    def finish(self):
        self._stop.set()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            pass
        super().finish()


class FakeSource(_Source):
    """Synthetic source for tests and --dry-run: a 400 Hz tone or silence on the session clock.
    No microphone is touched."""

    def __init__(self, label, kind, path, clock, name=None):
        super().__init__(label, name or f"fake:{kind}", path, clock)
        n = RATE  # one second tile; 400 Hz has a whole number of periods in it
        if kind == "sine":
            import math
            tile = b"".join(int(6000 * math.sin(2 * math.pi * 400 * i / RATE)).to_bytes(2, "little", signed=True)
                            for i in range(n))
        else:
            tile = b"\x00\x00" * n
        self.tile2 = tile * 2
        self.kind = kind

    def start(self):
        self.first_sample_offset_s = round(self.clock.now(), 4)
        self._start_off = self.first_sample_offset_s
        self._thread = threading.Thread(target=self._gen, daemon=True)
        self._thread.start()

    def _emit(self):
        target = int((self.clock.now() - self._start_off) * RATE)
        have = self.wav.n // 2
        while have < target:
            k = min(target - have, RATE)
            i = have % RATE
            seg = self.tile2[2 * i:2 * (i + k)]
            self.wav.write(seg)
            have += k
        self._note(0)

    def _gen(self):
        while not self._stop.is_set():
            self._emit()
            time.sleep(0.01)

    def finish(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._emit()
        self.wav.close()


# ---------------------------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------------------------


def list_sources():
    """Input sources from PipeWire/Pulse: name, description, state, sample spec. Metadata only;
    nothing is opened for recording."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True,
                             timeout=10, check=True).stdout
        return [{"name": s["name"], "description": s.get("description", ""), "state": s.get("state", ""),
                 "spec": (s["sample_specification"] if isinstance(s["sample_specification"], str)
                          else "{format} {channels}ch {rate}Hz".format(**s["sample_specification"]))}
                for s in json.loads(out)]
    except Exception:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True,
                             timeout=10, check=True).stdout
        rows = []
        for line in out.splitlines():
            f = line.split("\t")
            rows.append({"name": f[1], "description": "", "state": f[-1], "spec": f[3] if len(f) > 3 else ""})
        return rows


def print_sources():
    rows = list_sources()
    for i, s in enumerate(rows, 1):
        flag = []
        if s["name"].endswith(".monitor"):
            flag.append("monitor of an output, not a microphone")
        if "easyeffects" in s["name"]:
            flag.append("processed by Easy Effects, avoid for the raw dataset")
        print(f"{i}. {s['name']}\n     {s['description']} | {s['spec']} | {s['state']}"
              + (f"\n     NOTE: {'; '.join(flag)}" if flag else ""))
    print("\nPick the hardware inputs (the alsa_input.* lines): --desk-mic NAME --webcam-mic NAME. "
          "A unique part of the name is enough, for example --desk-mic Samson --webcam-mic C920.")


def resolve_source(spec, rows):
    names = [r["name"] for r in rows]
    if spec in names:
        return spec
    hits = [n for n in names if spec.lower() in n.lower() or spec.lower() in
            next(r for r in rows if r["name"] == n)["description"].lower()]
    if len(hits) != 1:
        raise SystemExit(f"--*-mic {spec!r} matches {len(hits)} sources; use --list-devices and a longer name")
    return hits[0]


# ---------------------------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------------------------


def write_session_json(path, session):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(session, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def run_block(b, clock, rng, say, rec, on_update=lambda: None):
    """Show the cues of one block, filling `rec` (already part of the session record, so an
    interrupted block is kept with the cues shown so far)."""
    rec.update({"name": b["name"], "kind": b["kind"], "conditions": b.get("conditions", {}),
                "start_offset_s": round(clock.now(), 3), "cues": [], "end_offset_s": None})
    rec["start_utc"] = clock.utc(rec["start_offset_s"])
    on_update()
    if b["kind"] == "silence":
        secs = float(b["seconds"])
        say(f"  SILENCE: do not speak for {secs:g} s (stay where you are)")
        end = rec["start_offset_s"] + secs
        t = rec["start_offset_s"] + 10
        while t < end:
            clock.sleep_until(t)
            say(f"  ... {end - clock.now():.0f} s left")
            t += 10
        clock.sleep_until(end)
    else:
        if b["kind"] == "kivi":
            items = [("kivi", "kivi")] * int(b["n"])
        else:
            items = [(f"near:{w.lower()}", w) for w in b["words"] for _ in range(int(b.get("each", 2)))]
            rng.shuffle(items)
        interval, jitter = float(b.get("interval_s", 5)), float(b.get("jitter_s", 1))
        t = rec["start_offset_s"] + LEAD_IN_S
        for i, (label, word) in enumerate(items, 1):
            clock.sleep_until(t)
            off = clock.now()
            shown = "KIVI" if b["kind"] == "kivi" else f'"{word}"'
            say(f"  >>> say {shown} now <<<   ({i}/{len(items)})", bold=True)
            rec["cues"].append({"index": i, "offset_s": round(off, 3), "utc": clock.utc(off), "label": label,
                                "word": word if b["kind"] == "near" else ""})
            on_update()
            t += interval + rng.uniform(-jitter, jitter)
        clock.sleep_until(rec["cues"][-1]["offset_s"] + TAIL_S)
    rec["end_offset_s"] = round(clock.now(), 3)
    rec["end_utc"] = clock.utc(rec["end_offset_s"])


def run(args, input_fn=input):
    if args.speaker:
        C.valid_speaker(args.speaker)
    C.valid_device(args.device)
    if args.split not in C.SPLITS:
        raise SystemExit(f"--split must be one of {C.SPLITS}")
    blocks = load_blocks(args.blocks) if args.blocks else default_blocks()
    blocks = expand_blocks(validate_blocks(blocks), args.only, args.repeat)
    tags = dict(t.split("=", 1) for t in args.tag or [])
    fake = args.dry_run or args.fake_source
    if fake:
        kinds = [args.fake_source or "sine"] * 2
        names = [f"fake:{kinds[0]}", f"fake:{kinds[1]}"]
        scale = args.time_scale or (0.01 if args.dry_run else 1.0)
    else:
        if not args.desk_mic or not args.webcam_mic:
            raise SystemExit("--desk-mic and --webcam-mic are required (see --list-devices)")
        if not shutil.which("parec"):
            raise SystemExit("parec not found (pulseaudio-utils / pipewire-pulse)")
        rows = list_sources()
        names = [resolve_source(args.desk_mic, rows), resolve_source(args.webcam_mic, rows)]
        if names[0] == names[1]:
            raise SystemExit("the desk mic and the webcam mic must be different sources")
        scale = 1.0
    wait = not (args.no_wait or args.dry_run)
    tty = sys.stdout.isatty()

    def say(msg="", bold=False):
        print(f"\033[1;7m{msg}\033[0m" if (bold and tty) else msg, flush=True)

    clock = Clock(scale)
    sid = clock.t0_utc.strftime("%Y%m%dT%H%M%SZ") + "-" + (args.speaker or "spkxx") + "-" + C.source_label(args.device)
    if args.dry_run:
        sid = "dry-" + sid
    sdir = Path(args.out_dir).expanduser() / sid
    sdir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sdir.mkdir(mode=0o700)
    session = {"schema": 1, "session_id": sid, "device": args.device, "tv_source": C.source_label(args.device),
               "speaker": args.speaker or "", "split": args.split, "tags": tags, "dry_run": bool(args.dry_run),
               "t0_utc": clock.t0_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
               "t0_mono_ns": clock.t0_mono_ns, "time_scale": scale, "seed": args.seed,
               "protocol": [b["name"] for b in blocks], "sources": {}, "blocks": [], "pauses": [],
               "status": "recording"}
    rng = random.Random(args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "little"))
    if fake:
        srcs = [FakeSource("pc-mic", kinds[0], sdir / "pc-mic.wav", clock, names[0]),
                FakeSource("webcam-mic", kinds[1], sdir / "webcam-mic.wav", clock, names[1])]
    else:
        srcs = [ParecSource("pc-mic", names[0], sdir / "pc-mic.wav", clock),
                ParecSource("webcam-mic", names[1], sdir / "webcam-mic.wav", clock)]
    say(f"Session {sid}\n  desk mic   : {names[0]}\n  webcam mic : {names[1]}\n"
        f"  satellite  : {args.device} (unmute it in Home Assistant before you start)\n"
        f"  {len(blocks)} blocks: " + ", ".join(b["name"] for b in blocks) + "\n  Cues are text only; nothing plays sound.")
    if not fake and not args.no_preflight:
        pre = C.pipeline_preflight(args.context, args.namespace, args.pod, args.container, args.model_name,
                                   Path(args.pipelines_file).read_text(encoding="utf-8") if args.pipelines_file else None)
        session["preflight"] = pre
        if "error" in pre:
            say(f"pre-flight: could not read the Assist pipelines ({pre['error']}); continuing")
        else:
            say(f"pre-flight: preferred Assist pipeline wake word is {pre['preferred_wake_word']!r}")
            if pre["live"] or pre["any_live"]:
                say("*" * 78 + f"\n* WARNING: {args.model_name!r} is a live wake word. Saying it can end the satellite's wake\n"
                    "* file mid-session (a detection). kivi_extract.py counts those cues as server detections,\n"
                    "* but audio after them is lost until the next run starts.\n" + "*" * 78)

    def snapshot():
        session["sources"] = {s.label: s.info() for s in srcs}
        session["end_offset_s"] = round(clock.now(), 3)
        write_session_json(sdir / "session.json", session)

    for s in srcs:
        s.start()
    try:
        if not all(s.wait_first() for s in srcs):
            raise SystemExit("a recorder produced no audio: " + "; ".join(s.error or "?" for s in srcs))
        snapshot()                    # session.json exists from here on, with the sources and first_sample_offset_s
        say("Recording both microphones. Ctrl-C stops the session (the files are kept).")
        pause_start = clock.now()
        for n, b in enumerate(blocks, 1):
            say(f"\nBlock {n}/{len(blocks)}: {b['name']}\n  {instruction(b)}")
            if wait:
                input_fn("  Press Enter when ready... ")
            session["pauses"].append({"before_block": n, "start_offset_s": round(pause_start, 3),
                                      "end_offset_s": round(clock.now(), 3)})
            rec = {"index": n}
            session["blocks"].append(rec)
            run_block(b, clock, rng, say, rec, snapshot)
            pause_start = clock.now()
            snapshot()
        session["status"] = "complete"
    except KeyboardInterrupt:
        session["status"] = "interrupted"
        say("\nInterrupted: keeping what was recorded.")
    finally:
        end = clock.now()
        for blk in session["blocks"]:
            if blk.get("end_offset_s") is None:
                blk["end_offset_s"], blk["end_utc"], blk["incomplete"] = round(end, 3), clock.utc(end), True
        for s in srcs:
            s.finish()
        session["end_offset_s"] = round(end, 3)
        session["end_utc"] = clock.utc(end)
        session["sources"] = {s.label: s.info() for s in srcs}
        if not fake:
            checks = {}
            for s in srcs:
                first = s.first_sample_offset_s or 0.0
                d = C.sample_drift(s.wav.n // 2, end - first)
                checks[s.label] = round(d, 5)
                if abs(d) > 0.005:
                    say(f"WARNING: {s.label} recorded {s.wav.n // 2 / RATE:.1f} s of audio in {end - first:.1f} s "
                        f"({d * 100:+.2f} %): samples were lost or the clock is off; its alignment may drift")
            session["sample_check"] = checks
        errs = [s.error for s in srcs if s.error]
        if errs:
            session["status"] = "error"
            session["errors"] = errs
        write_session_json(sdir / "session.json", session)
    n_cues = sum(len(b["cues"]) for b in session["blocks"])
    say(f"\nSession {session['status']}: {n_cues} cues in {len(session['blocks'])} blocks.\n"
        f"Files in {sdir}\nNext: kivi_extract.py --session-dir {sdir}")
    return sdir


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--print-protocol", action="store_true", help="print the default blocks as JSON")
    ap.add_argument("--device", default="c3-a", help="satellite under test (kivi_devices.json); default c3-a")
    ap.add_argument("--speaker", help="pseudonymous speaker id, e.g. spk01 (never a name)")
    ap.add_argument("--split", default="unassigned", choices=C.SPLITS, help="dataset split to record in the manifest")
    ap.add_argument("--desk-mic")
    ap.add_argument("--webcam-mic")
    ap.add_argument("--out-dir", default="~/kivi-sessions", help="where the owner's raw session lands (mode 700)")
    ap.add_argument("--blocks", help="JSON file with the block list (see --print-protocol)")
    ap.add_argument("--only", action="append", help="run only the named block(s)")
    ap.add_argument("--repeat", type=int, default=1, help="repeat the whole block list N times")
    ap.add_argument("--tag", action="append", metavar="K=V", help="free conditions, e.g. volume=loud speed=fast")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--model-name", default="kivi", help="the model under test, for the live-wake-word warning")
    ap.add_argument("--pipelines-file", help="a copy of assist_pipeline.pipelines instead of reading the pod")
    ap.add_argument("--no-preflight", action="store_true", help="do not read the Assist pipelines from the pod")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for Enter between blocks")
    ap.add_argument("--dry-run", action="store_true", help="fake tones, no microphone, compressed time, no waits")
    ap.add_argument("--fake-source", choices=["sine", "silence"], help="synthetic sources at real speed")
    ap.add_argument("--time-scale", type=float, help="wall seconds per session second (fake sources only)")
    a = ap.parse_args(argv)
    if a.list_devices:
        print_sources()
        return
    if a.print_protocol:
        print(json.dumps({"blocks": default_blocks()}, indent=1, ensure_ascii=False))
        return
    if a.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    run(a)


if __name__ == "__main__":
    main()
