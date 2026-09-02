"""Lateral chromatic aberration: the half-size per-channel fit, the mode
decision, and the acceptance gates (docs/GEOMETRIC_PLAN.md section 4.6).

The fit decodes at half size (`RAW_PARAMS_HALF_SIZE`): each output pixel
then comes from one Bayer quad with no interpolation, so the per-channel
geometry is true rather than smeared by the demosaic. Because
`K_half = K_full / 2`, normalised coordinates are identical at both
resolutions — nothing here is ever scaled back up (section 0.7).

The model is radial in normalised camera coordinates, per channel:

    r_c = r_g * (c0 + c1*r_g^2 + c2*r_g^4)

with the channel's own centre fitted alongside the coefficients —
decentring is plausible on an FTZ-adapted manual lens, and an assumed
centre shows up as a spurious tangential component the radial model
cannot absorb. Coefficients and centres are offsets from the principal
point, in normalised units, exactly as the profile records them.

Direction discipline (section 3.3): the fit measures where the red corner
*is* (`r_R = c0 * r_G`); the decoder is told what to *multiply* red by to
put it back (`red_scale = 1 / c0`). The test suite proves the round trip.

Every constant in this module is defined here and nowhere else.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from scipy.optimize import least_squares

from scanny_boy.events import Code
from scanny_boy.raw_decode import RAW_PARAMS

# The CA fit's decode deviates from production's `RAW_PARAMS` in exactly
# one documented way: `half_size=True`. Each output pixel then comes from
# one Bayer quad with no demosaic interpolation across channels, so the
# per-channel geometry is measured, not smeared (section 4.6). Defined here
# as an explicit derivation so a change to `RAW_PARAMS` cannot silently
# un-derive it.
RAW_PARAMS_HALF_SIZE: dict = {**RAW_PARAMS, "half_size": True}

# "scale" mode is enough when the radial terms contribute less than this
# many full-resolution pixels at the image corner (section 4.6).
CA_SCALE_ONLY_PX = 0.05
# Acceptance: residual misregistration after correction, in full-resolution
# pixels at the frame corners (section 4.6).
CA_RESIDUAL_ACCEPT_PX = 0.3
# Acceptance: the correction must improve on the uncorrected figure by at
# least this relative fraction.
CA_MIN_IMPROVEMENT_FRACTION = 0.30


class CAFitError(Exception):
    """Degenerate CA input — no corners survived in all three channels.
    The orchestrator maps this onto the flat-field family's error type."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ChannelFit:
    """One channel's radial model in normalised camera coordinates. The
    centre is an offset from the principal point."""

    c0: float
    c1: float
    c2: float
    center_x: float
    center_y: float

    def scale_at(self, r: float) -> float:
        return self.c0 + self.c1 * r**2 + self.c2 * r**4

    def forward(self, points: np.ndarray) -> np.ndarray:
        """Green-space normalised points -> this channel's observed
        position: the map composite's band map applies in "maps" mode
        (section 5.3, step 3)."""
        centre = np.array([self.center_x, self.center_y])
        delta = points - centre
        r = np.hypot(delta[:, 0], delta[:, 1])
        s = self.scale_at(r)
        return centre + delta * s[:, np.newaxis]

    def inverse(self, points: np.ndarray) -> np.ndarray:
        """This channel's observed position -> green-space, exactly for a
        pure scale and by fixed-point iteration otherwise. This is how the
        acceptance measurement undoes the model on real observed corners."""
        centre = np.array([self.center_x, self.center_y])
        delta = points - centre
        result = points.copy()
        for _ in range(16):
            s = self.scale_at(np.hypot(*(result - centre).T))
            candidate = centre + delta / s[:, np.newaxis]
            if np.max(np.abs(candidate - result)) < 1e-12:
                return candidate
            result = candidate
        return result


@dataclasses.dataclass(frozen=True)
class CAFitResult:
    mode: str  # "scale" | "maps"
    red: ChannelFit
    blue: ChannelFit
    # Present only in "scale" mode: the values handed to rawpy's
    # `chromatic_aberration` — the reciprocals of c0 (section 3.3).
    red_scale: float | None
    blue_scale: float | None
    misregistration_before_px: dict[str, float]
    misregistration_after_px: dict[str, float]
    radial_term_px_at_corner: dict[str, float]
    accepted: bool
    rejection_reason: str | None


