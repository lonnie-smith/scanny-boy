# Auto-crop: a film-format crop seeded at stitch time

After a negative is stitched, the user almost always opens crop mode, picks
the roll's format ratio, and drags the rect in until the rebate, the
negative holder, and the fill wedges are gone. This plan makes that
automatic:

1. **At stitch time**, when the roll's **Auto-crop** setting is ticked, the
   stitch seeds a `crop` op shaped to the roll's film format and sized to the
   largest window that holds **picture only**.
2. **In crop mode**, an **Auto** button runs the same detector against the
   negative as it currently displays and loads the result into the crop
   session. Nothing is recorded until Apply, exactly like **Original**.

Auto-crop is a per-roll setting, shown as an **Auto-crop** checkbox in the
Setup section of both the Capture sheet and the Add Scans sheet. It is **off
until ticked**.

This plan follows the house conventions (`docs/REBATE_ANCHORING.md`,
`docs/CAPTURE_QUEUE_PROGRESS_PLAN.md`): numbered chunks, each independently
green; every constant in exactly one module; and no threshold pinned without
a measurement the user has approved (§9).

---

## 0. Why this shape

### 0.1 The precedent is auto-rotate, and auto-crop follows it closely

`auto_rotate.py` already does most of what this needs:

- **It runs at stitch time, on the composite.** `_composite_and_publish`
  calls `estimate_rotation(result.image)` (`stitch_pipeline.py:2067`).
- **It never touches the published TIFF.** It appends one ops-log entry,
  `rotate_fine {"angle_deg", "source": "auto"}` (`stitch_pipeline.py:2185`),
  after the manifest write and before `sync_previews`, and reports it as a
  deferred `edit_recorded`.
- **It refuses rather than guesses.** If there is no rebate, too little scene,
  or the tilt is out of range, it returns `None` and seeds nothing.
- **It already separates picture from non-picture.** It builds a
  rebate/fill/scene mask in normalized density, then calls `minAreaRect` on
  the cleaned scene mask.

Auto-crop is the same pattern with a different op. It reuses the same mask
and runs right after the rotation it depends on. It departs from the
precedent in one way: on a re-stitch, an adopted negative whose crop is
still the automatic one gets a fresh auto crop (§4.4).

### 0.2 The crop is a normal `crop` op, so editing is free

The `crop` op (`repo.CROP_OP`, protocol 19) stores a tilted window in
published-TIFF pixels, plus `canvas` and `preset`. `roll info` and every
`edit_recorded` already report it with `x`/`y`/`canvas_*`, and
`EditStageView.beginCrop` (`EditStageView.swift:797`) already re-enters crop
mode on the full frame with the saved rect and preset drawn over it. So a
seeded crop needs **no new edit-sheet mechanics**:

- Activating crop mode shows the auto rect with the matching ratio preset
  selected.
- Dragging and pressing Apply records a user crop, which supersedes the auto
  one because the latest state op wins.
- **Original** followed by Apply clears it.
- A re-stitch that changes the canvas makes it stale exactly like a user crop
  (`previews.crop_is_live`), unless §4.4 replaces it.

### 0.3 The setting belongs in the roll's `setup` block

The roll already has a merge-updated, always-editable `setup` block:
`{"grid", "interval_seconds", "format"}`, written by `roll set-setup` and
decoded in Swift as `RollCaptureSetup`. The format lives there, and auto-crop
cannot run without the format. Both sheets already read and write that block
(`ConfigurationModel.rollFormat` / `setRollFormat`), and
`ContentView.syncCaptureRollSetup` keeps them in agreement. So `auto_crop`
becomes a fourth key, and the checkbox on either sheet is the same stored
value.

**This changes one documented stance.** `roll_manifest.py:457` and
`roll_folder.set_setup` say "nothing in stitching reads them". After this
plan, stitching reads `setup.format` and `setup.auto_crop`. Update both
comments and the schema description. They remain editable at any time and
are still not roll invariants: they shape an ops-log seed, never published
pixels.

**Why the stitch reads the roll, not a flag the app passes.** The Capture
tab's `StitchQueueModel` launches one `stitch` per negative in the
background, minutes after the user set things up. Reading the roll at stitch
start means the checkbox's current value applies to the next negative
without threading it through the queue. The CLI still gets a
`--no-auto-crop` override for parity with `--no-auto-rotate` (§4.3).

### 0.4 What "no material outside the image" means, operationally

The window must contain none of the following:

| excluded | how it is recognised | source |
|---|---|---|
| canvas fill (the wedges a tilted stitch or rotation leaves) | exactly the `NORMALIZED_FILL` code in every channel | `auto_rotate.estimate_rotation` |
| bare light, sprocket holes | thinner than rebate (at or above the thin anchor) | same thin-end test |
| rebate / clear base | thin in every channel, within `REBATE_SLACK` of the thin anchor | same |
| negative holder / carrier | dense, border-connected, featureless band | the recorded `film_extent` insets, plus a dense-border pass (§2.2 step 3) |
| rebate edge printing (frame numbers, DX marks) | sits inside the rebate band, so it is removed by the rectangle fit, not the mask | §2.2 step 5 |

