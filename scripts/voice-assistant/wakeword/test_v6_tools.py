"""Pure tests of the v6 helpers (no audio, no models)."""

import numpy as np

import v6_cuts as K
import v6_room as R


def test_steps_and_times():
    assert R.n_steps(1759) == 0
    assert R.n_steps(1760) == 1
    assert R.n_steps(1760 + 1280 * 3) == 4
    assert abs(R.step_time(0) - 0.11) < 1e-9
    assert abs(R.step_time(10) - (12800 + 1760) / 16000) < 1e-9


def test_detections_refractory():
    t = np.arange(0, 10, 0.08)
    p = np.zeros_like(t)
    p[[10, 11, 12, 40, 60, 80]] = 0.99
    det = R.detections(t, p, 0.5)
    assert [round(float(t[i]), 2) for i in det] == [0.8, 3.2, 6.4]


def test_evening_hours_split_at_local_evening():
    # 2026-11-10 16:00Z = 17:00 Madrid; 3 h -> 2 h inside 18-24
    eh = R.evening_hours("2026-11-10T16:00:00Z", 3 * 3600 * 16000)
    assert list(eh) == ["2026-11-10"] and abs(eh["2026-11-10"] - 2.0) < 1e-6
    assert R.is_evening("2026-11-10T16:00:00Z", 3600.0 + 60)[0]
    assert not R.is_evening("2026-11-10T16:00:00Z", 60.0)[0]


def test_window_view_ends_at_step():
    e = np.arange(40 * 96, dtype=np.float32).reshape(40, 96)
    v = R.window_view(e)
    assert v.shape == (25, 16, 96)
    assert np.array_equal(v[5][-1], e[20]) and np.array_equal(v[5][0], e[5])


def test_level_v6_mixture():
    import v6_augment as A
    rng = np.random.default_rng(0)
    x = np.array([A.level_v6(rng, -23.0, 5.0) for _ in range(20000)])
    assert x.min() >= -52.0 and x.max() <= -10.0
    loud = np.mean((x >= -22) & (x <= -14))
    assert loud > 0.3          # 25 % uniform branch plus part of the normal branch


def test_synth_cuts_positive_never_in_command():
    rng = np.random.default_rng(1)
    r = {"kind": "ctx_pos", "total_samples": "64000", "phrase_start": "0", "phrase_end": "20000", "cmd_start": "20500"}
    for _ in range(200):
        for cut, grp, _k in K.synth_cuts(r, rng):
            if grp == "pos":
                assert 20000 <= cut <= 20000 + 3200
            else:
                assert cut <= 20000 - 8000 or cut >= 20000 + 16000


def test_synth_cuts_confuser_rows():
    rng = np.random.default_rng(2)
    r = {"kind": "ctx_neg", "total_samples": "30000", "phrase_start": "0", "phrase_end": ""}
    cuts = K.synth_cuts(r, rng)
    assert len(cuts) == 1 and cuts[0][1] == "conf" and 0 <= cuts[0][0] <= 30000
