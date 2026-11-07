#!/usr/bin/env python3
"""Train the openWakeWord DNN head with a plain, balanced training loop.

Same model as openWakeWord's train.py (`dnn`: Flatten, Linear, LayerNorm, ReLU, n x FCN block,
Linear, Sigmoid; exported with `Model.export_model`), but without `auto_train`'s negative-weight
ramp (up to 1500x and doubled when the validation false-positive target is missed) and hard-example
filtering. For "kivi" that ramp collapsed every run to almost no detections (see the experiment
doc). Here the negative weight is a fixed, small factor and the operating point is chosen afterwards
by threshold on held-out audio.

    train_simple.py --work $WORK --out $WORK/runs/s1 [--steps 12000] [--layer 32] [--neg-weight 3] \
        [--pos-suffix _simple] [--neg-suffix _simple] [--adv 64]

Batch per step: positives 256, ACAV100M 512, MLS Spanish 128, adversarial negatives `--adv`, and
optional mined hard negatives (`--extra-neg FILE.npy:N`, from `fast_eval.py mine`). v3 additions:
`--extra-neg FILE.npy:N:W` gives that class its own loss weight W (default `--neg-weight`), and
`--extra-pos FILE.npy:N` adds N positives per step from another set (e.g. real takes).
"""
import argparse
import json
import os