Everything else counts as picture. The crop is the **largest rect of the
format's ratio that lies entirely inside the picture mask**, shrunk by a
small safety margin.

A fixed ratio means one dimension of the picture is usually trimmed. The
camera gate is rarely exactly nominal, and the rounded corners of a
120-format gate cost a little on each edge. That trim is the requested
behaviour, not a defect.

---

## 1. Aspect ratios

### 1.1 One table, keyed by the format string

`FilmFormat` (Swift) and `roll set-setup --format` share raw values:
`half-frame, 35mm, 6x3, 645, 6x6, 6x7, xpan, 6x9, 6x12, 6x17`.
`CropPreset` already uses matching raw values for the five cases it has
(`35mm, 645, 6x6, 6x7, 6x9`). So **the crop preset label stored on the op is
the format string**, and no mapping table is needed.

Crop ratios use real gate sizes, not nominal ones, following
`CropOverlay.swift`'s existing comment:

| format | gate (mm) | stored ratio (w : h) | status |
|---|---|---|---|
| half-frame | 18 × 24 | 18 / 24 (portrait-native) | new |
| 35mm | 36 × 24 | 36 / 24 | existing |
| 6x3 | 56 × 28 (6×6 half-frame) | 56 / 28 | new |
| 645 | 56 × 41.5 | 56 / 41.5 | existing |
| 6x6 | 56 × 56 | 1 | existing |
| 6x7 | 56 × 69.5 | 56 / 69.5 | existing |
| xpan | 65 × 24 | 65 / 24 | new |
| 6x9 | 56 × 84 | 56 / 84 | existing |
| 6x12 | 112 × 56 | 112 / 56 | new |
| 6x17 | 168 × 56 | 168 / 56 | new |

6×3 and 6×12 share a 2:1 ratio. That is fine: they remain distinct presets
with distinct labels, and the stored `preset` string tells them apart.

Orientation is never taken from the table:
- **Swift** orients to the display image (`CropSession.orientedRatio`).
- **Python** orients to the long axis of the detected picture rect (§2.2
  step 4), which gets panoramic formats right even on a square-ish canvas.

### 1.2 Both sides must agree

- **Swift:** add the five new `CropPreset` cases, with labels copied from
  `FilmFormat.label`. Add `CropPreset(format: FilmFormat)` and a test that
  every `FilmFormat` maps to a non-free preset. That test makes a future
  format addition fail loudly.
- **Python:** add `auto_crop.FORMAT_RATIOS: dict[str, float]` with the same
  values. Record it in `build_params()` and in CONTRACT.md's crop section.
- **Parity:** `CropSessionTests` and `auto_crop_test.py` each hard-code the
  literal table from CONTRACT.md. Two literal copies, each checked against
  the contract, is the house pattern (see `cli.py`'s `--format` choices
  comment).

---

## 2. The new module: `cli/src/scanny_boy/auto_crop.py`

### 2.1 Constants (provisional until §9)

```python
# The estimation runs on a bounded analysis copy, like auto_rotate.
ANALYSIS_MAX_EDGE = 1024
# Of the short side of the picture rect, applied inward after the fit:
# absorbs analysis-scale rounding, the feathered blend edge, and the soft
# rebate/picture transition.
AUTO_CROP_SAFETY_FRACTION = 0.005
# Non-picture pixels tolerated inside a candidate window, as a fraction of
# its area: dust and specks the morphological clean missed must not shrink
# the crop by a whole speck-width.
AUTO_CROP_MAX_INTRUSION = 0.0005
# Refuse when the picture mask covers less than this fraction of the
# covered canvas: detection failed, or this is not a normal frame.
AUTO_CROP_MIN_PICTURE_FRACTION = 0.25
# Refuse when the fitted window is smaller than this fraction of the picture
# rect's area: a ragged mask, a light leak read as rebate, or the wrong
# format for this film.
AUTO_CROP_MIN_FILL_FRACTION = 0.60
# Dense-border pass: a border-connected component denser than this
# normalized level, and flatter than AUTO_CROP_MAX_DENSE_SPREAD, is carrier.
AUTO_CROP_DENSE_LEVEL = ...        # pinned in §9
AUTO_CROP_MAX_DENSE_SPREAD = ...   # pinned in §9

FORMAT_RATIOS: dict[str, float] = {...}  # §1.1
AUTO_CROP_VERSION = 1
```

### 2.2 The detector works on a display image

The detector knows nothing about ops logs. Its input is always **a display
image**: the published pixels with some geometric transform already applied
and no crop. Its output is a rect in that image's pixels. It has two callers,
and each builds its own display image (§2.3):

