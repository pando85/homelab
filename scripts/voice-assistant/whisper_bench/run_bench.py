#!/usr/bin/env python3
"""Pod-side runner: for each config of a plan, start the real wyoming_faster_whisper server with
that config's arguments, warm it up, drive it with a Wyoming client over the fixed clip set and
write one JSON line per clip (raw transcript and latency) plus GPU memory samples.

    python3 run_bench.py --plan plan.json --manifest /work/data/manifest.csv --root /work/data \
        --out /work/out [--only NAME,NAME]

Runs inside the wyoming-whisper image (Python 3.10, wyoming, nvidia-smi). Configs run strictly one
after the other. The server is the production entrypoint with the same --model, --language,
--device and --initial-prompt as the live deployment; --hass-api is omitted (no token here), so
the prompt is the static verbs prefix only. Transcripts are written to the results files (synthetic
speech only) and never to the server log (no --debug).
"""
import argparse
import asyncio
import csv
import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wyoming_client as wc  # noqa: E402

PORT = 10399
COMMON = ["--model", "large-v3-turbo", "--language", "es", "--device", "cuda", "--local-files-only"]
# Same text as apps/home-assistant/values.yaml (live deployment).
PROMPT = ("Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio, "
          "pon, temporizador, minutos, volumen, brillo, por ciento, grados.")


class GpuSampler:
    """Samples total GPU memory (MiB) and utilisation with nvidia-smi in a thread."""

    def __init__(self, live_uri=None, period=0.25):
        self.period, self.live_uri = period, live_uri
        self.peak = 0
        self.samples = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.live_rtt = []

    def _read(self):
        try:
            o = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                                         "--format=csv,noheader,nounits"], text=True, timeout=5)
            mem, util = [int(x) for x in o.strip().split(",")]
            return mem, util
        except Exception:
            return None

    def _run(self):
        last_live = 0.0
        while not self._stop.is_set():
            r = self._read()
            if r:
                self.samples.append((time.time(), r[0], r[1]))
                self.peak = max(self.peak, r[0])
            if self.live_uri and time.time() - last_live > 10:
                last_live = time.time()
                self.live_rtt.append((time.time(), asyncio.run(wc.describe(self.live_uri))))
            time.sleep(self.period)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join(timeout=10)

    def reset_peak(self):
        self.peak = 0


def wait_port(port, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def load_manifest(path, root):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        r["pcm"] = wc.read_pcm(Path(root) / r["path"])
    return rows


def order(rows, seed=20261105):
    """Fixed shuffled order, identical for every config."""
    rows = sorted(rows, key=lambda r: (r["id"], r["cond"]))
    random.Random(seed).shuffle(rows)
    return rows


def run_config(cfg, rows, out_dir, sampler, warmup=10):
    name = cfg["name"]
    log = open(out_dir / f"server-{name}.log", "w")
    cmd = [sys.executable, "-m", "wyoming_faster_whisper", "--uri", f"tcp://127.0.0.1:{PORT}",
           "--data-dir", "/data", "--download-dir", "/data"] + COMMON + ["--initial-prompt", PROMPT] + cfg["args"]
    print(f"[{name}] start: {' '.join(a for a in cfg['args'])}", flush=True)
    base_mem = sampler.samples[-1][1] if sampler.samples else None
    sampler.reset_peak()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    result = {"name": name, "args": cfg["args"], "base_mem_mib": base_mem, "status": "ok"}
    try:
        if not wait_port(PORT):
            raise RuntimeError("server did not open the port")
        uri = f"tcp://127.0.0.1:{PORT}"
        kw = dict(cfg.get("client", {}))
        t0 = time.time()
        for r in rows[:warmup]:  # first request loads the model in the server (lazy); never paced
            asyncio.run(wc.transcribe(uri, r["pcm"], timeout=300))
        rows = rows[:cfg["limit"]] if cfg.get("limit") else rows
        result["warmup_s"] = round(time.time() - t0, 1)
        sampler.reset_peak()
        idle_before = sampler.samples[-1][1]
        res_path = out_dir / f"results-{name}.jsonl"
        with open(res_path, "w", encoding="utf-8") as f:
            for i, r in enumerate(rows):
                text, lat, _ = asyncio.run(wc.transcribe(uri, r["pcm"], timeout=120, **kw))
                f.write(json.dumps({"config": name, "id": r["id"], "cond": r["cond"], "audio_s": float(r["audio_s"]),
                                    "latency_s": round(lat, 4), "text": text, "t": round(time.time(), 2)},
                                   ensure_ascii=False) + "\n")
                f.flush()
                if i % 60 == 0:
                    print(f"[{name}] {i}/{len(rows)}", flush=True)
        result.update(peak_mem_mib=sampler.peak, mem_after_warmup_mib=idle_before, n=len(rows))
    except Exception as e:  # keep the matrix going; the failure is a result
        result.update(status=f"error: {type(e).__name__}: {e}"[:300])
        print(f"[{name}] {result['status']}", flush=True)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        # let the GPU memory drop back before the next config
        for _ in range(60):
            time.sleep(0.5)
            if sampler.samples and base_mem is not None and sampler.samples[-1][1] <= base_mem + 60:
                break
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0, help="only the first N clips (smoke test)")
    ap.add_argument("--live", default="", help="tcp:// URI of the live Whisper for Describe liveness pings")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    plan = json.load(open(a.plan))
    only = set(filter(None, a.only.split(",")))
    rows = order(load_manifest(a.manifest, a.root))
    if a.limit:
        rows = rows[:a.limit]
    sampler = GpuSampler(a.live or None).start()
    time.sleep(1)
    summary = []
    try:
        for cfg in plan["configs"]:
            if only and cfg["name"] not in only:
                continue
            summary.append(run_config(cfg, rows, out, sampler, warmup=min(10, len(rows))))
            with open(out / "summary.json", "w") as f:
                json.dump({"runs": summary}, f, indent=1)
            with open(out / "gpu_samples.json", "w") as f:
                json.dump({"samples": sampler.samples, "live_rtt": sampler.live_rtt}, f)
    finally:
        sampler.stop()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
