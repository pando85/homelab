"""Analysis dataset builder — normalize raw data to 15-min grid.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def build_dataset(
    *,
    data_dir: str | None,
    config_path: str | None,
    start: str,
    end: str,
) -> str:
    """Build the 15-minute analysis dataset from raw Parquet.

    Reads raw parts, computes counter deltas, assigns tariff periods,
    flags DST transitions and data-quality gaps, and writes the result
    to ``analysis/v1/<dataset_id>/``.

    Returns:
        The dataset_id (content hash).

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "analysis dataset builder is not yet implemented — see ADR-0003 §data contract"
    )
