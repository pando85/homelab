"""Analysis dataset builder — raw immutable Parquet to the 15-minute grid.

Reads the raw Prometheus archive **through the successful extraction manifests**
(``manifests/*.json`` with ``source == "prometheus"``), never by globbing the
part directory.  A failed extraction leaves orphaned parts behind but writes no
manifest, so resolving coverage from manifests ignores those orphans by
construction.  Writes a content-addressed 15-minute analysis dataset to
``analysis/v1/<dataset_id>/parts/`` validated against ``ANALYSIS_INTERVAL``.
Nothing here talks to the cluster: the builder is a pure function of the raw
archive, ``tariff.yaml`` and the requested window.

Input integrity
---------------
Every part a manifest references is validated against the manifest before use:
the file's **full SHA-256** (not the 8-hex-character filename prefix, which a
report or a filename can never vouch for on its own), the declared **row count**,
and the stored ``RAW_SAMPLE`` **schema** including its non-null invariants.  The
referenced path must resolve **under** ``data_dir``; a ``..`` escape or an
absolute path is rejected so a corrupt manifest cannot make the builder read the
host filesystem.  To close the time-of-check/time-of-use gap between hashing and
reading, each part is read as a single byte buffer that is hashed **and** parsed
from the very same bytes (:func:`pyarrow.BufferReader`), so a file that changes
after its manifest is caught by the hash rather than silently re-read.

Series stitching
----------------
``series_key`` excludes only ``instance``/``pod_ip``, so a label change to
``job``/``pod``/``service`` mints a **new series_key for the same physical
counter**.  The archive shows this twice for the whole-home counter: on
2025-06-16T05:00:09.902Z → 05:02:34.138Z a ``friendly_name`` capitalisation
change (``shellyem-main channel 1 energy`` → ``shellyem-main Channel 1 Energy``,
together with the HA ``instance`` IP moving 10.0.4.251 → 10.0.4.2) split ONE
series into two, and the Home Assistant ServiceMonitor was later renamed
(``job=home-assistant-hass`` in every 2025 series, ``job=home-assistant`` in the
2026-10 series).  The 2025-06-16 split is not a counter replacement: across it
the counter moved +0.0138 kWh in 144.236 s — an implied 344 W, entirely ordinary
for this home — so it is recorded as an era boundary, not a reset, and
``counter_resets: []`` is the measured result, not a default.  Eras are stitched
by **concatenating every series of an ``(entity, metric)`` group in strict
timestamp order** — never by summing two series as independent counters, which
would double-count the whole home.

Two samples from *different* series_keys at the *same* timestamp is an overlap,
and overlapping series make any stitching arbitrary (is it one counter or two?).
Verified absent over the cutover window, so this builder refuses: it raises
:class:`OverlappingSeriesError` rather than averaging, summing or picking.  Same
series_key, same timestamp, same value is just a re-extraction of one sample and
is deduplicated (overlapping extraction windows, and multiple manifests that
cover the same samples, are expected and idempotent); same key and timestamp
with a *different* value is a conflict and also raises.  A retry under a new
``extraction_id`` is therefore ignored at the logical level: identical
``(series_key, ts, value)`` samples collapse, genuine value conflicts surface.

Energy comes from counter deltas, never from power
--------------------------------------------------
The 15-minute load interval is the difference of the energy counter
(``sensor.shellyem_main_channel_1_energy``).  Instantaneous power is *never*
integrated and no load figure is ever scaled up from a per-minute delta: power is
a crosscheck signal only, and integrating it would fabricate energy where scrapes
are missing.  Nested submeters and the ``*_energy_returned`` export counter are
**not** added to gross load — if they are present in the inputs at all, since
they sit inside channel 1 and adding them double-counts.  What is inside channel
1 is a *user assertion*, not a measurement — see the next section: the builder
records the assertion with its date, states that the data does not verify it, and
names the specific entities it actually saw being dropped.

Meter identity is a user assertion, not a measurement
-----------------------------------------------------
Nothing in the archive says where the current transformers sit.  The wiring used
here — ch1 = whole-home feed, ch2 = aerotherm/appliance branch, servers nested
inside ch1 — is **user-confirmed on 2026-10-23** (ADR-0003 §data contract) and is
recorded in the build manifest as ``meter_identity_provenance`` with
``verified_from_data: false``.  It is not instrument-verified: the supporting
observation (a ~193 W floor that is never undercut, 520,151 samples, minimum
192.78 W) is *equally consistent with a subcircuit*, because a subcircuit that
never turns off looks exactly like a whole home that never turns off.  Two
consequences are published rather than glossed: the export channel
``sensor.shellyem_main_channel_1_energy_returned`` read **identically 0.0 kWh in
every one of the 520,151 samples** (1 distinct value), so no export was measured
on that channel all year and the ch1 sign is untested; and both it and
``sensor.shellyem_main_channel_1_power`` are marked ``used_for="not_used"``.

Every kWh that enters the build is accounted for
------------------------------------------------
A known total that reaches no interval is a defect, not a detail, so
:func:`reconcile_energy` states the identity it enforces:

``raw endpoint difference == Σ allocated + Σ declared_unattributed + residual``

with ``declared_unattributed`` itemised as gap spans crossing interval boundaries,
energy already allocated inside an interval that was then declared unusable (a gap
or reset touching it makes its split unknowable), spans lying wholly outside the
grid, the uncovered tail of spans clipped by the grid edge, and the counter change
across resets.  ``residual`` is what nothing explains; it must stay under
:data:`DEFAULT_RECONCILIATION_TOLERANCE_KWH` or the build raises
:class:`ReconciliationError` instead of publishing.

This check exists because of a real 8.1229 kWh leak.  The 2025 build reconciled a
raw endpoint difference of 5,546.7085 kWh against 5,521.2218 kWh allocated —
25.4867 kWh (0.459 % of the year) missing — while the manifest declared only
17.3638 kWh of it as ``unattributed_spans.gap_span_total_kwh``.  The other
8.1229 kWh, 0.146 % of annual energy, was silently discarded from the 296
intervals that received pieces of ordinary spans and were then nulled because a
gap span touched them too; it had no category and no declaration.  The allocation
arithmetic itself was verified sound (an independent re-implementation
reproduced the boundary-proportional split conserving exactly, max |diff| 0.0138
kWh per interval, Σdiff 0.0138 kWh = the era-boundary step).  The same 17.3638
explains why ``kwh_through_split_spans`` (367.5647) sits below an independent
boundary-crossing total (384.9285): gap spans are excluded from the split figure
by construction.

Delta attribution rules (all disclosed, never silent)
-----------------------------------------------------
Each consecutive sample pair ``(t0,v0),(t1,v1)`` produces a span carrying
``v1-v0`` over the half-open range ``[t0,t1)``:

``fully contained``
    The span lies inside one interval → that interval gets
    ``allocation_quality="exact"``.
``crosses an interval boundary`` (normal cadence)
    The span total is known and its distribution inside the span is not, so the
    delta is **split proportionally to time** at each crossed boundary: the
    counter is linearly interpolated at the boundary instant, each piece goes to
    the interval containing it, and every interval that received a piece of a
    split span carries ``allocation_quality="estimated_boundary"`` — never
    ``exact``, which stays reserved for fully contained spans.
    ``energy_quality`` remains ``measured`` because the number is a real counter
    delta; what is estimated is only *where inside the interval* it fell, which is
    exactly what the ``allocation_quality`` column exists to disclose.  The split
    telescopes by construction: the pieces of a span re-sum to that span's
    original delta, so the sum over any whole number of intervals remains an
    exact counter difference.  Consumers that need strictly bounded energy filter
    ``allocation_quality == "exact"``; the benchmark admits ``estimated_boundary``
    by default (it is a bounded, telescoping-preserving approximation, not a
    degraded fallback).
``gap`` (span longer than ``gap_factor`` × the median sample interval)
    Attributing a multi-hour delta to whichever interval happens to contain the
    span's right end would fabricate a spike there and zero everywhere else in
    the gap, so those intervals carry ``raw_grid_kwh=None`` with
    ``energy_quality="gap_partial"`` / ``"gap_total_unknown"`` and
    ``allocation_quality="unknown_gap"`` plus ``gap_seconds`` set to the actual
    span duration.  Nothing is ever interpolated **across a gap**, and a gap is
    never allowed to look like zero consumption.  The known total over the full
    span is preserved once per span in the build manifest.  A gap span lying
    entirely *inside* one interval is the exception: its total needs no
    distribution, so it is attributed (and still flagged).
``counter reset`` (``v1 < v0``)
    Never emitted as negative energy and never bridged with an invented value:
    the intervals the reset span touches get
    ``energy_quality="counter_reset_bridged"`` and ``raw_grid_kwh=None``, the
    reset is recorded in the manifest, and the timeline continues from ``v1``
    (the bridge) so post-reset deltas stay valid.
``unresolvable coverage`` (no sample span overlaps the interval at all)
    Nothing is known about it, so a lone reading inside it says nothing about
    consumption and no zero is fabricated: ``raw_grid_kwh=None``,
    ``energy_quality="gap_total_unknown"``,
    ``allocation_quality="unknown_gap"``.  ``energy_quality="measured"``
    always carries a number; an interval is never labelled measured while empty.

Why almost every span crosses a boundary (and why that is fine)
---------------------------------------------------------------
Measured on the live archive: samples arrive about every 60 s at an irregular
sub-second offset that **drifts** (the scrape phase was ``:39.219`` in the 2026
window and ``:34.733`` in 2025 — a property of the poller, not of the data), so
**essentially every** counter span straddles a 15-minute boundary.
Right-endpoint attribution therefore flagged almost 100 % of intervals
``allocation_quality="unknown_gap"``, which made the quality signal useless and
the dataset unbenchmarkable while throwing away information the archive actually
carries.  Bounded proportional splitting keeps the disclosure and restores
usability: the per-interval error is bounded by one sample cadence (≤60 s of a
900 s interval), so at most ~7 % of an interval's energy can move across its
edge, the daily total is untouched, and the manifest records the count and total
of every split span.

The counter is quantised: 15-minute deltas are fine, per-minute power is not
-----------------------------------------------------------------------------
Measured over the whole 2025 archive (520,150 steps between consecutive
readings: 466,460 positive, 53,690 zero, **none negative**), the whole-home
energy counter advances on a **0.1 Wh (0.0001 kWh) lattice**: the integer GCD of
every positive increment is exactly 100 µkWh, in all twelve months.  It is **not**
1.6 Wh — a claim that came from a small sample.  Over the full population 0
increments equal 0.0016 kWh, and only 7.96 % (37,138) are divisible by it,
against 6.25 % expected by chance on a 0.0001 kWh lattice; the same near-chance
shares hold for 0.0002 (51.4 % vs 50 %), 0.0005 (19.8 % vs 20 %), 0.001 (9.9 %
vs 10 %) and 0.002 (5.0 % vs 5 %), so 0.0016 was never special.  Every observed
delta *is* an integer multiple of 0.0001 kWh; the smallest one ever seen is
0.0033 kWh (3.3 Wh ≈ 193 W for 60 s).  The quantum is therefore **measured per
build** (GCD of the loaded increments, recorded with its sample size) and
:data:`COUNTER_QUANTISATION_KWH` is only the documented fallback when there is
nothing to measure.  A 15-minute delta is accurate to about ±0.1 Wh, roughly
0.08 % of a typical 120 Wh interval — not "±1.6 Wh, roughly 1.3 %".

Per-minute deltas are unreliable for a different reason: **update aliasing**.
Measured on the same population, 10.36 % of 60 s steps (53,684 of 518,146) carry
a **zero** increment, and in 100 % of those the co-timestamped power sensor read
≥200 W (median 441 W) — the device simply had not updated between the two
scrapes.  Zero-runs are length-1 (53,609 of them), so the next step carries about
two minutes of energy.  Nonzero 60 s increments imply 198 W minimum, 474 W
median, 3,744 W p99, 14,112 W maximum: a sawtooth, not load.  Because the
allocation sums whole spans into an interval and splits a straddling span by
*overlap time*, aliasing cancels: the same 15-minute boundary delta comes out
whether the energy ticks evenly or in aliased pairs.  That is the quantitative
reason energy comes from 15-minute counter deltas and never from integrating
instantaneous power or from a per-minute delta treated as a rate.

Prices are hourly: bucket by the tariff timezone's clock hour
-------------------------------------------------------------
PVPC publishes one price per clock hour, and the archive is consistent with that
— but **when an hour's price becomes effective is not recorded anywhere**.  The
label set on the price series is exactly ``__name__, domain, endpoint, entity,
friendly_name, instance, job, namespace, pod, service``: no device-side
timestamp and no staleness flag, so "hourly by construction" is external
knowledge about the Spanish tariff, not a measurement this pipeline can make.
What *is* measured, over all 522,644 price samples of the 2025 archive (8,723
hours carrying data): 98.06 % of those hours (8,554) are single-valued, 1.94 %
(169) carry exactly two values, never three or more.  The hour's first sample is
*not* at a fixed offset — first-observation offsets run 0.064 s … 2,708.684 s
with p05 3.9, p25 10.5, median 25.0, p75 38.5, p95 58.1 s and 56 distinct
whole-second values; 8,707 of the 8,723 hours (99.82 %) are first observed
inside their own first minute and 16 are later.  The ``:39`` figure formerly
quoted here was the 2025-01-01 scrape phase and is not a property of the data
(the 2026 window scrapes at ``:39.219``, 2025-01-01 at ``:34.733``, and the
median across the year is 25 s).  In 169 hours the first sample still carries
the *previous* hour's price, 168 of them switching at +60.1 s; those hours are
concentrated in one contiguous episode, 2025-03-31 → 2025-04-07 (17 in March =
2.56 % of March's hours, 151 in April = 21.42 % of April's), plus a single
isolated hour on 2025-12-31 — and 2025-12-31T21:00Z is also the only genuine
intra-hour conflict in the whole year (0.24521 → 0.16722 €/kWh at +1,206 s).

The previous rule — take the last sample at or before ``interval_start`` — read
the last sample before the hour top for the interval ``[15:00,15:15)``, i.e.
**hour 14's price**.  That misprices the first interval of every hour (one
interval in four, always the one at the hour top, where a price change lands):
replaying the old rule on hour-aligned synthetic data returns hour 14's price
for ``[15:00,15:15)`` and hour 15's for the three intervals after it.  A lag
that is systematic and invisible is worse than one that is loud, because it
silently biases every arbitrage comparison toward the pre-change price.  Each
interval now carries the price of the clock hour it belongs to, so all four
intervals of an hour share one value; the measured 98.06 %/99.80 % above is what
supports that rule, and the 1.94 % of disagreeing hours are the cost it pays
(declared, never averaged).

Hour bucketing uses the **configured tariff timezone** (``tariff.yaml`` →
``tariff_periods.timezone``, e.g. Europe/Madrid), because that is the wall clock
on which the published hour is defined; bucketing by UTC hour would shift the
price by one or two hours and break at DST transitions.  Buckets are keyed by the
*instant* at which that local hour starts, never by its wall-clock label, so the
two occurrences of a fall-back hour stay two distinct buckets (each with its own
four intervals) and the skipped spring-forward hour simply never appears.

The residual look-ahead is stated plainly rather than hidden: the hour's price is
first *observed* after the hour begins, so giving it to the interval that starts
at the hour top assumes a value earlier than it was archived.  The build records
the **measured** distribution of that assumption over every hour in the window —
median and percentiles as well as the maximum (2,708 s measured for 2025, not a
~60 s bound) — and there is deliberately no ``price_publication_lag`` column.
The per-hour ``details`` list in the manifest is a **truncated sample** capped at
``MAX_MANIFEST_DETAILS`` rows; no maximum or distribution is ever derived from
it, and a reader must not treat the sample as the year.

Agreement is verified, not assumed: if the samples inside one clock hour do
**not** all agree, nothing is averaged and nothing is silently picked — every
interval of that hour gets ``price_quality="ambiguous"`` with a null price and
the disagreement is counted in the manifest.  Nulling a disagreeing hour rather
than averaging it is a deliberate choice: the archive carries no information
about *when inside* the hour the new value became effective, so a mean would
invent a price that never existed.  An hour with no sample of its own
carries the last known value as ``forward_filled`` while that value is within
``staleness_seconds``, otherwise ``missing``.  ``price_quality="measured"`` means
a sample really was observed inside the hour and all of them agreed.  Negative
prices are legitimate (they are the best charging opportunities) and pass through
verbatim at every step — no filtering, no clamping.

Local time, tariff periods and DST
----------------------------------
The grid is generated in **UTC** and only then labelled with local time via
:mod:`zoneinfo`, which is what makes a 23-hour Spanish spring-forward day yield
exactly 92 intervals and a 25-hour fall-back day exactly 100.  ``tariff_period``
(1/2/3) comes from ``tariff.yaml`` — never hardcoded — and ``dst_transition`` is
derived from the measured local-day length, not from a date table.

Battery absence is an assumption, not a detection
-------------------------------------------------
:func:`detect_battery_active` can only say a battery is *present* when a
registered telemetry entity is actually seen, and *absent* only when a non-empty
telemetry registry saw none of its entities.  With an empty registry
(:data:`BATTERY_TELEMETRY_ENTITIES == ()`) it returns ``None`` — **unknown** —
because the absence of an unregistered metric proves nothing about hardware.
:func:`build_dataset` therefore takes an explicit ``battery_absent`` assumption
(default ``True``, the compatibility-preserving configuration) that is recorded
in the build manifest and reported, never silently derived from detection.  When
detection *does* find battery telemetry the build refuses: gross load would no
longer equal the feed meter and reconstruction is unimplemented (ADR-0003).

Deterministic output
--------------------
``dataset_id`` is a SHA-256 over the source part hashes, the canonical config
hash, the code version (including the working-tree dirty marker), the schema
version, the requested window and the builder parameters, so identical inputs
reproduce an identical id and identical bytes.  A dirty working tree produces a
different id from a clean one, so the reproducibility guarantee the id exists to
provide is preserved.  Parts are published with :func:`paths.write_part_atomic`
(no-clobber, content-addressed), and the dataset directory (parts plus
``build_manifest.json``) carries no wall-clock value so a rebuild of the same
inputs is byte-for-byte idempotent.  The run timestamp lives in the ordinary
extraction manifest under ``manifests/`` instead.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from energy_baseline.config import (
    TariffPeriods,
    load_config,
    resolve_config_path,
    tariff_period_for,
)
from energy_baseline.extract_prom import (
    DEAD_DUPLICATE_ENTITY_IDS,
    from_micros,
    iso_z,
    parse_utc,
    to_micros,
)
from energy_baseline.extract_prom import (
    SOURCE as PROM_SOURCE,
)

# ``manifest`` owns the git-version discovery and the canonical config hash;
# reusing them keeps the dataset id and the extraction manifests on the same
# definition of "code version" and "config hash".
from energy_baseline.manifest import (
    code_version as manifest_code_version,
)
from energy_baseline.manifest import (
    config_hash,
    new_manifest,
    write_manifest,
)
from energy_baseline.paths import parts_dir, resolve_data_dir, write_part_atomic
from energy_baseline.schemas import (
    ANALYSIS_INTERVAL,
    SCHEMA_VERSION,
    validate_table,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE = "analysis"
"""``source`` recorded on the run manifest for a build."""

PART_KIND = "intervals"
"""``kind`` component of the output part filenames."""

INTERVAL_SECONDS = 900
"""Analysis grid width: 15 minutes, always in UTC."""

DEFAULT_GAP_FACTOR = 3.0
"""A sample span longer than this many median sample intervals is a gap."""

DEFAULT_PRICE_STALENESS_SECONDS = 3600.0
"""Age bound for ``price_quality="measured"`` on the *carried* value.

