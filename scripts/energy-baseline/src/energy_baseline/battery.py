"""Battery physical simulator — SOC recurrence with signed flows.

Pure numeric kernel: no filesystem, network, or Parquet access.  Everything
here operates on :class:`BatterySpec`, :class:`Horizon` and plain per-interval
arrays, so the physics can be unit-tested offline and reused by the policies
(:mod:`energy_baseline.policies`), the oracle
(:mod:`energy_baseline.oracle`) and the benchmark CLI.

Units
-----
* ``capacity_kwh`` / ``power_kw`` — nameplate energy and power.
* per-interval flows are **energies** (kWh), not powers: an interval carries at
  most ``power_kw * interval_seconds / 3600`` kWh.  This keeps the simulator
  resolution-independent (15-min analysis grid, 1-min checks, a 23- or 25-hour
  DST day all work without change).
* ``charge_kwh`` / ``discharge_kwh`` are flows at the battery's **AC/metered
  terminals** — the same side ``gross_kwh`` is metered on — so net import is
  ``grid = load + charge - discharge``.
* prices are EUR/kWh **pretax**; tax is a separate concern
  (:func:`energy_baseline.config.tax_multiplier`).

Physics invariants (ADR-0003 §"Battery physical simulator")
-----------------------------------------------------------
For every interval ``t``, with ``h = interval_seconds / 3600``:

1. SOC recurrence ``soc[t] = soc[t-1] + charge[t]*eta_c - discharge[t]/eta_d``
   with ``soc[-1] = capacity * initial_soc_frac``.  Round-trip loss is applied
   on **both** legs: one-way ``eta_c`` on the way in, one-way ``eta_d`` on the
   way out, so a full cycle returns ``eta_c * eta_d`` of the energy put in.
2. ``capacity*soc_min_frac <= soc[t] <= capacity*soc_max_frac`` at *every*
   step, not just at the end of the horizon.
3. ``0 <= charge[t] <= power_kw*h`` and ``0 <= discharge[t] <= power_kw*h``.
4. **One binary mode per interval**: ``charge[t]`` and ``discharge[t]`` are
   never both positive.  Without this, a negative price makes simultaneous
   charge/discharge profitable out of thin air (the classic negative-price
   pathology), which is a modelling artefact, not arbitrage.
5. ``grid[t] = load[t] + charge[t] - discharge[t]`` and ``grid[t] >= 0`` —
   no intentional export.  Note the direction: charging *raises* import, so the
   no-export rule bounds ``discharge`` (``discharge[t] <= load[t] + charge[t]``,
   which the binary mode reduces to ``discharge[t] <= load[t]``) and says
   nothing about ``charge``.  There is no load-based upper bound on charging.
6. When a site import ceiling is configured
   (``Horizon.import_limit_kw is not None``, ADR-0003: "import ceiling is
   configured or unknown — not asserted from any sensor reading") it constrains
   the **charging decision**, which is ours to make — never the measured load,
   which is not::

       charge[t] <= max(0, import_limit_kw * h - load[t])

   Charging is capped by the residual headroom the load leaves under the
   ceiling, and is simply unavailable (``charge[t] = 0``) in an interval whose
   load already meets or exceeds it.  Discharge is not part of this rule: it
   *lowers* the draw, so it stays bounded by the no-export rule alone and
   remains available exactly when the ceiling is tightest.

   The inequality is also the right **basis**, which is the point that made
   this design correct before anyone could say why.  ``load[t]`` is
   per-interval *energy* (kWh) and ``import_limit_kw * h`` is that ceiling's
   energy allowance for the same interval, so the comparison is between the
   average power drawn *over an interval* and the contracted power.  Contracted
   power is enforced exactly that way: the distributor measures maximum demand
   as a 15-minute average of the meter reading, so on the 900 s analysis grid
   this constraint is evaluated on the same basis the contract is billed on.
   Sub-minute or few-second excursions above the contracted figure are normal,
   compliant behaviour and are not what the constraint governs.
7. **Net grid import may exceed the ceiling** when the load alone does, and
   that is not a violation.  The reason is structural, not empirical: the
   ceiling constrains a **decision we make** — how much to charge — not
   **history we measured**.  A recorded interval above the contracted power is
   a fact about the past that no dispatch plan caused and no dispatch plan can
   undo; declaring such history infeasible would invalidate the no-battery
   baseline — which *is* that measured history — and every benchmark built on
   it.  So those intervals are reported as an observation instead:
   :attr:`Schedule.ceiling_exceeding_intervals` counts them and
   :func:`ceiling_exceedance_indices` names them.  Neither :func:`check_feasible`
   nor :func:`simulate` nor the oracle treats them as an error.

   The ceiling's *value* is a contract fact, never a measurement: it is
   **5.0 kW, confirmed by the user from the contract document on 2026-10-23**.
   An earlier revision of this module justified invariant 6/7 by arguing from
   peaks — "the archive shows 6.94 kW, so the contract cannot really be 5 kW" —
   and that inference is a non-sequitur: an instantaneous reading says nothing
   about a limit enforced on 15-minute averages.  That reasoning was acted on,
   so it is recorded here as corrected, not merely reworded.  The data does not
   contradict a 5 kW contract.  Full-population 2025 figures (520 151 power
   samples, 34 744 clean intervals), one row per way of averaging the same
   archive::

       Basis                        Max          Above 5.0 kW
       Instantaneous power          8 678.9 W    427 / 520 151 samples  (0.082 %)
       15-min max-of-instantaneous  8.68 kW      159 / 34 877 intervals (0.46 %)
       15-min mean of samples       6.24 kW      6 / 34 877 intervals   (0.017 %)
       15-min counter-derived mean  6.08 kW      6 / 34 766 intervals   (0.017 %)
       Clean allocated kWh x 4      6.358 kW     5 / 34 744 intervals   (0.014 %)
       Daily peak of 15-min means   6.36 kW      5 / 365 days           (1.4 %)

   The instantaneous distribution behind the first row is unremarkable: p50
   436.4 W, mean 633.9 W, p99 3 588 W, p99.9 4 842 W, no negative samples.  The
   five contract-relevant 15-minute peaks are 6.36 kW (2025-03-12 17:45Z),
   5.80 (2025-11-22 13:15Z), 5.73 (2025-03-17 18:00Z), 5.19 (2025-10-16 18:30Z)
   and 5.01 kW (2025-01-08 18:00Z); monthly mean load runs 0.450-0.784 kW and
   the annual mean is 0.633 kW — 12.7 % of the ceiling.

   The 5-6 kW spread *between* those averaging methods is expected, not a
   defect: one skipped or aliased scrape moves up to 60 s of a 900 s interval
   across an edge, i.e. ±6.7 % of that interval's allocated energy, so any
   window sitting at 5.0-5.2 kW is inside the allocation noise of a hard
   5.000 kW gate.

   The peak that motivated the old argument does matter — to a different
   quantity.  The instantaneous 8.68 kW maximum is a fact about simultaneous
   household loads on the feed; it bears on the battery's **power rating**
   (``power_kw``, configured 2.5 kW) and on wiring/protection headroom, and it
   has no bearing whatsoever on contract feasibility.

   No safety margin is applied to the ceiling anywhere in this module:
   ``import_limit_kw`` is used exactly as configured, never derated, rounded or
   padded.
8. ``meter_cost_eur = sum(prices[t] * grid[t])`` over **usable** intervals only
   (pretax).
9. Unusable intervals (``usable[t] is False``) force ``charge = discharge = 0``
   and are excluded from cost.  Gap exclusions are identical for all three
   policies so comparisons stay fair.  A horizon with **no** usable intervals
   has an empty cost basis and is rejected by :class:`Horizon` — comparing an
   all-empty window would report zero cost for every policy.
10. Terminal SOC: the accounting convention is chosen per policy.
   * ``terminal_mode="fixed"`` (the default, and what :class:`BatterySpec`
     expresses): when ``terminal_soc_frac is None`` the battery must come back
     to ``capacity * initial_soc_frac`` — no free energy created or destroyed
     over the horizon.
   * ``terminal_mode="free"`` (oracle only): the end SOC is free within
     ``[initial SOC, capacity*soc_max_frac]`` and every step is floored at
     ``max(soc_min, initial)`` — a "causal floor" that stops the plan spending
     the seed charge it never paid for.  Because energy is then neither pinned
     nor ignored, the leftover inventory is **explicitly valued** at
     ``terminal_value_eur_per_kwh`` (EUR per DC kWh), which the caller must
     supply (``0.0`` is allowed and simply means "value the terminal inventory
     at zero").  This is the ADR's "initial and terminal SOC are equal or have
     an explicit defensible valuation" requirement.

Idle self-discharge is **not** modelled: SOC changes only through the charge and
discharge flows.  This is a documented, unmeasured assumption, not a measured
property — no standby/parasitic drain term is applied anywhere in this module.

Efficiency, EFC and the SOC window
----------------------------------
Efficiency and the SOC window are **independent** assumptions: an 80% usable
window (``soc_max_frac - soc_min_frac == 0.8``) says nothing about a 90%
one-way efficiency, and neither is derived from the other.  A round-trip
efficiency ``eta_rt`` maps to one-way values as ``eta_c = eta_d = sqrt(eta_rt)``
— the caller decides that mapping; this module only ever consumes one-way
values.

:func:`compute_efc` is defined on the **DC side** (the cells' physical
coulomb throughput, which is what degrades the battery): ``throughput_kwh`` is
the AC/metered energy delivered, but EFC divides the *DC* discharge throughput
(``AC / eta_d``) by the *usable DC window* (``capacity * (soc_max - soc_min)``).
A full usable-DC-window drain is therefore exactly 1 EFC even when ``eta_d <
1``.  ``Schedule.throughput_kwh`` itself stays AC-delivered (the metered value);
only ``Schedule.efc`` is DC-referenced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

import numpy as np

__all__ = [
    "BatteryPhysicsError",
    "BatterySpec",
    "Horizon",
    "Schedule",
    "ceiling_exceedance_indices",
    "check_feasible",
    "compute_efc",
    "grid_import_kwh",
    "horizon_cost_eur",
    "simulate",
    "soc_trajectory_kwh",
    "validate_spec",
]

# Physical violation tolerance.  Energy quantities are kWh on a ~5 kWh
# nameplate, so an absolute kWh tolerance is the meaningful scale; it is scaled
# by capacity for SOC-window checks (see :func:`check_feasible`).
_TOL_KWH = 1e-6

# Number of example intervals named in a violation message before it is
# collapsed into a count.  A one-year horizon has 35 040 intervals, so a
# violation list must be aggregated, not printed per interval.
_MAX_EXAMPLES = 3


class BatteryPhysicsError(ValueError):
    """A charge/discharge plan violates the simulator's physical invariants.

    Subclasses :class:`ValueError` so callers can catch either the specific
    error or ordinary argument validation.
    """


def _frozen_float_array(values) -> np.ndarray:
    """A private, read-only, contiguous float64 copy of *values*.

    Defensive copy so the caller's array cannot be mutated through (or by) the
    data carrier, and read-only so a downstream ``arr[...] = ...`` fails loudly
    instead of silently rewriting a schedule that has already been validated.
    """
    arr = np.array(values, dtype=float, copy=True).reshape(-1)
    arr.setflags(write=False)
    return arr


def _frozen_bool_array(values, n: int) -> np.ndarray:
    """A private, read-only bool copy of *values*, broadcast from a scalar."""
    arr = np.asarray(values)
    if arr.ndim == 0:
        arr = np.full(n, bool(arr))
    arr = np.array(arr, dtype=bool, copy=True).reshape(-1)
    arr.setflags(write=False)
    return arr


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatterySpec:
    """Battery nameplate and operating assumptions.

    ``capacity_kwh`` is nameplate (usable energy is derived from the SOC
    window, not stored separately).  A provisional 5.12 kWh nameplate is the
    project's stated battery size; all fields are user-configurable, none is
    hardcoded here.

    Deliberately *not* validated here: this is a plain data carrier, so a
    malformed spec can be handed to :func:`validate_spec`,
    :func:`check_feasible`, :func:`simulate` or
    :func:`energy_baseline.oracle.solve_oracle` and be rejected there with a
    precise message.
    """

    capacity_kwh: float
    power_kw: float
    charge_efficiency: float
    """One-way efficiency on the way *in* (0 < e <= 1)."""

    discharge_efficiency: float
    """One-way efficiency on the way *out* (0 < e <= 1)."""

    soc_min_frac: float
    """Lower SOC bound as a fraction of ``capacity_kwh``."""

    soc_max_frac: float
    """Upper SOC bound as a fraction of ``capacity_kwh``."""

    initial_soc_frac: float
    """SOC at ``soc[-1]``, i.e. before the first interval."""

    terminal_soc_frac: float | None = None
    """Required end-of-horizon SOC.  ``None`` => must equal
    ``initial_soc_frac`` (energy-neutral over the horizon)."""

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None and f.name != "terminal_soc_frac":
                object.__setattr__(self, f.name, float(value))

    # -- derived quantities --------------------------------------------------

    @property
    def usable_window_kwh(self) -> float:
        """Energy between the SOC bounds: ``capacity * (max_frac - min_frac)``.

        This is a **DC** quantity — the physical energy the cells can move —
        which makes it the correct EFC denominator (see :func:`compute_efc`).
        """
        return self.capacity_kwh * (self.soc_max_frac - self.soc_min_frac)

    def soc_bound_kwh(self, which: str) -> float:
        """Absolute SOC bound in kWh (``which`` is ``"min"``, ``"max"`` or ``"init"``)."""
        fracs = {
            "min": self.soc_min_frac,
            "max": self.soc_max_frac,
            "init": self.initial_soc_frac,
            "terminal": self.effective_terminal_soc_frac,
        }
        if which not in fracs:
            raise ValueError(f"unknown SOC bound {which!r}")
        return self.capacity_kwh * fracs[which]

    @property
    def effective_terminal_soc_frac(self) -> float:
        """Terminal SOC fraction, with ``None`` resolved to ``initial_soc_frac``."""
        return (
            self.initial_soc_frac
            if self.terminal_soc_frac is None
            else float(self.terminal_soc_frac)
        )

    def causal_floor_kwh(self) -> float:
        """Lowest SOC a seed-protecting plan may reach, in kWh.

        ``max(soc_min, initial)`` — the free oracle and the causal policy share
        this floor so both are compared on one common feasible set: neither can
        discharge below the energy that was already in the battery when the
        window opened (they never paid for it at the meter).
        """
        return max(self.soc_bound_kwh("min"), self.soc_bound_kwh("init"))

    def interval_energy_limit_kwh(self, interval_seconds: float) -> float:
        """Maximum one-directional energy in a single interval (power x duration)."""
        return self.power_kw * interval_seconds / 3600.0


@dataclass(frozen=True)
class Horizon:
    """The evaluation horizon: prices, gross load, which intervals count, and the
    site import ceiling if one is configured.

    ``usable`` is the gap-exclusion mask produced by the dataset builder.  An
    interval excluded there is excluded for *every* policy: zero battery flows
    and no cost contribution, so the cost basis is identical across policies.
    A mask that excludes **every** interval leaves an empty cost basis and is
    rejected — an all-unusable horizon would report zero cost for all three
    policies and make the comparison meaningless.

    ``prices``, ``load_kwh`` and ``usable`` are stored as private, read-only
    copies: mutating the arrays the caller passed in cannot change a Horizon
    that has already been validated, and nothing downstream can rewrite one in
    place.
    """

    interval_seconds: float
    """Duration of one analysis interval (900 s on the 15-minute grid; a DST
    day changes the *number* of intervals, never their duration)."""

    prices: np.ndarray
    """EUR/kWh, pretax, may be negative (PVPC publishes negative hours)."""

    load_kwh: np.ndarray
    """Gross household consumption per interval (battery not yet included)."""

    usable: np.ndarray
    """Bool mask; ``False`` => excluded from cost and forced to zero flows."""

    import_limit_kw: float | None = None
    """Contracted / configured site import ceiling in kW (``None`` = unknown).

    ADR-0003 requires this to be *configured or unknown*, never inferred from a
    sensor reading, so the default is ``None`` — an unknown ceiling leaves the
    import side unconstrained rather than silently assuming a number.  When set,
    the ceiling limits **how much a plan may charge** in an interval, leaving the
    headroom the measured load does not already take::

        charge[t] <= max(0, import_limit_kw * h - load[t])

    It is not a limit on measured load: a recorded interval above the ceiling is
    legitimate history (see the module docstring, invariant 7), so net grid
    import may exceed the ceiling and that is an observation
    (:func:`ceiling_exceedance_indices`), never a violation.  Because ``load`` is
    per-interval energy, the constraint is an average over the interval — the
    same basis contracted power is enforced on, and the reason peaks in the
    archive neither validate nor contradict this number (module docstring,
    invariant 6).  Enforced by :func:`check_feasible`, :func:`simulate` and
    :func:`energy_baseline.oracle.solve_oracle`; discharge is bounded only by the
    no-export rule, so it stays available when the ceiling is tightest.
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval_seconds", float(self.interval_seconds))
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {self.interval_seconds}")

        if self.import_limit_kw is not None:
            limit = float(self.import_limit_kw)
            if not math.isfinite(limit):
                raise ValueError(f"import_limit_kw must be finite, got {limit}")
            if limit < 0:
                raise ValueError(f"import_limit_kw must be >= 0, got {limit}")
            object.__setattr__(self, "import_limit_kw", limit)

        prices = np.array(self.prices, dtype=float, copy=True).reshape(-1)
        load = np.array(self.load_kwh, dtype=float, copy=True).reshape(-1)
        n = prices.size
        if load.size != n:
            raise ValueError(f"prices has {n} intervals, load_kwh has {load.size}")
        if n == 0:
            raise ValueError("horizon is empty: at least one interval is required")
        if not np.isfinite(prices).all():
            bad = int(np.argmax(~np.isfinite(prices)))
            raise ValueError(f"prices contains a non-finite value at t={bad}")
        if not np.isfinite(load).all():
            bad = int(np.argmax(~np.isfinite(load)))
            raise ValueError(f"load_kwh contains a non-finite value at t={bad}")

        usable = _frozen_bool_array(self.usable, n)
        if usable.size != n:
            raise ValueError(f"usable has {usable.size} entries, expected {n}")
        if not usable.any():
            raise ValueError(
                f"usable excludes all {n} intervals: an empty cost basis cannot be "
                f"compared across policies"
            )

        prices.setflags(write=False)
        load.setflags(write=False)
        object.__setattr__(self, "prices", prices)
        object.__setattr__(self, "load_kwh", load)
        object.__setattr__(self, "usable", usable)

    @property
    def n(self) -> int:
        """Number of intervals."""
        return int(self.prices.size)

    @property
    def interval_hours(self) -> float:
        """Interval duration in hours."""
        return self.interval_seconds / 3600.0

    @property
    def usable_count(self) -> int:
        """Number of intervals included in the cost basis (always ``>= 1``)."""
        return int(self.usable.sum())

    @property
    def interval_import_limit_kwh(self) -> float | None:
        """Import ceiling expressed per interval, or ``None`` when unconfigured.

        ``import_limit_kw * interval_hours`` — the same resolution-independent
        form the simulator uses for power (see
        :meth:`BatterySpec.interval_energy_limit_kwh`).
        """
        if self.import_limit_kw is None:
            return None
        return self.import_limit_kw * self.interval_hours

    @property
    def charge_headroom_kwh(self) -> np.ndarray | None:
        """Energy the site may still draw *for charging* in each interval, or ``None``.

        ``max(0, import_limit_kw * h - load[t])`` — the ceiling minus the measured
        load, floored at zero because a load that already meets or exceeds the
        ceiling makes charging **unavailable** for that interval rather than
        making the measurement illegal.  This single form is what the causal
        policy, :func:`check_feasible` and the oracle's charging rows all use, so
        the three cannot disagree about how much headroom an interval has.

        ``None`` when the ceiling is unknown: unconstrained, never an invented
        number.  The returned array is read-only, like the other arrays here.
        """
        if self.import_limit_kw is None:
            return None
        headroom = np.maximum(self.interval_import_limit_kwh - self.load_kwh, 0.0)
        headroom.setflags(write=False)
        return headroom


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """A chronologically feasible dispatch plan plus its accounting.

    ``soc_kwh`` is end-of-interval, same length as the horizon.
    ``soc_min_frac`` / ``soc_max_frac`` are the **achieved** extrema over the
    trajectory *including* the initial SOC (so a no-battery schedule reports
    both equal to ``initial_soc_frac``); they are for reporting and for
    cross-checking against the spec's window, not the bounds themselves.

    The accounting fields separate the two ways a plan's cost can be framed:

    * ``meter_cost_eur`` is the pretax metered energy cost over usable
      intervals — for a fixed/neutral cycle this is also ``cost_eur``.
    * ``inventory_adjustment_eur`` is the value credited (or charged) for the
      energy left in the battery at the end of the horizon relative to the
      start: ``-terminal_value_eur_per_kwh * (terminal_soc_kwh -
      initial_soc_kwh)``.  It is ``0.0`` for a fixed-terminal plan (the SOC
      returns to its seed, nothing to value) and non-zero only for a free oracle
      plan that ends with a different charge than it began.
    * ``cost_eur`` is the **total objective** actually minimised:
      ``meter_cost_eur + inventory_adjustment_eur``.  Savings comparisons must
      use it, not the raw meter cost, so a free-terminal plan that ends full is
      not compared unfairly against a closed-cycle one.

    ``cost_lower_bound_eur`` / ``cost_upper_bound_eur`` are bounds on the
    optimal ``cost_eur`` (``None`` for a traced, non-solved plan); the
    incumbent schedule is always feasible so its ``cost_eur`` is a valid
    upper bound.  The remaining new fields default so that a caller constructing
    a bare :class:`Schedule` (or ``dataclasses.replace``-ing an old one) stays
    valid.
    """

    charge_kwh: np.ndarray
    discharge_kwh: np.ndarray
    soc_kwh: np.ndarray
    grid_kwh: np.ndarray
    cost_eur: float
    efc: float
    throughput_kwh: float
    """Total **AC/metered** energy delivered by the battery (sum of
    ``discharge_kwh``).  ``efc`` is DC-referenced; this is the metered value."""

    soc_min_frac: float
    soc_max_frac: float
    solver_status: str = "not_applicable"
    solver_gap: float | None = None
    bound_kind: str = "none"
    is_certified: bool = False

    # --- terminal-inventory / bound accounting (defaults keep old callers safe) ---
    initial_soc_kwh: float = 0.0
    """DC energy stored before the first interval (the plan's seed)."""

    terminal_soc_kwh: float = 0.0
    """DC energy stored after the last interval."""

    terminal_value_eur_per_kwh: float = 0.0
    """Explicit EUR/DC-kWh valuation of terminal inventory (0.0 = fixed/neutral)."""

    meter_cost_eur: float | None = None
    """Pretax metered energy cost; ``None`` resolves to ``cost_eur`` (no
    inventory adjustment), so an old bare ``Schedule`` stays consistent."""

    inventory_adjustment_eur: float = 0.0
    """``-terminal_value_eur_per_kwh * (terminal - initial)``; 0.0 for a
    fixed-terminal plan."""

    cost_lower_bound_eur: float | None = None
    """Lower bound on the optimal ``cost_eur`` (``None`` when not solved)."""

    cost_upper_bound_eur: float | None = None
    """Upper bound on the optimal ``cost_eur`` — the incumbent's own cost."""

    solver_time_limit_s: float | None = None
    """Wall-clock limit handed to the solver, if this plan came from one."""

    ceiling_exceeding_intervals: int = 0
    """**Observation, not violation:** usable intervals whose net grid import is
    above the configured :attr:`Horizon.import_limit_kw`.

    It can only be non-zero because the *measured load* alone already exceeded the
    ceiling, which no dispatch decision caused and no dispatch can undo (module
    docstring, invariant 7).  ``0`` when no ceiling is configured or when the
    plan stays under it.  Indices come from :func:`ceiling_exceedance_indices`;
    the plan carrying them is feasible either way.
    """

    def __post_init__(self) -> None:
        for name in ("charge_kwh", "discharge_kwh", "soc_kwh", "grid_kwh"):
            object.__setattr__(self, name, _frozen_float_array(getattr(self, name)))
        for name in (
            "cost_eur",
            "efc",
            "throughput_kwh",
            "soc_min_frac",
            "soc_max_frac",
            "initial_soc_kwh",
            "terminal_soc_kwh",
            "terminal_value_eur_per_kwh",
            "inventory_adjustment_eur",
        ):
            object.__setattr__(self, name, float(getattr(self, name)))
        for name in (
            "solver_gap",
            "meter_cost_eur",
            "cost_lower_bound_eur",
            "cost_upper_bound_eur",
            "solver_time_limit_s",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, float(value))
        # A bare/legacy schedule carries only the total cost; treat it as the
        # meter cost with no inventory adjustment so both views agree.
        if self.meter_cost_eur is None:
            object.__setattr__(self, "meter_cost_eur", self.cost_eur)
        # The ceiling-exceedance observation is a count of intervals, so keep it
        # an exact non-negative int no matter what the caller handed in.
        exceeded = int(self.ceiling_exceeding_intervals)
        if exceeded < 0:
            raise ValueError(f"ceiling_exceeding_intervals must be >= 0, got {exceeded}")
        object.__setattr__(self, "ceiling_exceeding_intervals", exceeded)
        n = self.charge_kwh.size
        for name in ("discharge_kwh", "soc_kwh", "grid_kwh"):
            if getattr(self, name).size != n:
                raise ValueError(f"{name} has {getattr(self, name).size} entries, expected {n}")

    @property
    def net_flow_kwh(self) -> np.ndarray:
        """Battery-side exchange with the grid: positive when importing to charge."""
        return self.charge_kwh - self.discharge_kwh

    def as_dict(self) -> dict[str, object]:
        """Flat mapping matching the BENCHMARK_RESULT cost/cycle columns."""
        return {
            "cost_eur": self.cost_eur,
            "cost_basis": "untaxed",
            "efc": self.efc,
            "throughput_kwh": self.throughput_kwh,
            "soc_min": self.soc_min_frac,
            "soc_max": self.soc_max_frac,
            "solver_gap": self.solver_gap,
            "solver_status": self.solver_status,
            "bound_kind": self.bound_kind,
            "is_certified": self.is_certified,
        }

    def savings_vs(self, baseline: Schedule) -> float:
        """Cost the *baseline* schedule paid minus what this one pays.

        Positive means this schedule is cheaper than the baseline.  Losses are
        reported as negative savings — never clipped to zero.  Uses ``cost_eur``
        (the total objective, meter cost plus inventory adjustment) so a
        free-terminal plan is compared on the same basis as a closed one.
        """
        return baseline.cost_eur - self.cost_eur


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_spec(spec: BatterySpec) -> None:
    """Reject a physically meaningless battery specification.

    Raises:
        ValueError: If any parameter is non-finite, an efficiency is outside
            ``(0, 1]``, the SOC window is empty or inverted, the window leaves
            ``[0, 1]``, or the initial/terminal SOC sits outside the window
            (an unreachable starting or required ending state).
    """
    name = type(spec).__name__
    for f in fields(spec):
        if f.name == "terminal_soc_frac" and getattr(spec, f.name) is None:
            continue
        value = getattr(spec, f.name)
        if not math.isfinite(value):
            raise ValueError(f"{name}.{f.name} must be finite, got {value}")

    if spec.capacity_kwh <= 0:
        raise ValueError(f"{name}.capacity_kwh must be > 0, got {spec.capacity_kwh}")
    if spec.power_kw <= 0:
        raise ValueError(f"{name}.power_kw must be > 0, got {spec.power_kw}")
    for label, eta in (
        ("charge_efficiency", spec.charge_efficiency),
        ("discharge_efficiency", spec.discharge_efficiency),
    ):
        if not 0.0 < eta <= 1.0:
            raise ValueError(f"{name}.{label} must be in (0, 1], got {eta}")
    if not 0.0 <= spec.soc_min_frac < spec.soc_max_frac <= 1.0:
        raise ValueError(
            f"{name} needs 0 <= soc_min_frac < soc_max_frac <= 1, got "
            f"[{spec.soc_min_frac}, {spec.soc_max_frac}]"
        )
    if not spec.soc_min_frac <= spec.initial_soc_frac <= spec.soc_max_frac:
        raise ValueError(
            f"{name}.initial_soc_frac={spec.initial_soc_frac} is outside the "
            f"operating window [{spec.soc_min_frac}, {spec.soc_max_frac}]"
        )
    if not spec.soc_min_frac <= spec.effective_terminal_soc_frac <= spec.soc_max_frac:
        raise ValueError(
            f"{name}.terminal_soc_frac={spec.effective_terminal_soc_frac} is outside the "
            f"operating window [{spec.soc_min_frac}, {spec.soc_max_frac}]"
        )