```python
@dataclasses.dataclass(frozen=True)
class AutoCrop:
    """An axis-aligned window (x, y, w, h) in the full-resolution display
    image the caller described, and the numbers that justified it."""
    rect: tuple[int, int, int, int]
    ratio: float | None
    picture_fraction: float
    fill_fraction: float


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Why no crop was produced. `reason` is a stable token for the app and
    the evidence block: "unknown_format", "no_coverage", "little_picture",
    "no_fit", "ragged", "too_small"."""
    reason: str
    picture_fraction: float | None = None
    fill_fraction: float | None = None


def estimate_crop(
    analysis: np.ndarray,            # encoded uint16 display image, ≤ ANALYSIS_MAX_EDGE
    *,
    full_size: tuple[int, int],      # (height, width) of the full-res display image
    ratio: float | None,             # landscape-agnostic w:h, or None for unconstrained
    exclude: tuple[int, int, int, int] | None,  # §2.3 carrier hint, in analysis pixels
) -> AutoCrop | Refusal:
```

`analysis_display(image, *, quarter_turns, flipped, fine_angle_deg)` builds
the downscaled display copy. It downscales **first**, then flips, applies
`rotate_with_fill`, and applies quarter turns, the same order as
`previews._display_image`. That keeps the warp cheap at stitch time.

Steps:

1. **Decode the analysis copy** to normalized density.
2. **Mask out thin material: fill, bare light and rebate.** Factor
   `estimate_rotation`'s fill/thinness/rebate block into a shared
   `auto_rotate.picture_mask(normalized) -> (covered, rebate)`, so rotation
   and crop can never disagree about what rebate is. Rotation keeps its own
   guards on top. If this refactor changes any of rotation's outputs, the
   chunk is wrong: `auto_rotate_test.py` must pass unchanged.
3. **Mask out the dense carrier.** Remove border-connected, flat, dense
   components: a small normalized-space version of `withhold_dense_border`,
   gated by `AUTO_CROP_DENSE_*`. On any edge where that pass found nothing,
   also remove the `exclude` hint. The hint is meter-deep (it includes
   `FILM_EXTENT_MARGIN_CELLS` and the convergence travel), so it is a
   fallback, never the primary signal.
4. **Clean the mask and find the picture rect.** Open and close with the same
   kernel rule as `auto_rotate`, keep components of at least
   `SCENE_MIN_AREA_FRACTION`, and take the axis-aligned bounding box. This
   gives orientation (long axis, used to orient `ratio`) and the reference
   area for the fill guard.
5. **Fit the largest window inside the mask.** Build a summed-area table of
   `~picture`.
   - **With a ratio:** binary-search the window height `h`
     (`w = round(h · ratio)`). A window at `(x, y)` is feasible when its
     non-picture count is at most `AUTO_CROP_MAX_INTRUSION · w · h`; that is
     one vectorised SAT lookup over all origins. At the largest feasible `h`,
     choose the feasible origin whose centre is closest to the picture rect's
     centre. This keeps the crop symmetric when the picture is wider than the
     ratio. The search is O(log H · N).
   - **Without a ratio** (`ratio=None`, only from the Auto button, §6.2):
     find the maximal-area axis-aligned rectangle with the histogram-stack
     algorithm, O(N), applied to the picture mask after the intrusion
     tolerance is folded in by a 1-px erosion of isolated specks.
6. **Shrink and scale.** Inset each side by `AUTO_CROP_SAFETY_FRACTION` of the
   short side, holding the ratio when there is one. Scale to `full_size`,
   rounding every edge **inward**.
7. **Guards, each returning a `Refusal`:**

   | condition | reason |
   |---|---|
   | format not in `FORMAT_RATIOS` | `unknown_format` (callers check this before calling) |
   | nothing covered | `no_coverage` |
   | picture fraction below `AUTO_CROP_MIN_PICTURE_FRACTION` | `little_picture` |
   | no feasible window | `no_fit` |
   | fill fraction below `AUTO_CROP_MIN_FILL_FRACTION` | `ragged` |
   | a side below `repo.CROP_MIN_SIZE_PX` | `too_small` |

**Edge-to-edge scans with no rebate** are fine: the mask is everything
covered, so the crop is the largest window that avoids the fill wedges.

**Monochrome rolls** need no branch: the composite is promoted to three equal
channels exactly as in `estimate_rotation`.

### 2.3 The two callers, and mapping back to the op

Both callers finish with the same call. Take the full-resolution display rect
and pass it to `previews.display_crop_window_to_tiff` with:

- `tilt_deg=0.0`
- the display transform the caller used (`quarter_turns`,
  `flipped_horizontally`, `fine_angle_deg`)
- `crop_params=None`, `full_frame=True`

That returns the TIFF-space `(x, y, w, h, tilt_deg)` the op stores. **Do not
write a second mapping.** Reusing this function is what guarantees that
re-entering crop mode lands the rect exactly where the detector put it.

The **carrier hint** is the recorded `film_extent` insets when `detected`.
Convert them from grid cells to TIFF pixels (`× ANALYSIS_BLOCK_PX`, offset by
`analysis_rect`), treat them as an inner TIFF rect, and map its corners
forward through the same display transform with
`previews.tiff_crop_window_to_display`'s helpers. Then scale to analysis
pixels. One helper, `auto_crop.exclusion_hint(normalization, tiff_size,
transform, scale)`, serves both callers.