import numpy as np
import torch
import torch.nn as nn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=12000)
    ap.add_argument("--layer", type=int, default=32)
    ap.add_argument("--neg-weight", type=float, default=3.0)
    ap.add_argument("--adv", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--pos-suffix", default="_simple")
    ap.add_argument("--neg-suffix", default="_simple")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--acav", help="ACAV100M windows (default data/acav/acav_part.npy; a smaller random "
                                    "subset keeps it in the page cache on a busy machine)")
    ap.add_argument("--mls", help="Spanish speech negative windows (default data/feat_mls_es.npy)")
    ap.add_argument("--extra-neg", action="append", default=[],
                    help="FILE.npy:N[:W] extra negatives per step, optional loss weight W (default --neg-weight)")
    ap.add_argument("--extra-pos", action="append", default=[], help="FILE.npy:N extra positives per step")
    ap.add_argument("--n-pos", type=int, default=256, help="positives per step")
    ap.add_argument("--n-acav", type=int, default=512, help="ACAV100M windows per step (0: none)")
    ap.add_argument("--n-mls", type=int, default=128, help="Spanish speech windows per step")
    ap.add_argument("--pos-train", help="positive training features (default train_out/kivi/"
                                        "positive_features_train{pos-suffix}.npy)")
    ap.add_argument("--neg-train", help="adversarial training features (default by --neg-suffix)")
    ap.add_argument("--pos-test", help="validation positives (default by --pos-suffix)")
    ap.add_argument("--neg-test", help="validation adversarial negatives (default by --neg-suffix)")
    ap.add_argument("--init", help="start from the weights of an exported head (ONNX of the same layer size), "
                                   "for fine-tuning a previous model on new features")
    a = ap.parse_args()
    from openwakeword.train import Model

    torch.manual_seed(a.seed)
    torch.set_num_threads(4)  # the work is on the GPU; avoid oversubscribing a busy CPU
    rng = np.random.default_rng(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    W = a.work
    k = os.path.join(W, "train_out", "kivi")
    P = np.load(a.pos_train or os.path.join(k, f"positive_features_train{a.pos_suffix}.npy"), mmap_mode="r")
    N = np.load(a.neg_train or os.path.join(k, f"negative_features_train{a.neg_suffix}.npy"), mmap_mode="r")
    A = np.load(a.acav or os.path.join(W, "data", "acav", "acav_part.npy"), mmap_mode="r")
    M = np.load(a.mls or os.path.join(W, "data", "feat_mls_es.npy"), mmap_mode="r")
    Pt = torch.tensor(np.load(a.pos_test or os.path.join(k, f"positive_features_test{a.pos_suffix}.npy"))
                      .astype(np.float32), device=dev)
    Nt = torch.tensor(np.load(a.neg_test or os.path.join(k, f"negative_features_test{a.neg_suffix}.npy"))
                      .astype(np.float32), device=dev)
    V = np.load(os.path.join(W, "data", "valfeat", "validation_set_features.npy"))
    # every 4th window of the 11.3 h validation set (training-time indicator only; CPU, chunked)
    Vw = torch.tensor(np.stack([V[i:i + 16] for i in range(0, len(V) - 16, 4)]))

    extra = []
    for spec in a.extra_neg:
        f, n, *wt = spec.split(":")
        extra.append((np.load(f, mmap_mode="r"), int(n), float(wt[0]) if wt else a.neg_weight))
    extra_pos = []
    for spec in a.extra_pos:
        f, n = spec.rsplit(":", 1)
        extra_pos.append((np.load(f, mmap_mode="r"), int(n)))
    n_pos = a.n_pos + sum(n for _, n in extra_pos)

    def batch(X, n):
        i = np.sort(rng.choice(len(X), n, replace=False))
        return torch.tensor(np.asarray(X[i], dtype=np.float32), device=dev)

    oww = Model(n_classes=1, input_shape=(16, 96), model_type="dnn", layer_dim=a.layer)
    if a.init:
        import onnx
        from onnx import numpy_helper
        w0 = {i.name: torch.tensor(numpy_helper.to_array(i)) for i in onnx.load(a.init).graph.initializer}
        sd = oww.model.state_dict()
        assert set(sd) <= set(w0), f"--init: missing {set(sd) - set(w0)}"
        oww.model.load_state_dict({k: w0[k] for k in sd})
    net = oww.model.to(dev)
    logits = lambda x: net.last_layer(  # noqa: E731  (forward without the final sigmoid)
        net.blocks[0](net.relu1(net.layernorm1(net.layer1(net.flatten(x))))))
    opt = torch.optim.Adam(net.parameters(), a.lr)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.steps)
    hist = []
    for step in range(a.steps):
        parts = [batch(P, a.n_pos)] + [batch(X, n) for X, n in extra_pos] + \
            ([batch(A, a.n_acav)] if a.n_acav else []) + [batch(M, a.n_mls)] + \
            ([batch(N, a.adv)] if a.adv else []) + [batch(X, n) for X, n, _ in extra]
        x = torch.cat(parts)
        y = torch.zeros(len(x), device=dev)
        y[:n_pos] = 1
        w = torch.full((len(x),), a.neg_weight, device=dev)
        w[:n_pos] = 1
        if extra:
            off = len(x) - sum(n for _, n, _ in extra)
            for _, n, wt in extra:
                w[off:off + n] = wt
                off += n
        loss = (nn.functional.binary_cross_entropy_with_logits(logits(x).squeeze(1), y, reduction="none") * w).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if (step + 1) % 2000 == 0 or step + 1 == a.steps:
            with torch.no_grad():
                pr = net(Pt).squeeze(1)
                nr = net(Nt).squeeze(1)
                vr = torch.cat([net(Vw[i:i + 65536].to(dev)) for i in range(0, len(Vw), 65536)]).squeeze(1)
            vs = torch.sort(vr, descending=True).values
            row = {"step": step + 1, "loss": float(loss)}
            for fa in (0.5, 1, 5):
                t = float(vs[max(0, int(fa * 11.3 / 4))])
                row[f"thr@{fa}/h"] = round(t, 4)
                row[f"val_recall@{fa}/h"] = round(float((pr > t).float().mean()), 4)
                row[f"adv_pass@{fa}/h"] = round(float((nr > t).float().mean()), 4)
            hist.append(row)
            print(json.dumps(row), flush=True)
    os.makedirs(a.out, exist_ok=True)
    net.eval()
    oww.export_model(model=net.cpu(), model_name="kivi", output_dir=a.out)
    json.dump({"args": vars(a), "history": hist}, open(os.path.join(a.out, "history.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