The PVPC price is republished hourly, so an hour that has a sample of its own is
``measured`` regardless of age.  When an hour has no sample of its own and the
builder carries the last known value, that carry is ``forward_filled`` only while
the carried value is within this age; older carries become ``missing``.
"""

PRICE_PERIOD_SECONDS = 3600
"""PVPC publishes one price per clock hour.

Descriptive: the bucketing itself floors to the tariff timezone's wall-clock hour
start (see :func:`_clock_hour_start`) rather than adding 3600 s, because that is
what stays correct across a DST transition and in half-hour-offset zones.

Supported by measurement, not by the interval alone: 98.06 % of the 8,723 hours
of the 2025 archive that carry a price sample carry exactly one distinct value,
1.94 % carry two and none carries three or more.  The disagreeing hours are
nulled (``price_quality="ambiguous"``), never averaged, because the archive
records no effective time that would say which value held when.
"""

PRICE_LOOKAHEAD_NOTE = (
    "The clock hour's price is assigned to all four of its intervals, and it is "
    "first OBSERVED after the hour began; the archive records no effective time for "
    "any price sample, so that assignment is an assumption whose size is measured "
    "per build in price_alignment.first_observation_offset_seconds (median and "
    "percentiles over every hour, never from the truncated details sample)."
)
"""Recorded verbatim in the build manifest; no ``price_publication_lag`` column.

Deliberately free of a hardcoded offset: an earlier revision asserted "~H:00:39"
and "at most one sample interval (~60 s)".  Both were false for the full 2025
archive, where first-observation offsets run 0.064 s to 2,708.684 s (median
25.0 s; 16 of 8,723 hours are first observed after their own first minute).  The
``:39`` figure was one day's scrape phase, not a property of the data.
"""

PRICE_PUBLICATION_PROVENANCE = (
    "No field in the raw archive states when a price became effective: the label "
    "keys observed on the price series are listed in price_alignment."
    "observed_label_keys and contain no device-side time and no staleness flag. "
    "PVPC being hourly by construction is external knowledge about the Spanish "
    "tariff, not a measurement made here; what is measured is that 98.06 % of "
    "hours carry a single value.  Disagreeing hours are nulled rather than "
    "averaged because the data cannot say which value held when."
)
"""Honest provenance for the hourly assumption (see the module docstring)."""

COUNTER_QUANTISATION_KWH = 0.0001
"""Documented **fallback** for the whole-home counter's quantum: 0.1 Wh.

The value a build publishes is the *measured* one — the integer GCD of the
positive increments actually loaded (see :func:`measure_counter_quantisation`),
recorded in the manifest with its sample size.  This constant is used only when
the window carries no positive increment at all.

Measured on the full 2025 archive: GCD = 100 µkWh over 466,460 positive
increments (520,150 steps, none negative), the same value in all twelve months.
An earlier revision of this file asserted 0.0016 kWh (1.6 Wh) as "verified, every
observed delta is an integer multiple of it"; that was a small-sample
coincidence.  Over the whole population **zero** increments equal 0.0016 kWh and
only 7.96 % are divisible by it, against the 6.25 % that a 0.0001 kWh lattice
gives by chance — while 100 % are divisible by 0.0001.  Accuracy statements
follow the measured lattice: a 15-minute delta is bounded by about ±0.1 Wh,
roughly 0.08 % of a typical 120 Wh interval (not ±1.6 Wh / 1.3 %).
"""

LATTICE_CANDIDATES_KWH: tuple[float, ...] = (0.0001, 0.0002, 0.0005, 0.001, 0.0016, 0.002)
"""Divisors whose observed share is recorded next to the measured GCD.

Kept in the manifest so the 0.0016 mistake cannot be repeated by inspection: on a
0.0001 kWh lattice every one of these has a known chance share (0.0001 → 100 %,
0.0002 → 50 %, 0.0005 → 20 %, 0.001 → 10 %, 0.0016 → 6.25 %, 0.002 → 5 %), and
only the GCD reaches 100 %.
"""

DEFAULT_RECONCILIATION_TOLERANCE_KWH = 1e-6
"""Energy the build cannot explain before the build is considered trustworthy.

1 mWh: about 8,000x smaller than the 8.12 kWh this file once dropped silently and
orders of magnitude below the counter lattice, yet far above double-precision
accumulation noise (the full 2025 build reconciles to 0.0 kWh).  Exceeding it
means kWh entered the build and reached no interval *and* no declared category —
see :func:`reconcile_energy`.
"""

DEFAULT_LOOKBACK = timedelta(days=2)
"""How far before ``start`` raw samples are loaded.

The first interval needs the last sample at or before its own start to compute a
delta and a price; without a lookback every window would begin with an
unnecessary gap flag.
"""

GRID_COUNTER_ENTITIES: tuple[str, ...] = ("sensor.shellyem_main_channel_1_energy",)
"""Whole-home import counters — exactly one feed is the parent meter.

Gross load is the delta of that single feed, never a sum over entities.
"""

PRICE_ENTITIES: tuple[str, ...] = ("sensor.esios_pvpc",)
"""Price series (EUR/kWh, taxes excluded — see ADR-0003 §cost model)."""

NESTED_SUBMETER_ENTITIES: tuple[str, ...] = (
    "sensor.shellyem_main_channel_1_energy_returned",
    "sensor.shellyem_main_channel_1_power",
    "sensor.shellyem_main_channel_2_energy",
)
"""Series deliberately **not** added to gross load.

Channel 2 (aerotherm branch), the server submeters and the instantaneous power
sensor are all said to sit inside channel 1, so adding them would double-count;
``*_energy_returned`` is an export counter, not import.  Whether any of them was
actually *dropped* is a per-build measurement, not a claim: the build records an
entity as an exclusion only if it was observed in the inputs it read, and reports
the rest under ``configured_but_absent_from_inputs`` (see
:func:`disclose_exclusions`).  Listing an entity here has never meant the
archive contained it.
"""

METER_IDENTITY_ASSERTION: dict[str, object] = {
    "kind": "user_assertion",
    "asserted_on": "2026-10-23",
    "asserted_by": "homelab owner, during ADR-0003 review",
    "mapping": {
        "sensor.shellyem_main_channel_1_energy": "whole-home import feed (channel 1)",
        "sensor.shellyem_main_channel_2_energy": "aerotherm / appliance branch (channel 2)",
        "server submeters": "nested inside channel 1",
    },
    "verified_from_data": False,
}
"""Channel identity is an assertion the pipeline inherits, not something it measured.

Nothing in the archive records current-transformer placement, so no build
manifest field can document it.  The user confirmed this mapping on 2026-10-23
and ADR-0003 §data contract relies on it, which is exactly why the gross-load
series has a *settled assumption* rather than a verified basis.

The data does not corroborate it either way: the supporting observation — a
roughly 193 W floor that is essentially never undercut (measured minimum 192.78 W
over 520,151 whole-home power samples, median 436 W) — is **equally consistent
with a subcircuit that never turns off**, because a baseload that is always on
looks the same from either side of the meter.  Anyone reading this file should
treat "ch1 = whole home" as unverified wiring until a physical check or a
generation/discharge event breaks the tie.
"""

EXPORT_CHANNEL_PROVENANCE_NOTE = (
    "sensor.shellyem_main_channel_1_energy_returned read a single distinct value "
    "(0.0 kWh) across every sample in the archive, so no export was measured on "
    "that channel; the ch1 sign convention is therefore untested, and the "
    "channel-1-not-reversed conclusion does not rest on it."
)
"""Recorded beside the measured value range so the untested sign is visible."""

BATTERY_TELEMETRY_ENTITIES: tuple[str, ...] = ()
"""SEAM: battery charge/discharge counters, once a battery exists.

Empty because no battery telemetry has been registered.  An empty registry means
battery presence is **unknown**, not proven absent — see
:func:`detect_battery_active` and :func:`build_dataset`'s ``battery_absent``
assumption.  Adding an entity here is not by itself enough; :func:`resolve_gross_load`
still refuses to equate net grid with gross load.
"""

# Categorical values (allowed sets documented in ``schemas.py``).
ENERGY_MEASURED = "measured"
ENERGY_GAP_PARTIAL = "gap_partial"
ENERGY_GAP_TOTAL_UNKNOWN = "gap_total_unknown"
ENERGY_COUNTER_RESET_BRIDGED = "counter_reset_bridged"
ALLOCATION_EXACT = "exact"
ALLOCATION_ESTIMATED_BOUNDARY = "estimated_boundary"
ALLOCATION_UNKNOWN_GAP = "unknown_gap"
PRICE_MEASURED = "measured"
PRICE_FORWARD_FILLED = "forward_filled"
PRICE_MISSING = "missing"
PRICE_AMBIGUOUS = "ambiguous"
DST_SPRING_FORWARD = "spring_forward"
DST_FALL_BACK = "fall_back"

# Battery-presence states reported by :func:`detect_battery_active`.
BATTERY_PRESENT = "present"
BATTERY_ABSENT = "absent"
BATTERY_UNKNOWN = "unknown"

MAX_MANIFEST_DETAILS = 500
"""Cap on per-item detail lists in the build manifest (counts stay exact)."""

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BuildError(RuntimeError):
    """The dataset cannot be built from the inputs given."""


class OverlappingSeriesError(BuildError):
    """Two different ``series_key`` values share one timestamp.

    Raised instead of guessing whether the rows are one counter or two: the
    stitched total would depend on the choice made silently.
    """


class ConflictingSampleError(BuildError):
    """One series reported two different values for the same timestamp."""


class GridSeriesMissingError(BuildError):
    """No usable whole-home counter samples exist for the requested window."""


class ReconciliationError(BuildError):
    """Energy entered the build, reached no interval, and is in no declared term.

    The identity :func:`reconcile_energy` enforces is
    ``raw endpoint difference == Σ allocated + Σ declared_unattributed + residual``.
    A non-trivial ``residual`` means the build dropped kWh silently — the 2025
    archive leaked 8.1229 kWh (0.146 %) this way — so the dataset is refused
    rather than published with an unexplained hole in it.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawPoint:
    """One raw sample as an input to stitching (handy for tests and callers)."""

    ts: datetime
    value: float
    series_key: str

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError(f"RawPoint.ts must be timezone-aware, got naive {self.ts!r}")


@dataclass(frozen=True)
class EraBoundary:
    """Where one stitched timeline changes ``series_key`` (a deployment era)."""

    entity: str
    metric: str
    at: datetime
    """First sample of the new era."""
    previous_at: datetime
    """Last sample of the previous era."""
    from_series_key: str
    to_series_key: str
    gap_seconds: float
    """Timestamp distance between the two era edges (0.0 when adjacent)."""
    delta_kwh: float
    """Counter change across the boundary — informational, never summed twice."""

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "metric": self.metric,
            "era_boundary_at": iso_z(self.at),
            "previous_sample_at": iso_z(self.previous_at),
            "from_series_key": self.from_series_key,
            "to_series_key": self.to_series_key,
            "gap_seconds": self.gap_seconds,
            "delta_at_boundary": self.delta_kwh,
        }


@dataclass(frozen=True)
class StitchedSeries:
    """One logical timeline for an ``(entity, metric)`` across all its series."""

    entity: str
    metric: str
    unit: str
    times: tuple[datetime, ...]
    values: tuple[float, ...]
    series_keys: tuple[str, ...]
    era_boundaries: tuple[EraBoundary, ...]
    median_interval_seconds: float | None
    duplicates_dropped: int

    def __len__(self) -> int:
        return len(self.times)

    def times_us(self) -> tuple[int, ...]:
        """Microseconds since the epoch, precomputed for the hot loops."""
        return tuple(to_micros(t) for t in self.times)

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "metric": self.metric,
            "unit": self.unit,
            "samples": len(self),
            "eras": (len(self.era_boundaries) + 1) if self.times else 0,
            "distinct_series_keys": len(set(self.series_keys)),
            "first_ts": iso_z(self.times[0]) if self.times else None,
            "last_ts": iso_z(self.times[-1]) if self.times else None,
            "median_interval_seconds": self.median_interval_seconds,
            "duplicates_dropped": self.duplicates_dropped,
        }