| | stitch seeding (§4) | Auto button (`edit suggest-crop`, §6) |
|---|---|---|
| source pixels | `result.image` (the composite) | the published TIFF |
| display transform | seeded auto rotation only (no quarter turns, no flip) | the negative's full net state (quarter turns, flip, fine rotation), crop ignored |
| ratio | `FORMAT_RATIOS[setup.format]` | the session's preset, else the roll's format, else `None` (§6.2) |
| output | appends a `crop` op | reports a rect; records nothing |

Scratch and spot repair are **not** applied to the Auto button's analysis
copy. They are small, in-picture corrections that do not move the picture's
edges, and skipping them keeps the query fast.

---

## 3. Storage

### 3.1 The op

The stitch appends `repo.append_edit(out_dir, negative_id, repo.CROP_OP,
params)` with:

```json
{"x": ..., "y": ..., "w": ..., "h": ..., "tilt_deg": ...,
 "canvas": [width, height], "preset": "6x7", "source": "auto"}
```

`validated_crop_params` must accept an optional `source` whose only allowed
value is `"auto"`, matching `rotate_fine`'s. Check whether it currently
rejects unknown keys, and extend it if so.

`edit crop` gains `--source auto`, so an Auto-button crop the user applies
unchanged stays tagged auto (§6.3). A crop without `source` is the user's.
The latest crop op's `source` answers "is this still the automatic crop?",
which is what §4.4's reseed rule reads.

### 3.2 Reporting

`previews.crop_report` passes `source` through. `CropState` gains
`source: String?`, so the sidebar can say "Auto · 6×7" (§6.4). This is
additive to the `edit_recorded` / `roll info` crop object.

### 3.3 Evidence

The negative's record gains:

```json
"auto_crop": {"result": "seeded" | "reseeded" | "refused" | "no_format",
              "reason": "ragged" | null,
              "picture_fraction": 0.81, "fill_fraction": 0.93,
              "version": 1}
```

It is written by the stitch only, recorded, and read by nothing, so §9 can
see refusal rates on real rolls without re-running the stitch. It is absent
when auto-crop is off. `stitch_params` gains
`"auto_crop": auto_crop.build_params()` as a **non-invariant** entry, with the
same posture as the params auto-rotate records.

---

## 4. Pipeline

### 4.1 Where it runs

In `_composite_and_publish`, directly after
`auto_rotation_deg = estimate_rotation(...)` (`stitch_pipeline.py:2067`):

```python
auto_crop_result = None
if seed_crop is not None:              # the ratio, or None when not seeding
    try:
        analysis, scale = auto_crop.analysis_display(
            result.image, quarter_turns=0, flipped=False,
            fine_angle_deg=seed_fine_angle,     # §4.2
        )
        auto_crop_result = auto_crop.estimate_crop(
            analysis,
            full_size=result.image.shape[:2],
            ratio=seed_crop,
            exclude=auto_crop.exclusion_hint(record.normalization, ...),
        )
    except Exception:  # noqa: BLE001
        emit(WarningEvent(code=Code.AUTO_CROP_FAILED, ...))
```

Like scratch detection, **an exception never fails the stitch**.

### 4.2 Seeding order and events

The fine angle the crop is measured under is:
- for a **new** negative, `auto_rotation_deg or 0.0`;
- for an **adopted** negative (§4.4), the existing net `fine_angle_deg`, since
  rotation is never reseeded there.

Quarter turns and flip on an adopted negative are the user's display choices.
The crop is stored in TIFF space, so it is correct under any of them; measure
without them.

In the existing seeding block (`stitch_pipeline.py:2178`):

1. Append `rotate_fine`, if any (unchanged).
2. Re-read `net_edit_state`.
3. Map the rect (§2.3) and append the `crop` op.

`_composite_and_publish` currently returns one `auto_edit_fields` dict.
Change it to return a **list**, one entry per seeded op, each built as today
from the net state *after that op*. `run_stitch` extends `auto_edits`
instead of appending. The app then receives two ordinary `edit_recorded`
events, and the second carries the net crop. `EditModel` needs no change for
that.

### 4.3 Deciding whether to seed

In `run_stitch`, after the roll manifest loads:

```python
setup = roll.setup or {}
roll_format = setup.get("format")
crop_enabled = auto_crop and bool(setup.get("auto_crop", False))   # off until ticked
seed_ratio = FORMAT_RATIOS.get(roll_format) if crop_enabled else None
```

When `crop_enabled` and `roll_format` is `None`, emit `AUTO_CROP_NO_FORMAT`
(a warning) **once per run**, not once per negative, and record `no_format`.

CLI: `--no-auto-crop` on both `stitch` and `run` (`cli.py:267`, `:301`),
threaded through `run_pipeline.py:120` beside `auto_rotate`. The flag can only
turn seeding off; it never turns it on over a roll setting of off.

