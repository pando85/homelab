#!/usr/bin/env python3
"""Build a melspectrogram ONNX model that behaves like the streaming server when batched.

openWakeWord's melspectrogram model clamps its log-mel output at (max - 80 dB), with the max taken
over the **whole input tensor**. The streaming server (pyopen-wakeword) calls it on one 1760-sample
chunk at a time, so the floor is per chunk; batch feature extraction (training, `embed_clips`) puts
the floor at the loudest point of the whole batch. This rewrites the graph so the max is per batch
row; feeding it a batch of 1760-sample chunks reproduces the server's features exactly (checked
against pyopen-wakeword streaming to 4 decimals of the final score).

    stream_mel.py <openwakeword>/resources/models/melspectrogram.onnx melspectrogram_rowmax.onnx
"""
import sys

import onnx
from onnx import helper


def main(src, dst):
    m = onnx.load(src)
    nodes = []
    for n in m.graph.node:
        if n.op_type == "ReduceMax":
            n = helper.make_node("ReduceMax", list(n.input), list(n.output), axes=[1, 2, 3], keepdims=1)
        elif n.op_type == "Clip" and n.output[0] == m.graph.output[0].name:
            x, lo, hi = n.input
            nodes.append(helper.make_node("Max", [x, lo], ["rowmax_floor"]))
            n = helper.make_node("Min", ["rowmax_floor", hi], list(n.output))
        nodes.append(n)
    del m.graph.node[:]
    m.graph.node.extend(nodes)
    onnx.checker.check_model(m)
    onnx.save(m, dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
