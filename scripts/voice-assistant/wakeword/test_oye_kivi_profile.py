"""Config regressions for the *experimental* strict "oye Kivi" candidate.

No voice synthesis, audio access, download, GPU or Home Assistant connection required.
Run: python -m pytest test_oye_kivi_profile.py -q
"""
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent


def load(name):
    return yaml.safe_load((HERE / name).read_text(encoding="utf-8"))


def test_vnext_only_trains_the_actual_oye_kivi_phrase():
    old = load("v4_oye_kivi.yaml")
    new = load("vnext_oye_kivi.yaml")
    assert "ˈoɪɣa" in old["oye_phonemes"]  # historical v4 recipe remains reproducible
    assert "ˈoɪɣa" not in new["oye_phonemes"]
    assert sum(new["oye_phonemes"].values()) == pytest.approx(1.0)
    assert set(new["oye_phonemes"]) == set(old["oye_phonemes"]) - {"ˈoɪɣa"}


def test_vnext_preserves_holdouts_and_rebalances_specific_confusers():
    old = load("v4_oye_kivi.yaml")
    new = load("vnext_oye_kivi.yaml")
    assert new["voices"] == old["voices"]
    assert any(v[1] == "heldout" for v in new["voices"].values())
    assert new["kivi_phonemes"] == old["kivi_phonemes"]
    assert new["joiners"] == old["joiners"]
    assert new["near_sentences"] == old["near_sentences"]
    for phrase in ("fue Kivi", "ojo Kivi", "oye que vi", "oye aquí"):
        assert new["near_phrases"][phrase] > old["near_phrases"][phrase]
    # Acoustic near-collisions must not be silently relabelled without a DTW + human review.
    assert new["near_phrases"]["oye kiri"] == old["near_phrases"]["oye kiri"]
    assert "oiga Kivi" not in new["near_phrases"]