@dataclass(frozen=True)
class DeltaSpan:
    """The change between two consecutive samples of a stitched timeline."""

    t0: datetime
    t1: datetime
    start_value: float
    end_value: float
    delta: float | None
    """``v1 - v0``; ``None`` when unknowable (a counter reset)."""
    duration_seconds: float
    is_gap: bool
    is_reset: bool
    gap_threshold_seconds: float | None

    @property
    def crosses_boundary(self) -> bool:
        """True when the span is not fully inside a single grid interval."""
        step = to_micros(self.t1) - to_micros(self.t0)
        return step > 0 and _interval_index(self.t0) != _interval_index(self.t1)

    def known_total(self) -> float | None:
        return self.delta

    def as_dict(self) -> dict[str, object]:
        return {
            "span_start": iso_z(self.t0),
            "span_end": iso_z(self.t1),
            "start_value": self.start_value,
            "end_value": self.end_value,
            "known_total": self.delta,
            "duration_seconds": self.duration_seconds,
            "kind": "counter_reset" if self.is_reset else ("gap" if self.is_gap else "normal"),
            "gap_threshold_seconds": self.gap_threshold_seconds,
        }


@dataclass(frozen=True)
class IntervalEnergy:
    """Per-interval outcome of allocating counter deltas onto the grid."""

    raw_grid_kwh: float | None
    energy_quality: str
    allocation_quality: str
    gap_seconds: float
    sample_count: int


def _micro_kwh(value: float) -> int:
    """Counter reading as an exact integer number of micro-kWh.

    Counter values are decimal quantities stored as ``double``, so 0.0033 - 0.0032
    is not exactly 0.0001 in binary floating point.  Every lattice question is
    therefore asked of integers, never of float differences.
    """
    return round(value * 1_000_000)


def _nearest_rank(sorted_values: Sequence[float], quantile: float) -> float | None:
    """Nearest-rank quantile (the element at ``ceil(q * n)``, 1-based).

    A fixed, documented rank rule rather than an interpolated one: the manifest
    number must be reproducible from the sorted sample without a library choice.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    rank = math.ceil(quantile * n)
    return sorted_values[min(max(rank - 1, 0), n - 1)]


def measure_counter_quantisation(
    series: StitchedSeries,
    *,
    candidates: Sequence[float] = LATTICE_CANDIDATES_KWH,
) -> dict[str, object]:
    """Derive the counter's quantum from the loaded samples instead of asserting it.

    Returns the GCD (in kWh) of every **positive** increment of the stitched
    timeline, the sample size behind it, the divisibility share of each candidate
    divisor, and the monotonicity evidence.  A divisor is a lattice only if its
    share is 100 %; anything else is a coincidence that a small sample can make
    look meaningful (that is exactly how the discarded 0.0016 kWh claim arose).

    ``None`` for ``quantisation_kwh`` means there was nothing to measure (no
    positive increment), and the caller falls back to
    :data:`COUNTER_QUANTISATION_KWH`.
    """
    values = series.values
    if len(values) < 2:
        return {
            "quantisation_kwh": None,
            "basis": "no_samples_to_measure",
            "method": "gcd_of_positive_increments_in_micro_kwh",
            "positive_increment_count": 0,
            "step_count": max(len(values) - 1, 0),
            "candidate_divisibility": {},
        }
    micros = [_micro_kwh(v) for v in values]
    increments = [micros[i] - micros[i - 1] for i in range(1, len(micros))]
    positive = [d for d in increments if d > 0]
    gcd_micros = 0
    for d in positive:
        gcd_micros = math.gcd(gcd_micros, d)
    divisibility: dict[str, object] = {}
    for candidate in candidates:
        step_micros = _micro_kwh(candidate)
        if step_micros <= 0:
            continue
        hits = sum(1 for d in positive if d % step_micros == 0)
        exact = sum(1 for d in positive if d == step_micros)
        # On a lattice of ``gcd_micros``, a positive increment is a uniformly placed
        # multiple of it, so a divisor s is hit by chance with probability
        # gcd/lcm(gcd, s): 0.0016 on a 0.0001 lattice is expected to divide 6.25 % of
        # increments even though it is not the quantum at all.
        chance = 100.0 * gcd_micros / math.lcm(gcd_micros, step_micros) if gcd_micros else None
        divisibility[f"{candidate:.6f}"] = {
            "increments_divisible": hits,
            "share_divisible_percent": round(100.0 * hits / len(positive), 4) if positive else None,
            "increments_exactly_equal": exact,
            "chance_share_percent_on_measured_lattice": round(chance, 4)
            if chance is not None
            else None,
        }
    return {
        "quantisation_kwh": gcd_micros / 1_000_000 if gcd_micros else None,
        "basis": "measured_gcd" if gcd_micros else "no_positive_increments",
        "method": "gcd_of_positive_increments_in_micro_kwh",
        "positive_increment_count": len(positive),
        "zero_increment_count": sum(1 for d in increments if d == 0),
        "negative_increment_count": sum(1 for d in increments if d < 0),
        "step_count": len(increments),
        "smallest_positive_increment_kwh": min(positive) / 1_000_000 if positive else None,
        "largest_positive_increment_kwh": max(positive) / 1_000_000 if positive else None,
        "monotonic": not any(d < 0 for d in increments),
        "candidate_divisibility": divisibility,
        "fallback_constant_kwh": COUNTER_QUANTISATION_KWH,
    }


@dataclass(frozen=True)
class PriceAlignment:
    """Per-interval price in effect for the interval's clock hour."""

    value: float | None
    quality: str
    samples: int = 0
    """Raw samples observed inside the clock hour this interval belongs to."""
    distinct_values: int = 0
    """Distinct price values observed inside the hour (0 when no sample)."""
    hour_start: datetime | None = None
    """The UTC instant at which the hour bucket began (``None`` when absent)."""


@dataclass(frozen=True)
class EnergyReconciliation:
    """Closed energy accounting for one build: every kWh has a named home.

    The identity :func:`reconcile_energy` enforces is::

        counter_endpoint_kwh
            == allocated_kwh + declared_unattributed_kwh + residual_kwh

    where every term on the right is a *declared* category and ``residual`` is
    whatever nothing explains.  A residual above the tolerance is a silent leak:
    the 2025 archive lost 8.1229 kWh (0.146 % of the year) exactly this way,
    because energy already allocated inside an interval was discarded when a gap
    span touching that interval made the interval unusable, and no term carried it.
    """

    counter_endpoint_kwh: float
    allocated_kwh: float
    nulled_covered_kwh: float
    gap_span_kwh: float
    outside_grid_kwh: float
    partial_coverage_kwh: float
    reset_counter_change_kwh: float
    residual_kwh: float
    tolerance_kwh: float

    @property
    def declared_unattributed_kwh(self) -> float:
        """Energy that entered the build and reached no interval, all named."""
        return (
            self.nulled_covered_kwh
            + self.gap_span_kwh
            + self.outside_grid_kwh
            + self.partial_coverage_kwh
            + self.reset_counter_change_kwh
        )

    @property
    def within_tolerance(self) -> bool:
        """True when nothing remains unexplained beyond the documented tolerance."""
        return abs(self.residual_kwh) <= self.tolerance_kwh

    @property
    def quality_flag(self) -> str:
        """Machine-readable verdict; anything but ``ok`` means the build lied."""
        return "ok" if self.within_tolerance else "residual_out_of_tolerance"

    def as_dict(self) -> dict[str, object]:
        percent = (
            100.0 * abs(self.residual_kwh) / self.counter_endpoint_kwh
            if self.counter_endpoint_kwh
            else 0.0
        )
        return {
            "identity": (
                "counter_endpoint_kwh == allocated_kwh + nulled_covered_kwh + gap_span_kwh "
                "+ outside_grid_kwh + partial_coverage_kwh + reset_counter_change_kwh "
                "+ residual_kwh"
            ),
            "counter_endpoint_kwh": round(self.counter_endpoint_kwh, 9),
            "allocated_kwh": round(self.allocated_kwh, 9),
            "declared_unattributed_kwh": round(self.declared_unattributed_kwh, 9),
            "terms": {
                "nulled_covered_kwh": round(self.nulled_covered_kwh, 9),
                "gap_span_kwh": round(self.gap_span_kwh, 9),
                "outside_grid_kwh": round(self.outside_grid_kwh, 9),
                "partial_coverage_kwh": round(self.partial_coverage_kwh, 9),
                "reset_counter_change_kwh": round(self.reset_counter_change_kwh, 9),
            },
            "residual_kwh": round(self.residual_kwh, 9),
            "residual_percent_of_endpoint": round(percent, 9),
            "residual_tolerance_kwh": self.tolerance_kwh,
            "within_tolerance": self.within_tolerance,
            "quality_flag": self.quality_flag,
            "term_meanings": {
                "counter_endpoint_kwh": "last minus first reading of the loaded stitched counter",
                "allocated_kwh": "sum of the raw_grid_kwh values written onto intervals",
                "nulled_covered_kwh": (
                    "energy attributed from ordinary spans inside an interval that was then "
                    "nulled because a gap or reset span also touched it"
                ),
                "gap_span_kwh": (
                    "known totals of gap spans crossing interval boundaries, never distributed"
                ),
                "outside_grid_kwh": (
                    "spans lying wholly outside the requested grid (look-back and tail)"
                ),
                "partial_coverage_kwh": (
                    "span pieces the grid edge cut off, not placeable on any interval"
                ),
                "reset_counter_change_kwh": (
                    "signed counter change across reset spans (negative); the energy is "
                    "unknowable, so this is the counter's step, not consumption"
                ),
                "residual_kwh": (
                    "what nothing explains; must be within tolerance or the build is refused"
                ),
            },
        }


def reconcile_energy(
    *,
    counter_endpoint_kwh: float,
    allocated_kwh: float,
    nulled_covered_kwh: float = 0.0,
    gap_span_kwh: float = 0.0,
    outside_grid_kwh: float = 0.0,
    partial_coverage_kwh: float = 0.0,
    reset_counter_change_kwh: float = 0.0,
    tolerance_kwh: float = DEFAULT_RECONCILIATION_TOLERANCE_KWH,
) -> EnergyReconciliation:
    """Close the energy identity and expose the unexplained remainder.

    Pure function of the declared terms, so a build that fails to declare a
    category shows up as a residual instead of disappearing: passing the same
    numbers *without* the ``nulled_covered_kwh`` term leaves exactly the amount
    that term carried.  Raises nothing — callers decide whether a residual is
    fatal (see :func:`require_reconciled`).
    """
    declared = (
        nulled_covered_kwh
        + gap_span_kwh
        + outside_grid_kwh
        + partial_coverage_kwh
        + reset_counter_change_kwh
    )
    residual = counter_endpoint_kwh - (allocated_kwh + declared)
    return EnergyReconciliation(
        counter_endpoint_kwh=counter_endpoint_kwh,
        allocated_kwh=allocated_kwh,
        nulled_covered_kwh=nulled_covered_kwh,
        gap_span_kwh=gap_span_kwh,
        outside_grid_kwh=outside_grid_kwh,
        partial_coverage_kwh=partial_coverage_kwh,
        reset_counter_change_kwh=reset_counter_change_kwh,
        residual_kwh=residual,
        tolerance_kwh=tolerance_kwh,
    )


def require_reconciled(recon: EnergyReconciliation) -> None:
    """Refuse a build whose energy does not close.

    Raises:
        ReconciliationError: ``|residual|`` exceeds the tolerance.
    """
    if recon.within_tolerance:
        return
    raise ReconciliationError(
        f"energy reconciliation failed: counter endpoint {recon.counter_endpoint_kwh:.4f} kWh "
        f"!= allocated {recon.allocated_kwh:.4f} kWh + declared unattributed "
        f"{recon.declared_unattributed_kwh:.4f} kWh; unexplained residual "
        f"{recon.residual_kwh:.6f} kWh exceeds the {recon.tolerance_kwh} kWh tolerance. "
        "kWh entered this build, reached no interval, and is in no declared term — "
        "refusing to publish a dataset with an undeclared hole in it."
    )


@dataclass
class SpanLedger:
    """Totals that could not be attributed to a single interval (manifest).

    Every field is either an *allocated* quantity (energy that reached an
    interval) or a *declared unattributed* one (energy that entered the build and
    deliberately did not), so :func:`reconcile_energy` can close the identity
    against the raw counter endpoint instead of leaving kWh unexplained.
    """

    gap_span_count: int = 0
    gap_span_total_kwh: float = 0.0
    reset_span_count: int = 0
    boundary_span_count: int = 0
    boundary_span_total_kwh: float = 0.0
    boundary_piece_count: int = 0
    estimated_interval_count: int = 0
    unattributed_interval_count: int = 0
    # --- reconciliation terms (all kWh) ---
    counter_endpoint_kwh: float = 0.0
    allocated_kwh: float = 0.0
    nulled_covered_kwh: float = 0.0
    nulled_covered_interval_count: int = 0
    outside_grid_kwh: float = 0.0
    outside_grid_span_count: int = 0
    partial_coverage_kwh: float = 0.0
    partial_coverage_span_count: int = 0
    degenerate_span_count: int = 0
    reset_counter_change_kwh: float = 0.0
    details: list[dict[str, object]] = field(default_factory=list)

    def record(
        self,
        span: DeltaSpan,
        *,
        category: str,
        capped: bool,
        pieces: int = 0,
    ) -> None:
        if category == "gap":
            self.gap_span_count += 1
            self.gap_span_total_kwh += span.delta or 0.0
        elif category == "reset":
            self.reset_span_count += 1
        else:
            self.boundary_span_count += 1
            self.boundary_span_total_kwh += span.delta or 0.0
            self.boundary_piece_count += pieces
        if not capped and len(self.details) < MAX_MANIFEST_DETAILS:
            entry = span.as_dict()
            entry["category"] = category
            entry["pieces"] = pieces
            self.details.append(entry)

    def reconciliation(
        self, *, tolerance_kwh: float = DEFAULT_RECONCILIATION_TOLERANCE_KWH
    ) -> EnergyReconciliation:
        """Close the identity over the terms this ledger accumulated."""
        return reconcile_energy(
            counter_endpoint_kwh=self.counter_endpoint_kwh,
            allocated_kwh=self.allocated_kwh,
            nulled_covered_kwh=self.nulled_covered_kwh,
            gap_span_kwh=self.gap_span_total_kwh,
            outside_grid_kwh=self.outside_grid_kwh,
            partial_coverage_kwh=self.partial_coverage_kwh,
            reset_counter_change_kwh=self.reset_counter_change_kwh,
            tolerance_kwh=tolerance_kwh,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "gap_span_count": self.gap_span_count,
            "gap_span_total_kwh": round(self.gap_span_total_kwh, 9),
            "reset_span_count": self.reset_span_count,
            "boundary_span_count": self.boundary_span_count,
            "boundary_span_total_kwh": round(self.boundary_span_total_kwh, 9),
            "boundary_piece_count": self.boundary_piece_count,
            "estimated_interval_count": self.estimated_interval_count,
            "unattributed_interval_count": self.unattributed_interval_count,
            "nulled_covered_interval_count": self.nulled_covered_interval_count,
            "outside_grid_span_count": self.outside_grid_span_count,
            "partial_coverage_span_count": self.partial_coverage_span_count,
            "details_truncated": len(self.details) >= MAX_MANIFEST_DETAILS,
            "details": self.details,
        }


TRANSITION_OFFSET_BUCKETS_SECONDS: tuple[int, ...] = (5, 30, 60, 120, 300, 1800)
"""Upper edges of the published look-ahead buckets; the last bucket is open-ended.

Documented so the histogram is reproducible: a transition is placed in the first
bucket whose edge is ``>= offset`` and otherwise in ``over_1800``.
"""