**Timing on the Capture tab.** The setting is read when each negative's
`stitch` starts. Ticking or unticking mid-roll affects negatives whose stitch
has not yet started; already-stitched negatives keep what they have. Say so
in the checkbox's help text.

### 4.4 Re-stitch: replace a crop that is still automatic

For each negative, `seed_crop` is the ratio when `crop_enabled`, a format is
set, and **either**:

- the negative is new (`negative_id in new_negative_ids`); **or**
- it is adopted **and** its latest crop op carries `source: "auto"`.

It is `None` otherwise. That means an adopted negative with a user crop, or
no crop at all, is never touched. "No crop" on an adopted negative means the
user cleared it, or auto-crop was off when it was first stitched; either way
it is not the app's to fill in.

Read the latest crop op's `source` from the ops log **before** publish (the
same place `_covered_applied_source` reads prior state). A stale crop has no
live `state.crop`, but its op is still in the log.

On an adopted negative:
- if the new window equals the existing live one exactly, append nothing;
- otherwise append the new auto crop and record `result: "reseeded"`;
- if the detector refuses, **leave the old op alone**. If the canvas changed,
  it degrades to no crop through `crop_is_live`, the same as today. Record
  `refused`.

`_composite_and_publish`'s docstring and `run_stitch`'s `auto_rotate`
paragraph both say seeding never touches adopted negatives. Amend both to
name this one exception and why: it only ever replaces the app's own guess.

---

## 5. Roll setup: the Auto-crop checkbox

### 5.1 Model and command

- **`roll set-setup`**: add `--auto-crop {on,off}`. `roll_folder.set_setup`
  gains `auto_crop: bool | None` with the same merge rule. An unset key reads
  as off.
- **`RollCaptureSetup`** (`RollManifest.swift:90`): add `autoCrop: Bool`,
  decoded from `setup.auto_crop` with a default of `false`.
- **`CLICommand.rollSetSetup`** (`CLIRunner.swift:49`): add `autoCrop: Bool?`,
  which emits `--auto-crop on|off`.
- **`ConfigurationModel`**: add `rollAutoCrop` and `setRollAutoCrop(_:)`,
  mirroring `rollFormat` / `setRollFormat` (`ConfigurationModel.swift:463`),
  including `rollSetupError` and `isSettingRollSetup`.
- **`ContentView.captureRollSetupSyncKey`**: include `rollAutoCrop`, so the
  two sheets never disagree.

### 5.2 The two sheets

Extract one `RollFormatFields` view into `ConfigurationSubviews.swift`, holding
the Format picker and the checkbox, so the sheets cannot drift:

```swift
Picker("Format", selection: formatBinding) { ... }       // moved from CaptureStageView
Toggle("Auto-crop", isOn: autoCropBinding)
    .disabled(isSettingRollSetup)
    .help("Crops each newly stitched negative to the format's ratio, "
        + "excluding rebate and holder. Takes effect from the next negative "
        + "stitched. Adjust in Edit → Crop.")
if rollAutoCrop && rollFormat == nil {
    Text("Choose a format to auto-crop.")
        .font(.caption).foregroundStyle(.secondary)
}
```

- **Capture sheet** (`CaptureStageView.setupSection`): replace the existing
  Format picker with `RollFormatFields`.
- **Add Scans sheet** (`ContentView.configurationSections`, "Roll Setup"):
  today this section has **no Format picker**. Add `RollFormatFields` after
  `BaseFrameField` and before the grid picker.

---

## 6. The edit sheet

### 6.1 Presets

`CropPreset` gains the five new cases (§1.2), so `beginCrop`
(`EditStageView.swift:804`) resolves an auto crop's `preset` to the right
picker entry instead of falling back to `.free`.

### 6.2 The Auto button: `edit suggest-crop`

A new **pure query**, following `edit list-spots`' precedent
(`cli.py:792`): it reads, emits one event, and writes nothing.

```
scanny-boy edit suggest-crop --roll DIR --negative ID [--preset NAME]
```

- `--preset` is the crop session's current ratio preset (a `FORMAT_RATIOS`
  key). When it is omitted, the command uses the roll's `setup.format`. When
  there is no format either, it fits **unconstrained** (`ratio=None`), which
  gives the largest picture-only rect of any shape. A `--preset` that is not
  a key fails `INVALID_EDIT`.
