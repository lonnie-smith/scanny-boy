# CROP_PLAN — the `crop` op and the Edit tab's crop mode

The Edit tab gains a crop mode in the Geometry sidebar: a tilted crop
window drawn over the preview, constrained to one of the film-format
aspect ratios (or free), recorded as a nondestructive `crop` op and baked
only at export. Protocol version 18 → 19.

## 0. The op

`crop` is a **state op** in the same family as `tone`/`color`/`spots` —
the latest op wins, `--reset` clears. Its params live in **published-TIFF
pixels**, the same convention the `spots` op chose ("TIFF-space geometry
does not move when the display transform does", `repo.net_edit_state`):

```json
{"canvas": [w, h], "x": int, "y": int, "w": int, "h": int,
 "tilt_deg": number, "preset": string | null}
```

- `tilt_deg` is the window's **counter-clockwise tilt as displayed**. The
  CLI accepts ±45 (`CROP_TILT_MAX_DEG`; wider than the app's ±10 slider
  because a re-crop composed over an existing tilt adds algebraically);
  the app's slider is ±10 in 0.1° steps.
- The window is the axis-aligned rect `x/y/w/h`; the tilt tilts the
  *window*, and the crop **removes** the tilt: the output shows the
  window's content upright. The pixel semantics (`previews.apply_crop`):
  warp the image about the rect's centre by the stored tilt
  (`cv2.getRotationMatrix2D(centre, tilt, 1)` — cv2's positive angle is
  counter-clockwise, matching the convention), uncovering to the
  stitching fill sentinel exactly as `auto_rotate.rotate_with_fill`
  does, then slice the rect. The window a warp with angle `t` samples is
  the rect rotated counter-clockwise by `t` about its own centre.
- `canvas` guards a re-stitch (SPOTTING_PLAN §1.5's rule): a crop
  recorded against different TIFF dimensions is stale and ignored
  everywhere (`previews.crop_is_live`), degrading to the full frame. It
  degrades silently — unlike a spot set, a vanished crop changes no
  pixels — and `roll info` simply reports no crop.
- `preset` is a display label the app stores for sidebar continuity;
  nothing reads it back for geometry.

The size floor is 16×16 (`CROP_MIN_SIZE_PX`); a smaller window is a
mis-click.

## 1. Why the window is stored fully-composed

The user may crop twice: the second crop is drawn over the *first crop's
display*, and the ops log must still reduce to one meaningful window. So
`run_edit_crop` maps the drawn rect + tilt **backwards through the net
state** (`previews.display_crop_window_to_tiff`: quarter turns, then the
fine rotation, then the flip, then the live crop's own warp, each
inverted on the tilted rect's four corners) into one fully-composed
TIFF-space window. The replay therefore never composes crops — the
pipeline has exactly one crop step, and every later transform (a flip the
user records *after* a crop, say) lands on the cropped frame wholesale,
which is what the user expects.

Two rotations about different centres compose to one rotation plus a
translation, so any sequence of tilted crops *is* a single tilted window
in the original image — the composition is always expressible, which is
why this design closes.

Rounding: the mapped corners are fractional (the drawn tilt and any fine
rotation interpolate); the stored rect rounds to whole pixels with the
centre as the mapped corner average and extents as index distances + 1
(the same floor/ceil+1 convention `tiff_rect_to_display`'s bounding box
uses). Compositional error stays sub-pixel, the same order as the fine
rotation's own interpolation.

## 2. The replay order

`_display_image` (and `exporter.apply_edits`, pixel-for-pixel):

    spot repair → crop → flip → fine rotation → quarter turns

The crop sits right after the repair because both ops' coordinates are
TIFF space; the flip/fine/turns after it apply to the cropped frame.
With a live crop the display bounds are the window's dimensions (swapped
by odd net turns — `previews.display_shape`), which is what `roll info`'s
`crop` report names and what `edit render-region` then works in (its
exact fast path is disabled while a crop is live — the crop's own warp
interpolates, so the full-decode path runs, as it already does for the
fine rotation and a live repair).

The pixel-cache key (`cached_preview_codes`) gains the window
(rect + tilt); tone/colour stay out of it as ever.

## 3. Wire changes (protocol 19)

- `edit crop --roll DIR --negative ID (--x --y --width --height [--tilt]
  [--preset NAME] | --reset)` — one negative (crop is per-frame; the
  selection ops stay rotate/flip/delete). Validates the whole intent
  before writing: display-fit, size floor, tilt ceiling; the composed
  rect is clamped into the canvas (a fine rotation's fill wedge means
  the display can legitimately show sentinel pixels at its edges).
- `edit_recorded` gains `crop` on **every** edit confirmation — the net
  crop as a display-space report `{width, height, tilt_deg, preset}` or
  `null` — so Swift overwrites without caring which op was recorded
  (the same "always the net" treatment `rotation_quarter_turns` gets).
- `roll info`'s per-negative block gains the same `crop` field. Swift
  needs no rect — the preview it shows is already cropped, and a fresh
  crop session draws a new rect over it; it needs the cropped display
  dimensions (zoom/fit), the tilt (sidebar continuity), and the preset.
- `events.PROTOCOL_VERSION` 18 → 19; `schema.json` const, command enum,
  and the `edit_recorded` branch gain the field; CONTRACT.md updated.

## 4. Interactions

- **Spots.** The repair is replayed *before* the crop, so healing works
  unchanged on cropped negatives — but the marker overlay hides while a
  live crop exists (`_spots_for_report` reports no markers): over a
  cropped-and-tilted display the axis-aligned marker rects have no
  faithful drawing, and the Heal panel's counts still read from the
  manifest summary. Removing the crop brings the markers back.
- **Export.** The crop bakes at export (`apply_edits`): the exported JXL
  contains only the window's pixels, its dimensions the cropped
  display's, and the XMP provenance's `rendered.crop` records the window
  — the published TIFF beside the export still holds the full frame, and
  the record says which part of it the file is. The published TIFF is
  never touched, exactly like every other edit.
- **Previews.** The `crop` op joins the state ops that regenerate the
  preview from the net state (a tilted window is a warp; the incremental
  lossless path cannot do it). The cw/ccw/flip incremental path still
  works *after* a crop — a rot90/mirror of the cropped preview is exact.
- **100% zoom.** Region renders work in cropped display space; the app's
  crop-mode editing runs in the fit view.

## 5. The app (Edit tab, Geometry sidebar)

- A **Crop** section: Crop… (enters crop mode), the ratio picker —
  Free · 35mm (24×36) · 645 (56×41.5) · 6×6 (56×56) · 6×7 (56×69.5,
  the Mamiya RB67/RZ67 gate) · 6×9 (56×84) — the tilt slider, and
  Apply / Cancel / Reset-crop. Presets auto-match the display image's
  orientation (portrait image → sides swapped); they constrain the
  overlay only — the op stores the raw rect.
- Crop mode hides the spot markers, forces the fit view, and draws the
  overlay: dark scrim outside the rect, bright border, 8 resize handles,
  drag-to-move, the tilt rotating the rect live. Apply sends
  `edit crop` (one-shot session, `isCropping` busy flag like
  `isRotating`); the CLI's regenerated preview (cropped) invalidates the
  render caches via the generation token, which now carries the crop.
- Crop applies to the **anchor negative** — the frame the preview shows
  — not the multi-selection.

## 6. Test surface

- `repo_test`: net state (latest wins, reset clears, malformed degrades,
  `validated_crop_params` bounds).
- `previews_test`: exact discrete slices under every turns/flip
  combination; the tilt removing the drawn tilt (against the
  display-space definition); re-crop composition; staleness;
  `display_shape`/`crop_report`; `render_region` in cropped space; the
  marker mapping folding the crop in.
- `edits_test`: `run_edit_crop` end to end (TIFF untouched, preview
  cropped, tilt in TIFF space, reset, re-crop, rotation composition,
  validation, markers hidden).
- `exporter_test`: `apply_edits` with the window; the exported JXL's
  dimensions and pixels; the provenance record.
- `cli_test`/`events_test`: dispatch, `roll info` field, schema, version.
- Swift: `EditModelTests` (crop parse/apply/reset, generation token),
  `CLICommandTests` (args), `CLIEventTests` (version + field).