def _offset_bucket(offset: float) -> str:
    """Label for :data:`TRANSITION_OFFSET_BUCKETS_SECONDS` containing *offset*."""
    previous = 0
    for edge in TRANSITION_OFFSET_BUCKETS_SECONDS:
        if offset <= edge:
            return f"{previous + 1}_{edge}_seconds"
        previous = edge
    return f"over_{TRANSITION_OFFSET_BUCKETS_SECONDS[-1]}_seconds"


@dataclass
class PriceLedger:
    """Per-hour price statistics for the build manifest.

    The maxima and distributions are accumulated over **every** hour of the
    window as it is recorded, never over :attr:`details`, which is a truncated
    chronological sample.  Deriving a maximum from the sample was the defect this
    class now guards against: 500 sample rows from the first days of the archive
    all carry one scrape phase, and a reader who mistook them for the year would
    conclude the price always arrives at ``:39`` — which the full archive
    contradicts (median 25 s, maximum 2,708 s over 2025).
    """

    hour_timezone: str | None = None
    """The tariff timezone the clock-hour bucketing uses (set by the caller)."""

    hours_total: int = 0
    measured_hours: int = 0
    ambiguous_hours: int = 0
    forward_filled_hours: int = 0
    missing_hours: int = 0
    disagreement_count: int = 0
    intervals_ambiguous: int = 0
    max_carried_age_seconds: float = 0.0
    max_lookahead_seconds: float = 0.0
    # --- measured over all hours, independent of the capped details sample ---
    first_offsets: list[float] = field(default_factory=list)
    """Seconds between an hour's start and its first observed sample (all hours)."""

    distinct_value_histogram: dict[int, int] = field(default_factory=dict)
    """Number of distinct price values observed per hour -> hours (all hours)."""

    transition_buckets: dict[str, int] = field(default_factory=dict)
    """Hour-to-hour price changes per offset-from-hour-top bucket."""

    transitions_total: int = 0
    carry_previous_price_hours: int = 0
    carry_change_buckets: dict[str, int] = field(default_factory=dict)
    observed_label_keys: list[str] = field(default_factory=list)
    """Label keys actually observed on the price series (empty = not measured)."""

    details: list[dict[str, object]] = field(default_factory=list)

    def record_hour_population(self, *, distinct: int, first_offset_seconds: float | None) -> None:
        """Accumulate one hour **of the price series**, whether or not the grid covers it.

        Kept separate from :func:`record_hour` (which only sees hours that contain
        at least one grid interval) so the published look-ahead distribution is the
        population of hours with samples, not a window- or cap-limited subset.
        """
        if first_offset_seconds is not None:
            self.first_offsets.append(first_offset_seconds)
        self.distinct_value_histogram[distinct] = self.distinct_value_histogram.get(distinct, 0) + 1

    def record_hour(
        self,
        *,
        hour_start: datetime,
        quality: str,
        samples: int,
        distinct: int,
        distinct_values: tuple[float, ...] = (),
        carried_age_seconds: float | None = None,
        lookahead_seconds: float | None = None,
    ) -> None:
        self.hours_total += 1
        if quality == PRICE_MEASURED:
            self.measured_hours += 1
        elif quality == PRICE_AMBIGUOUS:
            self.ambiguous_hours += 1
            self.disagreement_count += 1
        elif quality == PRICE_FORWARD_FILLED:
            self.forward_filled_hours += 1
        else:
            self.missing_hours += 1
        if carried_age_seconds is not None and carried_age_seconds > self.max_carried_age_seconds:
            self.max_carried_age_seconds = carried_age_seconds
        if lookahead_seconds is not None and lookahead_seconds > self.max_lookahead_seconds:
            self.max_lookahead_seconds = lookahead_seconds
        if len(self.details) < MAX_MANIFEST_DETAILS:
            entry: dict[str, object] = {
                "hour_start": iso_z(hour_start),
                "quality": quality,
                "samples": samples,
                "distinct_values": distinct,
            }
            if distinct > 1 and distinct_values:
                entry["values"] = list(distinct_values)
            if carried_age_seconds is not None:
                entry["carried_age_seconds"] = carried_age_seconds
            if lookahead_seconds is not None:
                entry["lookahead_seconds"] = lookahead_seconds
            self.details.append(entry)

    def record_transition(
        self,
        *,
        offset_seconds: float,
        carried_previous_price: bool,
    ) -> None:
        """One hour-to-hour price change, measured from the start of the new hour.

        ``carried_previous_price`` marks the case where the hour's first sample
        still reported the *old* price: the assumption that hour H's price holds
        from ``H:00:00`` is then wrong for the first ``offset_seconds`` of it.
        """
        self.transitions_total += 1
        key = _offset_bucket(offset_seconds)
        self.transition_buckets[key] = self.transition_buckets.get(key, 0) + 1
        if carried_previous_price:
            self.carry_previous_price_hours += 1
            self.carry_change_buckets[key] = self.carry_change_buckets.get(key, 0) + 1

    def as_dict(self) -> dict[str, object]:
        offsets = sorted(self.first_offsets)
        n = len(offsets)

        def rank(quantile: float) -> float | None:
            value = _nearest_rank(offsets, quantile)
            return None if value is None else round(value, 3)

        percent = {
            "hours_with_a_sample": n,
            "method": (
                "every hour of the loaded price series that carries at least one sample, "
                "independent of the capped details list; nearest-rank percentiles"
            ),
            "min_seconds": round(offsets[0], 3) if offsets else None,
            "p05_seconds": rank(0.05),
            "p25_seconds": rank(0.25),
            "median_seconds": rank(0.50),
            "p75_seconds": rank(0.75),
            "p95_seconds": rank(0.95),
            "max_seconds": round(offsets[-1], 3) if offsets else None,
            "distinct_whole_second_values": len({int(o) for o in offsets}) if offsets else 0,
            "first_observed_within_own_minute_percent": (
                round(100.0 * sum(1 for o in offsets if o < 60.0) / n, 4) if n else None
            ),
            "hours_first_observed_after_60_seconds": sum(1 for o in offsets if o >= 60.0)
            if n
            else 0,
        }
        hours_with_samples = sum(self.distinct_value_histogram.values())
        single = self.distinct_value_histogram.get(1, 0)
        disagreeing = {str(k): v for k, v in sorted(self.distinct_value_histogram.items()) if k > 1}
        transitions = {
            "price_changing_hours": self.transitions_total,
            "offset_from_hour_top_buckets": dict(sorted(self.transition_buckets.items())),
            "changed_within_60_seconds_percent": (
                round(
                    100.0
                    * sum(
                        v for k, v in self.transition_buckets.items() if not k.startswith("over_")
                    )
                    / self.transitions_total,
                    4,
                )
                if self.transitions_total
                else None
            ),
            "hours_whose_first_sample_carried_the_previous_price": self.carry_previous_price_hours,
            "carried_previous_price_change_buckets": dict(
                sorted(self.carry_change_buckets.items())
            ),
            "carried_previous_price_percent_of_hours": (
                round(100.0 * self.carry_previous_price_hours / hours_with_samples, 4)
                if hours_with_samples
                else None
            ),
        }
        return {
            "rule": "clock_hour_aligned",
            "hour_timezone": self.hour_timezone,
            "lookahead_note": PRICE_LOOKAHEAD_NOTE,
            "publication_provenance": PRICE_PUBLICATION_PROVENANCE,
            "observed_label_keys": list(self.observed_label_keys),
            "hours_total": self.hours_total,
            "measured_hours": self.measured_hours,
            "ambiguous_hours": self.ambiguous_hours,
            "forward_filled_hours": self.forward_filled_hours,
            "missing_hours": self.missing_hours,
            "disagreement_count": self.disagreement_count,
            "intervals_ambiguous": self.intervals_ambiguous,
            "max_carried_age_seconds": self.max_carried_age_seconds,
            "max_lookahead_seconds": self.max_lookahead_seconds,
            "max_lookahead_basis": (
                "measured over every hour of the window as it was recorded; NOT derived "
                "from the capped details sample below"
            ),
            "first_observation_offset_seconds": percent,
            "hour_value_agreement": {
                "hours_with_samples": hours_with_samples,
                "single_valued_hours": single,
                "single_valued_percent": (
                    round(100.0 * single / hours_with_samples, 4) if hours_with_samples else None
                ),
                "multi_valued_hours_by_count": disagreeing,
                "max_distinct_values_observed_in_one_hour": (
                    max(self.distinct_value_histogram) if self.distinct_value_histogram else 0
                ),
            },
            "transitions": transitions,
            "details_truncated": len(self.details) >= MAX_MANIFEST_DETAILS,
            "details_are_sample_not_population": True,
            "details_note": (
                "First rows of the window in chronological order, capped at "
                f"{MAX_MANIFEST_DETAILS} hours. A sample for inspection only: every "
                "maximum, percentile and distribution above is accumulated over all "
                f"{self.hours_total} hours and must not be inferred from these rows."
            ),
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Series stitching
# ---------------------------------------------------------------------------

_INTERVAL_STEP_US = INTERVAL_SECONDS * 1_000_000


def _interval_index(dt: datetime) -> int:
    """Index of the UTC grid interval containing *dt* (floor-aligned epoch)."""
    us = to_micros(dt)
    return us // _INTERVAL_STEP_US


def stitch_series(
    *,
    entity: str,
    metric: str,
    unit: str,
    points: Iterable[RawPoint],
) -> StitchedSeries:
    """Concatenate every series of one ``(entity, metric)`` in timestamp order.

    The result is a single logical timeline: era boundaries are recorded where
    ``series_key`` changes, identical re-extracted samples are dropped, and
    genuine overlaps or value conflicts raise rather than being averaged.

    Raises:
        OverlappingSeriesError: two different ``series_key`` share a timestamp.
        ConflictingSampleError: one ``series_key`` has two values at a timestamp.
        ValueError: a timestamp is naive or non-finite.
    """
    ordered = sorted(points, key=lambda p: (to_micros(p.ts), p.series_key))

    times: list[datetime] = []
    values: list[float] = []
    keys: list[str] = []
    boundaries: list[EraBoundary] = []
    duplicates = 0
    # Sorted by (timestamp, series_key), so every sample sharing one instant is
    # adjacent: overlap and re-extraction are decided against the previous row
    # alone.  Checking every previously seen timestamp instead would make
    # stitching quadratic, and the live archive is ~1.5M samples per counter.
    prev_us: int | None = None
    prev_key = ""
    prev_value = 0.0

    for point in ordered:
        if not math.isfinite(point.value):
            raise BuildError(
                f"non-finite sample value {point.value!r} for entity={entity!r} "
                f"metric={metric!r} series_key={point.series_key!r} at {iso_z(point.ts)}"
            )
        us = to_micros(point.ts)
        if prev_us is not None and us == prev_us:
            if point.series_key == prev_key:
                if point.value != prev_value:
                    raise ConflictingSampleError(
                        f"conflicting samples for entity={entity!r} metric={metric!r} "
                        f"series_key={point.series_key!r} at {iso_z(point.ts)}: "
                        f"{prev_value!r} vs {point.value!r} — one series cannot report "
                        "two values for one instant"
                    )
                duplicates += 1  # same sample re-extracted by an overlapping window
                continue
            raise OverlappingSeriesError(
                f"overlapping series for entity={entity!r} metric={metric!r} at "
                f"{iso_z(point.ts)}: series_key {prev_key!r} and {point.series_key!r} "
                "both report a sample at the same instant. Stitching must concatenate "
                "non-overlapping eras; summing or averaging them would invent energy, "
                "so the build refuses. Investigate the scrape configuration."
            )

        if keys and keys[-1] != point.series_key:
            boundaries.append(
                EraBoundary(
                    entity=entity,
                    metric=metric,
                    at=point.ts,
                    previous_at=times[-1],
                    from_series_key=keys[-1],
                    to_series_key=point.series_key,
                    gap_seconds=(us - to_micros(times[-1])) / 1_000_000,
                    delta_kwh=point.value - values[-1],
                )
            )
        times.append(point.ts)
        values.append(point.value)
        keys.append(point.series_key)
        prev_us, prev_key, prev_value = us, point.series_key, point.value

    deltas = [
        (to_micros(times[i]) - to_micros(times[i - 1])) / 1_000_000 for i in range(1, len(times))
    ]
    median = statistics.median(deltas) if deltas else None

    return StitchedSeries(
        entity=entity,
        metric=metric,
        unit=unit,
        times=tuple(times),
        values=tuple(values),
        series_keys=tuple(keys),
        era_boundaries=tuple(boundaries),
        median_interval_seconds=median,
        duplicates_dropped=duplicates,
    )


# ---------------------------------------------------------------------------
# Counter deltas
# ---------------------------------------------------------------------------


def counter_deltas(
    series: StitchedSeries,
    *,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    gap_threshold_seconds: float | None = None,
) -> list[DeltaSpan]:
    """Turn a stitched timeline into consecutive-pair deltas over ``[t0, t1)``.

    ``gap_threshold_seconds`` defaults to ``gap_factor`` times the **measured**
    median sample interval, so the threshold follows the archive's real cadence
    instead of a hardcoded number.  A negative delta is a counter reset: the
    span is flagged, carries no value, and the timeline continues from the new
    (lower) reading — the reset is bridged, never integrated and never negative.

    Raises:
        BuildError: ``gap_factor`` is not positive, or a threshold resolves to a
            non-positive duration.
    """
    if gap_factor <= 0:
        raise BuildError(f"gap_factor must be > 0, got {gap_factor!r}")
    threshold = gap_threshold_seconds
    if threshold is None:
        threshold = (
            gap_factor * series.median_interval_seconds if series.median_interval_seconds else None
        )
    if threshold is not None and threshold <= 0:
        raise BuildError(f"gap threshold must be > 0, got {threshold!r}")

    spans: list[DeltaSpan] = []
    for i in range(1, len(series.times)):
        t0, t1 = series.times[i - 1], series.times[i]
        v0, v1 = series.values[i - 1], series.values[i]
        duration = (to_micros(t1) - to_micros(t0)) / 1_000_000
        is_reset = v1 < v0
        is_gap = threshold is not None and duration > threshold
        spans.append(
            DeltaSpan(
                t0=t0,
                t1=t1,
                start_value=v0,
                end_value=v1,
                # A reset's energy is unknowable: the counter lost its reference,
                # so `v1 - v0` is meaningless (and negative).  Bridge, do not invent.
                delta=None if is_reset else v1 - v0,
                duration_seconds=duration,
                is_gap=is_gap,
                is_reset=is_reset,
                gap_threshold_seconds=threshold,
            )
        )
    return spans


# ---------------------------------------------------------------------------
# UTC grid, local labels, tariff periods
# ---------------------------------------------------------------------------


def make_utc_grid(
    start: datetime,
    end: datetime,
    *,
    interval_seconds: int = INTERVAL_SECONDS,
) -> list[datetime]:
    """Return the UTC interval starts covering ``[start, end)`` in 15-min steps.

    The grid is generated in **UTC** and floor-aligned to the interval, so the
    local-day interval counts come from reality rather than from arithmetic on
    local wall-clock times: a 23-hour Spanish day yields 92 intervals and a
    25-hour day yields 100.  A ``start`` that is not interval-aligned floors down
    to the containing interval, so requested windows never drop a partial
    interval (Spain's UTC offsets are whole hours, so local midnight is always
    on the aligned grid).

    Raises:
        ValueError: a bound is naive, ``interval_seconds`` is not positive, or
            ``start`` is not before ``end``.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be > 0, got {interval_seconds!r}")
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware (UTC); got naive {value!r}")

    step_us = interval_seconds * 1_000_000
    start_us = to_micros(start.astimezone(UTC))
    end_us = to_micros(end.astimezone(UTC))
    if start_us >= end_us:
        raise ValueError(f"start must be before end: [{iso_z(start)}, {iso_z(end)})")

    aligned_us = start_us - (start_us % step_us)
    out: list[datetime] = []
    current = aligned_us
    while current < end_us:
        out.append(from_micros(current))
        current += step_us
    return out


def local_day_length_seconds(local_day: date, tz: ZoneInfo) -> float:
    """Measured length of a local calendar day in *tz* (3600-second precision).

    The subtraction happens in **UTC on purpose**: for two aware datetimes that
    share the same ``tzinfo`` object, CPython ignores the offsets and returns the
    wall-clock difference, which would report 86400 s for every day and make DST
    invisible.
    """
    following = local_day + timedelta(days=1)
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz).astimezone(UTC)
    end = datetime(following.year, following.month, following.day, tzinfo=tz).astimezone(UTC)
    return (end - start).total_seconds()


def dst_transition_for(local_day: date, tz: ZoneInfo) -> str | None:
    """``spring_forward``/``fall_back`` from the **computed** local-day length.

    Derived from :mod:`zoneinfo` arithmetic, never from a hardcoded date table,
    so a future rule change in the tz database is picked up automatically.
    """
    length = local_day_length_seconds(local_day, tz)
    if length < 24 * 3600:
        return DST_SPRING_FORWARD
    if length > 24 * 3600:
        return DST_FALL_BACK
    return None


def assign_tariff_period(periods: TariffPeriods, local_dt: datetime) -> int:
    """Return the configured tariff period (1/2/3) for a **local** timestamp.

    *local_dt* must already be converted to ``periods.timezone``; the lookup
    takes a wall-clock date plus hour so a UTC hour can never be mistaken for a
    local one.

    Raises:
        ValueError: rules are not configured for the applicable day type, or the
            hour is not covered.
    """
    return tariff_period_for(periods, local_dt.date(), local_dt.hour)


# ---------------------------------------------------------------------------
# Counter deltas onto the grid
# ---------------------------------------------------------------------------


def allocate_energy(
    series: StitchedSeries,
    spans: Sequence[DeltaSpan],
    interval_starts: Sequence[datetime],
    *,
    interval_seconds: int = INTERVAL_SECONDS,
) -> tuple[list[IntervalEnergy], SpanLedger]:
    """Assign counter deltas to UTC grid intervals without inventing anything.

    See the module docstring for the attribution cases.  Normal-cadence spans
    that cross a 15-minute boundary are split proportionally to time at each
    boundary; the pieces re-sum to the original delta (telescoping preserved
    exactly when the grid covers the whole span), so totals stay counter
    differences while ``allocation_quality="estimated_boundary"`` discloses the
    estimated placement.  Returns per-interval energy plus a
    :class:`SpanLedger` preserving the count and total of every span that needed
    a disclosed approximation (a boundary split) or that could not be attributed
    at all (a gap or a reset).

    Raises:
        BuildError: ``interval_seconds`` is not positive.
    """
    if interval_seconds <= 0:
        raise BuildError(f"interval_seconds must be > 0, got {interval_seconds!r}")
    step_us = interval_seconds * 1_000_000
    times_us = series.times_us()
    starts_us = [to_micros(t) for t in interval_starts]
    count = len(starts_us)

    attributed = [0.0] * count
    sample_count = [0] * count
    touched = [False] * count
    split_seen = [False] * count
    reset_seen = [False] * count
    gap_seen = [False] * count
    unknowable = [False] * count
    gap_seconds = [0.0] * count

    ledger = SpanLedger()
    # Anchoring the closed identity: every span delta of the loaded timeline sums to
    # this difference (telescoping), so the terms below must account for it exactly.
    if len(series.values) >= 2:
        ledger.counter_endpoint_kwh = series.values[-1] - series.values[0]
    recorded: set[int] = set()

    def disclose(index: int, span: DeltaSpan, category: str, *, pieces: int = 0) -> None:
        """Record a span in the ledger once, no matter how many intervals it touches."""
        if index in recorded:
            return
        recorded.add(index)
        ledger.record(span, category=category, capped=category != "reset", pieces=pieces)

    span_t0 = [to_micros(s.t0) for s in spans]
    span_t1 = [to_micros(s.t1) for s in spans]

    for k in range(count):
        i0_us = starts_us[k]
        sample_count[k] = bisect_left(times_us, i0_us + step_us) - bisect_left(times_us, i0_us)

    for index, span in enumerate(spans):
        t0_us = span_t0[index]
        t1_us = span_t1[index]
        if t1_us <= t0_us:
            # Degenerate span (two readings at one instant): nothing to distribute
            # and nothing to claim, so it must not mark an interval covered.
            ledger.degenerate_span_count += 1
            continue
        # Intervals overlapping the half-open span [t0, t1): the first candidate
        # may start before the span does, so back up one interval.
        k = max(bisect_left(starts_us, t0_us) - 1, 0)
        hits: list[tuple[int, int]] = []
        while k < count and starts_us[k] < t1_us:
            i0_us = starts_us[k]
            i1_us = i0_us + step_us
            k += 1
            if i1_us <= t0_us:
                continue
            overlap = min(t1_us, i1_us) - max(t0_us, i0_us)
            if overlap > 0:
                touched[k - 1] = True
                hits.append((k - 1, overlap))
        if not hits:
            # Wholly outside the grid (look-back before the first interval, or after
            # the last one): declared, never dropped, or the identity would not close.
            if span.is_reset:
                ledger.reset_counter_change_kwh += span.end_value - span.start_value
            else:
                ledger.outside_grid_kwh += span.delta or 0.0
                ledger.outside_grid_span_count += 1
            continue

        if span.is_reset:
            for slot, _overlap in hits:
                reset_seen[slot] = True
                unknowable[slot] = True
            ledger.reset_counter_change_kwh += span.end_value - span.start_value
            disclose(index, span, "reset")
            continue

        if span.is_gap:
            first_slot = hits[0][0]
            contained = t0_us >= starts_us[first_slot] and t1_us <= starts_us[first_slot] + step_us
            if len(hits) == 1 and contained:
                # The whole known total sits inside one interval: there is nothing
                # to distribute, so the energy is attributed even though the
                # cadence inside it was missing.
                attributed[first_slot] += span.delta or 0.0
                gap_seen[first_slot] = True
                gap_seconds[first_slot] = max(gap_seconds[first_slot], span.duration_seconds)
            else:
                for slot, _overlap in hits:
                    gap_seen[slot] = True
                    unknowable[slot] = True
                    gap_seconds[slot] = max(gap_seconds[slot], span.duration_seconds)
                disclose(index, span, "gap")
            continue

        # Normal cadence: split proportionally at each crossed boundary.
        delta = span.delta or 0.0
        first_slot = hits[0][0]
        contained = t0_us >= starts_us[first_slot] and t1_us <= starts_us[first_slot] + step_us
        if len(hits) == 1 and contained:
            attributed[first_slot] += delta
            continue

        duration_us = t1_us - t0_us
        covered = sum(overlap for _slot, overlap in hits)
        full = covered == duration_us  # pieces re-sum to delta exactly
        total = 0.0
        for position, (slot, overlap) in enumerate(hits):
            if position == len(hits) - 1 and full:
                piece = delta - total
            else:
                piece = delta * overlap / duration_us
            if piece < 0.0:  # float guard: delta >= 0 here (reset handled above)
                piece = 0.0
            attributed[slot] += piece
            total += piece
            split_seen[slot] = True
        if not full:
            # The grid edge clipped this span: the uncovered remainder reaches no
            # interval, so it is declared rather than vanishing.
            ledger.partial_coverage_span_count += 1
            ledger.partial_coverage_kwh += delta * (duration_us - covered) / duration_us
        disclose(index, span, "boundary", pieces=len(hits))

    out: list[IntervalEnergy] = []
    for k in range(count):
        if not touched[k]:
            # No sample span overlaps this interval: it is outside the timeline's
            # coverage (before the first or after the last reading).  A lone
            # sample here says nothing about consumption, so no zero.
            energy = ENERGY_GAP_TOTAL_UNKNOWN
            value: float | None = None
            allocation = ALLOCATION_UNKNOWN_GAP
            gs = float(interval_seconds)
        elif reset_seen[k]:
            energy = ENERGY_COUNTER_RESET_BRIDGED
            value = None
            allocation = ALLOCATION_UNKNOWN_GAP
            gs = gap_seconds[k]
        elif gap_seen[k] and unknowable[k]:
            energy = ENERGY_GAP_TOTAL_UNKNOWN if not sample_count[k] else ENERGY_GAP_PARTIAL
            value = None
            allocation = ALLOCATION_UNKNOWN_GAP
            gs = gap_seconds[k]
        elif gap_seen[k]:
            energy = ENERGY_GAP_PARTIAL
            value = attributed[k]
            allocation = ALLOCATION_ESTIMATED_BOUNDARY if split_seen[k] else ALLOCATION_EXACT
            gs = gap_seconds[k]
        elif split_seen[k]:
            energy = ENERGY_MEASURED
            value = attributed[k]
            allocation = ALLOCATION_ESTIMATED_BOUNDARY
            gs = gap_seconds[k]
        else:
            energy = ENERGY_MEASURED
            value = attributed[k]
            allocation = ALLOCATION_EXACT
            gs = gap_seconds[k]

        if value is None:
            ledger.unattributed_interval_count += 1
            # THE LEAK THIS BLOCK CLOSES: pieces of ordinary spans were placed here
            # and are then thrown away because a gap or reset span made the whole
            # interval unusable.  Discarding them is correct (an interval whose
            # interior placement is unknowable must not publish a number), but
            # discarding them *silently* is what made 8.12 kWh of 2025 energy
            # disappear without appearing in any manifest term.
            if attributed[k]:
                ledger.nulled_covered_kwh += attributed[k]
                ledger.nulled_covered_interval_count += 1
        elif allocation == ALLOCATION_ESTIMATED_BOUNDARY:
            ledger.allocated_kwh += value
            ledger.estimated_interval_count += 1
        else:
            ledger.allocated_kwh += value
        out.append(
            IntervalEnergy(
                raw_grid_kwh=value,
                energy_quality=energy,
                allocation_quality=allocation,
                gap_seconds=gs,
                sample_count=sample_count[k],
            )
        )
    return out, ledger


# ---------------------------------------------------------------------------
# Price alignment
# ---------------------------------------------------------------------------


def _clock_hour_start(value: datetime, tz: ZoneInfo) -> datetime:
    """The instant at which *value*'s clock hour began on *tz*'s wall clock.

    The bucket is keyed by the **instant** (not the wall-clock label), so the
    two occurrences of a fall-back hour stay two distinct buckets and the
    skipped spring-forward hour simply never appears.  ``astimezone`` resolves
    the fold for ambiguous times; ``replace`` preserves that fold, so the
    offset at the floored instant is the one actually in force.
    """
    local = value.astimezone(tz)
    return local.replace(minute=0, second=0, microsecond=0)


def align_price_hours(
    price: StitchedSeries | None,
    interval_starts: Sequence[datetime],
    *,
    timezone: ZoneInfo,
    staleness_seconds: float = DEFAULT_PRICE_STALENESS_SECONDS,
) -> tuple[list[PriceAlignment], PriceLedger]:
    """Assign each interval the price of the clock hour it belongs to.

    The tariff timezone is the wall clock on which PVPC's hourly price is
    defined; bucketing by UTC hour would shift the price by one or two hours
    and break at DST transitions.  Within each hour, all observed samples must
    agree — if they do not, every interval of that hour gets
    ``price_quality="ambiguous"`` with a null price, and the disagreement is
    counted once per hour in the returned :class:`PriceLedger`.  An hour with no
    sample of its own carries the last known value as ``forward_filled`` while
    that value is within ``staleness_seconds``, otherwise ``missing``.  Negative
    prices pass through untouched — they are real and are the best charging
    opportunities.

    Raises:
        BuildError: ``staleness_seconds`` is negative.
    """
    if staleness_seconds < 0:
        raise BuildError(f"staleness_seconds must be >= 0, got {staleness_seconds!r}")
    ledger = PriceLedger()

    if price is None or not price.times:
        alignments = [PriceAlignment(None, PRICE_MISSING) for _ in interval_starts]
        for hour_dt in dict.fromkeys(_clock_hour_start(i0, timezone) for i0 in interval_starts):
            ledger.record_hour(hour_start=hour_dt, quality=PRICE_MISSING, samples=0, distinct=0)
        return alignments, ledger

    times_us = price.times_us()
    # Precompute the hour bucket for every sample.  Bucketing is monotone
    # non-decreasing because flooring to an hour start never reorders instants.
    sample_hour_us = [to_micros(_clock_hour_start(t, timezone)) for t in price.times]
    # Per hour: (sample count, all samples agree, representative value, distinct
    # values, first-sample value, offset of the first sample, offset of the first
    # value change).  The last three feed the year-wide look-ahead distribution,
    # which must be measured over every hour rather than the capped detail sample.
    bucket_stats: dict[int, tuple[int, bool, float, tuple[float, ...]]] = {}
    hour_first_observation: dict[int, int] = {}
    hour_first_value: dict[int, float] = {}
    hour_last_value: dict[int, float] = {}
    hour_change_offset: dict[int, float] = {}
    for idx, bucket_us in enumerate(sample_hour_us):
        v = price.values[idx]
        observed = times_us[idx]
        st = bucket_stats.get(bucket_us)
        if st is None:
            bucket_stats[bucket_us] = (1, True, v, (v,))
            hour_first_observation[bucket_us] = observed
            hour_first_value[bucket_us] = v
        else:
            n, agrees, first, distinct = st
            bucket_stats[bucket_us] = (
                n + 1,
                agrees and v == first,
                first,
                distinct if v == first else tuple(sorted({*distinct, v})),
            )
        hour_last_value[bucket_us] = v  # samples arrive in time order
        if observed < hour_first_observation[bucket_us]:
            hour_first_observation[bucket_us] = observed
        if (
            v != hour_first_value[bucket_us]
            and bucket_us not in hour_change_offset
            and observed >= bucket_us
        ):
            # First sample inside the hour that disagrees with the hour's opening
            # value: the instant the new price actually became visible.
            hour_change_offset[bucket_us] = (observed - bucket_us) / 1_000_000

    def offset_seconds(bucket_us: int) -> float:
        return (hour_first_observation[bucket_us] - bucket_us) / 1_000_000

    # Hour-to-hour transitions and the "first sample still carries the previous
    # hour's price" case, over the whole population of hours with samples.
    previous_value: float | None = None
    for bucket_us in sorted(bucket_stats):
        _n, agrees, _representative, values = bucket_stats[bucket_us]
        offset = offset_seconds(bucket_us)
        ledger.record_hour_population(distinct=len(values), first_offset_seconds=offset)
        # The value in force at the end of the hour: the single agreed value, or
        # the last one observed when the hour was disputed.
        current = _representative if agrees else hour_last_value[bucket_us]
        if previous_value is not None and current != previous_value:
            change = hour_change_offset.get(bucket_us, offset)
            ledger.record_transition(
                offset_seconds=change,
                carried_previous_price=hour_first_value[bucket_us] == previous_value,
            )
        previous_value = current

    out: list[PriceAlignment] = []
    recorded_hours: set[int] = set()
    for i0 in interval_starts:
        hour_dt = _clock_hour_start(i0, timezone)
        hour_us = to_micros(hour_dt)
        stat = bucket_stats.get(hour_us)
        carried_age: float | None = None
        lookahead: float | None = None
        distinct_values: tuple[float, ...] = ()
        if stat is not None:
            samples, agrees, first, hour_values = stat
            distinct_values = hour_values
            if agrees:
                # The value is directly observed inside this hour.
                value: float | None = first
                quality = PRICE_MEASURED
                lookahead = (hour_first_observation[hour_us] - hour_us) / 1_000_000
            else:
                # Samples in this hour disagree: nothing is averaged, nothing is
                # picked.  The hour is disclosed as ambiguous and carries no price.
                value = None
                quality = PRICE_AMBIGUOUS
        else:
            # No sample in this hour: carry the last known value, bounded by
            # staleness; a price older than that proves nothing about this hour.
            position = bisect_left(times_us, hour_us)
            idx = position - 1
            samples = 0
            if idx < 0:
                value = None
                quality = PRICE_MISSING
            else:
                carried_age = (hour_us - times_us[idx]) / 1_000_000
                if carried_age <= staleness_seconds:
                    value = price.values[idx]
                    quality = PRICE_FORWARD_FILLED
                else:
                    value = None
                    quality = PRICE_MISSING
        if hour_us not in recorded_hours:
            recorded_hours.add(hour_us)
            ledger.record_hour(
                hour_start=hour_dt,
                quality=quality,
                samples=samples,
                distinct=len(distinct_values),
                distinct_values=distinct_values,
                carried_age_seconds=carried_age,
                lookahead_seconds=lookahead,
            )
        if quality == PRICE_AMBIGUOUS:
            # Every interval of a disputed hour is unusable, so this one counts
            # per interval while the disagreement itself is counted per hour.
            ledger.intervals_ambiguous += 1
        out.append(
            PriceAlignment(
                value=value,
                quality=quality,
                samples=samples,
                distinct_values=len(distinct_values),
                hour_start=hour_dt,
            )
        )
    return out, ledger


def align_prices(
    price: StitchedSeries | None,
    interval_starts: Sequence[datetime],
    *,
    timezone: ZoneInfo,
    staleness_seconds: float = DEFAULT_PRICE_STALENESS_SECONDS,
) -> list[PriceAlignment]:
    """Hour-aligned prices without the ledger (convenience wrapper).

    See :func:`align_price_hours` for the full contract and the timezone
    requirement.
    """
    aligned, _ledger = align_price_hours(
        price,
        interval_starts,
        timezone=timezone,
        staleness_seconds=staleness_seconds,
    )
    return aligned


# ---------------------------------------------------------------------------
# Gross load seam (battery)
# ---------------------------------------------------------------------------


def detect_battery_active(entities_seen: Iterable[str]) -> bool | None:
    """SEAM: report battery telemetry presence in the raw archive, or ``None``.

    A battery can only be reported *present* (``True``) when a registered
    telemetry entity is actually seen, and *absent* (``False``) only when a
    non-empty :data:`BATTERY_TELEMETRY_ENTITIES` registry saw **none** of its
    entities.  With an empty registry the absence of a metric that was never
    registered proves nothing about the hardware, so this returns ``None``
    ("unknown") instead of silently asserting the install has no battery.

    The caller must treat ``None`` as unknown and either accept an explicit
    ``battery_absent`` assumption or refuse to equate gross load with the raw
    grid meter — never invent an absence.
    """
    if not BATTERY_TELEMETRY_ENTITIES:
        return None  # empty registry: battery presence is unknown, not proven absent
    seen = set(entities_seen)
    return bool(seen & set(BATTERY_TELEMETRY_ENTITIES))


def resolve_gross_load(raw_grid_kwh: float | None, *, battery_active: bool) -> float | None:
    """Reconstruct gross household load from the raw grid reading.

    With **no battery** the feed meter *is* the household load, so gross equals
    raw grid exactly.  That identity is a property of the current installation,
    not a general truth: once a battery dispatches, channel 1 measures net grid
    exchange and gross load must be reconstructed as
    ``raw_grid + charge - discharge`` from battery AC telemetry (or the interval
    must be excluded as unusable for gross-load analysis, per ADR-0003 §data
    contract).  Rather than let that silently become wrong, this seam raises.

    Nested submeters (channel 2, servers) are **not** added here — they are
    already inside channel 1.

    Raises:
        NotImplementedError: a battery is active and gross-load reconstruction
            telemetry is not implemented yet.
    """
    if battery_active:
        raise NotImplementedError(
            "gross-load reconstruction with an active battery is not implemented: "
            "channel 1 then measures net grid exchange, so gross_kwh must be rebuilt from "
            "battery AC charge/discharge telemetry (or the interval flagged unusable). "
            f"See {__name__}.BATTERY_TELEMETRY_ENTITIES and ADR-0003 §data contract."
        )
    return raw_grid_kwh


# ---------------------------------------------------------------------------
# Raw input — resolved through successful extraction manifests
# ---------------------------------------------------------------------------


@dataclass
class RawInputs:
    """Everything the builder read out of the raw store."""

    series: dict[tuple[str, str], StitchedSeries]
    part_hashes: list[dict[str, str]]
    entity_audit: list[dict[str, object]]
    rows_read: int
    source_manifests: list[dict[str, object]] = field(default_factory=list)
    # Every part the successful manifests reference (name + declared full SHA),
    # regardless of window relevance.  This is what the dataset id hashes, so the
    # id freezes the whole resolved archive lineage — adding an out-of-window part
    # still changes the id even though its rows never enter this window.
    lineage: list[dict[str, str]] = field(default_factory=list)
    # Entities actually *seen* in the parts this build read, and entities any
    # successful raw manifest declares.  Both are needed to tell a real exclusion
    # ("present, deliberately dropped") from an untested intention ("configured,
    # never present"), which the manifest must not conflate.
    observed_entities: list[str] = field(default_factory=list)
    declared_entities: list[str] = field(default_factory=list)

    def timeline(self, entity: str) -> StitchedSeries | None:
        for key, value in self.series.items():
            if key[0] == entity:
                return value
        return None


def _compact_utc(dt: datetime) -> str:
    """Render an aware datetime as the compact UTC filename token ``%Y%m%dT%H%M%SZ``."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_window_bound(value: object) -> datetime | None:
    """Best-effort ISO parse of a manifest/part window bound to aware UTC.

    Returns ``None`` when the value is missing or unparseable so callers can fall
    back to a coarser bound instead of failing on a manifest that simply omits
    optional metadata.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_utc(value, name="window")
    except (ValueError, TypeError):
        return None


def _part_window(
    entry: dict[str, object],
    manifest_start: datetime | None,
    manifest_end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Determine the half-open UTC window a referenced part covers.

    Prefers the per-part ``chunk_start``/``chunk_end`` the Prometheus extractor
    records; falls back to the enclosing manifest's source window.  ``None``
    bounds mean "unknown, treat as relevant" (fail-safe: validate it).
    """
    cs = _parse_window_bound(entry.get("chunk_start"))
    ce = _parse_window_bound(entry.get("chunk_end"))
    if cs is not None and ce is not None:
        return cs, ce
    return manifest_start, manifest_end


def _overlaps(
    start: datetime | None,
    end: datetime | None,
    lo: datetime,
    hi: datetime,
) -> bool:
    """True when ``[start, end)`` overlaps ``[lo, hi)``, or bounds are unknown."""
    if start is None or end is None:
        return True  # cannot rule it out → keep and validate
    return start < hi and end > lo


def _int_or(value: object, default: int) -> int:
    """Coerce a manifest field to ``int`` if it is one, else *default*."""
    return value if isinstance(value, int) else default


def _entry_part_name(entry: dict[str, object]) -> str:
    """The part's file name from a manifest reference (name, else path basename)."""
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    raw = entry.get("path")
    if isinstance(raw, str) and raw:
        return Path(raw).name
    return ""


def _resolve_referenced_path(data_dir: Path, resolved_root: Path, entry: dict[str, object]) -> Path:
    """Resolve a manifest part reference to a path **contained under** *data_dir*.

    Rejects absolute paths and any ``..`` segment that escapes the data root, so
    a corrupt or hostile manifest can never make the builder read the host
    filesystem.  Falls back to the part name under the Prometheus parts dir when
    only a name is recorded.

    Raises:
        BuildError: the reference is absolute, escapes ``data_dir``, or has no
            usable path/name.
    """
    raw = entry.get("path")
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw)
        if candidate.is_absolute():
            raise BuildError(
                f"manifest references absolute part path {raw!r} — outside the data dir"
            )
        if ".." in candidate.parts:
            raise BuildError(f"manifest part path {raw!r} contains '..' — path traversal rejected")
        target = (data_dir / candidate).resolve()
    else:
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BuildError(f"manifest part reference has neither 'path' nor 'name': {entry!r}")
        target = (parts_dir(data_dir, PROM_SOURCE) / name).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise BuildError(
            f"manifest part path {raw!r} resolves to {target}, outside the data dir {resolved_root}"
        )
    return target


