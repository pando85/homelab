"""Monthly reconciliation report generator.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def generate_report(
    *,
    data_dir: str | None,
    config_path: str | None,
) -> None:
    """Generate a monthly reconciliation report.

    Produces EUR billed, distribution across intervals, equivalent full
    cycle count, and headroom versus the oracle bound.  Gap exclusions
    are identical across all policies.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "report generation is not yet implemented — see ADR-0003 §strategy comparison"
    )
