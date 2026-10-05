"""`HealParams`: the heal ops' params, bundled as one value.

`scratches.py` and `spots.py` each own one heal op's detection and repair;
previews and the exporter replay both, always in the same order, and fold
both into the same pixel-cache key. Before this module, that meant every
one of those call sites threading `scratches_params` and `spots_params` as
a matched pair of keyword arguments — `previews.py` alone carried the pair
through a dozen signatures. `HealParams` ends that: one bundled value, one
place that knows the replay order and the combined cache key. The `deband`
op (docs/DEBAND_PLAN.md) joined the dataclass without moving anything
downstream of here.

This module is a leaf: it imports only `scratches`, `deband`, `spots`, and
stdlib.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import TYPE_CHECKING

from scanny_boy import deband, scratches, spots

if TYPE_CHECKING:
    from scanny_boy.library.repo import EditState


@dataclasses.dataclass(frozen=True)
class HealParams:
    """The heal ops' net params for one negative — a state like `tone` or
    `color`, but bundled so callers thread one keyword instead of one per
    op. `None` means "no live op of that kind", exactly as `EditState`'s
    own `scratches`/`spots` fields mean it."""

    scratches: dict | None = None
    deband: dict | None = None
    spots: dict | None = None

    @classmethod
    def from_state(cls, state: EditState) -> HealParams:
        """`HealParams` from a negative's net `EditState`
        (`repo.net_edit_state`) — the heal fields, unpacked."""
        return cls(scratches=state.scratches, deband=state.deband, spots=state.spots)


# The no-op default: a caller with no heal state to thread passes this (or
# just leaves the keyword at its default) instead of constructing an empty
# `HealParams()` at every call site.
NONE = HealParams()


def apply(image_codes, heal: HealParams):
    """Apply the heal ops in their canonical replay order — scratch
    correction, then band removal, then spot repair — the order
    `previews._display_image` and the exporter run them in. One place
    defines the order, so the two callers can never drift apart. Scratch
    tables were fitted on un-debanded pixels, spot detection wants clean
    ones, and all three live in TIFF space ahead of any geometry."""
    image = scratches.apply(image_codes, heal.scratches)
    image = deband.apply(image, heal.deband)
    return spots.apply_repair(image, heal.spots)


def _spots_cache_key(params: dict | None) -> tuple:
    """The spot half of the pixel-cache key.

    A live spot set changes decoded pixels — its repair is the first step
    of the display replay — so the whole set is folded into the key,
    hashed rather than compared: the masks can be large and the set is
    bounded (`MAX_SPOTS`), so a hash of its canonical JSON is both cheap
    and exact. Swift's counterpart term is `EditModel.spotsTerm`
    (`repair#count#rejected`), a coarser summary of the same rule; the two
    sites are commented at each other because they cannot share a
    definition — one lives in Swift, one here."""
    if params is None:
        return (None,)
    canonical = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ("spots", digest)


def _scratches_cache_key(params: dict | None) -> tuple:
    """The scratches half of the pixel-cache key.

    A live scratches op changes decoded pixels — its correction is the
    first step of the display replay — so the whole set is folded into the
    key, hashed rather than compared.  Swift's counterpart term is
    `EditModel.scratchesTerm` (`enabled#count#stale`), a coarser summary
    of the same rule; the two sites are commented at each other."""
    if params is None:
        return (None,)
    canonical = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ("scratches", digest)


def _deband_cache_key(params: dict | None) -> tuple:
    """The deband part of the pixel-cache key.

    A live deband op changes decoded pixels, so the whole op (every
    region's table, strength, axis, enabled) is folded into the key, hashed
    rather than compared. Swift's counterpart term is
    `EditModel.debandTerm` (`enabled#strength#axis#regionCount#stale`), a
    coarser summary of the same rule; the two sites are commented at each
    other."""
    if params is None:
        return (None,)
    canonical = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ("deband", digest)


def cache_key(heal: HealParams) -> tuple:
    """The heal ops' combined pixel-cache-key contribution: the spot
    element, the scratch element, then the deband element. Adding the
    deband element **changes the key's shape** (three elements, was two);
    that is fine because the pixel cache is in-memory only, so no old key
    can ever be compared with a new one."""
    return (
        _spots_cache_key(heal.spots),
        _scratches_cache_key(heal.scratches),
        _deband_cache_key(heal.deband),
    )