def _validate_manifest_part(
    *,
    path: Path,
    entry: dict[str, object],
) -> pa.Table:
    """Read one referenced part as bytes and validate it against its manifest entry.

    Hashes the bytes and parses the **same** buffer, so the integrity check and
    the data come from one immutable snapshot (no re-read that could observe a
    file that changed after the hash).  Enforces the full SHA-256, the declared
    row count and the ``RAW_SAMPLE`` schema (including non-null invariants).

    Raises:
        BuildError: missing file, checksum mismatch, row-count mismatch, or a
            schema that does not match ``RAW_SAMPLE``.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"manifest references missing/unreadable part {path.name}: {exc}") from exc

    digest = hashlib.sha256(data).hexdigest()
    declared_sha = entry.get("sha256")
    if isinstance(declared_sha, str) and declared_sha:
        if digest != declared_sha:
            raise BuildError(
                f"raw part {path.name}: sha256 mismatch — manifest says {declared_sha}, "
                f"file is {digest} (a report cannot trust the 8-char filename hash alone)"
            )
    # Parse from the exact bytes we hashed, not a second read of the path.
    try:
        table = pq.read_table(pa.BufferReader(pa.py_buffer(data)))
    except Exception as exc:  # unreadable payload must not pass silently
        raise BuildError(f"unreadable raw part {path.name}: {exc}") from exc
    try:
        validate_table("RAW_SAMPLE", table)
    except (TypeError, ValueError) as exc:
        raise BuildError(f"raw part {path.name} does not match RAW_SAMPLE: {exc}") from exc
    declared_rows = entry.get("rows")
    if isinstance(declared_rows, int) and declared_rows != table.num_rows:
        raise BuildError(
            f"raw part {path.name}: row count mismatch — manifest says {declared_rows}, "
            f"file has {table.num_rows}"
        )
    return table


def _fold_into_audit(
    audit: dict[tuple[str, str, str], dict[str, object]],
    series_keys: dict[tuple[str, str], set[str]],
    table: pa.Table,
    label_keys: dict[tuple[str, str], set[str]],
) -> None:
    """Accumulate whole-archive entity statistics for one part.

    Aggregated with Arrow kernels rather than a Python loop over every row: the
    audit describes the *whole* archive (all entities, all years), so a per-row
    loop would materialise ~1.5M dictionaries on every build.
    """
    summary = (
        table.group_by(["entity", "metric", "unit"])
        .aggregate(
            [
                ("ts", "min"),
                ("ts", "max"),
                ("ts", "count"),
                ("value", "min"),
                ("value", "max"),
            ]
        )
        .to_pylist()
    )
    for row in summary:
        key = (row["entity"], row["metric"], row["unit"])
        entry = audit.setdefault(
            key,
            {
                "entity": key[0],
                "metric": key[1],
                "unit": key[2],
                "samples": 0,
                "first_ts": None,
                "last_ts": None,
                "value_min": None,
                "value_max": None,
            },
        )
        entry["samples"] = int(entry["samples"]) + int(row["ts_count"])
        first, last = row["ts_min"], row["ts_max"]
        if entry["first_ts"] is None or first < entry["first_ts"]:
            entry["first_ts"] = first
        if entry["last_ts"] is None or last > entry["last_ts"]:
            entry["last_ts"] = last
        # Accumulated min/max is exact across parts (unlike a per-part distinct
        # count, which cannot be summed), and ``value_min == value_max`` proves the
        # series carried one value wherever it was read: that is how "this channel
        # reported no export all year" becomes a measurement with a sample size.
        v_min, v_max = row["value_min"], row["value_max"]
        if entry["value_min"] is None or (v_min is not None and v_min < entry["value_min"]):
            entry["value_min"] = v_min
        if entry["value_max"] is None or (v_max is not None and v_max > entry["value_max"]):
            entry["value_max"] = v_max

    # Label keys per (entity, metric) over every distinct label set, cheaply: the
    # cardinality of distinct label sets is one row per series, not per sample.
    # This is how the build can state that the archive carries no effective-time
    # or staleness field instead of asking a reader to trust a docstring.
    for row in (
        table.group_by(["entity", "metric", "labels_json"]).aggregate([("ts", "count")]).to_pylist()
    ):
        key = (row["entity"], row["metric"])
        try:
            labels = json.loads(row["labels_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(labels, dict):
            continue
        keys = label_keys.setdefault(key, set())
        keys.update(str(name) for name in labels)

    # Distinct series_keys per (entity, metric): era count is diagnostic only, so
    # the cardinality of the group-by (one row per series) is enough.
    for row in (
        table.group_by(["entity", "metric", "series_key"]).aggregate([("ts", "count")]).to_pylist()
    ):
        series_keys[(row["entity"], row["metric"])].add(row["series_key"])


def _iter_prometheus_manifests(data_dir: Path) -> list[dict[str, object]]:
    """Load every **successful** Prometheus extraction manifest under ``data_dir``.

    Reads ``data_dir/manifests/*.json`` and keeps only manifests with
    ``source == PROM_SOURCE`` and a non-null ``finished_at`` (a failed extraction
    writes no manifest, and an unfinished record must not be treated as
    complete).  Unparseable manifests are skipped with a warning — coverage is
    established by the manifests that *do* parse, never by an absent one.
    """
    manifests_dir = data_dir / "manifests"
    if not manifests_dir.exists():
        return []
    out: list[dict[str, object]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("skipping unreadable manifest %s: %s", path.name, exc)
            continue
        if not isinstance(record, dict):
            log.warning("skipping non-object manifest %s", path.name)
            continue
        if record.get("source") != PROM_SOURCE:
            continue  # HA history / analysis run manifests are not raw inputs
        if record.get("finished_at") is None:
            log.warning("skipping incomplete prometheus manifest %s (no finished_at)", path.name)
            continue
        if not isinstance(record.get("part_files"), list):
            continue
        out.append(record)
    return out


def load_raw_inputs(
    data_dir: Path,
    *,
    entities: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> RawInputs:
    """Resolve raw timelines for *entities* through the successful raw manifests.

    Coverage is read from ``data_dir/manifests/*.json`` (``source ==
    "prometheus"`` only), never by globbing the part directory: an orphaned part
    left by a failed extraction has no manifest entry and is therefore ignored.
    Each referenced part is validated (full SHA-256 of its bytes, declared row
    count, ``RAW_SAMPLE`` schema, path containment under ``data_dir``) from a
    single read, and a part is only required when its chunk window overlaps the
    selected ``[window_start - lookback, window_end)`` range — parts irrelevant
    to this window are neither read nor demanded.  Rows are limited to that
    range for the timelines, while the audit summary describes every part that
    *was* loaded so the manifest can state which entities exist and which were
    deliberately left out.

    Raises:
        BuildError: no manifest references any relevant part, a referenced part
            is missing/corrupt/mismatched/traversing, or an overlap/conflict is
            discovered while stitching.
    """
    manifests = _iter_prometheus_manifests(data_dir)
    if not manifests:
        raise BuildError(
            f"no raw parts: no successful prometheus manifests in {data_dir / 'manifests'} — "
            "run the extract command first"
        )

    resolved_root = data_dir.resolve()
    wanted = set(entities)
    # ``ts`` is ``timestamp[us, tz=UTC]``: compare against aware datetimes, since
    # an epoch-integer comparison has no pyarrow kernel and fails inside the scan.
    lo_dt = (window_start - lookback).astimezone(UTC)
    hi_dt = window_end.astimezone(UTC)
    groups: dict[tuple[str, str, str], list[RawPoint]] = defaultdict(list)
    audit: dict[tuple[str, str, str], dict[str, object]] = {}
    series_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    label_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    part_hashes: list[dict[str, str]] = []
    source_manifests: list[dict[str, object]] = []
    rows_read = 0
    loaded_parts = 0
    # Entities named by a successful raw manifest anywhere in the archive, whether
    # or not this window read their parts.  An exclusion can only be claimed when
    # the entity was *seen*; anything else is a configuration that was never
    # exercised, and the two must not look the same in a manifest.
    declared_entities: set[str] = set()
    # id/lineage over EVERY referenced part (name + declared SHA only — no file is
    # touched, so an irrelevant or even a missing out-of-window part still freezes
    # the archive identity without being demanded).
    lineage: list[dict[str, str]] = []
    lineage_seen: set[tuple[str, str]] = set()

    for record in manifests:
        m_start = _parse_window_bound(record.get("source_window_start"))
        m_end = _parse_window_bound(record.get("source_window_end"))
        for name in record.get("entities") or []:
            if isinstance(name, str):
                declared_entities.add(name)
        lineage_parts: list[dict[str, object]] = []
        for entry in record.get("part_files", []):
            if not isinstance(entry, dict):
                continue
            declared_sha = str(entry.get("sha256", ""))
            part_name = _entry_part_name(entry)
            key = (part_name, declared_sha)
            if key not in lineage_seen:
                lineage_seen.add(key)
                lineage.append({"part": part_name, "sha256": declared_sha})
            lineage_parts.append(
                {
                    "name": part_name,
                    "sha256": declared_sha,
                    "rows": _int_or(entry.get("rows"), 0),
                }
            )

            cs, ce = _part_window(entry, m_start, m_end)
            if not _overlaps(cs, ce, lo_dt, hi_dt):
                continue  # irrelevant to this window: referenced for lineage only
            path = _resolve_referenced_path(data_dir, resolved_root, entry)
            table = _validate_manifest_part(path=path, entry=entry)
            _fold_into_audit(audit, series_keys, table, label_keys)

            selected_rows = 0
            if wanted and table.num_rows:
                ts_col = table.column("ts")
                mask = pc.and_(
                    pc.greater_equal(ts_col, pa.scalar(lo_dt, pa.timestamp("us", tz="UTC"))),
                    pc.less(ts_col, pa.scalar(hi_dt, pa.timestamp("us", tz="UTC"))),
                )
                mask = pc.and_(
                    mask,
                    pc.is_in(
                        table.column("entity"), value_set=pa.array(sorted(wanted), pa.string())
                    ),
                )
                selected = table.filter(mask)
                selected_rows = selected.num_rows
                if selected_rows:
                    cols = {
                        name: selected.column(name).to_pylist()
                        for name in ("entity", "metric", "unit", "series_key", "ts", "value")
                    }
                    for i in range(selected_rows):
                        ts = cols["ts"][i]
                        value = cols["value"][i]
                        if ts is None or value is None:
                            raise BuildError(
                                f"raw part {path.name}: null in a non-nullable column at row {i}"
                            )
                        groups[(cols["entity"][i], cols["metric"][i], cols["unit"][i])].append(
                            RawPoint(
                                ts=ts,
                                value=float(value),
                                series_key=cols["series_key"][i],
                            )
                        )
            part_hashes.append({"part": path.name, "sha256": declared_sha, "rows": selected_rows})
            rows_read += selected_rows
            loaded_parts += 1

        if lineage_parts:
            source_manifests.append(
                {
                    "extraction_id": record.get("extraction_id"),
                    "config_hash": record.get("config_hash"),
                    "source_window_start": record.get("source_window_start"),
                    "source_window_end": record.get("source_window_end"),
                    "row_count": record.get("row_count"),
                    "parts": lineage_parts,
                }
            )

    if loaded_parts == 0:
        raise BuildError(
            f"no raw parts: no prometheus manifest references a part within "
            f"[{iso_z(lo_dt)}, {iso_z(hi_dt)}) — run the extract command first"
        )

    stitched = {
        (entity, metric): stitch_series(entity=entity, metric=metric, unit=unit, points=points)
        for (entity, metric, unit), points in groups.items()
    }
    entity_audit = []
    for key, entry in sorted(audit.items()):
        item = dict(entry)
        item["first_ts"] = iso_z(item["first_ts"]) if item["first_ts"] else None
        item["last_ts"] = iso_z(item["last_ts"]) if item["last_ts"] else None
        item["series_keys"] = len(series_keys.get((key[0], key[1]), ()))
        item["label_keys"] = sorted(label_keys.get((key[0], key[1]), set()))
        # One value across the whole entity is worth stating explicitly: it is what
        # separates a dead channel from a channel that was merely left unused.
        low, high = item.get("value_min"), item.get("value_max")
        item["constant_value"] = low is not None and low == high
        item["used_for"] = (
            "grid"
            if key[0] in GRID_COUNTER_ENTITIES
            else ("price" if key[0] in PRICE_ENTITIES else "not_used")
        )
        entity_audit.append(item)

    log.info(
        "loaded %d raw sample(s) for %d timeline(s) from %d referenced part(s) across "
        "%d manifest(s); %d entity/metric group(s) present",
        rows_read,
        len(stitched),
        loaded_parts,
        len(source_manifests),
        len(entity_audit),
    )
    return RawInputs(
        series=stitched,
        part_hashes=part_hashes,
        entity_audit=entity_audit,
        rows_read=rows_read,
        source_manifests=source_manifests,
        lineage=lineage,
        observed_entities=sorted({str(item["entity"]) for item in entity_audit}),
        declared_entities=sorted(declared_entities),
    )


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def code_version() -> str:
    """Git commit of the package, dirty-aware.

    Delegates to :func:`energy_baseline.manifest.code_version`, the single shared
    definition used by ``benchmark`` too, so a dirty tree cannot produce the same
    ``dataset_id`` as a clean one.
    """
    return manifest_code_version()


def compute_dataset_id(
    *,
    part_hashes: Sequence[dict[str, object]],
    config_hash: str,
    code_version: str,
    schema_version: int,
    window_start: datetime,
    window_end: datetime,
    params: dict[str, object],
) -> str:
    """SHA-256 over the deterministic inputs of a build.

    Identical source parts, config, code, schema, window and parameters produce
    an identical id — and therefore identical bytes in the same directory.  Any
    change to any of them produces a new dataset instead of mutating an old one.
    """
    payload = json.dumps(
        {
            "parts": [
                {"part": str(p.get("part")), "sha256": str(p.get("sha256"))}
                for p in sorted(part_hashes, key=lambda p: str(p.get("part")))
            ],
            "config_hash": config_hash,
            "code_version": code_version,
            "schema_version": schema_version,
            "window_start": iso_z(window_start),
            "window_end": iso_z(window_end),
            "params": params,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Table assembly
# ---------------------------------------------------------------------------


def _merge_contiguous(intervals_us: Sequence[int], step_us: int) -> list[dict[str, object]]:
    """Collapse sorted interval starts into ``[start, end)`` ranges."""
    ranges: list[dict[str, object]] = []
    for us in sorted(intervals_us):
        if ranges and us == ranges[-1]["_end_us"]:
            ranges[-1]["_end_us"] = us + step_us
            ranges[-1]["intervals"] = int(ranges[-1]["intervals"]) + 1
            continue
        ranges.append({"_start_us": us, "_end_us": us + step_us, "intervals": 1})
    for item in ranges:
        item["start"] = iso_z(from_micros(int(item["_start_us"])))
        item["end"] = iso_z(from_micros(int(item["_end_us"])))
        item.pop("_start_us")
        item.pop("_end_us")
    return ranges


def build_interval_table(
    *,
    grid: StitchedSeries | None,
    price: StitchedSeries | None,
    interval_starts: Sequence[datetime],
    periods: TariffPeriods,
    timezone: ZoneInfo,
    dataset_id: str,
    interval_seconds: int = INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    gap_threshold_seconds: float | None = None,
    price_staleness_seconds: float = DEFAULT_PRICE_STALENESS_SECONDS,
    battery_active: bool = False,
) -> tuple[pa.Table, SpanLedger, PriceLedger]:
    """Assemble the ``ANALYSIS_INTERVAL`` table for one window (no filesystem).

    Returns the table, the :class:`SpanLedger` of spans whose totals could not
    be attributed to a single interval, and the :class:`PriceLedger` of
    per-hour price statistics for the build manifest.

    ``battery_absent`` (the output column) records the caller's decision that no
    battery is dispatching, so gross load may equal the raw feed reading.  It is
    always the *inverse* of ``battery_active`` here because a build only reaches
    this function when absence is either detected as a fact or explicitly assumed
    (see :func:`build_dataset`).

    Raises:
        BuildError: ``timezone`` is unknown.
    """
    tz_name = str(timezone)
    spans = (
        counter_deltas(grid, gap_factor=gap_factor, gap_threshold_seconds=gap_threshold_seconds)
        if grid
        else []
    )
    energies, ledger = allocate_energy(
        grid
        or StitchedSeries(
            entity="",
            metric="",
            unit="",
            times=(),
            values=(),
            series_keys=(),
            era_boundaries=(),
            median_interval_seconds=None,
            duplicates_dropped=0,
        ),
        spans,
        interval_starts,
        interval_seconds=interval_seconds,
    )
    prices, price_ledger = align_price_hours(
        price,
        interval_starts,
        timezone=timezone,
        staleness_seconds=price_staleness_seconds,
    )

    rows: dict[str, list[object]] = {name: [] for name in ANALYSIS_INTERVAL.names}
    dst_cache: dict[date, str | None] = {}
    for i0, energy, price_align in zip(interval_starts, energies, prices, strict=True):
        i1 = i0 + timedelta(seconds=interval_seconds)
        local = i0.astimezone(timezone)
        local_day = local.date()
        if local_day not in dst_cache:
            dst_cache[local_day] = dst_transition_for(local_day, timezone)
        gross = resolve_gross_load(energy.raw_grid_kwh, battery_active=battery_active)
        rows["interval_start"].append(i0)
        rows["interval_end"].append(i1)
        rows["tz_local"].append(tz_name)
        rows["tariff_period"].append(assign_tariff_period(periods, local))
        rows["local_day"].append(local_day)
        rows["dst_transition"].append(dst_cache[local_day])
        rows["raw_grid_kwh"].append(energy.raw_grid_kwh)
        rows["gross_kwh"].append(gross)
        rows["battery_absent"].append(not battery_active)
        rows["energy_quality"].append(energy.energy_quality)
        rows["allocation_quality"].append(energy.allocation_quality)
        rows["sample_count"].append(energy.sample_count)
        rows["gap_seconds"].append(energy.gap_seconds)
        rows["price_eur_per_kwh"].append(price_align.value)
        rows["price_quality"].append(price_align.quality)
        rows["dataset_id"].append(dataset_id)

    table = pa.table(
        {name: pa.array(rows[name], ANALYSIS_INTERVAL.field(name).type) for name in rows},
        schema=ANALYSIS_INTERVAL,
    )
    return table, ledger, price_ledger


def _coerce_bound(name: str, value: str | datetime) -> datetime:
    """Normalise a window bound (ISO string or aware datetime) to aware UTC.

    Raises:
        ValueError: a naive datetime, or a string with no explicit offset.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware (UTC); got naive {value!r}")
        return value.astimezone(UTC)
    return parse_utc(value, name=name)


def _meter_identity(entity_audit: Sequence[dict[str, object]]) -> dict[str, object]:
    """Meter wiring as what it is: a dated user assertion plus measured side notes.

    The archived data records no current-transformer placement, so channel
    identity cannot be confirmed from it — the assertion is published with its
    provenance, its date, and an explicit ``verified_from_data: false``.  The
    measured parts (a constant export channel, the power floor) are attached from
    the entity audit so the reader can see exactly how far the data does and does
    not go.
    """
    by_entity: dict[str, dict[str, object]] = {}
    for item in entity_audit:
        by_entity.setdefault(str(item["entity"]), item)

    channels: dict[str, object] = {}
    for entity in (*NESTED_SUBMETER_ENTITIES, *GRID_COUNTER_ENTITIES):
        item = by_entity.get(entity)
        if item is None:
            continue
        channels[entity] = {
            "samples": item.get("samples"),
            "value_min": item.get("value_min"),
            "value_max": item.get("value_max"),
            "constant_value": bool(item.get("constant_value")),
            "used_for": item.get("used_for"),
        }
    return {
        **METER_IDENTITY_ASSERTION,
        "observed_channels": channels,
        "export_channel_note": EXPORT_CHANNEL_PROVENANCE_NOTE,
        "not_verified_because": (
            "no archived field documents CT placement, and a floor that is essentially "
            "never undercut is equally consistent with a subcircuit that never turns off; "
            "only a physical check, or a generation/discharge event that breaks the tie, "
            "would verify channel identity from data"
        ),
    }


def disclose_exclusions(inputs: RawInputs) -> dict[str, object]:
    """Split the configured exclusions into *seen and dropped* vs *never present*.

    A manifest that lists an entity under "excluded" asserts that the pipeline
    handled it, which is only true if the entity was in the inputs.  The 2025
    build asserted two exclusions that could never have happened
    (``sensor.shellyem_main_channel_2_energy`` and ``sensor.esios_pvpc_2`` appear
    in no raw manifest and in no part, and ``raw_rows_read`` was exactly the
    energy plus price row count), so the claim was vacuous while looking like
    evidence of care.  Keeping the two cases in different keys makes an untested
    intention readable as one.
    """
    seen = set(inputs.observed_entities)
    declared = set(inputs.declared_entities)
    used = set(GRID_COUNTER_ENTITIES) | set(PRICE_ENTITIES)
    configured = {entity: "nested_submeter" for entity in NESTED_SUBMETER_ENTITIES}
    configured.update({entity: "dead_duplicate" for entity in sorted(DEAD_DUPLICATE_ENTITY_IDS)})

    observed_and_dropped: list[dict[str, object]] = []
    absent: list[dict[str, object]] = []
    for entity, role in sorted(configured.items()):
        if entity in seen:
            samples = next(
                (
                    int(item["samples"])
                    for item in inputs.entity_audit
                    if item["entity"] == entity and item["metric"]
                ),
                0,
            )
            observed_and_dropped.append(
                {
                    "entity": entity,
                    "role": role,
                    "samples": samples,
                    "used_for": "not_used",
                    "status": "observed_in_inputs_and_deliberately_dropped",
                }
            )
        else:
            absent.append(
                {
                    "entity": entity,
                    "role": role,
                    "status": (
                        "declared_by_a_raw_manifest_but_not_in_this_window"
                        if entity in declared
                        else "never_observed_in_any_input"
                    ),
                    "note": (
                        "excluded from gross load by configuration, but the exclusion "
                        "is untested because the entity is not in the inputs"
                    ),
                }
            )
    unused_seen = sorted(seen - used - set(configured))
    return {
        "note": (
            "an entity appears in nested_submeters_excluded_from_gross or "
            "dead_duplicates_excluded only if it was observed in the parts this build "
            "read; configured entities that were never seen appear under "
            "configured_but_absent_from_inputs instead"
        ),
        "nested_submeters_excluded_from_gross": [
            item["entity"] for item in observed_and_dropped if item["role"] == "nested_submeter"
        ],
        "dead_duplicates_excluded": [
            item["entity"] for item in observed_and_dropped if item["role"] == "dead_duplicate"
        ],
        "configured_but_absent_from_inputs": absent,
        "observed_and_dropped": observed_and_dropped,
        "other_entities_seen_and_ignored": unused_seen,
        "entities_observed_in_inputs": sorted(seen),
        "entities_declared_by_raw_manifests": sorted(declared),
    }


def _build_params(
    *,
    tz_name: str,
    interval_seconds: int,
    gap_factor: float,
    gap_threshold_seconds: float | None,
    price_staleness_seconds: float,
    lookback: timedelta,
    battery_absent: bool,
    battery_detection: str,
    exclusions: dict[str, object],
    reconciliation_tolerance_kwh: float,
) -> dict[str, object]:
    """The ``params`` block for the dataset id and both manifests.

    Every numeric knob carries an explicit unit in its key name so a reader (or a
    report) cannot mistake a seconds value for milliseconds or a dimensionless
    factor for a threshold.
    """
    return {
        "timezone": tz_name,
        "interval_seconds": interval_seconds,
        "gap_factor_intervals": gap_factor,
        "gap_threshold_seconds": gap_threshold_seconds,
        "price_staleness_seconds": price_staleness_seconds,
        "lookback_seconds": int(lookback.total_seconds()),
        # Part of the builder contract, not a report-time knob: the id must change
        # if the acceptance threshold changes, because the threshold is recorded in
        # the (immutable) build manifest of the dataset it accepted.
        "reconciliation_tolerance_kwh": reconciliation_tolerance_kwh,
        # Battery state is a recorded *assumption*, not a detection: absence is
        # only a fact when a non-empty registry saw none of its entities.
        "battery_absent": battery_absent,
        "battery_absent_is_assumption": battery_detection == BATTERY_UNKNOWN,
        "battery_detection": battery_detection,
        "battery_telemetry_entities": list(BATTERY_TELEMETRY_ENTITIES),
        # Exclusions are a *measurement of this build's inputs*, not a static
        # list: only entities actually observed and deliberately dropped are
        # claimed as excluded (see :func:`disclose_exclusions`).
        "nested_submeters_excluded_from_gross": exclusions["nested_submeters_excluded_from_gross"],
        "dead_duplicates_excluded": exclusions["dead_duplicates_excluded"],
        "configured_but_absent_from_inputs": exclusions["configured_but_absent_from_inputs"],
    }


def _id_params(params: dict[str, object]) -> dict[str, object]:
    """The deterministic subset of ``params`` that feeds the dataset id."""
    return {
        key: params[key]
        for key in (
            "interval_seconds",
            "gap_factor_intervals",
            "gap_threshold_seconds",
            "price_staleness_seconds",
            "lookback_seconds",
            "reconciliation_tolerance_kwh",
            "battery_absent",
            "battery_detection",
        )
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_dataset(
    *,
    data_dir: str | None,
    config_path: str | None,
    start: str | datetime,
    end: str | datetime,
    lookback: timedelta = DEFAULT_LOOKBACK,
    interval_seconds: int = INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    gap_threshold_seconds: float | None = None,
    price_staleness_seconds: float = DEFAULT_PRICE_STALENESS_SECONDS,
    battery_absent: bool = True,
    reconciliation_tolerance_kwh: float = DEFAULT_RECONCILIATION_TOLERANCE_KWH,
) -> Path:
    """Build the 15-minute analysis dataset from the raw archive.

    Resolves coverage from the successful Prometheus manifests, computes counter
    deltas, assigns tariff periods, flags DST transitions and data-quality gaps,
    and writes the result to ``analysis/v1/<dataset_id>/`` plus a build manifest.
    A run record goes to ``manifests/`` (where the wall-clock lives); the dataset
    directory itself is timestamp-free and byte-for-byte reproducible.

    Args:
        start: Inclusive window bound (ISO-8601 with an offset, or aware datetime).
        end: Exclusive window bound.
        battery_absent: Explicit configuration **assumption** that no battery is
            dispatching, so gross load may equal the whole-home feed counter.
            ``True`` is the compatibility-preserving default.  It is recorded in
            the build manifest and reported — never silently derived from the
            archive, because an unregistered battery metric proves nothing.  When
            the archive *does* carry registered battery telemetry the build
            refuses regardless of this flag.
        reconciliation_tolerance_kwh: Largest unexplained energy the build will
            accept, in kWh.  The identity
            ``counter endpoint == Σ allocated + Σ declared unattributed + residual``
            is evaluated over the terms :class:`SpanLedger` accumulated; a residual
            beyond this tolerance means kWh entered the build, reached no interval,
            and belongs to no declared category, so the build is refused instead of
            publishing a dataset with a hole in it.  Defaults to
            :data:`DEFAULT_RECONCILIATION_TOLERANCE_KWH` (1 mWh).

    Returns:
        The dataset directory (:class:`~pathlib.Path`).

    Raises:
        BuildError: no referenced parts, a corrupt/missing/traversing part, a
            schema mismatch, an overlap/conflict, a missing grid counter, an
            unknown timezone, or ``battery_absent=False`` with battery presence
            unknown (gross load cannot then be established).
        ReconciliationError: this build's own energy accounting did not close.
        NotImplementedError: battery telemetry is detected (ADR-0003 gross-load
            reconstruction is unimplemented).
        ValueError: a naive or inverted window bound.
        FileNotFoundError: tariff config not found.
    """
    root = resolve_data_dir(data_dir)
    cfg_path = resolve_config_path(config_path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"tariff config not found: {cfg_path}")
    cfg = load_config(cfg_path)

    t_start = _coerce_bound("start", start)
    t_end = _coerce_bound("end", end)
    if t_start >= t_end:
        raise ValueError(f"start must be before end: [{iso_z(t_start)}, {iso_z(t_end)})")

    tz_name = cfg.tariff_periods.timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise BuildError(f"timezone {tz_name!r} is not usable: {exc}") from exc

    inputs = load_raw_inputs(
        root,
        entities=list(GRID_COUNTER_ENTITIES) + list(PRICE_ENTITIES),
        window_start=t_start,
        window_end=t_end,
        lookback=lookback,
    )

    grid_series = inputs.timeline(GRID_COUNTER_ENTITIES[0])
    if grid_series is None or len(grid_series) < 2:
        found = len(grid_series) if grid_series else 0
        raise GridSeriesMissingError(
            f"no usable whole-home counter samples for {GRID_COUNTER_ENTITIES[0]} in "
            f"[{iso_z(t_start)}, {iso_z(t_end)}) — {found} 1 sample is not enough to "
            "compute a delta, need at least 2"
        )

    # Battery: detect, then reconcile with the explicit assumption.  Presence in
    # the data is a hard refusal; unknown presence with an opted-out assumption is
    # a refusal to invent gross load.
    seen_entities = [str(item["entity"]) for item in inputs.entity_audit]
    battery_detected = detect_battery_active(seen_entities)
    if battery_detected is True:
        raise NotImplementedError(
            "battery telemetry detected in the archive — gross-load reconstruction is not "
            "implemented; channel 1 then measures net grid exchange (ADR-0003 §data contract)."
        )
    if battery_detected is None and not battery_absent:
        raise BuildError(
            "battery presence is unknown (no battery telemetry registered) and "
            "battery_absent=False refuses to assume absence: gross load cannot be "
            "established. Pass battery_absent=True to assume no battery, or register "
            "BATTERY_TELEMETRY_ENTITIES so presence can be detected."
        )
    # Reaching here, absence is either a detected fact (False) or an explicit
    # assumption (True).  gross == raw grid is then valid.
    battery_detection = (
        BATTERY_ABSENT if battery_detected is False else BATTERY_UNKNOWN
    )  # detected present already raised
    battery_active = False

    interval_starts = make_utc_grid(t_start, t_end, interval_seconds=interval_seconds)
    price_series = inputs.timeline(PRICE_ENTITIES[0])
    cfg_digest = config_hash(cfg_path)
    exclusions = disclose_exclusions(inputs)
    params = _build_params(
        tz_name=tz_name,
        interval_seconds=interval_seconds,
        gap_factor=gap_factor,
        gap_threshold_seconds=gap_threshold_seconds,
        price_staleness_seconds=price_staleness_seconds,
        lookback=lookback,
        battery_absent=battery_absent,
        battery_detection=battery_detection,
        exclusions=exclusions,
        reconciliation_tolerance_kwh=reconciliation_tolerance_kwh,
    )
    dataset_id = compute_dataset_id(
        part_hashes=inputs.lineage,
        config_hash=cfg_digest,
        code_version=code_version(),
        schema_version=SCHEMA_VERSION,
        window_start=t_start,
        window_end=t_end,
        params=_id_params(params),
    )

    table, ledger, price_ledger = build_interval_table(
        grid=grid_series,
        price=price_series,
        interval_starts=interval_starts,
        periods=cfg.tariff_periods,
        timezone=tz,
        dataset_id=dataset_id,
        interval_seconds=interval_seconds,
        gap_factor=gap_factor,
        gap_threshold_seconds=gap_threshold_seconds,
        price_staleness_seconds=price_staleness_seconds,
        battery_active=battery_active,
    )
    price_ledger.hour_timezone = tz_name
    # Fail before publishing, not after: a build whose energy does not close must
    # not leave a dataset directory behind at all.  Parts are content-addressed and
    # no-clobber, so writing them first would keep the leaked dataset on disk even
    # though the run aborted.
    reconciliation = ledger.reconciliation(tolerance_kwh=reconciliation_tolerance_kwh)
    require_reconciled(reconciliation)

    # --- write the immutable, timestamp-free dataset ---
    out_dir = root / "analysis" / f"v{SCHEMA_VERSION}" / dataset_id
    parts_out = out_dir / "parts"
    sink = io.BytesIO()
    pq.write_table(table, sink)
    part_bytes = sink.getvalue()
    part_hash = hashlib.sha256(part_bytes).hexdigest()
    grid_start = interval_starts[0]
    grid_end = interval_starts[-1] + timedelta(seconds=interval_seconds)
    part_name = (
        f"{PART_KIND}-{_compact_utc(grid_start)}-{_compact_utc(grid_end)}-{part_hash[:8]}.parquet"
    )
    part_path = parts_out / part_name
    write_part_atomic(part_path, part_bytes)

    raw_values = table.column("raw_grid_kwh").to_pylist()
    excluded_us = [
        to_micros(row)
        for row, value in zip(table.column("interval_start").to_pylist(), raw_values, strict=True)
        if value is None
    ]
    spans = counter_deltas(
        grid_series, gap_factor=gap_factor, gap_threshold_seconds=gap_threshold_seconds
    )
    gaps = [s.as_dict() for s in spans if s.is_gap]
    counter_resets = [s.as_dict() for s in spans if s.is_reset]
    series_dicts = [grid_series.as_dict()]
    era_boundaries = [b.as_dict() for b in grid_series.era_boundaries]
    if price_series:
        series_dicts.append(price_series.as_dict())
        era_boundaries.extend(b.as_dict() for b in price_series.era_boundaries)

    quality_counts = {
        col: dict(Counter(table.column(col).to_pylist()))
        for col in ("energy_quality", "allocation_quality", "price_quality")
    }
    quantisation = measure_counter_quantisation(grid_series)
    measured_step = quantisation.get("quantisation_kwh")
    # Cross-check the declared allocation against what actually landed in the
    # Parquet: a value that changed between allocate_energy and the written column
    # would break the identity from the other side.
    table_allocated = math.fsum(v for v in raw_values if v is not None)
    require_reconciled(reconciliation)
    price_ledger.observed_label_keys = next(
        (
            [str(k) for k in (item.get("label_keys") or [])]
            for item in inputs.entity_audit
            if item["entity"] in PRICE_ENTITIES
        ),
        [],
    )
    meter_identity = _meter_identity(inputs.entity_audit)
    manifest: dict[str, object] = {
        "dataset_id": dataset_id,
        "source": SOURCE,
        "schema_version": SCHEMA_VERSION,
        "code_version": code_version(),
        "config_hash": cfg_digest,
        "requested_window": {"start": iso_z(t_start), "end": iso_z(t_end)},
        "grid_window": {"start": iso_z(grid_start), "end": iso_z(grid_end)},
        "params": params,
        "part_files": [
            {
                "path": str(part_path.relative_to(root)),
                "name": part_name,
                "kind": PART_KIND,
                "sha256": part_hash,
                "rows": table.num_rows,
            }
        ],
        "raw_lineage": inputs.source_manifests,
        "entity_audit": inputs.entity_audit,
        "input_exclusions": exclusions,
        "meter_identity": meter_identity,
        "series": series_dicts,
        "era_boundaries": era_boundaries,
        "energy_reconciliation": reconciliation.as_dict(),
        "unattributed_spans": ledger.as_dict(),
        "boundary_allocation": {
            "rule": "proportional_split_at_boundary",
            "spans_split": ledger.boundary_span_count,
            "kwh_through_split_spans": round(ledger.boundary_span_total_kwh, 9),
            "kwh_through_split_spans_note": (
                "boundary-crossing spans at normal cadence only: gap spans (and their "
                "totals) are declared separately under unattributed_spans, so this is "
                "smaller than an independent count of every span that crosses an "
                "interval edge by exactly that gap total"
            ),
            "pieces_written": ledger.boundary_piece_count,
            "intervals_estimated": ledger.estimated_interval_count,
            "telescoping": "pieces of a span re-sum to its delta, so totals are exact",
            "quantisation_kwh": measured_step
            if measured_step is not None
            else COUNTER_QUANTISATION_KWH,
            "quantisation_measurement": quantisation,
            "quantisation_fallback_used": measured_step is None,
        },
        "price_alignment": price_ledger.as_dict(),
        "gaps": gaps,
        "counter_resets": counter_resets,
        "coverage": {
            "interval_count": table.num_rows,
            "grid_samples": int(sum(table.column("sample_count").to_pylist())),
            "raw_rows_read": inputs.rows_read,
            "price_samples": len(price_series) if price_series else 0,
            "intervals_with_energy": sum(1 for v in raw_values if v is not None),
            "grid_last_interval_end": iso_z(grid_end),
            "table_allocated_kwh": round(table_allocated, 9),
            "ledger_allocated_kwh": round(ledger.allocated_kwh, 9),
            "allocated_disagreement_kwh": round(abs(table_allocated - ledger.allocated_kwh), 12),
        },
        "quality_counts": quality_counts,
        "excluded_intervals": {
            "count": len(excluded_us),
            "ranges": _merge_contiguous(excluded_us, interval_seconds * 1_000_000),
        },
    }

    # The dataset directory must hold no wall-clock value; refuse to leak one.
    build_manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode(
        "utf-8"
    )
    _assert_no_wall_clock(build_manifest_bytes)
    write_part_atomic(out_dir / "build_manifest.json", build_manifest_bytes)

    _write_run_manifest(
        root=root,
        cfg_path=cfg_path,
        t_start=t_start,
        t_end=t_end,
        dataset_id=dataset_id,
        params=params,
        row_count=table.num_rows,
        part_files=manifest["part_files"],
        quality_counts=quality_counts,
    )
    return out_dir


def _assert_no_wall_clock(data: bytes) -> None:
    """Guard the deterministic build manifest against any wall-clock value leaking in.

    The dataset directory must be byte-for-byte reproducible, so the manifest it
    carries may never mention a run timestamp.
    """
    text = data.decode("utf-8")
    for forbidden in ("started_at", "finished_at", "timestamp"):
        if forbidden in text:
            raise BuildError(
                f"build manifest unexpectedly contains {forbidden!r} — the dataset "
                "directory must carry no wall-clock value"
            )


def _write_run_manifest(
    *,
    root: Path,
    cfg_path: Path,
    t_start: datetime,
    t_end: datetime,
    dataset_id: str,
    params: dict[str, object],
    row_count: int,
    part_files: list[dict[str, object]],
    quality_counts: dict[str, dict[str, int]],
) -> Path:
    """Write the ordinary run manifest (with the wall-clock) under ``manifests/``."""
    run = new_manifest(
        source=SOURCE,
        config_path=cfg_path,
        params={
            **params,
            "dataset_id": dataset_id,
            "requested_start": iso_z(t_start),
            "requested_end": iso_z(t_end),
        },
        entities=list(GRID_COUNTER_ENTITIES) + list(PRICE_ENTITIES),
        source_window_start=iso_z(t_start),
        source_window_end=iso_z(t_end),
        started_at=iso_z(datetime.now(UTC)),
    )
    run.row_count = row_count
    run.finished_at = iso_z(datetime.now(UTC))
    run.part_files = part_files
    run.quality_flags = sorted(
        f"energy_quality={value}:{count}"
        for value, count in quality_counts["energy_quality"].items()
    )
    run.coverage_notes = (
        f"dataset_id={dataset_id}; built {row_count} interval(s) from the raw Prometheus archive."
    )
    return write_manifest(root, run)