def _as_plan(horizon: Horizon, charge_kwh, discharge_kwh) -> tuple[np.ndarray, np.ndarray]:
    """Coerce a proposed plan to float arrays and reject structural nonsense."""
    charge = np.asarray(charge_kwh, dtype=float).reshape(-1)
    discharge = np.asarray(discharge_kwh, dtype=float).reshape(-1)
    n = horizon.n
    for label, arr in (("charge_kwh", charge), ("discharge_kwh", discharge)):
        if arr.size != n:
            raise ValueError(f"{label} has {arr.size} entries, horizon has {n} intervals")
        if not np.isfinite(arr).all():
            bad = int(np.argmax(~np.isfinite(arr)))
            raise ValueError(f"{label} contains a non-finite value at t={bad}")
    return charge, discharge


def _first_indices(mask: np.ndarray) -> np.ndarray:
    """Up to ``_MAX_EXAMPLES`` indices where ``mask`` is True."""
    return np.flatnonzero(mask)[:_MAX_EXAMPLES]


def _describe(label: str, mask: np.ndarray, detail: np.ndarray | None = None) -> str:
    """Aggregate a per-interval violation mask into one readable message."""
    hits = int(mask.sum())
    where = _first_indices(mask)
    if detail is None:
        body = ", ".join(f"t={int(i)}" for i in where)
    else:
        body = ", ".join(f"t={int(i)}: {float(detail[int(i)]):.6g}" for i in where)
    more = "" if hits <= _MAX_EXAMPLES else f" (+{hits - _MAX_EXAMPLES} more)"
    return f"{label} at [{body}]{more}"