- Build the analysis copy from the published TIFF under the negative's full
  net state with the crop ignored (§2.3's right-hand column), run
  `estimate_crop`, and report in **full-frame display space**, the space
  `CropSession` works in.

A new event, `crop_suggested`:

```json
{"event": "crop_suggested", "negative_id": "…",
 "rect": {"x": 212, "y": 148, "width": 5410, "height": 3606} | null,
 "canvas_width": 5832, "canvas_height": 3888,
 "preset": "35mm" | null,
 "refused": null | "ragged"}
```

`rect` is `null` exactly when `refused` is set. A refusal is **not an error**:
the command exits 0, because "couldn't find the picture edges" is an answer.

### 6.3 The button

In `EditStageView.cropSection`, beside **Original** (`EditStageView.swift:1077`),
in an `HStack`:

```swift
Button("Auto") { onAutoCrop() }
    .disabled(edit.isCropping || edit.isSuggestingCrop)
    .help("Fit the largest picture-only window, excluding rebate and holder, "
        + "to the selected ratio (or the roll's format)")
```

- **`EditModel.suggestCrop(negative, preset:) async -> CropSuggestion?`** runs
  the query and sets `isSuggestingCrop` while it is in flight. It returns the
  rect and preset, or a refusal reason.
- **On success, `CropSession.applySuggestion(rect:preset:)`** does four
  things:
  - sets `rect`, clamped with `CropGeometry.clampFrame`;
  - resets `tiltDegrees = 0` (the suggestion is measured without extra tilt);
  - sets `preset` to the returned one, so a Free session given a roll format
    lands on that format;
  - remembers the rect as `suggestedRect`.

  Guard the preset assignment so it does not trigger `applyPreset`'s
  reshaping: the `onChange` handler skips when `rect == suggestedRect`. It
  records nothing, the same as Original.
- **On refusal,** show one caption line under the buttons: "Couldn't find the
  picture's edges on this frame." Leave the rect alone.
- **Apply:** `applyCrop` passes `source: "auto"` when
  `cropSession.rect == suggestedRect && tiltDegrees == 0`. Any drag, tilt or
  preset change clears `suggestedRect`. So an untouched Auto crop stays
  eligible for §4.4's reseed, and a touched one becomes the user's.
- **Spinner:** reuse the existing `ProgressView` slot in the Apply row while
  `isSuggestingCrop`.

### 6.4 Sidebar label

When crop mode is not active and `negative.crop?.source == "auto"`, the crop
section shows a secondary caption: "Auto · 6×7". There are no other
behavioural differences.

---

## 7. Contract and schema

- `CONTRACT.md`: protocol **22 → 23**. Document:
  - `setup.auto_crop` and `roll set-setup --auto-crop`
  - `stitch`/`run --no-auto-crop`
  - the crop op's `source` and `edit crop --source auto`
  - `edit suggest-crop` and the `crop_suggested` event, including refusal
    tokens (§2.2 step 7)
  - the §4.4 reseed rule
  - the ratio table (§1.1)
  - `AUTO_CROP_NO_FORMAT` and `AUTO_CROP_FAILED` (warnings)
  - the per-negative `auto_crop` evidence block
- `roll-manifest.schema.json`:
  - `setup.auto_crop: boolean | null`
  - the per-negative `auto_crop` block
  - update `setup`'s description ("never consulted by stitching" is no
    longer true)
- `schema.json`: the `crop_suggested` event; the `crop` object in
  `edit_recorded` gains an optional `source`.
- `events.Code`: the two warnings. `CLICode+FriendlyNames.swift`: their names.
- DB: `setup` is already one JSON text column (migration 0018), so **no
  migration**.
- `ROLL_MANIFEST_FORMAT_VERSION`: additive nullable keys, so no bump, unless
  the schema's `setup` object is `additionalProperties: false` *and* old
  readers validate against it. Check that in chunk AC-3.

---

## 8. Chunks

### AC-1: ratios

**Files:** `CropOverlay.swift`, a new `auto_crop.py` (table only),
`CONTRACT.md`.

**Do:** the new `CropPreset` cases, `CropPreset(format:)`, and
`FORMAT_RATIOS`.

**Tests:**
- `CropSessionTests`: every `FilmFormat` maps to a preset; the ratios match
  the literal table; half-frame and XPan orient correctly on landscape and
  portrait images.
- `auto_crop_test.py`: the same table.

**Green when:** the crop picker offers all ten formats and nothing else
changed.

### AC-2: the detector

**Files:** `auto_crop.py`, `auto_rotate.py` (the `picture_mask` extraction).

**Do:** §2.1–§2.2, `analysis_display`, `exclusion_hint`.

**Tests** (fast tier, synthetic encoded composites from
`synthetic_scene_support`):
- picture inside a rebate band inside fill yields a window fully inside the
  picture at the exact ratio, centred;
- the same scene built through `analysis_display` with a 2° fine angle, a
  quarter turn, and a flip yields rects that map back through
  `display_crop_window_to_tiff` to the same TIFF window within 2 px;
- a dense border band on one edge is excluded; with the dense pass disabled,
  the `exclude` hint alone still excludes it;
- rounded gate corners: the window avoids the corners, and the fill fraction
  stays above the guard;
- a picture wider than the ratio: the window's height equals the picture
  height and it is horizontally centred;
- an edge-to-edge picture with fill wedges: the window avoids the wedges;
- `ratio=None` returns the maximal-area rect on an L-shaped mask;
- a single dust speck inside the picture does not shrink the window;
- each refusal token is produced by its condition;
- a monochrome composite works;
- `auto_rotate_test.py` passes unchanged.

