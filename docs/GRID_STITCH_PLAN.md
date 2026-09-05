# 2D grid stitching

Plan for stitching a negative from an R×C grid of scans rather than a
single strip. Constrained so that **every cell shows some rebate**, which
means `min(R, C) <= 2` — every cell touches the grid's outer boundary.

**Target workload.** 24MP frames (6000×4000), each covering a 24×36mm
patch of film — 166.7 px/mm on both axes — stepped so neighbours overlap
by 12mm on the frame's long dimension and 8mm on its short one. Both are
exactly **one third**, so the step is 2/3 of the frame in both directions
and the overlap fraction is uniform. Largest grid 5×2. §7.1 works out what
that costs and §7.3 what the overlap implies; the short version is that
the generous overlap makes the pair graph unusually well constrained, that
a grid is *cheaper* than the equivalent strip — and that **nothing beyond
2×2 runs today**, because `_attempt_solve` hands `estimate_peak_bytes` the
whole canvas as every frame's bounding box. Chunk G-0 (§1a) fixes that and
is a hard prerequisite for everything else; with it, 5×2 fits a 64 GB
machine with 26% headroom. Chunks G-4 and G-6 buy headroom on top.

**Notation.** Grids are written `across×down` throughout, matching
`--grid AxD`. The target's largest is `5×2` — five frames across, two
down — never `2×5`, which would name a different (and also legal) grid.

Written to be implemented in order. Each chunk is independently testable
and leaves the tree green.

---

## 0. What already works, and what does not

This matters more than usual here, because the instinct is to rewrite the
solver, and the solver needs no changes at all.

**Already shape-agnostic — do not touch:**

- `stitch_pipeline._attempt_solve` registers **all pairs**
  (`for i ... for j in range(i+1, ...)`), not just consecutive ones. A
  grid's vertical and diagonal neighbours are already attempted.
- `layout.solve_layout` is a general 2D similarity solve over an arbitrary
  pair graph (three weighted least-squares problems: log-scale, rotation,
  translation). Nothing in it assumes collinearity.
- `layout.check_connectivity` is union-find over accepted pairs.
- `layout.largest_valid_rect` builds a real 2D coverage mask and runs a
  histogram-and-stack sweep. Correct for a grid as written.
- `composite.composite`'s warp/accumulate pass, the gain solve
  (`layout.solve_gains`), normalization, `auto_rotate.estimate_rotation`
  (it fits `minAreaRect` to the scene mask — orientation-agnostic), the
  crop, TIFF writing, the edits ops log, export.
- `detect_rebate` and `withhold_dense_border` are density-based and
  geometry-independent — they look at pixel darkness, not at how frames
  are arranged — so grids do not affect them. Examined explicitly because
  #77 recently touched dense-border sanitation: no change needed, but they
  are on the do-not-touch list rather than merely unexamined.

**Actually blocking:**

1. `layout.Layout.strip_axis` is a *single* axis, and
   `composite._feather_weight` ramps along it. For a grid the axis is set
   to `None` (see below) and the blend silently falls back to
   `cv2.distanceTransform` — the isotropic feather whose 50/50 border
   collapse is exactly what `docs/STITCH_QUALITY_PLAN.md` §1.1 removed.
   **This is the only image-quality blocker.**
2. `layout.solve_layout` nulls `strip_axis` whenever
   `strip_spread_ratio > STRIP_SPREAD_RATIO` (0.15). At the target
   geometry a 3×2 measures 0.408, a 2×2 measures 0.667, and even the
   longest 5×2 measures 0.236 (§7.2). So (1) fires for every grid *in
   scope* — the one exception is an 8×2, which measures 0.144 and would
   slip under the threshold, but `MAX_PER_NEGATIVE = 12` admits no grid
   with 16 frames, so the qualifier is a precision point, not a live gap.
3. `stitch_pipeline._attempt_solve` emits
   `Code.STITCH_LAYOUT_UNEXPECTED` on that same condition. Every grid
   negative would warn. It is also the *only* structural sanity gate on
   the solved layout, so removing it leaves nothing in its place.
4. There is no way to say "this batch is a grid". `--per-negative N`,
   `Manifest.shots_per_negative`, `pipeline.build_groups` and
   `selection.group` all carry a flat count.
5. `stitch_pipeline._attempt_solve` passes the **whole canvas** as every
   frame's bounding box to `estimate_peak_bytes`
   (`(layout.canvas_size[1], layout.canvas_size[0])`,
   `stitch_pipeline.py:589`), so the gate charges `frame_count` canvases
   where `composite` allocates `frame_count` frame-sized boxes. At the
   target workload that is 198 GB of required RAM for a 5×2 instead of
   47.5, and it refuses every grid above 2×2 on a 64 GB machine before a
   single frame is warped. Pre-existing, and a bug in the estimate's
   *inputs* rather than its formula. Chunk G-0 (§1a).

---

## 1. Answering the design question: dims yes, order no

**Specify the grid dimensions. Do not depend on the capture order.**

Dimensions are worth requiring because they buy two things nothing else
can supply:

- **A defensible feather.** The fix for blocker (1) is a *separable*
  two-axis ramp: the product of two 1-D ramps, one along the grid's row
  direction and one along its column direction. To build it you need to
  know which of the two axes is which and how many cells sit on each, and
  the placed centres alone tell you neither. `strip_axis` is no help: it
  is nulled for every grid (§0), and even unnulled it names one direction
  with no cell count attached. With `R` and `C` known, cell assignment is
  a bijection check over a handful of frames, and the axes come from the
  solved frame rotations (§4.1) — exact at any grid shape.
- **A structural sanity gate to replace `strip_spread_ratio`.** With `R`
  and `C` you can check that the solved centres form a bijection onto the
  R×C cells with roughly uniform pitch. That catches the real 2D failure
  mode — a frame that slid half a cell or more, which fails the bijection
  outright (§4.1), or drifted a fraction of one, which the alignment check
  (§4.2b) catches at two rows where the pitch check is blind — neither of
  which `global_rms_px` catches, because a consistently-wrong layout can
  still fit its own pairs well.

Capture order is worth **declaring as a convention but not trusting**:

- It buys little, and the one thing it might have bought turns out to be
  actively unwanted. Pair discovery is already exhaustive and the solve
  needs no seed. The tempting use of order is to prune diagonal pairs as
  corner-touching slivers — but at this capture geometry they are nothing
  of the kind: a diagonal neighbour shares 2000×1333 px, **2.67 MP, 11% of
  a frame**, far above `MIN_PAIR_INLIERS` (40) and `MIN_GAIN_OVERLAP_PX`
  (1000). In a 5×2 there are 8 such pairs out of 21 that overlap at all
  (§7.3), so pruning them would discard **38% of the usable constraints**.
  Register all pairs and let `_row_weight` weight them.
- It is fragile. Import order is filename/timestamp order. It breaks the
  first time a frame is reshot, a file is renamed, or the user picks files
  out of order — and a wrong cell map is worse than no cell map, because
  it feeds the feather.
- Deriving cell assignment from the *solved* geometry is strictly more
  robust and costs about twenty lines.

So: **serpentine order is a documented assumption used only for the UI
grouping preview and for a warning.** §4 assigns cells from geometry, then
compares against the serpentine expectation and warns on disagreement
(`STITCH_GRID_ORDER_UNEXPECTED`) without changing behaviour. That gives
the user the diagnostic value of the convention with none of its risk.

---

## 1a. Chunk G-0 — make the memory estimate mean what it says

**Blocking, and independent of everything else.** Do this first: without
it no grid larger than 2×2 reaches the compositor at the target workload,
so none of G-1…G-5 can be validated against a real scan.

### 1a.1 The bug

`_attempt_solve` (`stitch_pipeline.py:589`) calls:

```python
estimate_peak_bytes(
    layout.canvas_size,
    frame_size,
    (layout.canvas_size[1], layout.canvas_size[0]),   # <- frame_bbox_size
    len(paths),
    ...,
)
```

The third parameter is `frame_bbox_size`, and the whole canvas is passed
for it. `estimate_peak_bytes` then charges `frame_count × canvas` for its
`all_warped` term, where `composite` actually allocates `frame_count`
boxes of `_frame_bbox` size — one frame, plus a little for rotation. With
one negative on a canvas barely wider than a frame the two agree closely
enough that nothing noticed. At ten frames on a 22000×6667 canvas they
differ by a factor of nine.

### 1a.2 The fix

`_frame_bbox` needs only `placement.matrix()`, the frame size and the
canvas size, all of which `_attempt_solve` already holds. Compute the real
boxes and pass the largest:

```python
boxes = [
    composite_module.frame_bbox(p.matrix(), *frame_size, layout.canvas_size)
    for p in layout.placements
]
frame_bbox_size = (max(b[3] for b in boxes), max(b[2] for b in boxes))
```

