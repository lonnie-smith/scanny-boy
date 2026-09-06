# Punchlist

Deferred work, each with an attachment point so it can be picked up without
re-deriving context.

## Monochrome merge weights: measure the real per-channel sigma (docs/MONOCHROME_PLAN.md §8)

`normalization.MONO_MERGE_WEIGHTS = (0.25, 0.50, 0.25)` is a minimum-variance
estimator built from a Bayer CFA's site counts (green has roughly twice the
photon count of red or blue), not from a measurement of this rig's actual
per-channel noise. Two effects push the real gain over green-only lower
than the naive `sqrt(2)`: demosaicing correlates the channels (R and B at a
green site are partly interpolated *from* green), and R/B carry worse
post-demosaic MTF than G. Measuring the real per-channel sigma from the
flat-field calibration frames — which is exactly what they exist for — and
re-deriving the weights from it is worth doing, but needs its own
measurement protocol; it was explicitly out of scope for
docs/MONOCHROME_PLAN.md.