# ---------------------------------------------------------------------------
# Core trace
# ---------------------------------------------------------------------------


def soc_trajectory_kwh(spec: BatterySpec, charge: np.ndarray, discharge: np.ndarray) -> np.ndarray:
    """End-of-interval SOC (kWh) for a plan, from the signed-flow recurrence.

    Single source of truth for the recurrence: the simulator, both policies and
    the oracle all take their SOC from here, so a "feasible" schedule can never
    disagree with the physics by construction.
    """
    delta = charge * spec.charge_efficiency - discharge / spec.discharge_efficiency
    return spec.soc_bound_kwh("init") + np.cumsum(delta)


def grid_import_kwh(horizon: Horizon, charge: np.ndarray, discharge: np.ndarray) -> np.ndarray:
    """Net grid import per interval: ``load + charge - discharge``."""
    return horizon.load_kwh + charge - discharge


def horizon_cost_eur(horizon: Horizon, grid: np.ndarray) -> float:
    """Pretax energy cost over **usable** intervals only.

    Unusable (gap-excluded) intervals contribute nothing, identically for every
    policy.  Negative prices make individual terms (and the total) negative;
    that is correct, not a value to clamp.
    """
    mask = horizon.usable
    if not mask.any():
        return 0.0
    return float(np.dot(horizon.prices[mask], grid[mask]))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ceiling_exceedance_indices(
    horizon: Horizon,
    grid_kwh,
    *,
    tol: float = _TOL_KWH,
) -> np.ndarray:
    """Indices of **usable** intervals whose net grid import tops the ceiling.

    This is an *observation*, not an error.  The ceiling limits charging
    decisions; it cannot un-measure the past, so a plan whose import exceeds the
    ceiling because the **load** alone does is still feasible (see the module
    docstring, invariant 7) and lands here instead of in
    :func:`check_feasible`'s violations.  Counts and indices are restricted to
    usable intervals, the same basis as the cost: a gap-excluded interval has no
    trustworthy measurement to make a claim about.

    Returns an empty integer array when no ceiling is configured — an unknown
    ceiling can never be exceeded.

    Args:
        horizon: The horizon carrying the ceiling and the usable mask.
        grid_kwh: Net grid import per interval (kWh); usually
            :func:`grid_import_kwh` or :attr:`Schedule.grid_kwh`.
        tol: Absolute kWh slack, so plans are judged with the same tolerance the
            rest of the kernel uses.

    Raises:
        ValueError: If *grid_kwh* does not match the horizon's length or holds a
            non-finite value.
    """
    grid = np.asarray(grid_kwh, dtype=float).reshape(-1)
    if grid.size != horizon.n:
        raise ValueError(f"grid_kwh has {grid.size} entries, horizon has {horizon.n} intervals")
    if not np.isfinite(grid).all():
        bad = int(np.argmax(~np.isfinite(grid)))
        raise ValueError(f"grid_kwh contains a non-finite value at t={bad}")
    ceiling_kwh = horizon.interval_import_limit_kwh
    if ceiling_kwh is None:
        return np.empty(0, dtype=int)  # an unknown ceiling can never be exceeded
    return np.flatnonzero(horizon.usable & (grid > ceiling_kwh + tol))


