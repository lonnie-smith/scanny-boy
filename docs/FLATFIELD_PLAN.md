# Flat-field correction

An implementation plan, in the shape `CONTRIBUTING.md` asks for: numbered
chunks, one topic and one pull request each, merged in order. Section 2 is
the locked decisions; changing one of them means editing this file and
`DECISIONS.md` together, not editing code until it behaves differently.

Modelled on NegPy's flat-field feature, adapted to this program's
architecture — in particular to the rule that **Python owns every decision**
and to the fact that a roll's `processing_params` is an invariant.

---

## 1. What it is and why it goes where it goes

A copy-stand capture is darker at the corners than at the centre: lens
falloff plus an uneven light source. Flat-field correction measures that
falloff once, from a reference shot of the **bare light source with no
negative in the holder**, and divides it back out of every frame.

The correction is **multiplicative gain only**. There is no black-frame
subtraction — same as NegPy.

**Where it sits in the pipeline.** Immediately after `raw_decode.decode_raw`
and before the intermediate TIFF is written — i.e. inside the convert stage,
per frame, ahead of both the stitch stage's photometric gain solve and the
blend:

```
decode  →  FLAT FIELD  →  base TIFF → nested EXIF → stage → publish
                                    ↓
                          stitch: detect → match → solve
                                → warp → gain solve → blend → encode
```

That ordering is not arbitrary. The stitch stage's per-frame per-channel
gains (`layout.solve_gains`, `composite.py`) exist to reconcile frames of one
negative with each other; they are a **global scalar per frame per channel**
and cannot represent a spatial gradient. Correcting vignetting before they
run means the residual they are asked to explain is real exposure mismatch,
not falloff — which also makes `overlap_mad` a cleaner measurement, since
overlapping regions sit at different distances from each frame's own optical
centre and therefore disagree *spatially* before correction.

---

## 2. Locked decisions

### 2.1 The gain map

Ported from NegPy's `negpy/features/flatfield/logic.py:compute_gain`, values
unchanged:

| Constant | Value | Why |
| --- | --- | --- |
| `GAIN_MAP_MAX_EDGE` | `256` | Falloff is low-frequency; full resolution buys nothing. |
| `BLUR_SIGMA_DIVISOR` | `16` | `sigma = max(h, w) / 16` on the downsampled map, so dust and noise in the reference are not baked into the correction. |
| `GAIN_MIN`, `GAIN_MAX` | `0.25`, `4.0` | A near-black edge in the reference must not become an extreme multiplier. |

Computation, per channel independently:

1. Decode the reference with the project's **locked `RAW_PARAMS`**
   (`raw_decode.decode_raw`), then `romm.decode_to_linear` — one linear
   float32 array.
2. Downsample with `INTER_AREA` so `max(h, w) <= GAIN_MAP_MAX_EDGE`.
3. `cv2.GaussianBlur` each channel with `sigma = max(h, w) / BLUR_SIGMA_DIVISOR`.
4. `gain = mean(blurred_channel) / blurred_channel`.
5. `np.clip(gain, GAIN_MIN, GAIN_MAX)`.

**On white balance.** NegPy decodes its reference linear with no white
balance; this plan reuses the locked `RAW_PARAMS` (`use_camera_wb=True`)
instead, and that is *not* a deviation in result. Step 4 divides each channel
by its own mean, so any constant per-channel scale — which is exactly what a
white-balance multiplier is — cancels identically. Reusing the one decode
path the project already treats as load-bearing is worth more than matching
NegPy's parameter list. **This gets a test**: two references differing only
by a per-channel constant must produce byte-identical gain maps.

### 2.2 The reference file

`.NEF` only. NegPy accepts ordinary images too; this program has exactly one
decode path and one colour story, and a JPEG reference would have to be
guessed into linear light. Non-RAW references are a punchlist item, not
scope.

The profile records the reference's full-resolution **aspect ratio**. A
run whose frames differ from it by more than 1% emits
`FLATFIELD_ASPECT_MISMATCH` — a warning, not a failure, because the user may
legitimately know better, but a portrait reference against landscape scans
would otherwise stretch the correction silently.