### AC-3: setup field, CLI, and the query

**Files:** `roll_folder.py`, `roll_manifest.py` (comment), `cli.py`,
`run_pipeline.py`, `edits.py` (`run_edit_suggest_crop`; `run_edit_crop`
accepts `source`), `repo.py` (`source` validation), `previews.py`
(`crop_report` passes `source`), `events.py`, the contract and schemas.

**Do:** §3.1–§3.2, §4.3's flags and setting read (not yet seeding), §5.1's
CLI half, §6.2, §7.

**Tests:**
- `roll_folder_test.py`: merge rules; unset reads off.
- `cli_test.py`:
  - `set-setup --auto-crop` round-trip and `--no-auto-crop` parse;
  - `edit crop --source auto` stores and reports `source`;
  - `edit suggest-crop` on a `work_dir` negative emits `crop_suggested` with
    a rect inside the canvas, writes nothing (ops log unchanged), honours
    `--preset`, falls back to the roll format, then to unconstrained, and
    reports a refusal with exit 0;
  - an unknown `--preset` fails `INVALID_EDIT`.
- Schema conformance.

### AC-4: seeding and reseeding

**Files:** `stitch_pipeline.py`.

**Do:** §3.3 and §4.1–§4.4.

**Tests** (`stitch_pipeline_test.py`):
- **New negative, format set, auto-crop on:** `rotate_fine` (if any) then
  `crop` with `source: "auto"`; two `edit_recorded` events; evidence
  `seeded`.
- **Unset or off:** no crop op, no evidence block.
- **No format:** exactly one `AUTO_CROP_NO_FORMAT` per run, whatever the
  negative count.
- **`--no-auto-crop` on a ticked roll:** no crop op.
- **Adopted negative whose latest crop is auto,** re-stitched with a changed
  canvas: a new auto crop against the new canvas; evidence `reseeded`.
- **Adopted negative whose latest crop is auto,** re-stitched with an
  identical result: no new op.
- **Adopted negative with a user crop:** untouched.
- **Adopted negative with no crop:** untouched.
- **Adopted negative whose reseed is refused:** old op left; evidence
  `refused`.
- **Detector exception:** `AUTO_CROP_FAILED` warning, and the stitch
  succeeds.
- **Crop-mode round-trip:** `edit crop --full-frame` with the reported
  `x/y/w/h` reproduces the stored window.

Slow tier: a real sample roll's seeded crop lies inside the recorded film
extent.

### AC-5: Mac setup and edit-sheet display

**Files:** `RollManifest.swift`, `CropState.swift`, `CLIRunner.swift`,
`ConfigurationModel.swift`, `ConfigurationSubviews.swift`,
`CaptureStageView.swift`, `ContentView.swift`, `EditStageView.swift` (§6.1,
§6.4), `CLICode+FriendlyNames.swift`.

**Do:** §5 and §6.1, §6.4.

**Tests:**
- `ConfigurationModelTests`: `setRollAutoCrop` issues the right command and
  updates state; `autoCrop` defaults to false when absent.
- `CLICommandTests`: `--auto-crop on|off` argument shape.
- `RollLibraryTests`: `setup.auto_crop` and `crop.source` decode.
- `ContentViewTests`: the sync key includes auto-crop.

May land in parallel with AC-4, but not before AC-3.

### AC-6: the Auto button

**Files:** `CLIRunner.swift` (`.editSuggestCrop`, `.editCrop(source:)`),
`CLIEvent.swift` (`crop_suggested`), `EditModel.swift`, `CropOverlay.swift`
(`applySuggestion`, `suggestedRect`), `EditStageView.swift`.

**Do:** §6.2–§6.3.

**Tests:**
- `CLICommandTests`: `suggest-crop` shape with and without `--preset`;
  `edit crop --source auto`.
- `CLIEventTests`: `crop_suggested`, both the rect and the refusal form.
- `CropSessionTests`:
  - `applySuggestion` sets the rect, zero tilt and the preset without
    reshaping;
  - a drag, tilt or preset change clears `suggestedRect`.
- `EditModelTests`: Apply after an untouched suggestion sends
  `--source auto`; after a drag, it does not.

May land any time after AC-3.

### AC-7: measurement gate

§9. Constants in `auto_crop.py` are pinned here, with the date and the
measurement in each constant's comment.

---

## 9. Measurement gate (pins the constants)

Auto-crop is off by default, so this gate does not block shipping the
checkbox. It **does** block trusting it: run it before recommending that users
tick the box.

Stitch one real roll per available format, at least 35mm, 645 or 6×7, and a
panoramic format (XPan, 6×12 or 6×17) if available. Include at least:
- one negative with the holder visible on an edge;
- one with bare light at an edge;
- one tightly framed, edge-to-edge picture;
- one high-key frame whose sky borders the rebate.

The high-key frame is the most likely to be read as rebate.

