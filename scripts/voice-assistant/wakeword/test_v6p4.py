import pytest

import v6p4_data as D
import v6p4_select as S


@pytest.mark.parametrize("day,want", [("2026-11-10", "train"), ("2026-11-12", "train"), ("2026-11-05", "train"),
                                      ("2026-11-14", "selection"), ("2026-11-13", "test"), ("2026-11-15", "test"),
                                      ("2026-11-16", "later")])
def test_day_roles(day, want):
    assert D.role(day) == want


def test_selection_day_is_never_a_training_day():
    assert D.SEL_DAY not in D.TRAIN_DAYS and S.SEL_DAY == D.SEL_DAY
    assert all(int(d[-2:]) % 2 == 0 or d < "2026-11-08" for d in D.TRAIN_DAYS)


def test_table_md_is_sorted_and_counts_only():
    from collections import Counter
    t = D.table_md(["a", "b"], Counter({("x", "1"): 2, ("a", "2"): 5}), 2)
    assert t.splitlines()[2:] == ["| a | 2 | 5 |", "| x | 1 | 2 |"]


def test_tries_are_four_slacks_from_recall_match():
    assert S.SLACKS == (0.0, 0.01, 0.02, 0.03) and S.GRID[0] == 0.9 and S.GRID[-1] == 0.9995