### 2.3 Storage

Gain maps live beside the library database and the previews, in Application
Support:

```
~/Library/Application Support/ScannyBoy/
    library.db
    previews/
    flatfield/<profile_id>.npz      ← new
```

`flatfield.flatfield_root()` is `library_db_path().parent / "flatfield"`,
exactly mirroring `previews.previews_root()`, so `SCANNY_BOY_LIBRARY_DB`
relocates gain maps along with everything else and the test suite gets
per-test isolation for free.

The `.npz` holds one float32 `(h, w, 3)` array plus a format version. The
profile is **self-contained**: once created, the original reference file can
be moved or deleted. Its path is kept as provenance only and is never read
again.

Profile metadata is a row in the library database (new table
`flatfield_profiles`), not a JSON sidecar — Swift is forbidden from reading
the library's storage directly, so profiles have to come back through a CLI
event either way, and the database is already that record.

### 2.4 The flat-field profile is a roll invariant

`flatfield.profile_token(profile)` — `{"profile_id", "gain_map_sha256",
"params"}` — is folded into `processing_params` under the key `flat_field`.
`processing_params` is already compared by
`roll_manifest.check_roll_invariants`, so this needs **no new comparison
code**: a roll locks to one profile with its first run, and a run using a
different profile (or none) is refused with `ROLL_INVARIANT_MISMATCH`.

Three consequences, all intended:

- **A roll can never mix corrected and uncorrected negatives.** That is the
  point.
