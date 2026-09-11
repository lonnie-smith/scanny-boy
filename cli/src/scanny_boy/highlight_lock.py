"""The roll-level highlight-colour lock (docs/ROLL_HIGHLIGHT_LOCK.md).

`normalization.analyze_bounds` ties the dense end's per-channel colour
(`c_floors`) to whatever the *individual negative's* brightest near-neutral
content measures — `_same_pixel_color_floor_refs`, recorded per negative as
`highlight_refs`. That is the only per-negative estimate of highlight
colour balance there is, so a negative whose brightest content is
legitimately coloured (a sunset, a tungsten interior, blue-shade snow) gets
its highlights forced toward grey, and two negatives on the same roll with
different scene content get different highlight colour balance even though
they were shot on the same film stock through the same chemistry.

This module closes that gap **without touching a published pixel**
(docs/DECISIONS.md's rule that published TIFFs never change): it derives
one roll-level highlight-colour estimate from the per-negative numbers
every stitch already records, and a render-time correction
(`corrected_floors`, applied through `color.read_metering` and consumed by
`render.py`/`tone.py`) retargets each negative's dense-end colour at
display time. The published TIFF, its `floors`/`ceils`, and everything
`normalization.py` computed at stitch time are exactly what they always
were; only the preview and export encode read something different.

## Sign convention (§-1, read this first)

Everything here is log10 *density*, not brightness. A negative's **base**
(clear film) transmits nearly all the light that hits it, so its density is
the least negative value on the roll — closest to zero. A **scene
highlight** is the *densest* silver on the negative (it blocks the most
light reaching the scanner, because a bright scene point exposed the film
hardest), so its log-density is *more negative* than base. `H` (a
negative's own highlight reference) is therefore always **below** `B`
(the roll's base density): `H - B < 0` in every channel, on any real roll.
Every gate and every sign in this module is written against that
direction; get it backwards and `compute_roll_highlight_lock` silently
returns `None` on every real roll, because every candidate fails the
amplitude gate (this shipped once, briefly, exactly that way — see
`docs/DECISIONS.md`'s entry for the fix).

## Why every ratio here is green-relative, not median-relative (§0)

`analyze_bounds` recentres its three-channel colour references on their own
**median** (`c_floors[ch] - median(c_floors)`) — the right choice there,
where the point is robustness against one channel being pulled by a single
bad measurement (COLOR_PLAN's reasoning, carried into
`docs/CAST_REMOVAL_PLAN.md`). But `median` of three numbers is not linear —
`median(a) + median(b) != median(a + b)` in general — and this module needs
to add and subtract deviation vectors (a negative's own colour deviation,
the roll's locked base colour deviation, the roll's ratio) and have the
algebra come out exact. Recentring on a **fixed reference channel**
instead — green, channel index 1, the same channel `color.cast_slopes`
already never modifies ("green is the reference channel... exposure stays
anchored") — is an ordinary linear functional (`x - x[1]`), so every
identity below holds exactly, not approximately.

## The roll estimate (§1)

For a qualifying negative — one whose `highlight_refs` (`H`) is non-null,
i.e. the per-negative neutral gate found a trustworthy near-neutral set —
the film's dense-end colour is captured as a **ratio to green**, not an
absolute offset: `k_ch = (H_ch - B_ch) / (H_G - B_G)`, where `B` is the
roll's locked film-base density (`roll.film_base["density"]`, the same
anchor `stitch_pipeline._locked_base_refs` feeds into `analyze_bounds`'
thin end) and `G` is green's channel index. Film layers carry different
contrasts, so a negative's overall highlight *amplitude* above base
(`H_G - B_G`, always negative — §-1) legitimately varies with exposure and
scene brightness; the *ratio* between channels does not — it is a property
of the dye set, constant for the roll. `k_ch` is undefined (skipped) when
`H_G - B_G` is not sufficiently negative (within `_MIN_AMPLITUDE` of zero,
or — a measurement failure — positive): the green channel shows no
measurable highlight density above base, so there is nothing to form a
ratio from.

The roll's `K_ch` is the **median over qualifying negatives** of `k_ch` —
robust to one negative's meter having latched something unusual (an
ordinary median over independent samples — no linearity requirement here,
this axis is never added to anything). `K_1` (green) is always exactly
`1.0`. A roll with no qualifying negative — every gate fell back, or there
is no locked film base yet — has no lock, and the render path is the
identity (§3).

`HighlightLock` also carries `base` — the roll's locked film-base density,
copied verbatim from `roll.film_base["density"]` at the moment the lock was
built. `corrected_floors` needs an *absolute* density reference to recover
a negative's own highlight amplitude (§2); `k` alone (a ratio, with no
absolute level) cannot supply one.

`base` by itself is not always the right absolute reference for a *given*
negative, though: the base frame is shot once, and a negative's own capture
can drift from it (a dimmed light panel, most plausibly) or use a different
exposure than the base frame's own EXIF (§3). `base_offset_for` resolves
that per negative and returns the scalar `corrected_floors`/
`compute_roll_highlight_lock` add to `base` before using it — see its own
docstring for the order (measured drift, then matched EXIF, then no
correction).

## The render-time correction (§2)

`corrected_floors` retargets one negative's *recorded* `floors` (the
per-channel dense-end bound the published pixels were actually stretched
with — never `unclamped_floors`, which is not what the pixels saw) to the
roll's `K`, while holding two things fixed: the negative's own green-channel
floor (so a corrected negative's brightness reference is unchanged, and —
see the identity property below — this is also what makes green itself
never move) and the negative's own highlight *amplitude* above base (so a
strongly cross-processed or unusually exposed negative is not forced to
the roll's typical contrast, only its typical hue).

Define, relative to green: `own_dev[ch] = floors[ch] - floors[G]` and
`base_dev[ch] = ceils[ch] - ceils[G]`. Because `analyze_bounds` builds
`floors[ch] = mean_lf + (F[ch] - median(F))` (`F` is whichever per-channel
reference this negative's dense end actually used — the gated `H` when the
neutral gate succeeded, the plain-percentile fallback when it did not) and
`ceils[ch] = mean_lc + (B[ch] - median(B))`, the `mean_lf`/`mean_lc` luma
terms and the `median(F)`/`median(B)` recentring terms are each a *common
additive constant across channels* — so they cancel exactly in the
green-relative subtraction: `own_dev[ch] = F[ch] - F[G]` and
`base_dev[ch] = B[ch] - B[G]`, **exactly**, with no `median`-of-three
anywhere, **and no `H` needed** — `own_dev`/`base_dev` come from the
already-recorded `floors`/`ceils` alone, for *every* negative, qualifying
or not.

The one quantity that genuinely needs `H` is the amplitude, and it must
come from `H` directly — **not from fitting the negative's own R/B
deviations against `(K - 1)`.** An earlier version of this module
estimated the amplitude by least-squares projection of `own_dev -
base_dev` onto `(K - 1)`; that is wrong, and not merely approximately so.
For typical colour negative film `K` is close to `(0.9, 1.0, 1.1)`
(REBATE_ANCHORING's own base-density asymmetry), so `K - 1` is
approximately the pure red–blue axis — exactly where a warm/cool scene
cast (sunset, tungsten, open shade) *lives*. A projection onto `(K - 1)`
keeps whatever part of the scene's own cast already lies along that axis
and *only* corrects the part perpendicular to it — so a real warm/cool
cast on a qualifying negative could pass through the "correction"
essentially untouched (verified numerically: a cast of `(+0.08, 0,
-0.08)` — exactly along a `(0.9, 1, 1.1)`-shaped `(K - 1)` — survived the
projection formula unchanged in both the before and after states, while a
cast of `(+0.08, 0, +0.08)`, perpendicular to it, was fully removed). That
is backwards: the whole point of this feature is to retarget a negative's
highlight *hue*, and a fitted amplitude built from the negative's own hue
cannot do that when the roll's own colour axis and the cast's axis
coincide, which for real colour film they typically do.

**Qualifying negative** (`H` recorded): the amplitude is read directly off
the measurement, no fitting:

```
a = H_G - lock.base_G                       # this negative's own reading
dev_new[ch] = base_dev[ch] + a * (K[ch] - 1)
floor_new[ch] = floors[G] + dev_new[ch]
```

This is exact, and — because `a` is a real measurement, not a fit — proven
below to reduce to `own_dev` exactly whenever this negative's own ratio
already equals `K`, with **no approximation error at any step**: unlike
the retired projection, this formula fully replaces the highlight colour
with the roll's `K`, scaled by this negative's own real amplitude,
regardless of which axis the negative's own cast happened to sit on.

**Non-qualifying negative** (`H` is `None` — the gate fell back to plain
percentiles): there is no recorded `H` to read an amplitude from, and
reading one from the negative's own R/B `floors` would reintroduce exactly
the bug just described (an amplitude built from the untrustworthy colour
this correction exists to override). Instead, the amplitude is
approximated from the **green channel alone** — no R or B read at all:

```
a_approx = floors[G] - lock.base_G
```

Substituting `floors[G] = mean_lf + (F[G] - median(F))` and
`lock.base_G = B_G`, `a_approx = (mean_lf - B_G) + (F[G] - median(F))`,
while the quantity it stands in for is `a_true = F[G] - B_G` (the
analogous "real" amplitude, defined the same way the qualifying branch's
`a` is, just against the untrustworthy `F` instead of a validated `H`).
The error is exactly `a_approx - a_true = mean_lf - median(F)`: the
negative's overall near-densest scene luma percentile (`mean_lf`,
`BASE_LUMA_CLIP` = 0.01% of the *whole frame*, weighted) against the
median of this negative's own three (possibly untrustworthy) per-channel
dense-end references. Both are drawn from the same physical region of the
frame — the near-extreme dense tail — so the two are typically close, and
the error shrinks as the fallback's own chroma shrinks (a fallback with a
small red/green/blue spread has `median(F)` close to every channel,
`F[G]` included). It grows with the fallback's own chroma spread — which
is to say, it is weakest exactly where the correction has the least to
gain anyway (a near-neutral fallback needs little retargeting) and
strongest on a genuinely colourful highlight, where even an imperfectly
calibrated *magnitude* still moves the colour in the *roll's own,
correctly measured* direction, `(K - 1)`, which is the property this
feature is chiefly about. This is documented as an approximation, not
silently assumed exact; a future measurement pass could bound
`mean_lf - median(F)` empirically across real rolls, which is out of this
plan's scope.

Either branch's `a` is gated the same way the roll estimate's own
`k_ch` is (§1): not sufficiently negative (within `_MIN_AMPLITUDE` of
zero, or positive) means no trustworthy amplitude, and the negative's
`floors` are returned unchanged — no correction is better than a
division-by-near-zero one.

At `ch == G` in either branch: `floor_new[G] = floors[G] + base_dev[G] + a
* (K[G] - 1) = floors[G] + 0 + a * 0 = floors[G]`: **green's own floor is
left exactly as recorded**, for every negative, correction or not, the
same guarantee `color.cast_slopes` gives the display curve.

**Identity property, and why it is exact** (qualifying branch only — the
non-qualifying branch has no "own ratio" to compare against, by
definition): when a qualifying negative's own `k` already equals the
roll's `K`, i.e. `(H_ch - B_ch) / (H_G - B_G) = K_ch` for every channel,

```
dev_new[ch] = base_dev[ch] + a * (K[ch] - 1)
            = (B_ch - B_G) + (H_G - B_G) * K_ch - (H_G - B_G)
            = (B_ch - B_G) + (H_ch - B_ch) - (H_G - B_G)   # since (H_G-B_G)*K_ch = H_ch-B_ch
            = H_ch - H_G
            = own_dev[ch]
```

— exactly, algebraically, no fitting residual — so `floor_new == floor_old`
for every channel, to the bit
(`highlight_lock_test.py::test_identity_when_negative_matches_roll`).

**Degenerate `K`** no longer needs its own case: because `a` is read (or
approximated) directly rather than fitted, there is no `dot(K - 1, K - 1)`
denominator to vanish. A roll whose `K` carries no colour variation at all
(`K` uniformly `1.0`) simply produces `dev_new = base_dev` for every
negative — the dense end tied to the film base's own colour, no cast —
which is what it should mean physically (a roll with no measurable
highlight cast has nothing to retarget toward).

## What is not corrected, and why (§3)

Only the **dense end**. The thin end (`ceils`) is already the roll's own
locked film base (REBATE_ANCHORING §4) — there is nothing per-negative
about it to lock. Mono rolls: `compute_roll_highlight_lock` returns `None`
by construction (`measure_highlight_refs` never records `highlight_refs`
for a single-channel grid, so no negative on a mono roll ever qualifies),
and `corrected_floors` is a no-op on anything but a 3-channel `floors`.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import RollManifest

# The green channel's index in every 3-channel colour array this module
# touches (`floors`, `ceils`, `highlight_refs`, `film_base["density"]`) —
# the codebase's RGB convention throughout (`normalization.LUMA_R/G/B`,
# `MONO_MERGE_WEIGHTS`). See §0 for why green, specifically, is the fixed
# reference every ratio and deviation here is taken against.
_GREEN = 1

# Bumped whenever the roll-estimate arithmetic changes in a way that makes
# an old `k` not comparable with a freshly computed one — the same role
# `NORMALIZE_FORMAT_VERSION` plays for the per-negative meters. Recorded,
# not currently read back by anything: the estimate is recomputed from the
# roll's negatives on every run (§4 of the plan doc), so a stale version
# only matters if a future reader wants to know whether to trust a value it
# did not just compute itself.
HIGHLIGHT_LOCK_MEASURE_VERSION = 2

# An amplitude (green-channel density above base, §-1: always negative on
# real film) closer to zero than this is not a highlight reading — the
# green channel shows no measurable density above base (a measurement
# failure, or content that never got denser than clear film) — and a
# *positive* amplitude is impossible on real film, a sign that something
# upstream is wrong. log10 D units, deliberately tiny relative to a real
# amplitude (commonly a full stop or more, ~0.3+): this guards only
# against division by ~zero or a sign flip, not against a legitimately
# faint highlight.
_MIN_AMPLITUDE = 1e-4


def _qualifying_amplitude(value: float) -> bool:
    """§-1/§1's gate, shared by the roll estimate and the render-time
    correction: an amplitude must be negative and at least `_MIN_AMPLITUDE`
    away from zero to be trusted."""
    return value <= -_MIN_AMPLITUDE


@dataclasses.dataclass(frozen=True)
class HighlightLock:
    """The roll's highlight-colour estimate (§1): `k` is the per-channel
    ratio to green, `(H - B) / (H_G - B_G)`, median over `qualifying_count`
    negatives whose own `highlight_refs` measurement was trustworthy.
    `k[1]` (green) is always exactly `1.0`. `base` is the roll's locked
    film-base density this `k` was measured against — `corrected_floors`
    needs it as an absolute reference point (§1/§2)."""

    k: tuple[float, float, float]
    base: tuple[float, float, float]
    qualifying_count: int
    measure_version: int = HIGHLIGHT_LOCK_MEASURE_VERSION

    def to_dict(self) -> dict:
        return {
            "k": list(self.k),
            "base": list(self.base),
            "qualifying_count": self.qualifying_count,
            "measure_version": self.measure_version,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> HighlightLock | None:
        """Never raises: a malformed or foreign-shaped block reads back as
        `None` (no correction), the same posture `color.read_metering`
        takes on a malformed `normalization` block."""
        if not isinstance(data, dict):
            return None
        k = data.get("k")
        base = data.get("base")
        if not isinstance(k, list) or len(k) != 3:
            return None
        if not isinstance(base, list) or len(base) != 3:
            return None
        try:
            k_values = tuple(float(v) for v in k)
            base_values = tuple(float(v) for v in base)
        except (TypeError, ValueError):
            return None
        if not all(np.isfinite(v) for v in (*k_values, *base_values)):
            return None
        qualifying_count = data.get("qualifying_count")
        if isinstance(qualifying_count, bool) or not isinstance(qualifying_count, int):
            return None
        return cls(
            k=k_values,
            base=base_values,
            qualifying_count=qualifying_count,
            measure_version=int(
                data.get("measure_version", HIGHLIGHT_LOCK_MEASURE_VERSION)
            ),
        )


def _locked_base_density(roll: RollManifest) -> np.ndarray | None:
    block = roll.film_base
    if block is None:
        return None
    density = block.get("density")
    if not density or len(density) != 3:
        return None
    try:
        values = np.asarray([float(v) for v in density], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(values)):
        return None
    return values


def base_offset_for(record: dict | None) -> float | None:
    """docs/ROLL_HIGHLIGHT_LOCK.md §3: the scalar to add to `lock.base`
    (every channel alike — a common-mode level, not a colour) to get
    *this negative's* base reference, `B_scan`. In order:

    1. `base_check.level_offset` when present — a measured drift (this
       negative's own rebate density against the roll anchor), which
       catches things EXIF cannot (light-panel dimming, for one).
    2. `0.0` when the negative's `exposure_matched` flag is `True` — no
       measured drift, but the base frame's absolute level applies as
       recorded because the EXIF shutter/aperture/ISO matched.
    3. `None` otherwise — no trustworthy absolute level for this
       negative; the caller must not correct it."""
    if not record:
        return None
    base_check = record.get("base_check")
    if isinstance(base_check, dict):
        level_offset = base_check.get("level_offset")
        if isinstance(level_offset, (int, float)) and not isinstance(level_offset, bool):
            if np.isfinite(level_offset):
                return float(level_offset)
    if record.get("exposure_matched") is True:
        return 0.0
    return None


def compute_roll_highlight_lock(roll: RollManifest) -> HighlightLock | None:
    """Recompute the roll's highlight-colour lock from its negatives'
    recorded `normalization.highlight_refs` (§1). Pure and cheap — it reads
    already-recorded per-negative numbers, no image I/O — so it is safe to
    call on every run and every negative removal (`stitch_pipeline.py`,
    `edits.py`'s delete path); it must be, because the qualifying set
    changes whenever a negative is added or removed.

    Returns `None` when the roll has no locked film base (nothing to
    measure ratios against — REBATE_ANCHORING §3.2's run gate ensures a
    roll cannot have published negatives without one, but a roll can be
    inspected before its first stitch), or when no negative qualifies
    (mono roll, every gate fell back, or a degenerate green amplitude on
    every candidate)."""
    base = _locked_base_density(roll)
    if base is None:
        return None

    ratios: list[np.ndarray] = []
    for negative in roll.negatives:
        block = negative.normalization
        if not block:
            continue
        offset = base_offset_for(block)
        if offset is None:
            continue
        refs = block.get("highlight_refs")
        if not isinstance(refs, list) or len(refs) != 3:
            continue
        try:
            h = np.asarray([float(v) for v in refs], dtype=np.float64)
        except (TypeError, ValueError):
            continue
        if not np.all(np.isfinite(h)):
            continue
        diff = h - (base + offset)
        amplitude = float(diff[_GREEN])
        if not _qualifying_amplitude(amplitude):
            continue
        k = diff / amplitude
        if not np.all(np.isfinite(k)):
            continue
        ratios.append(k)

    if not ratios:
        return None

    k_roll = np.median(np.stack(ratios, axis=0), axis=0)
    return HighlightLock(
        k=(float(k_roll[0]), float(k_roll[1]), float(k_roll[2])),
        base=(float(base[0]), float(base[1]), float(base[2])),
        qualifying_count=len(ratios),
    )


def corrected_floors(
    floors: tuple[float, ...],
    ceils: tuple[float, ...],
    lock: HighlightLock | None,
    highlight_refs: tuple[float, ...] | None = None,
    base_offset: float | None = None,
) -> tuple[float, ...]:
    """One negative's dense-end bound, retargeted to the roll's highlight
    colour (§2). Identity — `floors` returned unchanged — whenever there is
    nothing to correct: no lock, no `base_offset` (§3 — this negative has no
    trustworthy absolute base level, see `base_offset_for`), a mono negative
    (`len(floors) != 3`), a malformed input, or a green-channel amplitude
    too close to zero to trust. Never raises.

    `highlight_refs` is this negative's own recorded (unmodified) measurement
    — pass it whenever it is available (a qualifying negative); omit it (or
    pass `None`) for a negative whose own neutral gate fell back, which
    takes the documented green-only approximation instead (§2).

    `base_offset` (§3) is added to `lock.base` — every channel alike, a
    common-mode level shift — to get *this negative's* base reference
    (`base_offset_for` derives it: a measured rebate drift, or `0.0` when
    the negative's EXIF exposure matched the base frame's)."""
    if (
        lock is None
        or base_offset is None
        or len(floors) != 3
        or len(ceils) != 3
    ):
        return tuple(floors)

    floors_arr = np.asarray(floors, dtype=np.float64)
    ceils_arr = np.asarray(ceils, dtype=np.float64)
    if not (np.all(np.isfinite(floors_arr)) and np.all(np.isfinite(ceils_arr))):
        return tuple(floors)
    if not np.isfinite(base_offset):
        return tuple(floors)

    base_green = lock.base[_GREEN] + base_offset

    if highlight_refs is not None and len(highlight_refs) == 3:
        try:
            h_green = float(highlight_refs[_GREEN])
        except (TypeError, ValueError):
            return tuple(floors)
        if not np.isfinite(h_green):
            return tuple(floors)
        amplitude = h_green - base_green
    else:
        # Non-qualifying negative: the green-only approximation (§2). Reads
        # no R/B colour — only this negative's own recorded green floor and
        # the roll's exact green base density.
        amplitude = float(floors_arr[_GREEN]) - base_green

    if not _qualifying_amplitude(amplitude):
        return tuple(floors)

    own_dev = floors_arr - floors_arr[_GREEN]
    base_dev = ceils_arr - ceils_arr[_GREEN]
    k_dev = np.asarray(lock.k, dtype=np.float64) - 1.0
    dev_new = base_dev + amplitude * k_dev
    floor_new = floors_arr[_GREEN] + dev_new

    # Safety net, same posture as `clamp_bounds`' degenerate-clamp discard:
    # a correction must never invert or degenerate a channel's stretch.
    # Per channel, not per negative — one channel's correction landing on
    # the wrong side of `ceils` must not throw away the other two channels'
    # otherwise-good correction. Green is never a candidate here in
    # practice — `dev_new[_GREEN] == 0` always — but the loop still runs
    # over it for symmetry and because the guard is cheap.
    result = list(floors)
    for ch in range(3):
        candidate = float(floor_new[ch])
        if not np.isfinite(candidate):
            continue
        if candidate >= ceils_arr[ch]:
            continue
        result[ch] = candidate
    return tuple(result)
