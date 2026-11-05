#!/usr/bin/env python3
"""Train an openWakeWord model from already computed features, with the training knobs exposed.

Same data loading and `Model.auto_train` as openWakeWord's train.py (`--train_model`), but logs the
validation history (recall, accuracy, false positives per hour) to JSON and lets the negative weight,
the steps and the per-class batch sizes be changed without editing train.py. Used for the
`max_negative_weight` sweep in the experiment doc.

    train_driver.py --config $WORK/kivi.rendered.yaml --out $WORK/sweep/w300 \
        [--steps 25000] [--max-negative-weight 300] [--batch adversarial_negative=100,positive=100] \
        [--workers 4]

Writes <out>/kivi.onnx and <out>/history.json.
"""
import argparse
import json
import logging
import os

import numpy as np
import torch
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--max-negative-weight", type=float)
    ap.add_argument("--target-fp", type=float)
    ap.add_argument("--batch", default="", help="class=n,... overrides of batch_n_per_class")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--neg-suffix", default="", help="use negative_features_{train,test}<suffix>.npy")
    ap.add_argument("--pos-suffix", default="", help="use positive_features_{train,test}<suffix>.npy")
    ap.add_argument("--layer-size", type=int)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    torch.manual_seed(a.seed)
    np.random.seed(a.seed)

    from openwakeword.data import mmap_batch_generator
    from openwakeword.train import Model

    cfg = yaml.safe_load(open(a.config))
    if a.steps:
        cfg["steps"] = a.steps
    if a.max_negative_weight:
        cfg["max_negative_weight"] = a.max_negative_weight
    if a.layer_size:
        cfg["layer_size"] = a.layer_size
    if a.target_fp:
        cfg["target_false_positives_per_hour"] = a.target_fp
    for kv in filter(None, a.batch.split(",")):
        k, v = kv.split("=")
        cfg["batch_n_per_class"][k] = int(v)
    os.makedirs(a.out, exist_ok=True)
    feat_dir = os.path.join(cfg["output_dir"], cfg["model_name"])

    input_shape = np.load(os.path.join(feat_dir, f"positive_features_test{a.pos_suffix}.npy"), mmap_mode="r").shape[1:]
    oww = Model(n_classes=1, input_shape=input_shape, model_type=cfg["model_type"],
                layer_dim=cfg["layer_size"], seconds_per_example=1280 * input_shape[0] / 16000)

    def f(x, n=input_shape[0]):
        if n != x.shape[1]:
            x = np.vstack(x)
            return np.array([x[i:i + n, :] for i in range(0, x.shape[0] - n, n)])
        return x

    files = dict(cfg["feature_data_files"])
    data_transforms = {k: f for k in files}
    files["positive"] = os.path.join(feat_dir, f"positive_features_train{a.pos_suffix}.npy")
    files["adversarial_negative"] = os.path.join(feat_dir, f"negative_features_train{a.neg_suffix}.npy")
    label_transforms = {k: (lambda x: [1 for _ in x]) if k == "positive" else (lambda x: [0 for _ in x])
                        for k in files}
    gen = mmap_batch_generator(files, n_per_class=cfg["batch_n_per_class"],
                               data_transform_funcs=data_transforms, label_transform_funcs=label_transforms)

    class IterDataset(torch.utils.data.IterableDataset):
        def __iter__(self):
            return gen

    X_train = torch.utils.data.DataLoader(IterDataset(), batch_size=None, num_workers=a.workers,
                                          prefetch_factor=16)
    V = np.load(cfg["false_positive_validation_data_path"])
    V = np.array([V[i:i + input_shape[0]] for i in range(0, V.shape[0] - input_shape[0], 1)])
    X_val_fp = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(
        torch.from_numpy(V), torch.from_numpy(np.zeros(V.shape[0], np.float32))), batch_size=len(V))
    P = np.load(os.path.join(feat_dir, f"positive_features_test{a.pos_suffix}.npy"))
    N = np.load(os.path.join(feat_dir, f"negative_features_test{a.neg_suffix}.npy"))
    y = np.hstack((np.ones(len(P)), np.zeros(len(N)))).astype(np.float32)
    X_val = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(
        torch.from_numpy(np.vstack((P, N))), torch.from_numpy(y)), batch_size=len(y))

    best = oww.auto_train(X_train=X_train, X_val=X_val, false_positive_val_data=X_val_fp,
                          steps=cfg["steps"], max_negative_weight=cfg["max_negative_weight"],
                          target_fp_per_hour=cfg["target_false_positives_per_hour"])
    oww.export_model(model=best, model_name=cfg["model_name"], output_dir=a.out)
    hist = {k: [float(np.asarray(v).ravel()[0]) if np.asarray(v).size else None for v in vals]
            for k, vals in oww.history.items() if k.startswith("val_")}
    json.dump({"config": {k: cfg[k] for k in ("steps", "max_negative_weight", "batch_n_per_class",
                                               "target_false_positives_per_hour")}
               | {"neg_suffix": a.neg_suffix, "pos_suffix": a.pos_suffix, "layer_size": cfg["layer_size"]},
               "history": hist, "best_scores": [{k: float(v) for k, v in s.items()} for s in oww.best_model_scores]},
              open(os.path.join(a.out, "history.json"), "w"), indent=1)
    print("done", a.out)


if __name__ == "__main__":
    main()
