#!/usr/bin/env python3
"""TV-like background clips for Kivi v2: AudioSet balanced-train clips filtered by label.

Keeps clips with at least one TV-like label (music, singing, speech, conversation, television,
radio, laughter, applause, crowd) and none of the stationary-noise labels (white or pink noise,
hum, fans, air conditioning, engines idling, wind, rain, hiss ...): the satellite's noise
suppression removes stationary noise, so v2 does not train on it. Writes 16 kHz mono 10 s WAVs
(peak-normalised to -1 dBFS) and a CSV with the labels of each clip.

    v2_prep_bg.py data/audioset_v2/bal_train_0*.parquet --out data/bg_tv
"""
import argparse
import csv
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TV_LIKE = {
    "Music", "Singing", "Choir", "Song", "Speech", "Male speech, man speaking", "Female speech, woman speaking",
    "Child speech, kid speaking", "Conversation", "Narration, monologue", "Television", "Radio",
    "Laughter", "Applause", "Cheering", "Crowd", "Chatter", "Hubbub, speech noise, speech babble",
    "Speech synthesizer", "Theme music", "Soundtrack music", "Background music", "Video game music",
    "Pop music", "Rock music", "Hip hop music", "Electronic music", "Classical music", "Jazz",
}
STATIONARY = {
    "White noise", "Pink noise", "Noise", "Static", "Hum", "Mains hum", "Buzz", "Hiss", "Fan",
    "Mechanical fan", "Air conditioning", "Vacuum cleaner", "Hair dryer", "Engine", "Idling",
    "Wind", "Wind noise (microphone)", "Rain", "Raindrop", "Rain on surface", "Waterfall", "Stream",
    "Ocean", "Waves, surf", "Rumble", "Environmental noise", "Pulse", "Sine wave", "Tone",
    "Electric shaver, electric razor", "Blender", "Microwave oven", "Sewing machine", "Lawn mower",
    "Chainsaw", "Drill", "Power tool", "Aircraft engine", "Jet engine", "Train", "Rail transport",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import pyarrow.parquet as pq
    import soundfile as sf
    from prep_data import decode
    os.makedirs(a.out, exist_ok=True)
    n, seen = 0, 0
    with open(os.path.join(a.out, "labels.csv"), "w", newline="") as fo:
        w = csv.writer(fo)
        w.writerow(["file", "labels"])
        for p in a.parquet:
            f = pq.ParquetFile(p)
            for rg in range(f.num_row_groups):
                for r in f.read_row_group(rg, columns=["audio", "human_labels"]).to_pylist():
                    seen += 1
                    lab = set(r["human_labels"] or [])
                    if not (lab & TV_LIKE) or (lab & STATIONARY):
                        continue
                    x = decode(r["audio"]["bytes"])[:160000]
                    if len(x) < 32000 or np.max(np.abs(x)) < 1e-3:
                        continue
                    name = f"tv_{n:05d}.wav"
                    sf.write(os.path.join(a.out, name), (x / np.max(np.abs(x)) * 0.89 * 32767).astype(np.int16), 16000)
                    w.writerow([name, ";".join(sorted(lab))])
                    n += 1
    print(f"{n} of {seen} clips kept in {a.out}")


if __name__ == "__main__":
    main()