For each negative, report:
- the `auto_crop` evidence block;
- a preview with the seeded rect overlaid;
- how many pixels of picture were lost on each side compared with a
  hand-drawn best crop.

Pin `AUTO_CROP_SAFETY_FRACTION`, the two dense-border constants and the two
refusal fractions so that:
- **no seeded crop includes non-picture**, which is the requirement;
- median picture loss is under ~1% per side.

A high-key frame that loses picture to the rebate test is acceptable **only
if** it refuses rather than seeding a visibly short crop. If it seeds short,
tighten `AUTO_CROP_MIN_FILL_FRACTION` or add a symmetry refusal (opposite
edges trimmed by very different amounts means refuse).

---

## 10. Decisions taken

| # | question | decision |
|---|---|---|
| 1 | 6×3 gate | 56 × 28 mm, a 6×6 half-frame; ratio 2:1 (§1.1) |
| 2 | default for rolls with no `auto_crop` key | **off until ticked** (§4.3, §5.1) |
| 3 | re-stitch | reseed an adopted negative whose latest crop op is `source: "auto"`; never touch a user crop or an absent one (§4.4) |
| 4 | re-running on demand | an **Auto** button in crop mode, backed by `edit suggest-crop`; fills the session, records nothing until Apply (§6.2–§6.3) |

---

## 11. Rejected alternatives

- **Crop the published TIFF.** It would break the nondestructive model, every
  replay path, and re-crop.
- **Use `film_extent` insets alone.** They are meter-deep, since they include
  the margin and convergence travel, so crops would lose picture, and they
  do not see rebate at all. They survive only as the §2.2 step 3 fallback.
- **Fit `minAreaRect` and shrink to the ratio.** The enclosing rect *contains*
  the rounded corners and any ragged edge. "No non-picture inside" needs an
  *inscribed* fit, which is why §2.2 step 5 uses the summed-area search.
- **Let the app pass `--auto-crop FORMAT` per invocation.** The Capture queue
  would need the setting threaded into every background stitch, and the CLI
  would have two sources of truth for the format (§0.3).
- **An Auto button that records the op directly** (`edit auto-crop`). Crop
  mode is a local session: Original, the ratio picker and tilt all stay
  unrecorded until Apply. A button that wrote to the ops log mid-session would
  be the only control that does, and would leave Cancel unable to undo it.
- **Reseed every adopted negative on re-stitch.** It would overwrite crops the
  user drew by hand. The `source` tag exists so the reseed can tell the two
  apart.
- **A tilted auto crop.** The seeded rotation already squares the frame;
  compounding a second tilt makes the rect harder to reason about in crop
  mode, for no gain.

---

## 12. Implementation notes

Where the code differs from, or settles a question the plan left open.
AC-1 to AC-6 are implemented and tested; **AC-7 (§9) has not been run** — it
needs real rolls and the user's approval, so every constant in `auto_crop.py`
is still provisional.

- **Dense-border pass (§2.2 step 3)** is `auto_crop._dense_carrier`. It adds
  two constants the plan did not name, `AUTO_CROP_DENSE_MIN_AREA_FRACTION` and
  `AUTO_CROP_DENSE_MIN_SPAN_FRACTION`, so a small dense mark or a compact
  blob touching the border is not read as a holder. `AUTO_CROP_DENSE_LEVEL`
  (0.0, the dense-end anchor) and `AUTO_CROP_MAX_DENSE_SPREAD` are pinned in
  §9 like the rest.
- **The `exclude` hint applies per edge**, only where the dense pass found
  nothing on that edge, and it is the *outside* of the inner window that is
  excluded. Edges with no recorded inset are pushed out of the canvas so they
  constrain nothing. Under a fine rotation the inscribed axis-aligned rect is
  used, which can only exclude more.
- **§2.2 step 5, no ratio:** the "1-px erosion of isolated specks" is not
  done; the open/close of step 4 already removes specks, and an erosion would
  shrink every edge.
- **Ratio orientation** uses the landscape form of the ratio (`max(r, 1/r)`),
  turned to the picture rect's long axis, because `FORMAT_RATIOS` stores
  `half-frame`, `645`, `6x7` and `6x9` portrait-native.
- **Refusal tokens:** `unknown_format` is not produced by the detector
  (callers check first); the evidence block also records `error` when the
  detector raised.
- **Adopted negatives are measured under their own mirror and net fine
  angle** (§4.2 said to measure without the flip). A mirror negates the net
  fine angle, so measuring it unmirrored would apply the rotation the wrong
  way and mis-fit the crop.
- **Evidence storage (§3.3)** needed a column: migration `0019` adds a
  nullable `negatives.auto_crop`. `stitch_params["auto_crop"]` is excluded
  from the roll-invariant comparison (`ROLL_PROFILE_STITCH_PARAMS_KEYS`).
- **Protocol:** the feature landed at 23 and merged to 24; `CONTRACT.md` now
  says 24. The `crop_suggested` event carries `rect`, not top-level `x/y/...`.