def _fit_channel(green_norm: np.ndarray, channel_norm: np.ndarray) -> ChannelFit:
    """Fit `(c0, c1, c2, center_x, center_y)` for one channel against its
    green correspondences. Both arrays are normalised points relative to
    the principal point: `green_norm` is green-space, `channel_norm` is the
    channel's undistorted observed position."""

    def residuals(p: np.ndarray) -> np.ndarray:
        fit = ChannelFit(*p)
        centre = np.array([fit.center_x, fit.center_y])
        delta = channel_norm - centre
        r_c = np.hypot(delta[:, 0], delta[:, 1])
        r_g = np.hypot(green_norm[:, 0], green_norm[:, 1])
        return r_c - r_g * fit.scale_at(r_g)

    ratio = np.hypot(channel_norm[:, 0], channel_norm[:, 1]) / np.hypot(
        green_norm[:, 0], green_norm[:, 1]
    )
    c0_start = float(np.median(ratio[np.isfinite(ratio) & (ratio > 0)]))
    if not np.isfinite(c0_start) or c0_start <= 0:
        c0_start = 1.0

    result = least_squares(
        residuals,
        np.array([c0_start, 0.0, 0.0, 0.0, 0.0]),
        loss="huber",
        f_scale=0.001,
    )
    return ChannelFit(*result.x)


def _corner_points(green_norm: list[np.ndarray], count: int = 4) -> np.ndarray:
    """The detected corners nearest each of the four image corners, pooled
    over the held-out frames — where the acceptance measurement happens
    (section 4.6: "at the frame corners"). Normalised units."""
    frame_corner_radius = float(np.hypot(0.5, 0.5))
    picked = []
    for points in green_norm:
        if len(points) == 0:
            continue
        radii = np.hypot(points[:, 0], points[:, 1])
        near = points[radii > 0.7 * frame_corner_radius]
        if len(near) > 0:
            picked.append(near)
    if not picked:
        pooled = np.concatenate(green_norm, axis=0)
        radii = np.hypot(pooled[:, 0], pooled[:, 1])
        order = np.argsort(radii)[::-1][:count]
        return pooled[order]
    return np.concatenate(picked, axis=0)


def _misregistration(
    fit: ChannelFit,
    green_corners: list[np.ndarray],
    channel_corners: list[np.ndarray],
    fx: float,
) -> tuple[float, float]:
    """Mean R–G (or B–G) displacement at the frame corners, before and
    after the correction, in full-resolution pixels. "After" inverts the
    model on the channel's own observed corners — the honest residual,
    including model error."""
    # Match each green corner to the nearest same-frame channel corner.
    before: list[float] = []
    after: list[float] = []
    for points_g, points_c in zip(green_corners, channel_corners, strict=True):
        if len(points_c) == 0:
            continue
        near_green = _corner_points([points_g])
        if len(near_green) == 0:
            continue
        dist = np.hypot(
            near_green[:, 0, np.newaxis] - points_c[np.newaxis, :, 0],
            near_green[:, 1, np.newaxis] - points_c[np.newaxis, :, 1],
        )
        nearest = points_c[dist.argmin(axis=1)]
        before.extend(np.hypot(*(nearest - near_green).T))
        corrected = fit.inverse(nearest)
        after.extend(np.hypot(*(corrected - near_green).T))
    if not before:
        raise CAFitError(Code.CHROMATIC_FIT_REJECTED, "no corner correspondences survived")
    return float(np.mean(before)) * fx, float(np.mean(after)) * fx