def check_feasible(
    spec: BatterySpec,
    horizon: Horizon,
    charge_kwh,
    discharge_kwh,
    *,
    enforce_terminal_soc: bool = True,
    tol: float = _TOL_KWH,
) -> list[str]:
    """Describe every physical invariant violated by a plan.

    Returns an empty list when the plan is feasible.  Never raises for a
    *physical* violation — malformed input (wrong length, NaN, an impossible
    :class:`BatterySpec`) raises :class:`ValueError` instead, because those are
    programming errors rather than dispatch results.

    Charging is bounded by the power limit, SOC headroom and — only when
    :attr:`Horizon.import_limit_kw` is configured — the site import ceiling, as
    the headroom the load leaves: ``charge[t] <= max(0, ceiling*h - load[t])``
    (see :attr:`Horizon.charge_headroom_kwh`).  The **no-export** rule never
    bounds charging (charging raises import), and the ceiling never bounds
    *discharge*: discharging lowers the draw, so it stays available exactly when
    the ceiling is tightest.

    A net grid import above the ceiling is **not** a violation here.  The only
    way an interval can top the ceiling is a measured load that already does —
    history, not a decision — so it is reported as an observation by
    :func:`ceiling_exceedance_indices` and by
    :attr:`Schedule.ceiling_exceeding_intervals`, and plans that contain it stay
    feasible.  What *is* a violation is charging beyond the headroom, which a
    plan could have avoided.

    Args:
        enforce_terminal_soc: When ``False`` the horizon-level terminal SOC
            accounting rule is skipped.  The causal policy and the free-mode
            oracle use this: they cannot guarantee end-of-horizon energy
            neutrality — but :func:`check_feasible` with the default still
            reports the mismatch, and the fixed-mode
            :func:`energy_baseline.oracle.solve_oracle` enforces it.
        tol: Absolute kWh slack for every comparison, scaled by
            ``capacity_kwh`` for SOC-window checks.

    Raises:
        ValueError: If *spec* is meaningless or the plan shape/values are bad.
    """
    validate_spec(spec)
    charge, discharge = _as_plan(horizon, charge_kwh, discharge_kwh)

    limit = spec.interval_energy_limit_kwh(horizon.interval_seconds)
    soc = soc_trajectory_kwh(spec, charge, discharge)
    grid = grid_import_kwh(horizon, charge, discharge)
    floor_kwh = spec.soc_bound_kwh("min")
    ceil_kwh = spec.soc_bound_kwh("max")
    soc_tol = max(tol, tol * spec.capacity_kwh)

    violations: list[str] = []

    neg = (charge < -tol) | (discharge < -tol)
    if neg.any():
        violations.append(_describe("negative flow (charge/discharge must be >= 0)", neg))

    over = (charge > limit + tol) | (discharge > limit + tol)
    if over.any():
        violations.append(
            _describe(
                f"flow above power limit {limit:.6g} kWh/interval",
                over,
                detail=np.maximum(charge, discharge),
            )
        )

    both = (charge > tol) & (discharge > tol)
    if both.any():
        violations.append(
            _describe("simultaneous charge and discharge (one binary mode required)", both)
        )

    forced = ~horizon.usable & ((charge > tol) | (discharge > tol))
    if forced.any():
        violations.append(_describe("nonzero flow in an unusable interval", forced))

    below = soc < floor_kwh - soc_tol
    if below.any():
        violations.append(
            _describe(f"SOC below window floor {floor_kwh:.6g} kWh", below, detail=soc)
        )

    above = soc > ceil_kwh + soc_tol
    if above.any():
        violations.append(
            _describe(f"SOC above window ceiling {ceil_kwh:.6g} kWh", above, detail=soc)
        )

    exported = grid < -tol
    if exported.any():
        violations.append(
            _describe(
                "grid import negative (intentional export is not allowed)", exported, detail=grid
            )
        )

    # The configured site import ceiling limits the *charging decision*, never
    # the measurement: charge[t] <= max(0, ceiling*h - load[t]), i.e. a plan may
    # only top up the headroom the load leaves.  Skipped entirely when the ceiling
    # is unknown, because "unknown" must not silently become a number.  Net grid
    # import above the ceiling is deliberately NOT checked here: the only way it
    # can happen is a measured load that already exceeds the ceiling, which is
    # history no dispatch can rewrite (module docstring, invariant 7).  Such
    # intervals are surfaced as an observation by ceiling_exceedance_indices /
    # Schedule.ceiling_exceeding_intervals, and discharge is unconstrained by the
    # ceiling because it lowers the draw.
    headroom_kwh = horizon.charge_headroom_kwh
    if headroom_kwh is not None:
        over_headroom = charge > headroom_kwh + tol
        if over_headroom.any():
            violations.append(
                _describe(
                    "charge above the site import ceiling headroom for "
                    f"{horizon.interval_import_limit_kwh:.6g} kWh/interval",
                    over_headroom,
                    detail=charge,
                )
            )

    if enforce_terminal_soc:
        target = spec.soc_bound_kwh("terminal")
        drift = abs(float(soc[-1]) - target)
        if drift > soc_tol:
            violations.append(
                f"terminal SOC {soc[-1]:.9g} kWh != required {target:.9g} kWh "
                f"(drift {drift:.3g} kWh > tol {soc_tol:.3g})"
            )

    return violations