- **Existing rolls will refuse new runs.** Their `processing_params` has no
  `flat_field` key. This is the same breakage the gain-normalization merge
  (#59) already caused through `stitch_params`; the remedy is the same —
  start a new roll.
- The key is **absent**, not `null`, when no profile is given, so a
  no-profile run still compares equal to a pre-flat-field roll. Anyone still
  using the CLI without `--flatfield` is unaffected.

`name` is deliberately **not** in the token: renaming a profile must not
invalidate a roll.

### 2.5 Required in the app, optional in the CLI

`--flatfield` is an optional flag on `convert`, `run`, and `probe`. The app
always passes one and disables Stitch until a profile is chosen. The CLI
stays a general tool and the existing test suite keeps working unchanged.

### 2.6 It costs no new progress step

Applying the gain happens inside the existing `PipelineStep.DECODE`
boundary. `STEPS_PER_FRAME` stays `3`. A fourth step would change what "one
conversion unit" costs, and `run_pipeline`'s `STITCH_UNITS_PER_FRAME = 2` /
`STITCH_UNITS_PER_NEGATIVE = 9` were calibrated against the current value —
re-deriving them is not worth a finer progress bar.

### 2.7 Memory: banded application, one shared map

The naive implementation — decode the whole 24.5MP frame to linear float32,
multiply by a full-resolution gain map, encode back — costs roughly
**294 MB** for the linear copy and another **294 MB** for a full-resolution
gain map, *per worker*. `concurrency.py` budgets **640 MiB per worker** and
that figure was measured without any of it. At `--jobs 4` this would blow the
budget.

So:

- The full-resolution gain map is materialised **once per run**, shared
  read-only across workers (~294 MB, one allocation, not per worker). Every
  frame of a run has the same dimensions — `consistency.py` already requires
  a shared orientation across the selection, and `read_active_size` already
  reads the size the run plans for.
- The multiply is applied **in horizontal bands** (`FLATFIELD_BAND_ROWS =
  512`), decoding, multiplying, and re-encoding each band back into the same
  `uint16` array in place. Peak transient per worker is band-sized — about
  37 MB — instead of 294 MB.

Net effect: the per-worker budget is untouched and needs no re-measurement.

### 2.8 The extra round trip through the transfer curve

The decoded frame is gamma-encoded `uint16`; the correction is multiplicative
and therefore only valid in linear light. Applying it means
`decode_to_linear → multiply → encode_from_linear`, one round trip more than
the pipeline does today.

`romm.DECODE_LUT` and `encode_from_linear` are exact inverses to within one
code, so this is not a meaningful loss — but it must be **proved, not
assumed**: a test asserts that a gain map of exactly `1.0` everywhere
round-trips a real decoded frame to byte-identical pixels.

Two related notes:

- `encode_from_linear` clips at `1.0`. Where the correction boosts an
  already-bright pixel past full scale it clips. On a negative scan the light
  through the film base sits well below clipping, so headroom exists — but
  the pipeline emits `FLATFIELD_HIGHLIGHT_CLIPPED` when more than 0.1% of a
  frame's pixels are clipped by the correction, rather than losing highlights
  silently.
- The punchlist item about writing intermediates in linear gamma would remove
  this round trip entirely. Not scope here; worth knowing they interact.

---

## 3. The chunks

### F-1 — `flatfield.py`: the maths and the store

New `cli/src/scanny_boy/flatfield.py`, no CLI surface yet. Every constant in
section 2.1 and 2.7 is defined **here and nowhere else**, following the
project's existing discipline for measured thresholds.

```python
GAIN_MAP_MAX_EDGE = 256
BLUR_SIGMA_DIVISOR = 16
GAIN_MIN, GAIN_MAX = 0.25, 4.0
GAIN_MAP_FORMAT_VERSION = 1
FLATFIELD_BAND_ROWS = 512
CLIPPED_PIXEL_WARN_FRACTION = 0.001

@dataclasses.dataclass(frozen=True)
class FlatFieldProfile:
    profile_id: str
    name: str
    gain_map_path: str
    gain_map_sha256: str
    source_path: str | None      # provenance only; never read again
    reference_width: int
    reference_height: int
    params: dict                 # how this map was built
    scanny_boy_version: str
    created_at: str

def flatfield_root() -> Path
def compute_gain(linear: np.ndarray) -> np.ndarray
def build_gain_map(reference: Path) -> tuple[np.ndarray, int, int]
def save_gain_map(profile_id: str, gain_map: np.ndarray) -> tuple[Path, str]
def load_gain_map(profile: FlatFieldProfile) -> np.ndarray
def resize_gain_map(gain_map, width: int, height: int) -> np.ndarray
def apply_in_place(pixels: np.ndarray, full_res_gain: np.ndarray) -> int
def profile_token(profile: FlatFieldProfile) -> dict
def build_params() -> dict
```

`apply_in_place` mutates the `uint16` frame band by band and returns the
count of pixels the correction clipped, so the caller can decide whether to
warn.

Failure modes are typed: `FlatFieldError(code, message)` for a missing or
corrupt `.npz`; reference decode failures propagate `UnsupportedRawError` /
`UnreadableRawError` from `metadata.py` unchanged, because a bad reference
NEF is a bad NEF and already has stable codes.

**Tests** (`flatfield_test.py`, co-located):

- A synthetic radial-falloff reference produces a map that boosts corners,
  leaves the centre near `1.0`, and clamps at the bounds.
- A perfectly flat reference produces an all-ones map (identity).
- Applying a map to the falloff image it was derived from flattens it within
  tolerance.
- The white-balance invariance test from §2.1.
- A gain map of `1.0` round-trips a decoded frame to identical bytes (§2.8).
- Banded application equals whole-array application, exactly.
- Save/load round-trips the array and the hash; dimensions never exceed
  `GAIN_MAP_MAX_EDGE`.

### F-2 — Profiles in the library, and the `flatfield` command family

**Schema.** Alembic revision `0002_flatfield_profiles`, following
`0001_initial`'s shape:

```
flatfield_profiles(
  profile_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,        -- the dropdown must be unambiguous
  gain_map_path TEXT NOT NULL,
  gain_map_sha256 TEXT NOT NULL,
  source_path TEXT,
  reference_width INTEGER NOT NULL,
  reference_height INTEGER NOT NULL,
  params TEXT NOT NULL,             -- JSONText
  scanny_boy_version TEXT NOT NULL,
  created_at TEXT NOT NULL)
```

`FlatFieldProfileRow` in `library/models.py`; `save_flatfield_profile`,
`list_flatfield_profiles`, `load_flatfield_profile`,
`delete_flatfield_profile`, and `rolls_using_flatfield` in `library/repo.py`.

**Commands.**

```
flatfield create --reference FILE --name NAME
flatfield list
flatfield delete --profile ID
```

`create` decodes, builds, saves, inserts, and emits `flatfield_created`.
`list` emits `flatfield_list`. `delete` **refuses** when any roll's
`processing_params.flat_field.profile_id` names it — the gain map is the only
thing that could reproduce that roll — with `FLATFIELD_PROFILE_IN_USE`;
otherwise it removes the row and the `.npz` and emits `flatfield_deleted`.

Each mirrors `roll init`/`roll list`'s `started` … `finished` bracketing and
carries no `run_id`; none is a pipeline run.

**New codes:** `FLATFIELD_PROFILE_NOT_FOUND`, `FLATFIELD_PROFILE_EXISTS`,
`FLATFIELD_PROFILE_IN_USE`, `FLATFIELD_GAIN_MAP_MISSING`,
`FLATFIELD_ASPECT_MISMATCH`, `FLATFIELD_HIGHLIGHT_CLIPPED`.

**Protocol version 5 → 6.** Per `ARCHITECTURE.md` §16, adding events and
codes means touching these four together, in one commit:
`events.py`, `shared/contract/CONTRACT.md`, `shared/contract/schema.json`,
`mac/ScannyBoy/CLIBridge/CLIEvent.swift` (whose string↔case mapping is tested
for completeness in `CLIEventTests.swift`), plus
`CLIEvent.supportedProtocolVersion`.

No change is needed to `manifest.schema.json` or
`roll-manifest.schema.json`: both already declare `processing_params` as an
open `{"type": "object"}`.

### F-3 — Applying it in the convert stage

`--flatfield ID` on `convert`, `run`, and `probe`.

`pipeline.run_convert(..., flatfield_profile_id: str | None = None)`:

1. **Before anything writes**, load the profile and its gain map. A missing
   profile or unreadable map raises `ConvertFailure`, alongside the existing
   `--jobs` memory check — the run fails having touched nothing.
2. `processing_params = raw_decode.jsonable_raw_params()`, plus
   `{"flat_field": flatfield.profile_token(profile)}` **only when a profile
   was given** (§2.4).
3. Compare the profile's aspect ratio against `read_active_size(selected[0])`;
   warn `FLATFIELD_ASPECT_MISMATCH` past 1%.
4. `resize_gain_map` once to the run's frame size; hand it to `_GroupContext`.
5. `_stage_one_frame` calls `apply_in_place` right after `decode_raw`, before
   `write_base_tiff`, inside the existing DECODE step boundary. Aggregate the
   clipped-pixel count and warn once per frame past the threshold.

`run_pipeline.run_full` threads `flatfield_profile_id` to `run_convert` and
does nothing else with it.

**`stitch_pipeline.py` needs no change at all.** It reads
`processing_params` off the work manifest and carries it into
`RollInvariants` already; putting the token there is what buys that.

`probe.py`'s `_preview_roll` builds the same candidate `RollInvariants`, so a
profile that disagrees with the selected roll surfaces as `rollError` in Add
Scans *before* Stitch is pressed rather than as a run failure.

**Tests:** an intermediate TIFF written with a synthetic profile differs from
the uncorrected one in the expected direction and by the expected amount; the
work manifest carries the token; a no-profile run carries no `flat_field`
key; an unknown id fails before the output folder is touched; a roll built
with profile A refuses a run with profile B; `probe --roll` reports that
mismatch.

### F-4 — The app: managing profiles

- `FlatFieldProfile` value type (mirroring `Roll.swift`) and
  `FlatFieldModel` — `@MainActor @Observable`, with `profiles`, `refresh()`,
  `create(reference:name:)`, `delete(_:)`, each one CLI call. Created in
  `RootView.resolveRunnerIfNeeded` beside the other models and passed down.
- `CLICommand.flatfieldCreate/List/Delete`.
- **Menu:** a `CommandGroup(after: .newItem)` item, "Flat-Field Profiles…",
  posting `.scannyBoyRequestFlatFieldProfiles` — the same notification
  pattern `Re-stitch…` already uses, and correct for the same reason: one
  window, one `ContentView` that could want it.
- **Sheet:** the profile list, each row with a trash button (confirmation
  warning that it cannot be recovered, and the CLI's
  `FLATFIELD_PROFILE_IN_USE` refusal shown as an alert), plus **New
  Profile…** — an `NSOpenPanel` limited to NEF, a name field, and Create with
  a spinner, since building a profile decodes a RAW and takes seconds. Modeled
  on `NewRollSheet`.

Deletion goes through the CLI, unlike deleting a roll folder
(`NSWorkspace.recycle`): a gain map is app-private data with a database row,
not a user document in the library.

### F-5 — The app: requiring a profile on Add Scans

- `ConfigurationModel.flatFieldProfileID: String?`, persisted under
  `com.lonniesmith.scanny-boy.lastFlatFieldProfile`, with a `didSet` that
  calls `scheduleValidation()` — the same shape `selectedFiles` and
  `rollURL` already have.
- When the selected roll is already locked to a profile, pre-select it and
  say so. The id arrives through `roll info`'s
  `processing_params.flat_field.profile_id`; `RollManifest.swift` exposes it
  as a typed accessor. Swift reads a field the CLI handed it and decides
  nothing — within the rule.
- `runEnabled` gains `flatFieldProfileID != nil`.
- A **Flat Field** section in `configurationSections`: a `Picker` over
  `flatField.profiles` and a Manage… button opening the F-4 sheet, with an
  inline hint when nothing is selected and a note when the roll is locked.
- `runCommand()` and the validation `probe` both pass `--flatfield`.

**Tests:** `ConfigurationModelTests` — Stitch stays disabled with no profile
even when everything else validates, and the roll's locked profile
pre-selects. `CLICommandTests` — argument order for the three new commands
and for `run`/`probe` with `--flatfield`.

### F-6 — Documentation

- `docs/ARCHITECTURE.md`: the §6.1 data-flow diagram, §5's module map, §7's
  colour section (the extra linear round trip and the clipping warning), §9's
  invariant list, §11's memory notes, and §13's app description.
- `docs/DECISIONS.md`: a flat-field section recording §2 of this file.
- `README.md` wherever it lists the command surface.
- `docs/punchlist.md`: the deferred pieces — non-RAW references, a
  per-image/per-negative toggle (NegPy has one; this design applies a
  profile to a whole roll by construction), black-frame subtraction, and
  re-measuring `MAX_OVERLAP_MAD` now that overlaps arrive de-vignetted.

---

## 4. Deliberate differences from NegPy

| NegPy | Here | Why |
| --- | --- | --- |
| Per-image "Apply Flat Field" toggle | Per-roll, by construction | A roll's invariants exist to stop one roll holding inconsistently processed negatives. A per-negative toggle would defeat them. |
| Reference may be RAW or an ordinary image | `.NEF` only | One decode path, one colour story. |
| `flatfield_token()` invalidates a render cache | The same token invalidates a **roll** | There is no render cache here; the equivalent guarantee is the invariant check. |
| Correction applied at render time, skipped for stitched composites and applied per tile instead | Applied once at convert time, per frame | This program's frames *are* the tiles; the intermediate TIFF is the natural place. |
| Reference decoded with no white balance | Decoded with the locked `RAW_PARAMS` | Per-channel normalisation makes the two identical (§2.1), and reuse beats a second decode configuration. |

## 5. Risks

1. **Existing rolls stop accepting new runs** (§2.4). Accepted; same as #59.
2. **Memory** (§2.7). The banded design is what keeps the 640 MiB per-worker
   budget honest. If the shared full-resolution map's ~294 MB proves
   objectionable, the fallback is resizing per band from the 256px map —
   cheaper in memory, fiddlier to get exactly right at band boundaries, and
   worth doing only if measurement demands it.
3. **Highlight clipping** (§2.8). Warned, not prevented.
4. **A profile is only as good as its reference.** A reference shot with the
   holder in place, at a different focus distance, or at a different aperture
   corrects the wrong falloff, and nothing in the software can tell. The
   aspect-ratio check catches only the crudest version of this.