def fit_ca(
    train: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    heldout: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    frame_width: int,
    frame_height: int,
    geometry: tuple[float, float, float, float] | None,
) -> CAFitResult:
    """Fit and gate the CA model (section 4.6).

    Each frame tuple is `(red, green_for_red, blue, green_for_blue)` — the
    channel's *undistorted* corner positions in normalised coordinates
    relative to the principal point, intersected by id across the three
    channels and undistorted with the accepted green coefficients (or zero
    ones when geometry was rejected). Each channel pair carries its own
    id-aligned green corners, because a corner missing in blue but present
    in red must not shift the red fit's correspondences. `frame_width`/
    `frame_height` set the pixel scale for reporting. `geometry` is
    `(k1, k2, cx, cy)` — accepted here only as provenance for the caller;
    the undistortion itself has already happened upstream.
    """
    if not train:
        raise CAFitError(Code.CHROMATIC_FIT_REJECTED, "no frames survived CA detection")

    # Normalised coordinates are resolution-independent, so this is the
    # *full*-resolution fx: every pixel figure this module reports — the
    # mode-decision radial term and the misregistration measurements — is
    # expressed in full-resolution pixels (section 4.6).
    fx = float(max(frame_width, frame_height))

    red_fit = _fit_channel(
        np.concatenate([frame[1] for frame in train]),
        np.concatenate([frame[0] for frame in train]),
    )
    blue_fit = _fit_channel(
        np.concatenate([frame[3] for frame in train]),
        np.concatenate([frame[2] for frame in train]),
    )

    # Mode decision: the radial terms' contribution in full-resolution
    # pixels at the image corner (section 4.6).
    corner_r = float(np.hypot(0.5, 0.5))
    radial = {
        "red": abs(red_fit.scale_at(corner_r) - red_fit.c0) * corner_r * fx,
        "blue": abs(blue_fit.scale_at(corner_r) - blue_fit.c0) * corner_r * fx,
    }
    if max(radial.values()) < CA_SCALE_ONLY_PX:
        mode = "scale"
        red_scale = 1.0 / red_fit.c0
        blue_scale = 1.0 / blue_fit.c0
    else:
        mode = "maps"
        red_scale = None
        blue_scale = None

    red_before, red_after = _misregistration(
        red_fit, [f[1] for f in heldout], [f[0] for f in heldout], fx
    )
    blue_before, blue_after = _misregistration(
        blue_fit, [f[3] for f in heldout], [f[2] for f in heldout], fx
    )

    before = {"red": red_before, "blue": blue_before}
    after = {"red": red_after, "blue": blue_after}

    def clears_gate(channel: str) -> bool:
        residual_ok = after[channel] < CA_RESIDUAL_ACCEPT_PX
        # No aberration measured means nothing was corrected: an
        # unimproved (or unmeasurable) fit is dropped, not carried.
        improved = before[channel] > 0 and (
            before[channel] - after[channel]
        ) / before[channel] >= CA_MIN_IMPROVEMENT_FRACTION
        return residual_ok and improved

    accepted = all(clears_gate(channel) for channel in ("red", "blue"))
    rejection_reason = None
    if not accepted:
        rejection_reason = (
            f"held-out misregistration red {before['red']:.2f}px -> "
            f"{after['red']:.2f}px, blue {before['blue']:.2f}px -> "
            f"{after['blue']:.2f}px; the gates need residual < "
            f"{CA_RESIDUAL_ACCEPT_PX}px and >= "
            f"{CA_MIN_IMPROVEMENT_FRACTION * 100:.0f}% improvement"
        )

    return CAFitResult(
        mode=mode,
        red=red_fit,
        blue=blue_fit,
        red_scale=red_scale if mode == "scale" else None,
        blue_scale=blue_scale if mode == "scale" else None,
        misregistration_before_px=before,
        misregistration_after_px=after,
        radial_term_px_at_corner=radial,
        accepted=accepted,
        rejection_reason=rejection_reason,
    )


def channel_ca_px(
    green_norm: list[np.ndarray], other_norm: list[np.ndarray], fx: float
) -> float:
    """Mean displacement between one channel's corners and green's, pooled
    over frames, in full-resolution pixels — the shared measurement behind
    the report's `detection_channel_ca_px` (section 4.6: it gates
    nothing; it exists so the detect-on-green question can be settled
    with a number later)."""
    displacements: list[float] = []
    for points_g, points_c in zip(green_norm, other_norm, strict=True):
        n = min(len(points_g), len(points_c))
        if n == 0:
            continue
        displacements.extend(np.hypot(*(points_c[:n] - points_g[:n]).T))
    if not displacements:
        return 0.0
    return float(np.mean(displacements)) * fx
