#!/usr/bin/env python3
"""Convert an openWakeWord "dnn" model from ONNX (train.py output) to TFLite.

train.py's own converter needs onnx_tf, which does not install with current TensorFlow. The DNN is
small (Flatten, Linear, LayerNorm, ReLU, n x [Linear, LayerNorm, ReLU], Linear, Sigmoid), so this
script rebuilds it as a TensorFlow function with the ONNX weights as constants and converts that. It then checks that ONNX and
TFLite outputs agree on random and, if given, real feature windows.

    export_tflite.py kivi.onnx kivi.tflite [--check-features positive_features_test.npy]

Run in a venv with tensorflow-cpu 2.16, onnx and onnxruntime (separate from the training venv).
"""
import argparse

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("onnx_path")
    ap.add_argument("tflite_path")
    ap.add_argument("--check-features")
    ap.add_argument("--calibrate-threshold", type=float,
                    help="shift the output so that probability T becomes 0.5 (the server's default "
                         "threshold, which is global for all models): sigmoid(z - logit(T))")
    a = ap.parse_args()

    import onnx
    from onnx import numpy_helper
    import tensorflow as tf

    m = onnx.load(a.onnx_path)
    w = {i.name: numpy_helper.to_array(i) for i in m.graph.initializer}
    for node in m.graph.node:  # torch dedups equal tensors and links them with Identity / Constant
        if node.op_type == "Identity" and node.input[0] in w:
            w[node.output[0]] = w[node.input[0]]
        elif node.op_type == "Constant":
            w[node.output[0]] = numpy_helper.to_array(node.attribute[0].t)
    inp = m.graph.input[0].type.tensor_type.shape.dim
    shape = [d.dim_value for d in inp][1:]
    # Walk the graph: Gemm -> Dense; the decomposed LayerNorm ends in Mul(weight) + Add(bias), with
    # Add(ReduceMean_1, eps) inside; Relu and the final Sigmoid follow.
    ops, eps = [], 1e-5
    for node in m.graph.node:
        t = node.op_type
        if t == "Gemm":
            W = w[node.input[1]]
            if any(at.name == "transB" and at.i == 1 for at in node.attribute):
                W = W.T
            ops.append(["dense", W, w[node.input[2]]])
        elif t == "Add" and "ReduceMean_1" in node.input[0]:
            eps = float(w[node.input[1]])
        elif t == "Mul" and "Div" in node.input[0] and node.input[1] in w:
            ops.append(["ln", w[node.input[1]], None])
        elif t == "Add" and "Mul" in node.input[0] and node.input[1] in w and ops and ops[-1][0] == "ln":
            ops[-1][2] = w[node.input[1]]
        elif t == "Relu":
            ops.append(["relu", None, None])
        elif t == "Sigmoid":
            ops.append(["sigmoid", None, None])
        elif t not in ("Flatten", "ReduceMean", "Sub", "Pow", "Sqrt", "Div", "Mul", "Add", "Identity", "Constant"):
            raise SystemExit(f"unsupported op {t}: only the openWakeWord dnn model is handled")
    shift = 0.0
    if a.calibrate_threshold:
        t = a.calibrate_threshold
        shift = float(np.log(t / (1 - t)))
        last = max(i for i, o in enumerate(ops) if o[0] == "dense")
        ops[last][2] = ops[last][2] - shift  # same ordering of outputs, decision at 0.5 == T before
    consts = [(k, tf.constant(p1, tf.float32) if p1 is not None else None,
               tf.constant(p2, tf.float32) if p2 is not None else None) for k, p1, p2 in ops]

    @tf.function(input_signature=[tf.TensorSpec([1, *shape], tf.float32)])
    def forward(x):
        h = tf.reshape(x, [1, -1])
        for kind, p1, p2 in consts:
            if kind == "dense":
                h = tf.matmul(h, p1) + p2
            elif kind == "ln":
                mu = tf.reduce_mean(h, axis=-1, keepdims=True)
                var = tf.reduce_mean(tf.square(h - mu), axis=-1, keepdims=True)
                h = (h - mu) * tf.math.rsqrt(var + eps) * p1 + p2
            elif kind == "relu":
                h = tf.nn.relu(h)
            else:
                h = tf.math.sigmoid(h)
        return h

    lin = [o for o in ops if o[0] == "dense"]
    module = tf.Module()
    module.forward = forward
    conv = tf.lite.TFLiteConverter.from_concrete_functions([forward.get_concrete_function()], module)
    data = conv.convert()
    open(a.tflite_path, "wb").write(data)

    import onnxruntime as ort
    sess = ort.InferenceSession(a.onnx_path, providers=["CPUExecutionProvider"])
    interp = tf.lite.Interpreter(model_content=data)
    interp.allocate_tensors()
    ii, oo = interp.get_input_details()[0], interp.get_output_details()[0]
    X = np.random.default_rng(0).normal(0, 1, (200, *shape)).astype(np.float32)
    if a.check_features:
        F = np.load(a.check_features, mmap_mode="r")
        X = np.concatenate([X, np.asarray(F[:800], dtype=np.float32)])
    diffs = []
    for row in X:
        o1 = sess.run(None, {sess.get_inputs()[0].name: row[None]})[0].ravel()[0]
        if shift:
            o1 = 1 / (1 + np.exp(-(np.log(o1 / (1 - o1 + 1e-12) + 1e-12) - shift)))
        interp.set_tensor(ii["index"], row[None])
        interp.invoke()
        o2 = interp.get_tensor(oo["index"]).ravel()[0]
        diffs.append(abs(o1 - o2))
    print(f"{a.tflite_path}: {len(data)} bytes, input {ii['shape']}, {len(lin)} linear layers; "
          f"max |onnx - tflite| over {len(X)} inputs = {max(diffs):.2e}")
    assert max(diffs) < 1e-3


if __name__ == "__main__":
    main()