def compute_efc(spec: BatterySpec, throughput_kwh: float) -> float:
    """Equivalent full cycles on the **DC** side.

    ``throughput_kwh`` is the AC/metered energy the battery delivered (the sum of
    the discharge array).  Degradation is driven by the coulomb the *cells*
    moved, not the energy that reached the meter, so the DC discharge throughput
    is ``throughput_kwh / eta_d`` and one EFC is one crossing of the *usable DC
    window* (``capacity * (soc_max - soc_min)``)::

        efc = (throughput_kwh / discharge_efficiency) / usable_window_kwh

    A full usable-DC-window drain is therefore exactly 1.0 EFC even when
    ``eta_d < 1``: the cells delivered the whole window's energy; the round-trip
    loss means less of it reached the meter, which is captured by
    ``throughput_kwh`` (AC), not by EFC.

    Raises:
        ValueError: If the usable window is not positive, or throughput is
            non-finite or negative.
    """
    window = spec.usable_window_kwh
    if window <= 0:
        raise ValueError(
            f"usable window is {window} kWh; EFC is undefined (need soc_max > soc_min)"
        )
    value = float(throughput_kwh)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"throughput_kwh must be finite and >= 0, got {value}")
    dc_throughput = value / spec.discharge_efficiency
    return dc_throughput / window