Promote `_frame_bbox` to a public `frame_bbox` while you are there — it is
part of the module's interface now, not an internal of `composite()`.
Taking the per-axis max across frames (rather than any one frame's box)
keeps the value an upper bound on every frame, which is what the
`frame_count ×` multiplier assumes.

Note the two shapes differ: `_frame_bbox` returns `(x, y, width, height)`
and `estimate_peak_bytes` wants `(height, width)`, the same
`frame_size`-style ordering it already uses. Getting that backwards is
silent — it produces a plausible number — so assert the orientation in the
test rather than eyeballing it.

### 1a.3 What it changes

Required RAM (`2 × peak`, since `check_memory_budget` fails above half of
physical memory) at the target workload:

| Grid | Today | After G-0 |
|---|---|---|
| 2×2 | 44.5 GB | 22.3 GB |
| 3×2 | 83.1 GB | 30.7 GB |
| 4×2 | 134.3 GB | 39.1 GB |
| **5×2** | **197.9 GB** | **47.5 GB** |
| 6×2 | 274.0 GB | 55.9 GB |

A 64 GB machine allows 32 GB of estimated peak, so today everything from
3×2 up is refused outright and 2×2 passes with almost nothing to spare.
After G-0 the target 5×2 fits with 26% headroom.

It also un-refuses strips that never needed refusing: a 3×1 of these
frames is charged 31.3 GB today against 18.4 GB after, and a 10×1 goes
from 226 GB to 50.4 GB.

### 1a.4 Tests

`composite_test.py`: `frame_bbox` is public and returns a frame-sized box
for an unrotated placement, and a slightly larger one for a rotated one.
`stitch_pipeline_test.py`: for a synthetic layout whose canvas is much
larger than one frame, the estimate scales with the frame box and not with
the canvas — monkeypatch `physical_memory_bytes` and assert that a case
which raises `INSUFFICIENT_MEMORY` today now passes, so the test states
the behaviour change rather than restating the formula. Assert the 5×2
number from §1a.3 directly; §8 wants that check early and this is where it
belongs.

---

## 2. Chunk G-1 — the grid spec, plumbed end to end

No image code changes. Pure plumbing, fully testable. This chunk also
carries the protocol version bump, the `--grid` CLI surface, and the
one-time landing of the new `_stitch_params` keys.

### 2.0 Protocol version bump

This plan adds three event codes (`STITCH_GRID_ORDER_UNEXPECTED` §4.4,
`STITCH_SPILL_TO_DISK` §6a.6, `INVALID_GRID` §2.2) and a CLI flag, so the
protocol bumps **9 → 10**. The sibling plans do this explicitly
(`GEOMETRIC_PLAN.md` "6 → 7", `STITCH_QUALITY_PLAN.md` "7 → 8"); the full
touch list, because it spans both languages:

- `events.py:18` — `PROTOCOL_VERSION = 9` → `10`, and the comment above it
  gains a "Protocol 10 (2D grid stitching): …" paragraph.
- `shared/contract/schema.json` — the `protocol_version` const.
- `events_test.py` — `test_protocol_version_is_nine` asserts the literal
  `9` (twice: the version-history test at ~line 229 and the header test);
  rename and update both.
- `CONTRACT.md` — the version-history section at the **top** of the file,
  plus the code table (three new codes) and the `--grid` paragraphs (§2.6).
- Swift: two switch arms in `CLIEvent.swift` (~411 and ~482) that map
  warning codes to friendly messages — **not** `_friendly_failure_message`,
  which handles *failures* and is the wrong pointer for the two new
  warnings (`INVALID_GRID` is an error, and touches that path instead) —
  plus the exhaustive code list in `CLIEventTests.swift:254`.
- Pre-existing drift, fixed while in these files:
  `docs/ARCHITECTURE.md:104` still says the protocol version is 6, and
  `CONTRACT.md`'s history stops at 8.

### 2.1 Representation

One new value type, in `cli/src/scanny_boy/selection.py`:

```python
@dataclasses.dataclass(frozen=True)
class GridSpec:
    """The 2D arrangement of one negative's scans.

    `across` runs left-to-right in capture space, `down` runs
    top-to-bottom; `across * down` is the batch's scans-per-negative. A
    strip is `GridSpec(across=N, down=1)`, which is what a batch with no
    grid declares, so the strip path is the R=1 case of the grid path and
    not a separate code path.

    `min(across, down) <= 2` by feature constraint: every cell must show
    rebate, which only holds when every cell touches the grid boundary.
    """
    across: int
    down: int

    @property
    def count(self) -> int: ...
    @property
    def is_strip(self) -> bool:  # down == 1 or across == 1
```

Validation lives beside it (`validate_grid`, raising a new
`InvalidGridError(ValueError)`, also in `selection.py`). It must **not**
raise `SelectionUsageError`: that exception is documented as "--files
doesn't correspond to the catalogue", and both handlers that catch it
(`pipeline.py:325`, `probe.py:350`) map it to `Code.NO_FILES` — a bad grid
shape reported as NO_FILES would be actively misleading. `InvalidGridError`
is a distinct type; `cli.py` maps it to `Code.INVALID_GRID` (§2.2) and no
other handler touches it. Rules: `across >= 1`, `down >= 1`,
`min(across, down) <= 2`, `across * down <= MAX_PER_NEGATIVE` (12, which
already admits the 5×2 target with room — do not raise the cap; §7.1 shows
12 frames of 24MP sits at the memory gate even after G-0).

`MIN_PER_NEGATIVE` and `MAX_PER_NEGATIVE` currently live in `cli.py`
(lines 74–76), and `selection.py` must not import from `cli.py` — the
dependency runs the other way. **Move both to `selection.py`** and have
`cli.py` import them; its existing range check at `cli.py:779` keeps
working unchanged.

`selection.group` is unchanged: grouping is still chunk-by-count, using
`spec.count`.

### 2.2 CLI surface

Add `--grid AxD` (e.g. `--grid 3x2`) to `probe`, `prepare`, and `run`,
everywhere `--per-negative` is accepted (`cli.py` lines ~129, ~146, ~177).
`stitch` still takes neither — it reads the batch from the work manifest.

**`required=True` comes off `--per-negative`.** It is currently
`required=True` on `prepare` (cli.py:146) and `run` (cli.py:177). Replace
that with an exactly-one-of check: exactly one of `--grid` / `--per-negative`
must be supplied on those two commands, and on `probe` when `--files` is
given (the existing conditional check, extended). Omitting both is an
argparse-usage error naming both flags — this is a change from today's
failure mode (argparse's own "the following arguments are required:
--per-negative"), and it is the behaviour the plan specifies. The
mutual-exclusion half cannot be expressed by `argparse` across two
optional flags cleanly here because `probe`'s is conditional, so do the
pair check in the same manual block rather than mixing mechanisms.

Rules, enforced in `cli.py`'s validation block (~line 773). The block has
two error mechanisms today and the split is deliberate — name it:

- **`_usage_error(parser, …)`** (argparse-style usage error) for:
  malformed `--grid` (message naming the `AxD` form), the
  mutual-exclusion violation, and the exactly-one-of violation above.
- **`ErrorEvent(code=Code.INVALID_GRID)` + return 2** for: a well-formed
  `--grid` whose `GridSpec` fails `validate_grid` (message naming the
  `min(across, down) <= 2` rule and why — every cell must show rebate —
  or the count cap), and the `MIN_PER_NEGATIVE`/`MAX_PER_NEGATIVE` range
  check, which runs against `spec.count` and keeps its existing
  `INVALID_PER_NEGATIVE` code when the flag was `--per-negative` but uses
  `INVALID_GRID` when it was `--grid`.
- `INVALID_GRID` is a new code: add to `events.py`, the `CONTRACT.md` code
  table, and the Swift error rendering (part of the §2.0 bump).
- Two existing checks in that block need updating, and neither is
  optional: `probe --files requires --per-negative` (`cli.py:777`) becomes
  "requires `--per-negative` or `--grid`", and the range check
  (`cli.py:779`) runs against `spec.count`.
- Internally, immediately normalise: `--per-negative N` becomes
  `GridSpec(across=N, down=1)`. **Every consumer on the stitch path — the
  work manifest, `_attempt_solve`, `solve_layout`, the roll manifest —
  sees a `GridSpec`, never a bare int.** This is the single most important
  structural choice in the chunk: it stops the grid being a special case.
  The *grouping* helpers are the deliberate exception and keep their `int`
  signatures — `selection.group`, `selection.nearest_valid_counts` and
  `pipeline.build_groups` chunk by count and genuinely do not care about
  shape. Callers pass `spec.count`.

**Consumers of the count, enumerated** so the implementer checks each
rather than discovering them at review time. All take the flat count and
need no signature change — callers pass `spec.count` — but each must be
confirmed:

- `selection.group`, `pipeline.build_groups` — the grouping helpers
  already excepted above.
- `largest_group_size` at `pipeline.py:836` and `probe.py:193` (feeds the
  disk check).
- `run_pipeline.py:156`'s `negative_count` (progress accounting).
- `concurrency.resolve_worker_count(shots_per_negative, jobs)` — worth
  singling out because it runs **its own memory budget** and can refuse a
  workload before `estimate_peak_bytes` is ever reached; confirm a 12-frame
  grid passes it at the target workload rather than finding out in G-5.

### 2.3 Work manifest

`manifest.Manifest` keeps `shots_per_negative` (it is required by
`shared/contract/manifest.schema.json` and by `run`'s resume comparison at
`manifest.py:492`) and gains a sibling:

```python
grid: dict[str, int] | None = None   # {"across": A, "down": D}
```

`None` means a pre-grid manifest, read back as
`GridSpec(across=shots_per_negative, down=1)`. `shots_per_negative` must
always equal `across * down`; assert on load.

The resume comparison at `manifest.py:492` must compare the **grid**, not
just the count: a 3×2 and a 6×1 batch are not the same batch even though
both are six scans. Extend the existing mismatch error.

Schema (`shared/contract/manifest.schema.json`): add an optional `grid`
object with required `across`/`down` integers ≥ 1. `additionalProperties`
is `false` in places — check and update.

### 2.4 Roll manifest

`roll_manifest.NegativeRecord` gains:

```python
grid: dict[str, int] | None = None      # {"across": A, "down": D} as declared
grid_cells: dict[str, list[int]] | None = None  # member name -> [row, col], solved (§4)
grid_pitch_ratio: float | None = None      # §4.2(a); null when unmeasurable
grid_alignment_ratio: float | None = None  # §4.2(b)
```

All four are null for a negative written by a pre-grid build. Add to
`to_dict`, to the reader, and to `shared/contract/roll-manifest.schema.json`
(note `additionalProperties: false` at lines 202 and 393).

**`_stitch_params` churns once here, by design.** It already carries
`"feather": composite_module.FEATHER`; it gains exactly three keys in this
chunk and is then not touched again until G-4:

- `"grid_pitch_ratio_min"` and `"grid_alignment_ratio_max"` — the two
  constants §4.2 will wire up in G-3. Define the constants themselves
  **here**, beside `STRIP_SPREAD_RATIO` in `layout.py`, with the values
  §4.2 gives (0.6 and 0.25); G-3 only reads them. Defining an unused
  constant for one chunk is the price of landing the params once.
- `"feather_floor_fraction"` — the new `_FEATHER_FLOOR_FRACTION` constant
  in `composite.py` (§5.1), likewise defined here with its starting value
  and unused until G-4.

Adding keys to `_stitch_params` makes every roll written by an earlier
build fail `check_roll_invariants`, which compares the whole dict bar
`geometry` (`roll_manifest.py:559`). Accepted deliberately: nothing has
shipped, and the remedy is to delete the old roll folders. G-4's change
of the `FEATHER` *value* is the one remaining, unavoidable churn (§5.1);
it re-breaks the same check and the same remedy applies — say so in
G-4's summary rather than letting it surprise anyone.

### 2.5 Swift app

- `ConfigurationModel`: replace the stored `perNegative: Int?` with a
  stored **optional `across: Int?`** and a stored **non-optional
  `down: Int = 1`**, and make `perNegative` a *computed* `Int?` returning
  `across * down` when `across` is set and `nil` otherwise. **Down
  defaults to 1 and is not optional**: with both pickers optional, a
  plain strip run would need two selections where it needs one today.
  Defaulting down leaves only Across
  carrying the "not chosen yet" state, so the nil gate and today's flow
  survive intact. Two things still do **not** survive the naive version
  of the change:
  - `perNegative`'s `didSet` (line 94 — `guard perNegative != oldValue
    else { return }`, which re-validates the selection because grouping
    and divisibility depend on the count) cannot live on a computed
    property. Factor its body into a private method and call it from
    `didSet` on *both* stored properties, keeping the same-value guard
    against the previously computed product so a no-op edit still costs
    nothing.
  - `across == nil` is the "not chosen yet" state. It gates `runEnabled`
    (line 166) and drives the `perNegativeHint` caption; it must stay
    optional, or Convert silently enables before the user has chosen
    anything.
- `ConfigurationModel`'s two call sites that forward the count
  (`~206` and `~275`) must now pass across/down through explicitly —
  including the **probe** call, which is what feeds the grouping preview;
  a grid configured in the UI must preview as a grid.
- `ContentView.swift` (~line 302): the "Scans per negative" picker becomes
  two pickers, "Across" (1…12) and "Down" (1…2), inside the same section.
  Across keeps a `Text("Choose…").tag(Int?.none)` row so the unchosen
  state is reachable; Down does **not** — it defaults to 1 and shows
  1…2 directly. The existing hint text rewords to show while `across` is
  nil. Constrain the menus so the product stays ≤ 12 and `min <= 2` —
  with Down capped at 2, that reduces to clamping Across to `12 / down`.
  Show the product ("6 scans per negative") as caption text so the user
  still sees the number the grouping preview uses. Keep the
  `perNegativePicker` accessibility identifier on the Across picker and
  add `downPicker`, so the existing UI tests keep a handle.
- **`2×5` is CLI-only.** The Notation section calls it legal and
  `validate_grid` accepts it, but the UI caps Down at 2, so it is
  reachable only through `--grid 2x5` on the command line. State this
  wherever the grid shapes are documented (§2.6) so nobody reads the UI
  as exhaustive.
- `CLIRunner.swift` (three call sites, ~69/102/140): emit `--grid AxD`
  instead of `--per-negative N` whenever `down > 1`; keep emitting
  `--per-negative N` when `down == 1`, so a strip run's command line is
  byte-identical to today's and existing Swift tests do not churn.

### 2.6 Docs

`shared/contract/CONTRACT.md` (the command table at ~line 89, the
`--per-negative` paragraph at ~line 206, the version history per §2.0),
`docs/ARCHITECTURE.md` (~126, and the stale protocol version at ~104),
`README.md`. State the `min(across, down) <= 2` rule and its reason once,
in CONTRACT.md, and reference it elsewhere. Note there that `2×5` is legal
and CLI-only (§2.5).

### 2.7 Tests

`selection_test.py`: `GridSpec` validation, including the rejection of
3×3 with a message naming the rebate rule. `cli_test.py`: `--grid`
parsing, mutual exclusion with `--per-negative`, **omitting both flags on
`prepare`/`run` (the new exactly-one-of failure, asserting the changed
failure mode)**, the implied count, malformed-grid and
`InvalidGridError`→`INVALID_GRID` usage errors. `manifest_test.py`:
round-trip with and without `grid`, the
`shots_per_negative == across * down` assertion, and the resume mismatch
between 3×2 and 6×1. Schema tests via the existing
`manifest_schema_test_support.py` / `roll_manifest_schema_test_support.py`.
`events_test.py`: the renamed protocol-version test (§2.0). Swift:
`ConfigurationModel` with down defaulting to 1, across optional,
`perNegative` computed; `CLIRunner`'s emission rule.

---

## 3. Chunk G-2 — the layout gate

Small, and it unblocks running a grid end to end even before the feather
lands (with a known-imperfect blend).

### 3.1 `layout.py`

`solve_layout` gains a keyword-only `grid: GridSpec | None = None`
(default `None` preserves every existing call and every existing test).

- Keep `strip_spread_ratio` and `strip_axis` exactly as they are. They
  remain correct and meaningful for `grid.is_strip` and for `None`.
- Add to `Layout`:

  ```python
  grid_axes: tuple[tuple[float, float], tuple[float, float]] | None
  cells: dict[str, tuple[int, int]] | None   # name -> (row, col)
  grid_pitch_ratio: float | None      # None when no axis has 3+ positions
  grid_alignment_ratio: float | None
  ```

  All `None` unless a non-strip `grid` was passed and cell assignment
  succeeded (§4).

### 3.2 `stitch_pipeline._attempt_solve`

The `strip_spread_ratio > STRIP_SPREAD_RATIO` warning at ~line 570 becomes
conditional:

- `grid.is_strip` (or no grid): unchanged. Same condition, same
  `STITCH_LAYOUT_UNEXPECTED` code, same message.
- Non-strip grid: the spread-ratio check is **not** run — it is the wrong
  question. The §4 cell-assignment check runs instead, and emits
  `STITCH_LAYOUT_UNEXPECTED` with a grid-specific message when the
  assignment is not a clean bijection or the pitch/alignment checks trip.

Keep the code enum unchanged; do **not** add a new failure code for this.
It is the same finding ("the solved layout is not the shape we expected")
with a different test behind it, and the Swift side already renders it.

### 3.3 Tests

`layout_test.py`: a synthetic 3×2 of placements solves without the
strip warning; the existing strip tests still pass with `grid=None` and
with an explicit 1D `GridSpec`. Assert order-independence of the solve
still holds with a grid passed (the existing test is the model).

---

## 4. Chunk G-3 — cell assignment and the sanity gate

New private helpers in `layout.py`, called from `solve_layout` when a
non-strip grid is supplied.

### 4.1 Assignment

Input: the shifted placements — each frame's centre transformed into
canvas space, and each frame's solved `rotation_deg` — plus
`GridSpec(across=C, down=R)`.

1. **Take the axes from the solved rotations, not from an SVD of the
   centres.** The frames were stepped along the camera's own sensor axes,
   so the grid's column and row directions *are* the frames' axes: with
   `theta` the circular mean of the placements' `rotation_deg`, the
   candidate axes are `(cos theta, sin theta)` — the frames' width
   direction — and `(-sin theta, cos theta)`. This is exact whatever the
   grid's shape, cell count or pitch, needs no clustering, and is why §1
   does not lean on the centre cloud.

   Keep the SVD of the mean-subtracted centres (the one `_strip_geometry`
   already computes — refactor so one SVD serves both) as a
   **cross-check**, not as the source. For a regular grid its two
   right-singular vectors span the same pair, so the bases should agree
   to within a degree or so. They stop agreeing as the singular values
   converge, and there the SVD basis is not merely ambiguous in *ordering*
   but arbitrary in *direction*: at equal singular values numpy may return
   a basis rotated 45° off the grid, which no amount of trying both
   orderings recovers. Disagreement beyond a few degrees is itself a
   signal — treat it as assignment failure (step 4). §7.2 measures how far
   the target geometry actually sits from that edge.
2. **Assign by snapping to the nearest cell centre, not by cutting
   gaps.** Project the centres onto each candidate axis. Along one axis
   the grid has `C` distinct positions each shared by `R` frames, along
   the other `R` positions each shared by `C`. For a candidate axis with
   `n` expected positions, estimate the pitch as `(max − min) / (n − 1)`
   over the projections and snap each projection to the nearest integer
   multiple of that pitch. The correct (axis, orientation) assignment is
   the one whose snapping yields the declared structure — `C` groups of
   `R` along the across axis, `R` groups of `C` along the down axis. With
   `min(R, C) <= 2` and `R*C <= 12` this is a handful of frames and a
   brute-force check over both assignments is fine and clearer than
   clustering.

   Snap-to-nearest is chosen over the obvious alternative — sorting the
   projections and cutting at the `C−1` largest gaps — deliberately, and
   the reason is the magnitude boundary it buys. Gap-cutting fails first
   for exactly the drift check §4.2(b) exists to catch: a bottom-row
   frame shifted even ~0.4 of a cell sideways already merges two
   projections into one group and produces unequal groups, so *every*
   sideways failure would collapse into "assignment failed" and the
   alignment ratio would be unreachable. Snapping gives a clean split:

   - a frame displaced **less than half a cell** snaps to its true cell;
     the assignment succeeds and sub-cell drift is what §4.2(b) measures
     and warns about;
   - a frame displaced **half a cell or more** snaps into a neighbouring
     cell, the bijection in step 4 fails, and assignment fails outright —
     all grid fields `None`, distance-transform fallback, warning.

   When `R == C` — which under the rebate rule means only 2×2 — both
   orderings succeed and nothing in the geometry breaks the tie; break it
   in favour of the frames' own width direction, `(cos theta, sin theta)`,
   which is the axis a 2×2 was stepped along first by the documented
   capture habit. (Pitch-estimate robustness: a sub-cell displacement
   leaves the extent unchanged when interior and changes it by at most
   the drift at an edge, so the estimate stays well inside the half-cell
   snapping tolerance in exactly the regime check (b) owns. A full-cell
   displacement can distort the estimate, but the mis-snapping still
   fails the bijection — same outcome either way.)
3. Emit `cells: {name: (row, col)}`.
4. **Bijection check**: every one of the `R*C` cells is claimed exactly
   once. If not, no assignment — return `None` for all four grid fields
   (`grid_axes`, `cells`, `grid_pitch_ratio`, `grid_alignment_ratio`),
   and let `_attempt_solve` warn.

**Assignment failure costs blend quality, not just a diagnostic.** With
`grid_axes` `None` and `strip_axis` already nulled by the spread ratio,
`Layout.feather_axes()` (§5.1) comes back empty and the blend falls back
to the distance transform — §0's blocker (1), back at runtime. So the
`STITCH_LAYOUT_UNEXPECTED` message for a failed assignment must say the
negative was blended isotropically and why that is visible, not merely
that the layout looked odd; and §4.5 asserts the fallback path, not only
the warning.

### 4.2 Regularity checks

Two checks, because neither alone covers the shapes this feature targets.
The division of labour with §4.1's bijection check is by displacement
magnitude: **half a cell or more** fails assignment outright (§4.1 step
4); **sub-cell drift** keeps a clean bijection and is what these two
checks measure.

**(a) Pitch ratio.** For each axis, sort the cell-centroid positions along
it and take the gaps between adjacent positions; `grid_pitch_ratio` is
`min(gap) / max(gap)`, worst of the two axes. A regular grid is near 1.0.
Its remaining role after the bijection check takes the whole-cell failures
is graduated irregularity — a wrong step size, or drift that grows across
the row — which keeps a clean bijection and near-zero column spread.
(A single frame displaced a whole cell fails the bijection before this
check runs; a frame displaced a fraction of a cell moves its column's
centroid by only half the displacement, so check (b) is the more sensitive
of the two to isolated sub-cell drift.)

**This check needs at least three positions on an axis to say anything.**
With `k` positions there are `k - 1` gaps, and a single gap has nothing to
be compared against — the ratio is trivially 1.0. So for the target
workload (§7.1) it constrains the across-axis of a 5×2 and **says nothing
at all about the down-axis**, and for a 2×2 it is vacuous in both
directions. Report `None` for an axis with fewer than three positions
rather than a meaningless 1.0, and take the worst over the axes that
actually have a value. If neither axis qualifies, `grid_pitch_ratio` is
`None` and check (b) carries the gate alone.

**(b) Cross-axis alignment.** This is the primary check for an N×2 grid,
and the one that works where (a) cannot. Frames sharing a column should
sit at the same across-position, and frames sharing a row at the same
down-position. So: for each column, take the spread (max − min) of its
members' across-projections; divide by the median pitch along the
across-axis. Same for rows against the down-axis. `grid_alignment_ratio`
is the worst such value — 0 for a perfect grid, and up to just under 0.5
for the largest displacement that still assigns to the true cell (§4.1's
half-cell snapping boundary). `GRID_ALIGNMENT_RATIO_MAX = 0.25` therefore
means: drift beyond a quarter of a cell warns, drift beyond half a cell
has already failed assignment below.

This catches exactly the N×2 failure that (a) cannot: one frame in the
bottom row displaced sideways relative to the top row by a fraction of a
cell. It needs no three positions on any axis, which is why it survives
2×2.

New module constants beside `STRIP_SPREAD_RATIO` — **defined in G-1**
(§2.4) so `_stitch_params` lands once, wired up here in G-3:

```python
# A grid whose adjacent cell pitch varies by more than this along an axis
# with enough cells to measure it, or whose rows and columns are this far
# out of alignment relative to the cell pitch, is not the grid that was
# declared — most likely a frame solved into the wrong cell. Warnings, not
# failures: the negative still publishes and the user can judge the canvas.
# (Displacement of half a cell or more is caught earlier, by the bijection
# check in §4.1; these govern sub-cell drift only.)
GRID_PITCH_RATIO_MIN = 0.6
GRID_ALIGNMENT_RATIO_MAX = 0.25
```

**Both are unmeasured starting values.** Treat them the way
`docs/STITCH_QUALITY_PLAN.md` treats its constants: landed (in G-1, §2.4)
and recorded in `_stitch_params` as `"grid_pitch_ratio_min"` and
`"grid_alignment_ratio_max"`, record the measured
`grid_pitch_ratio`/`grid_alignment_ratio` per negative in
`NegativeRecord`, and revisit at a user gate once there are real scans to
measure against. Say so in the docstring. `GRID_ALIGNMENT_RATIO_MAX = 0.25`
is the looser guess of the two — a quarter of a cell of row-to-row drift
is visibly wrong but well clear of ordinary registration slop — and is the
more likely of the pair to need moving.

### 4.3 Axes

`grid_axes = (across_axis, down_axis)` — the two unit vectors from §4.1's
step 1, ordered by step 2 so `across_axis` corresponds to the `across`
dimension.
These feed §5. As with `strip_axis`, the §5 weight formula is symmetric
under a sign flip, so no sign canonicalisation is needed; say so.

### 4.4 The order warning

In `_attempt_solve`, after the solve: compute the serpentine cell sequence
implied by `GridSpec` and the member order in `GroupRecord.members`, and
compare against `layout.cells`. On disagreement emit a new warning code
`Code.STITCH_GRID_ORDER_UNEXPECTED` naming the frames that landed
elsewhere. **Warning only** — the solved assignment always wins. Record
the solved assignment as `NegativeRecord.grid_cells` regardless.

Add the code to `events.py` and to `CONTRACT.md`'s code table. On the
Swift side this is a *warning*, so it goes to the warning-message switch
arms in `CLIEvent.swift` (~411/~482) and the exhaustive code list in
`CLIEventTests.swift:254` — the §2.0 bump list — **not** to
`_friendly_failure_message`, which handles failures only and would send
the implementer to the wrong place.

Serpentine is defined as: start at cell (0, 0), traverse the `across`
dimension, reverse direction each row. This matches the stated capture
habit. Do not try to detect other traversals.

### 4.5 Tests

`layout_test.py`: synthetic 5×2, 3×2 and 2×2 grids assign correctly from
geometry alone, in shuffled input order; `grid_axes` are orthogonal and
point the expected way, including for a grid solved at a deliberate 3°
rotation, which is the case that separates the rotation-derived axes from
the centre-cloud SVD. A layout whose frames are placed at 45° to the
declared grid fails assignment and returns `None` for all **four** grid
fields, and the resulting layout composites through the distance-transform
fallback (§4.1) — assert that, not only the warning. For the gates, the
magnitude boundary of §4.1 step 2 is what the tests pin down:

- a frame displaced **a full cell** — along a row on a 5×2, or sideways in
  the bottom row of an N×2 — fails the **bijection** check: all four
  fields are `None`, the layout warns, and the blend falls back. (It does
  *not* reach the pitch or alignment checks; the old claim that a
  full-cell sideways shift "trips the alignment check" was unsatisfiable,
  since §4.1's snapping fails assignment at half a cell first.)
- a frame displaced **~0.4 of a cell** sideways in the bottom row of an
  N×2 keeps its true cell, so all four fields are present, and the
  **alignment** check fires while the pitch check does not: the displaced
  column's centroid moves by only half the displacement, leaving
  `grid_pitch_ratio` ≈ 0.67 — above the 0.6 floor — while
  `grid_alignment_ratio` = 0.4 exceeds its 0.25 ceiling. That asymmetry is
  the reason (b) exists and should be asserted explicitly, including that
  the fields are populated (a gap-cutting implementation returns `None`
  here, which is why it was rejected in §4.1).
- `grid_pitch_ratio` is `None` for a 2×2 (no axis has three positions)
  and not-None for the across-axis of a 5×2.

`stitch_pipeline_test.py`: the order warning fires
for a reversed member list and does not fire for serpentine order.

---

## 5. Chunk G-4 — the separable feather

The image-quality payload. Do this last; everything above is testable
without it.

### 5.1 `composite.py`

Replace the `axis` parameter of `_feather_weight` with `axes`:

```python
def _feather_weight(mask, bbox_x, bbox_y, axes):
    """Blend weight for one warped frame, in its own bounding box.

    `axes` is a tuple of one or two unit vectors. Along each, the weight
    ramps from the frame's own extent on that axis — distance from the
    nearer end — and the returned weight is the *product* of the per-axis
    ramps, floored once at the end. One axis is the strip case
    (docs/STITCH_QUALITY_PLAN.md section 1.3), unchanged. Two axes is a
    grid: the ramp is separable, so a pixel's crossfade profile across a
    vertical seam is the same at the top of the canvas as in the middle,
    and likewise for horizontal seams — the same guarantee the strip ramp
    makes, in both directions at once. Empty `axes` (a layout that is
    neither) falls back to the distance transform.
    """
```

The existing single-axis body becomes a `_axis_ramp(mask, bbox_x, bbox_y,
axis)` helper; `_feather_weight` multiplies the ramps and applies both the
floor and the `weight[~covered] = 0.0` **after** the product, not
per-axis, so a covered pixel keeps a positive weight and a four-way corner
does not land on `floor²` (§7.3). `_axis_ramp` keeps the
`if not covered.any()` early-out the current `_feather_weight` has — the
projection-and-ramp arithmetic is undefined on an all-empty mask, and the
guard is easy to lose when extracting the helper.

**Scale each ramp to [0, 1] before multiplying, in the two-axis case.**
Today's ramp is in *pixels*: it runs to about half the frame's extent,
~3000 at this workload, and `_FEATHER_FLOOR = 1.0` is documented as a
pixel distance ("every covered pixel keeps a positive weight, the same
invariant `cv2.distanceTransform` gave for free"). A raw product of two
such ramps is in px², running to ~6e6, against which a floor of 1.0 is
~2000× weaker in relative terms — the literal invariant still holds, but
nothing else the constant's comment says about it does. Divide each ramp
by its own `(s_max - s_min) / 2` so the product is dimensionless in
[0, 1] and the floor is a fixed *fraction* of full weight. The blend is
unaffected: the accumulate pass normalises by the summed weight, so any
per-frame constant factor cancels.

That makes the floor mean two different things on the two paths, so give
it two names: keep `_FEATHER_FLOOR = 1.0` (px) governing the one-axis
path — §5.3 requires the strip weights to stay byte-identical, which rules
out rescaling there — and use `_FEATHER_FLOOR_FRACTION` for the two-axis
path. That constant is **defined in G-1** (§2.4) so `_stitch_params` lands
once; its starting value is `1e-3`, chosen as the same order as the strip
floor's relative magnitude (1.0 px against a ~3000 px ramp is ~3e-4) and
documented as an unmeasured starting value like the §4.2 constants — it
is recorded in `_stitch_params` as `"feather_floor_fraction"` and
revisited at the same user gate. Scale only when `len(axes) == 2`.

Call sites: the one real call is `composite.py:619`; `composite.py:509` is
the numbered-steps docstring inside `composite()` and needs the same
change in prose. Both take
`layout.grid_axes or ((layout.strip_axis,) if layout.strip_axis else ())`.
Put that normalisation in one small helper on `Layout`
(`Layout.feather_axes()`) rather than duplicating the expression.

Update `FEATHER` to a single new value, `"axis-separable"`, recorded for
**every** roll — strip or grid — because strips now run the same
separable code path; the one-axis case is just the degenerate one-axis
tuple. The tempting refinement, `"strip-axis"` for one axis and
`"grid-axes"` for two, cannot be recorded at roll level:
`_stitch_params(profile)` is computed from module constants and has no way
to know whether a roll contains a grid negative. And the compatibility
clause this paragraph used to carry — "keep the old string for strip-only
rolls so existing manifests still compare equal" — is both impossible for
the same reason and unnecessary: §2.4 has already accepted breaking
`check_roll_invariants` for pre-existing rolls, so the value changes
everywhere at once. Record the actual axis count per negative in the
negative record, where it belongs.

### 5.2 Memory

`estimate_peak_bytes`: `docs/STITCH_QUALITY_PLAN.md` §1.4 added
`feather_scratch = bbox_pixels * 4 * 2`. With two ramps and a product it
becomes `bbox_pixels * 4 * 3`. One additive term, not per-frame. Update
the docstring and
`composite_test.test_peak_estimate_counts_the_source_frame_and_the_safety_factor`.

At the target workload this third buffer is one bbox of float32 — 96MB
raw, ~336MB after `MEMORY_SAFETY_FACTOR` — against the 23.8 GB peak §7.1
gives for a 5×2 after G-0. It does not move the feasibility line. Do not
let it become a reason to complicate the formula.

**While you are here: stop retaining the feather weight.**
`_WarpedFrame.weight` is a bbox-sized float32 held for every frame until
the accumulate pass — 0.89 GB of the 6.79 GB live total at 5×2, the third
largest term. But it is *only* read in the accumulate loop; the two
photometric passes use `.linear` and `.mask` and never touch it. So drop
the field and call `_feather_weight(entry.mask, entry.x, entry.y, axes)`
inside the accumulate loop instead.

This is a ~13% cut for a few lines, and G-4 is the right moment because it
is already rewriting that function. Two consequences: `estimate_peak_bytes`
drops `bbox_pixels * 4` from the per-frame term and keeps only the
`feather_scratch` term (now live for one frame at a time in both passes,
which is what it always claimed to be); and the separable ramp is computed
once per frame rather than held, so the product form costs nothing extra
in residency. In gate terms it takes a 5×2 from 47.5 GB of required RAM
to 44.0 — the warp branch stops binding and the canvas branch takes over,
which is the whole subject of §7.4 and the reason G-6 needs its second
half. See §7.4 for the structural options this does *not* attempt.

### 5.3 Tests (`composite_test.py`)

Mirror §1.6 of `docs/STITCH_QUALITY_PLAN.md`:

- Every existing feather test still passes (`test_feather_weights_sum_to_one_inside_coverage`,
  `test_reconstruction_is_order_independent`,
  `test_uncovered_pixels_are_exactly_fill_color`, …).
- **New:** with two axes, for two frames in the **same row** (A and B),
  the ratio `w_A / (w_A + w_B)` at a fixed position along the across-axis
  is equal at the top, middle, and bottom of their vertical overlap band —
  the 2D analogue of the strip regression, and the whole point of the
  chunk. This is the correct invariant, and the form an earlier draft of
  this test ("frame A's normalised contribution is equal at the top,
  middle, and bottom") was not: in a 2-row grid frame A does not reach
  the bottom of the vertical overlap at all, and in the four-way band its
  absolute normalised share varies with y as row 1 fades in. The ratio is
  the quantity that is y-invariant, and it holds because same-row frames
  share a down-extent, so their along-down ramp factors cancel in the
  ratio. Assert the ratio, not the share.
- **New:** the same, transposed, for same-**column** frames across a
  horizontal overlap (the ratio between frames sharing an x-extent is
  x-invariant).
- **New:** a one-axis `axes` tuple reproduces today's strip weights
  exactly (byte-for-byte), and an empty tuple reproduces the distance
  transform exactly. These two are the no-regression guarantees.
- **New:** a synthetic 2×2 scene with a known pattern reconstructs it, and
  a deliberate 3 px misregistration in one cell produces a bounded step
  rather than a widening blur toward the canvas corners.
- **New — the four-way corner (§7.3).** At 1/3 overlap, 7.3% of a 5×2
  canvas is covered by *four* frames at once. Build a 2×2 at that overlap
  and assert, over the region all four cover: every weight is strictly
  positive (the `_FEATHER_FLOOR` is applied to the product, not per-axis),
  the four normalized weights sum to 1, and the blend is smooth — no
  interior pixel's weight vector jumps by more than a small bound between
  neighbouring pixels. This is the case the isotropic transform gets
  wrong and the reason the ramp is separable rather than one-dimensional.

---

## 6. Chunk G-5 — end to end

- `stitch_pipeline_test.py`: a full synthetic 3×2 run publishes a TIFF of
  the expected canvas size, with `grid` and `grid_cells` in the roll
  manifest and no `STITCH_LAYOUT_UNEXPECTED` warning.
- `run_pipeline_test.py`: `run --grid 3x2` end to end.
- Slow tier: if grid sample NEFs exist in `tests/fixtures/nef/`, add a
  `slow`-marked real-scan case following the existing gate-B pattern
  (`sample_nef_support.stage_samples`). If they do not, **do not fabricate
  one** — note in the chunk's summary that the real-scan validation is
  outstanding and needs a shot grid from the user.
- Swift: `ConfigurationModel` grid selection, picker constraints, and the
  `--grid` vs `--per-negative` emission rule. Model-level tests only;
  the integration tier stays gated on `SCANNY_BOY_SLOW_TESTS=1`.
- `docs/DECISIONS.md`: one entry recording (a) why dims are required and
  order is not trusted, (b) the separable-product feather, its axes taken
  from the solved frame rotations, and the alternatives rejected (a single
  axis fitted by SVD — conditional on a capture geometry nothing checks,
  and carrying no cell counts even when it holds; per-pair midline bands —
  needs overlap geometry the accumulate pass does not carry),
  (c) `GRID_PITCH_RATIO_MIN`, `GRID_ALIGNMENT_RATIO_MAX` and
  `_FEATHER_FLOOR_FRACTION` as unmeasured constants awaiting a gate, and
  (d) G-0: that `estimate_peak_bytes`'s `frame_bbox_size` is a per-frame
  box, and that passing the canvas there refused every grid above 2×2.

---

## 6a. Chunk G-6 — spill warped frames to a memmap, and free the canvas

**Independent of G-1…G-5, but not of G-0.** It touches only `composite.py`
and its memory accounting, and can be built at any point after G-0. It is
not required for 5×2 — G-0 alone gets there with 26% headroom (§7.1).

**Build both halves or neither.** The spill on its own buys *nothing*.
`estimate_peak_bytes` takes the max of two branches, and once the warped
frames stop dominating, the other one binds: `accum + weight +
log_density + normalized + result`, 46 bytes per canvas pixel, which no
amount of spilling touches. §6a.9's accumulator release is what makes that
branch smaller than the spilled warp branch, and it is equally worthless
alone, because then the warp branch still binds. Together the pair takes a
5×2 from 47.5 GB of required RAM to 28.7 (§7.4), and it is the only thing
in this plan that moves the ceiling past 6×2.

### 6a.1 What spills, and what does not

Only `_WarpedFrame.linear` — the bbox-sized float32 RGB warp, 2.68 GB of
the 6.79 GB live total at 5×2 (§7.4). It is the one large per-frame buffer
that is written once and then only read.

Do **not** spill the eroded mask (0.45 GB total at 5×2, and read by both
photometric passes), and do not spill the canvas accumulators. The feather
weight is gone already if G-4 landed; if this chunk is built first, do the
feather-weight change from §5.2 here instead — it is a few lines and the
same idea.

### 6a.2 Where the files go

`_composite_and_publish` already creates a per-negative `staging_dir`
(`stitch_pipeline.py:1395`) and `shutil.rmtree`s it in a `finally`
(line 1625). Put the spill files there, in a `warp/` subdirectory.
Cleanup, including on failure and on cancel, is then already correct and
you write no new teardown.

Compositing is strictly sequential — `run_stitch`'s `for entry in solved`
loop (line 982) composites one negative at a time — so at most one
negative's spill set exists at once. Do not add locking or per-thread
paths for a concurrency that does not exist.

### 6a.3 `composite.py`

`composite()` gains a keyword-only `spill_dir: Path | None = None`.
`None` must reproduce today's behaviour **exactly**, including identical
output pixels; that equivalence is the chunk's main test.

When set, after each frame is warped and clipped:

```python
path = spill_dir / f"{placement.name}.warp"
mm = np.memmap(path, dtype=np.float32, mode="w+",
               shape=(bbox_height, bbox_width, 3))
mm[:] = warped
mm.flush()          # not optional — see 6a.5
del warped
```

and `_WarpedFrame.linear` holds `mm`. The two photometric passes and
`_pair_overlap` need no changes: they take slice views, and a memmap slice
reads through transparently.

### 6a.4 Apply the gains lazily, not in place

Today `_WarpedFrame` is documented as mutable — "the solved per-frame gain
is applied in place to `linear`" — and `composite` does
`warped_by_name[name].linear *= gains[name]` for every frame. On a memmap
that re-dirties and rewrites every page: a full extra 2.68 GB
read-modify-write, which is most of the I/O cost of this chunk and buys
nothing.

Change it to carry the scalar instead:

- add `gain: np.ndarray` to `_WarpedFrame`, defaulting to
  `np.ones(3, dtype=np.float32)` and set from `solve_gains` — a float32
  array, not a tuple, for the reason below — and never multiplied through;
- the post-gain MAD pass applies it to the **overlap slice only**
  (`a_sub[shared] * a_frame.gain` against `b_sub[shared] * b_frame.gain`),
  a small region. Order matters for memory: select `[shared]` **first**,
  then multiply. Writing `a_sub * a_frame.gain` materialises the whole
  overlap rect (~96 MB at this workload) only to throw most of it away
  when `[shared]` selects from the product; today's code selects first.
- the accumulate pass folds it into the term it already builds:
  `entry.linear * entry.gain * entry.weight[:, :, None]`.

**The result is not bit-identical to today's, and the tests must not ask
for that.** Today's `linear *= np.asarray(gains[name], dtype=np.float32)`
rounds to float32 once, in place, before anything else touches the buffer.
The lazy form multiplies in a different order — and, if `gain` is left as
a tuple of Python floats, in float64, because a float32 array times a
tuple of Python floats upcasts the whole expression. Storing the gain as a
float32 array keeps the arithmetic in float32; the reassociation still
leaves last-ulp differences in the accumulated sum. So §6a.8's guard for
this change is a tolerance test, not a byte comparison. The byte-identical
test belongs to the *spill*, where the arithmetic really is unchanged.

This is worth doing **whether or not the spill is enabled** — it removes a
full pass over every warped frame from the in-RAM path too. Update the
`_WarpedFrame` docstring, which currently advertises the mutation.

### 6a.5 The honesty caveat

A memmap's dirty pages are still memory pressure until they are flushed;
they simply stop being counted as RSS. `mm.flush()` after each frame's
write is what makes the pages clean and reclaimable, and it is the whole
reason this chunk works rather than merely relabelling the problem. Say so
in the docstring.

Consequently **`MEMORY_SAFETY_FACTOR = 3.5` does not transfer.** It was
measured against the current allocation pattern (`composite.py:59`,
"measured, not padding"). The spill path has a different one. So:

- `estimate_peak_bytes` gains a keyword-only `spill: bool = False`. When
  true, `all_warped` counts **one** resident warped frame rather than
  `frame_count` of them; the mask term stays `frame_count`-scaled.
- Do **not** invent a second safety factor. Keep 3.5 for now, record
  `"memory_safety_factor_spill_measured": false` in `_stitch_params`, and
  flag in the chunk summary that the spill path's factor needs measuring
  against a real 5×2 run — peak RSS under `/usr/bin/time -l` against the
  analytic number — before anyone leans on it. That measurement is a user
  gate, exactly like `GRID_PITCH_RATIO_MIN`.

### 6a.6 Choosing the path

Adaptive, not a flag. Mirror the CLAHE fallback in `_solve_negative`,
which is the codebase's existing idiom for "try the good path, fall back
loudly":

In `_attempt_solve` (`stitch_pipeline.py:588`), where `check_memory_budget`
runs today:

1. Estimate with `spill=False`. If it fits, done — `spill_dir=None`, and
   nothing about today's behaviour changes for any current workload.
2. If it does not fit, estimate with `spill=True`. If *that* fits, set
   `entry.use_spill = True` (new field on `_SolvedNegative`, carried to
   `_composite_and_publish` alongside `layout` and `ca_maps`) and emit a
   new warning `Code.STITCH_SPILL_TO_DISK` naming both estimates and
   saying the negative will composite from disk and take longer.
3. If neither fits, raise `INSUFFICIENT_MEMORY` as today, with a message
   reporting the spill estimate too — so the number a user sees is the
   best the tool can actually do.

Add `STITCH_SPILL_TO_DISK` to `events.py`, to `CONTRACT.md`'s code table,
and to the Swift warning map. It is informational, not a failure.

### 6a.7 Disk accounting

The spill is real bytes on the work volume: `frame_count × bbox_pixels ×
12`, about **2.7 GiB** for 5×2 at 24MP (the unit here is GiB, like every
other figure in this document — 2.68 GiB exactly). It is transient, but
it coexists with the staged TIFF.

`_required_free_bytes` (`stitch_pipeline.py:722`) currently takes only
canvas sizes. Extend it to take, per negative, the spill bytes that
negative will need (0 when it is not spilling), and add the **largest**
one — not the sum — since compositing is sequential and each negative's
spill is deleted before the next begins. That mirrors the existing "lone
extra `S`" reasoning for the staged file, and the docstring should say so.

Note the volumes can differ: the spill lives under `staging_dir`, i.e. the
**output** volume, which is what `check_disk_space` already checks. Good —
but confirm it rather than assuming, and if `--work` ever moves the
staging dir to another volume, this needs a second check.

### 6a.8 Tests (`composite_test.py`)

- **The equivalence test, first and most important:** the same synthetic
  scene composited with `spill_dir=None` and with a `tmp_path` spill dir
  produces **byte-identical** `CompositeResult.image`, gains, and every
  overlap metric. Write this before the implementation.
- Lazy gains: with `spill_dir=None`, the linear canvas matches the
  pre-change in-place implementation to within 1e-6 relative, and no
  published uint16 code differs by more than 1. This is the regression
  guard for §6a.4 touching the non-spill path; it is a tolerance test, not
  a byte comparison, for the reason §6a.4 gives.
- Freeing the accumulators (§6a.9) changes no output at all: the same
  synthetic scene is byte-identical before and after, and
  `estimate_peak_bytes`'s second branch drops by exactly
  `canvas_pixels * 16`.
- The spill directory is empty (or gone) after `composite` returns,
  including when it raises partway through — parametrise a fault injected
  in the accumulate loop.
- `estimate_peak_bytes(spill=True)` counts one warped frame, not
  `frame_count`; assert the 5×2 numbers from §7.4 within a tolerance.
- `stitch_pipeline_test.py`: a negative that does not fit in RAM but fits
  with spill emits `STITCH_SPILL_TO_DISK` and publishes; one that fits
  neither still raises `INSUFFICIENT_MEMORY`. Fake the budget by
  monkeypatching `physical_memory_bytes` — do not allocate real gigabytes
  in a test.
- Slow tier: a real 5×2 (or the largest available strip) through the spill
  path, asserting the output matches the in-RAM run.

### 6a.9 Release the canvas accumulators before the normalization pass

The other half of this chunk, and three lines.

`composite` never frees `accum` or `weight_canvas`. Both are dead after
the division at `composite.py:705` — `covered` is derived from
`weight_canvas` on the line before and is what the rest of the function
reads — yet both stay in scope until the return, alongside
`result_linear`, `img_log`, `normalized` and `encoded` in turn. That is 16
bytes per canvas pixel held for no reason across the whole normalization
pass: 2.2 GB at 5×2.

Add `del accum, weight_canvas` immediately after `result_linear` is
filled, and drop the two terms from `estimate_peak_bytes`'s second branch,
so `accum + weight + log_density + normalized + result` becomes
`log_density + normalized + result`.

Order matters and is easy to get wrong: `covered = weight_canvas > 0` and
`result_linear = np.zeros_like(accum)` both have to run first. The
byte-identical test in §6a.8 is what catches a `del` placed one line too
early.

---

---

## 7. Sizing at the target workload

### 7.1 24MP frames, up to 5×2 — the numbers

Computed with the real `composite.estimate_peak_bytes` at the capture
geometry above (6000×4000 frames, 1/3 overlap on both axes), **as it will
be called once G-0 lands** — each frame's canvas bbox taken as its full
frame size, the realistic worst case since rotation only grows it
slightly. The last column is the same function as `_attempt_solve` calls
it *today*, with the whole canvas passed as every frame's bbox (§1a).

| Grid | Frames | Canvas | Canvas MP | TIFF | Est. peak | RAM required | (today) |
|---|---|---|---|---|---|---|---|
| 2×2 | 4 | 10000×6667 | 66.7 | 0.37 GB | 11.1 GB | 22.3 GB | 44.5 GB |
| 3×2 | 6 | 14000×6667 | 93.3 | 0.52 GB | 15.4 GB | 30.7 GB | 83.1 GB |
| 4×2 | 8 | 18000×6667 | 120.0 | 0.67 GB | 19.6 GB | 39.1 GB | 134.3 GB |
| **5×2** | **10** | **22000×6667** | **146.7** | **0.82 GB** | **23.8 GB** | **47.5 GB** | **197.9 GB** |
| 6×2 | 12 | 26000×6667 | 173.3 | 0.97 GB | 28.0 GB | 55.9 GB | 274.0 GB |

Canvas dimensions are given with the frame's long axis along the grid's
long axis. Shooting the frames rotated 90° transposes the canvas
(5×2 becomes 14667×10000) and changes **nothing** downstream: same film
area, same resolution, same pixel count, same memory. Do not add an
orientation concept.

"RAM required" is `2 × peak`, because `check_memory_budget` fails above
half of physical memory (`_USABLE_MEMORY_FRACTION = 0.5`).

Four conclusions, all of which the implementer should treat as settled:

1. **G-0 is not optional.** The last column is what the gate computes
   right now, and a 64 GB machine allows 32 GB of estimated peak. So today
   3×2 and everything above is refused outright, and 2×2 passes with
   almost nothing to spare. Fix the estimate's inputs (§1a) before
   anything else in this plan.

2. **File-size limits are a non-issue.** The largest case is 0.97 GB
   against `MAX_STITCHED_BYTES` of 3.5 GB, and 26000 px against
   `MAX_CANVAS_DIMENSION` of 30000. Do not spend effort on
   `check_output_size`; it will not fire at this workload. (It does fire
   on long *strips* — a 10×1 of these frames is 42000 px wide — but that
   is a pre-existing `OUTPUT_DIMENSIONS_LARGE` warning, not a grid
   concern.)

3. **A grid is cheaper than the equivalent strip.** 5×2 costs less than a
   10×1 strip of the same frames — 23.8 GB against 25.2 — because it packs
   them into a squarer, smaller canvas. Grids introduce **no new memory
   regime**: frame count was always the driver, and `--per-negative 10` is
   already permitted today. Do not add grid-specific memory guards.

4. **After G-0, 5×2 fits a 64 GB machine with 26% headroom** (23.8 GB peak
   against a 32 GB budget). 6×2 needs 55.9 GB and is close enough to the
   edge that 5×2 is the ceiling. Raising it further needs G-4 *and* both
   halves of G-6 together (§7.4); no one of them moves the number
   alone.

### 7.2 What 5×2 means for §4

Measured `strip_spread_ratio` (second singular value over first) at the
exact capture geometry — steps of 4000 px across and 2667 down:

| Grid | 2×2 | 3×2 | 4×2 | 5×2 |
|---|---|---|---|---|
| spread ratio | 0.667 | 0.408 | 0.298 | 0.236 |

Two things follow. First, every one is far above `STRIP_SPREAD_RATIO =
0.15`, so §0's blockers (1)–(3) fire for every grid **in scope** — the
precise claim, since an 8×2 would measure 0.144 and slip under the
threshold, but `MAX_PER_NEGATIVE = 12` admits no such grid. Second,
**none of them is degenerate**: the singular values are never equal here,
because the two steps differ. A 2×2 goes degenerate only at equal steps on
both axes, which a 3:2 frame at uniform overlap never produces.

Say that precisely, because it is easy to over-claim in either direction.
§4.1 takes its axes from the solved rotations *not* because the SVD is
ambiguous at this geometry — it is not — but because the SVD's guarantee
is conditional on a frame aspect and overlap nothing in the code checks,
degrades silently rather than loudly when that fails, and yields no cell
counts even when it works. The rotations are unconditional. The SVD stays
as §4.1's cross-check.

The tests should still cover 2×2 and 3×2 rather than only the easy 5×2:
2×2 is where the `R == C` tie-break in §4.1 step 2 is the only thing
choosing an ordering, and where `grid_pitch_ratio` is vacuous (§4.2).

### 7.3 What 1/3 overlap implies

Two consequences of the capture geometry that shape G-3 and G-4.

**The pair graph is dense and well constrained.** In a 5×2, of the 45
pairs registration attempts, **21 genuinely share canvas area**: 8
horizontal (4 per row), 5 vertical, and **8 diagonal**. The diagonals
overlap by 2.67 MP — 11% of a frame — which is real, feature-rich data,
not a sliver. So:

- Keep all-pairs registration (§1). Pruning to 4-neighbours would throw
  away 38% of the constraints.
- `check_connectivity` will never be close to failing at this geometry;
  a `STITCH_UNDERCONSTRAINED` here means something is genuinely wrong,
  not that the grid is marginal.
- The gain solve is over-determined by a comfortable margin, and every
  usable pair clears `MIN_GAIN_OVERLAP_PX = 1000` by three orders of
  magnitude.
- Registration cost is `n(n-1)/2` pairs — 45 for a 5×2 against 3 for a
  3×1 strip. Fifteen times the pairwise work. It is not the dominant cost
  (warping is), but it is no longer negligible, and the `MATCH` progress
  step will feel slower. Do not "optimize" it by pruning.

**Up to four frames cover the same pixel.** With 1/3 overlap on both axes,
a canvas pixel is covered by 1, 2, or **4** frames — never 3. In a 5×2:
50.8% of the canvas by one frame, 41.9% by two, and **7.3% by four**, in
the corner regions where four cells meet.

This is the case the separable feather (§5) has to get right, and it is a
positive argument for the product form. Near a four-way corner each frame
sits near the end of *both* its ramps, so all four weights are small and
comparable, and the normalized result is a smooth four-way blend. The
isotropic distance transform collapses toward equal weights at the borders
instead — the failure `docs/STITCH_QUALITY_PLAN.md` §1.1 removed for
strips, which would otherwise reappear at exactly these corners.

Two implementation consequences, both already specified in §5.1 but worth
restating because a four-way corner is where they bite:

- `_FEATHER_FLOOR` must be applied to the **product**, not per-axis.
  Applied per-axis, a corner pixel lands on `floor²`, which is not the
  invariant the floor exists to protect.
- The accumulate pass normalizes by the summed weight, so any positive
  weights give a convex combination; correctness at four-way coverage is
  automatic. It is the *smoothness* that needs the ramp, and the test in
  §5.3 for it is the four-frame corner case.

### 7.4 Why every frame stays resident, and what could change it

Not required for this feature — after G-0, 5×2 fits. Recorded because the
question comes up and the answer is not obvious from the code.

**Why.** The photometric gain solve is global: `solve_gains` needs every
used pair's overlap means before any gain is known, and the gains are
per-frame scalars applied to the warped pixels *before* accumulation. Once
`accum += w_i * x_i` has run you cannot retroactively rescale one frame's
contribution out of the sum. The post-gain overlap MAD then needs both
frames' pixels again, after the gains land. So `composite` warps
everything, holds it all, solves, applies, and only then accumulates.

At 5×2 that is 6.79 GB genuinely live:

| Term | | Share |
|---|---|---|
| 10 warped frames | 4.02 GB | 59% |
| — linear float32 RGB | 2.68 GB | 40% |
| — feather weight float32 | 0.89 GB | 13% (removed by G-4, §5.2) |
| — eroded mask | 0.45 GB | 7% |
| accum canvas float32 RGB | 1.64 GB | 24% |
| weight canvas float32 | 0.55 GB | 8% |
| source frame + linear decode | 0.40 GB | 6% |
| feather scratch (2 × bbox float32) | 0.18 GB | 3% |

**But that is only one of the two branches.** `estimate_peak_bytes` takes
the max of the table above — the **warp branch** — and a **canvas
branch**: `accum + weight + log_density + normalized + result`, 46 bytes
per canvas pixel, the normalization pass with the accumulators still in
scope. At 5×2 the two are 6.79 GB and 6.28 GB. They are close, and after
G-0 they trade places as soon as anything touches the first. That is the
single most important fact in this section, because it means several
otherwise-sensible optimisations are worth exactly nothing on their own:

| | Warp branch | Canvas branch | Peak | RAM demanded |
|---|---|---|---|---|
| After G-0 | 6.79 GB | 6.28 GB | 6.79 | 47.5 GB |
| + feather weight computed lazily (§5.2, G-4) | 5.99 | 6.28 | 6.28 | 44.0 GB |
| + spill warped frames (§6a.1–6a.8) | 3.57 | 6.28 | 6.28 | **44.0 GB — no gain** |
| + release the accumulators (§6a.9) | 3.57 | 4.10 | 4.10 | **28.7 GB** |
| Release frames on a neighbour window (4) instead of spilling | 4.11 | 4.10 | 4.11 | 28.8 GB; warp pass runs twice |
| Banded canvas as well | 3.57 | small | 3.57 | ~25 GB; large, touches normalization |

Read it downward: the spill row is the point. **Spilling the warped frames
buys nothing until the canvas branch comes down too**, because the
normalization pass then sets the peak — and the accumulator release buys
nothing alone, because the warp branch would still bind. §6a is worth
building only as the pair.

`MEMORY_SAFETY_FACTOR = 3.5` on top of `_USABLE_MEMORY_FRACTION = 0.5`
means the check demands **7× the binding branch's live bytes** — 47.5 GB
of physical RAM to permit a composite whose largest branch holds 6.8 GB.
The 3.5 was measured against real peak RSS, so it is not padding; numpy
temporaries are real. But it is a calibrated policy number, and every
change in the table above invalidates the calibration and requires
re-measuring it.

The **neighbour window** is the weaker option and is *not* in scope. Like
the spill it attacks only the warp branch, so the same warning applies: it
can do nothing until the canvas branch comes down either. It then lands
level with the spill while paying a doubled warp pass for it, because it
holds four full warped frames where the spill holds one. Recorded only
because the reasoning is worth keeping. A frame can be released once the
last pair it participates in has been measured; ordering frames so that
window stays small turns `frame_count × bbox` into `window × bbox`. For an
N×2 grid in column order the window is 4 frames — and §4's cell assignment
hands you the neighbour graph for free. It helps **strips more than
grids**: a 12×1 would drop to a 2–3 frame window. But note there is no
failing case pulling for it — after G-0 a 12×1 of these frames is charged
60.0 GB of required RAM, which a 64 GB machine allows with 6% to spare.
Worth revisiting only if disk, not RAM, turns out to be the constraint,
and note it composes with the spill rather than competing with it, since
the window bounds what is *live* and the spill bounds what is *resident*.

Do not lower `MEMORY_SAFETY_FACTOR` to make a case fit. It is measured
against the current allocation pattern and means nothing once that pattern
changes — which is exactly why G-6 leaves it at 3.5 and flags the spill
path's own factor as needing separate measurement (§6a.5).

---

## 8. Sequencing and risk

| Chunk | Depends on | Risk |
|---|---|---|
| **G-0 memory estimate** | — | Low to write, **blocking** to skip: nothing above 2×2 runs without it. A two-line input fix plus a test that states the behaviour change. Do it first. |
| G-1 spec plumbed | — | Low. Wide but mechanical; the `GridSpec`-everywhere rule is what keeps it so. Includes the protocol bump (§2.0) and the one-time `_stitch_params` landing (§2.4). |
| G-2 layout gate | G-1 | Low. |
| G-3 cell assignment | G-2 | Medium. Rotation-derived axes and snap-to-nearest assignment (§4.1) are the only novel logic; brute force over ≤ 12 frames keeps it honest. The gate's magnitude boundary (≥ half cell = bijection failure, sub-cell = warnings) is what §4.5 pins. |
| G-4 separable feather | G-3 | Medium. Touches the hottest loop in the codebase. The byte-for-byte strip-equivalence tests are the safety net — write them first. The `FEATHER` value change re-breaks `check_roll_invariants` (§2.4, §5.1): same remedy, delete old roll folders. |
| G-5 end to end | G-4 | Low, but real-scan validation may be blocked on fixtures. |
| G-6 spill + accumulator release | **G-0** | Medium. Independent of the grid work, but worthless unless both halves land (§7.4). The byte-identical equivalence test is the safety net — write it before the implementation. |

**Memory is the one risk that cuts across chunks, and it has two parts.**
The first is G-0: as the gate is called today, a 3×2 is charged 83 GB and
a 5×2 198 GB, so grid work cannot be validated on real scans at all until
it lands. Do it first and check it with a `5×2` call to
`estimate_peak_bytes` in a test — that costs nothing and pins the number.
The second is the residual margin *after* G-0: 47.5 GB required against 64
GB physical, a 26% margin with 6×2 the next step up at 55.9 GB. A grid is
cheaper than the strip of the same frame count, so G-1…G-5 need no design
changes for it, and G-6 is where the margin grows if it ever needs to.

If the two streams are being built by different people, G-6 and G-4 both
touch `composite.py`'s per-frame buffers (§5.2's lazy feather weight and
§6a.4's lazy gains are the same kind of change). Land one before starting
the other rather than merging them in parallel.

After G-0 and G-2 a grid runs end to end with a known-imperfect blend,
which is the natural place to look at a real scan before investing in
G-4.

Run `cd cli && uv run pytest` while iterating; `--slow` before finishing
G-4, G-5 and G-6, since all three touch registration/compositing.