def simulate(
    spec: BatterySpec,
    horizon: Horizon,
    charge_kwh,
    discharge_kwh,
    *,
    enforce_terminal_soc: bool = True,
    tol: float = _TOL_KWH,
) -> Schedule:
    """Validate a plan against the physics and return its full accounting.

    The returned :class:`Schedule` carries ``solver_status="not_applicable"``,
    ``solver_gap=None``, ``bound_kind="none"`` and ``is_certified=False`` — a
    traced plan is not a solver result.  ``meter_cost_eur == cost_eur`` and
    ``inventory_adjustment_eur == 0.0`` (a traced plan does not value terminal
    inventory); the oracle overrides those fields.
    ``ceiling_exceeding_intervals`` counts the usable intervals whose net import
    topped a configured ceiling: an observation about the measured load, and no
    reason to raise.

    Raises:
        BatteryPhysicsError: If any invariant in :func:`check_feasible` is
            violated; the message lists every violation found.
        ValueError: If *spec* is meaningless or the plan is structurally invalid.
    """
    validate_spec(spec)
    charge, discharge = _as_plan(horizon, charge_kwh, discharge_kwh)

    violations = check_feasible(
        spec,
        horizon,
        charge,
        discharge,
        enforce_terminal_soc=enforce_terminal_soc,
        tol=tol,
    )
    if violations:
        raise BatteryPhysicsError(
            f"infeasible battery plan ({len(violations)} violation class(es)):\n  "
            + "\n  ".join(violations)
        )

    soc = soc_trajectory_kwh(spec, charge, discharge)
    grid = grid_import_kwh(horizon, charge, discharge)
    throughput = float(discharge.sum())  # AC/metered delivered energy
    meter_cost = horizon_cost_eur(horizon, grid)
    initial_kwh = spec.soc_bound_kwh("init")
    fracs = np.concatenate(([spec.initial_soc_frac], soc / spec.capacity_kwh))
    return Schedule(
        charge_kwh=charge,
        discharge_kwh=discharge,
        soc_kwh=soc,
        grid_kwh=grid,
        cost_eur=meter_cost,
        efc=compute_efc(spec, throughput),
        throughput_kwh=throughput,
        soc_min_frac=float(fracs.min()),
        soc_max_frac=float(fracs.max()),
        initial_soc_kwh=initial_kwh,
        terminal_soc_kwh=float(soc[-1]),
        meter_cost_eur=meter_cost,
        inventory_adjustment_eur=0.0,
        # Recorded, not enforced: importing above the ceiling because the measured
        # load did is history, and this plan is feasible regardless.
        ceiling_exceeding_intervals=int(ceiling_exceedance_indices(horizon, grid, tol=tol).size),
    )


def _denoise(values: np.ndarray, tol: float) -> np.ndarray:
    """Snap solver noise to exact zeros.

    HiGHS returns ``-1e-12`` where the model says ``0`` and ``4.9999999e-1``
    where it says ``0.5``.  Only magnitudes at or below *tol* are touched, and
    *tol* is the same slack the plan is validated against, so denoising cannot
    change a feasible plan into an infeasible one — it only keeps the arrays
    written to Parquet free of signed zeros and 1e-12 kWh "energy".
    """
    return np.where(np.abs(values) <= tol, 0.0, values)
