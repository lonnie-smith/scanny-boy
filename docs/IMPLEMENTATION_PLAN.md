# Scanny Boy — Implementation Plan (Phase 1)

**Scope:** Workflow steps 1–5 — pick a folder, select a sequential run of `.NEF`
files, declare shots-per-negative, declare colour/B&W, supply a capture date,
and produce one 16-bit TIFF per source frame with correct metadata.

Stitching (step 6+) is **out of scope** but the architecture below is built so
it drops in without rework. Section 9 lists what Phase 1 deliberately leaves
ready for it.

**Audience:** implementation agents, handed one chunk at a time. Each chunk is
one branch, one PR, independently verifiable.

---

## 0. Setup tasks — Lonnie does these first

These need your credentials or your hardware. Nothing in Chunk 0+ works until
they're done.

### 0.1 Upgrade the GitHub CLI

Yours is v2.20.2 (November 2022). Modern `gh` subcommands used later in this
plan won't exist.

```bash
brew upgrade gh && gh --version
```

### 0.2 Install XcodeGen

```bash
brew install xcodegen && xcodegen --version
```

### 0.3 Confirm the rest of your toolchain

Already verified present and current on your machine — run this only if
something breaks later:

```bash
sw_vers && xcodebuild -version && swift --version && uv --version && python3 --version
```

Baseline recorded 2026-08-27: macOS 14.6.1 (arm64), Xcode 16.2, Swift 6.0.3,
uv 0.11.7, Python 3.13.3, Homebrew 6.0.19.

### 0.4 Supply test fixtures

Create the directory and drop real files in:

```bash
mkdir -p /Users/lsmith/dev/scanny-boy/tests/fixtures/nef
```

Put in **at least 8 real Nikon Z f `.NEF` files**: two complete negatives'
worth at 4 shots each, shot on your actual copy stand with your actual
technique. This directory is gitignored — nothing large enters the repo.

Ideally also include one deliberately awkward pair (minimal overlap, or a
visibly hand-nudged frame) so Phase 2's stitcher has a hard case to fail
against early rather than late.

### 0.5 Create the GitHub repo

Public, per your decision — unlimited free Actions minutes, and nothing here is
sensitive. Chunk 0 does the actual push; this just reserves the name.

```bash
gh repo create scanny-boy --public --description "Film negative scanning pipeline for multi-shot camera scans" --confirm
```

---

## 1. Locked decisions

Agents: these were decided deliberately. **Do not re-litigate them.** If one
appears to block you, stop and report rather than substituting your own choice.

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **All pixel work in Python.** Swift is UI, orchestration, and progress only. | `rawpy` *is* LibRaw (Cython binding over the real C++ lib), so there is no fidelity cost, and it avoids C++ interop entirely. |
| D2 | **Always write 16-bit RGB.** The B&W flag is metadata only; it does not change channel count. | The Z f has a Bayer CFA — there is no true grayscale capture. Collapsing to 1 channel bakes in an RGB→gray weighting that can't be undone. Green has 2× the photosites, so the weighting is not neutral. |
| D3 | **Disk is bounded by streaming, not by dropping channels.** Convert and (later) stitch one negative at a time; delete that negative's intermediates before starting the next. | 24.5MP × 3ch × 16-bit ≈ 146 MB/file. A 36-exposure roll at 4 shots ≈ 21 GB if batched; ≈ 600 MB peak if streamed. |
| D4 | **ProPhoto (ROMM) primaries, gamma-encoded, `no_auto_bright=True`.** | Gamma at 16-bit is near-losslessly reversible and hugely improves both eyeballing and OpenCV feature detection. `no_auto_bright` is non-negotiable — auto-brightness is per-image and would destroy the roll-consistent exposure the whole workflow depends on. |
| D5 | **Sort by EXIF `DateTimeOriginal`, filename as tiebreak.** Non-divisible selection is a hard error. | Survives the `DSC_9999→DSC_0001` rollover and card merges. Silent mis-grouping would corrupt a whole roll invisibly. |
| D6 | **App Sandbox off.** Intermediates in `NSTemporaryDirectory()`. | Local personal tool. A sandboxed parent cannot easily hand security-scoped access to a child process. Re-enabling is a distribution-time task, noted in §9. |
| D7 | **XcodeGen.** `project.yml` is source of truth; `.xcodeproj` is generated and gitignored. | `.xcodeproj` is a UUID-cross-referenced plist that agents reliably corrupt. YAML + globs makes "add a file" a no-op. |
| D8 | **Public repo, CI on Linux + macOS.** | Public = free unlimited Actions minutes. |
| D9 | **Output named for the group's first frame**, e.g. `DSC_0042.NEF → DSC_0042.tif`. | Traceable to source, stable across re-runs, collision-free. |
| D10 | **Parallel decode** across physical cores, with `--jobs 1` available. | LibRaw decode is CPU-bound; near-linear speedup on Apple silicon. |

### Verified environment facts

Probed on this machine on 2026-08-27 — these are measurements, not assumptions:

- `rawpy` 0.27.0 bundles **LibRaw 0.22.1**; prebuilt arm64 wheel, no compilation.
- `opencv-python-headless` **5.0.0.93**. ⚠️ `cv2.Stitcher.SCANS` does **not**
  exist as an attribute in OpenCV 5 — the constant is `cv2.Stitcher_SCANS` at
  module level. Code written against the old spelling will `AttributeError`.
- `tifffile` 2026.8.23 writes 16-bit RGB with deflate + arbitrary tags (verified).
- `exifread` 3.5.1 — pure Python, reads NEF. Preferred over anything needing a
  compiled exiv2, because it must survive PyInstaller freezing.
- **`/System/Library/ColorSync/Profiles/ROMM RGB.icc` exists on macOS** (568
  bytes). Its `rTRC` is ICC parametric funcType 3 with
  `g=1.8, a=1.0, b=0.0, c=0.0625, d=0.001953125` — i.e. gamma 1.8 with a linear
  toe of slope 16. **The rawpy call must therefore be `gamma=(1.8, 16)`, not the
  BT.709 default `(2.222, 4.5)`.** Mismatching the encoding gamma against the
  embedded profile makes every output file subtly wrong everywhere it is opened.

---

## 2. Architecture

```
┌───────────────────────────────────────────────┐
│  ScannyBoy.app  (SwiftUI, macOS 14+)          │
│  folder pick · selection · grouping preview   │
│  shots-per-negative · colour/B&W · film date  │
│  output dir · progress · cancel               │
└───────────────┬───────────────────────────────┘
                │  spawns, streams NDJSON on stdout
                ▼
┌───────────────────────────────────────────────┐
│  scanny-boy  (Python CLI, PyInstaller-frozen) │
│  probe   — read EXIF, sort, group, validate   │
│  convert — rawpy decode → 16-bit ProPhoto TIF │
│  (phase 2) stitch — OpenCV SCANS/affine       │
└───────────────────────────────────────────────┘
```

**Why the CLI is the real product.** Everything testable lives in Python and is
exercised head-first from a terminal. Swift stays a thin shell that can be
verified by eye. This is deliberate given you're not an experienced Swift dev —
when something is wrong, it will almost always be wrong in the layer you can
`pytest`.

**Dev vs release.** In development the app invokes the CLI from `cli/.venv`
(fast iteration, no freeze step). Release builds invoke the PyInstaller binary
bundled in `Resources/cli/`. Chunk 7 implements the switch.

---

## 3. The CLI contract

This replaces the placeholder in `shared/contract/`. Chunk 1 writes it.

### 3.1 Transport

- **stdout** — one JSON object per line (NDJSON), UTF-8, flushed per line.
- **stderr** — human-readable logs only. Never parsed.
- **exit 0** success · **1** operation failed · **2** bad usage · **130** cancelled.

### 3.2 Events

Every line has `{"event": "...", ...}`.

```jsonc
{"event":"started","command":"convert","total":8,"run_id":"…"}
{"event":"progress","index":3,"total":8,"path":"…/DSC_0042.NEF","stage":"decode"}
{"event":"item_done","index":3,"total":8,"input":"…/DSC_0042.NEF","output":"…/DSC_0042.tif","bytes":146313728,"ms":1840}
{"event":"item_failed","index":4,"total":8,"input":"…/DSC_0043.NEF","error":"…","recoverable":true}
{"event":"finished","ok":true,"succeeded":8,"failed":0,"ms":14210}
{"event":"error","message":"…","code":"NOT_DIVISIBLE"}
```

`index` is 1-based and **must be attributed correctly under parallelism** — the
worker returns its own index rather than the parent inferring it from
completion order.

### 3.3 Commands

```
scanny-boy probe --input DIR [--files a.NEF b.NEF …] --per-negative N
scanny-boy convert --files … --out DIR --film-date YYYY-MM-DD
                   [--mono] [--jobs N] [--per-negative N]
```

`probe` is read-only: it sorts, groups, validates divisibility, and emits the
grouping the UI will display. It never writes. The app calls `probe` to
populate its preview, then `convert` to do the work.

### 3.4 Cancellation

The app sends `SIGTERM`. The CLI must install a handler that stops dispatching
new work, terminates the pool, deletes partial outputs from the in-flight
negative, emits `{"event":"finished","ok":false,"cancelled":true}`, and exits
`130`. Completed negatives are left in place.

---

## 4. Chunks

Each chunk: one branch, one PR, merged before the next starts.

Agents must run `pytest` and the chunk's verification block before opening the
PR, and paste real output into the PR body — not a claim that it passed.

---

### Chunk 0 — Repo foundation

**Branch:** `chunk-00-repo-foundation`

**Goal:** A committed, pushed repo with CI green and the scaffold's dead
placeholder code removed.

**Do:**

1. `git init` is already done but there are **zero commits**. Make the initial
   commit from the current scaffold.
2. Extend `.gitignore`: add `tests/fixtures/`, `mac/*.xcodeproj/`,
   `.venv/`, `docs/.DS_Store`.
3. Delete the placeholder `scan` command end-to-end — `cli/src/scanny_boy/core/scanner.py`,
   its test, the `scan` branch in `cli.py`, `mac/ScannyBoy/Models/ScanResult.swift`,
   and the `scan` section of `shared/contract/CONTRACT.md`. It models nothing
   this project does and will otherwise be cargo-culted forward.
4. Rewrite `cli/pyproject.toml` dependencies: `rawpy>=0.27`, `tifffile>=2026.1`,
   `numpy>=2`, `exifread>=3.5`. Dev extras: `pytest`, `pytest-cov`, `ruff`,
   `pyinstaller`. Add `opencv-python-headless>=5` as an **optional** `stitch`
   extra — Phase 1 must not pay OpenCV's 46 MB import cost.
5. Switch `scripts/bootstrap.sh` from `python3 -m venv` + `pip` to `uv venv` +
   `uv pip install` (uv is installed and ~10× faster).
6. Add `.github/workflows/ci.yml`:
   - job `python` on `ubuntu-latest` — `uv sync`, `ruff check`, `pytest`.
   - job `swift` on `macos-14` — `brew install xcodegen`, `xcodegen generate`,
     `xcodebuild build test`. Mark `continue-on-error: true` until Chunk 7
     lands a real project, then remove that.
7. Push and set `main` protected, requiring the `python` check.

**Verify:**

```bash
./scripts/bootstrap.sh && cli/.venv/bin/pytest && gh run list --limit 1
```

**Done when:** repo is on GitHub, CI `python` job is green, no reference to
`scan`/`ScanResult` remains (`grep -ri "scanresult\|def scan(" . --exclude-dir=.git`
returns nothing).

---

### Chunk 1 — CLI skeleton and contract

**Branch:** `chunk-01-cli-contract`

**Goal:** The NDJSON event protocol and command surface exist and are tested,
with no image code yet.

**Do:**

1. Write `shared/contract/CONTRACT.md` from §3 above. Update `schema.json` to
   define each event type.
2. `cli/src/scanny_boy/events.py` — dataclasses for each event plus an
   `EventEmitter` that writes one JSON line to a stream and **flushes every
   line**. Unflushed output is the single most common cause of a Swift UI that
   shows no progress until the very end.
3. `cli/src/scanny_boy/cli.py` — argparse with `probe` and `convert`
   subcommands, full argument validation, exit codes per §3.1. Commands may
   emit `started`/`finished` with stub counts.
4. Structured errors: an exception type carrying a stable `code`
   (`NOT_DIVISIBLE`, `UNREADABLE_RAW`, `NO_FILES`, `OUTPUT_NOT_WRITABLE`) so
   Swift can branch on `code`, never on message text.

**Tests** (`events_test.py`, `cli_test.py`, co-located per your convention):

- Each event serialises to exactly one line, and the line round-trips.
- Emitter flushes per line (assert against an unbuffered spy stream).
- `probe` with no `--per-negative` exits 2.
- Bad `--film-date` format exits 2 with `code` set.
- Unknown subcommand exits 2.

**Verify:**

```bash
cli/.venv/bin/scanny-boy probe --input /tmp --per-negative 4 | head
cli/.venv/bin/scanny-boy convert --films-date bogus; echo "exit=$?"   # expect 2
```

**Done when:** `pytest` green; every event in §3.2 validates against `schema.json`.

---

### Chunk 2 — RAW decode core ⭐

**Branch:** `chunk-02-raw-decode`

**This is the pixel heart of the project. Verify it hardest.**

**Goal:** One `.NEF` in, one correct 16-bit ProPhoto TIFF out.

**Do:**

Create `cli/src/scanny_boy/core/decode.py` with a single pure function taking a
path and returning a numpy array plus metadata. **These parameters are load-bearing —
use exactly these:**

```python
import rawpy

RAW_PARAMS = dict(
    output_bps=16,
    gamma=(1.8, 16),                              # MUST match ROMM RGB.icc (see §1)
    no_auto_bright=True,                          # preserves roll-consistent exposure
    use_camera_wb=True,                           # as-shot WB; consistent across the roll
    use_auto_wb=False,
    output_color=rawpy.ColorSpace.ProPhoto,
    demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
    user_flip=0,                                  # do NOT auto-rotate — stitching needs
                                                  # sensor-native orientation
    four_color_rgb=False,
    median_filter_passes=0,
    highlight_mode=rawpy.HighlightMode.Clip,
)
```

Each of `no_auto_bright`, `user_flip=0`, and the gamma pair has a comment
because each will look wrong to someone later. Leave the comments in.

Then `cli/src/scanny_boy/core/tiff.py`:

- `tifffile.imwrite(..., photometric="rgb", compression="deflate")`.
  Deflate is lossless and typically recovers 30–50% on photographic data.
- Embed the ICC profile in TIFF tag **34675** (`InterColorProfile`), read from
  `/System/Library/ColorSync/Profiles/ROMM RGB.icc`. If that path is missing
  (Linux CI), skip embedding and log a warning — do not fail.

**Tests:**

- Synthetic: feed a known array through the TIFF writer, read back, assert
  dtype `uint16`, shape `(h,w,3)`, photometric RGB, ICC tag present on macOS.
- Fixture-gated (`pytest.mark.skipif` when `tests/fixtures/nef` is empty — CI
  must stay green without fixtures):
  - A real NEF decodes without raising.
  - Output is `uint16`, 3-channel, and matches the Z f's expected ~6048×4032.
  - **Determinism:** decoding the same file twice is byte-identical.
  - **Roll consistency:** decode two different frames; assert neither has been
    independently auto-stretched. Concretely, assert `no_auto_bright` is
    actually in effect by decoding one file with and without it and asserting
    the mean differs — this catches a silently-dropped parameter.

**Verify — do this by eye, it is the point of the chunk:**

```bash
cli/.venv/bin/python -c "
from scanny_boy.core.decode import decode_raw
from scanny_boy.core.tiff import write_tiff
import glob
src = sorted(glob.glob('tests/fixtures/nef/*.NEF'))[0]
arr, meta = decode_raw(src)
print('shape', arr.shape, 'dtype', arr.dtype, 'min', arr.min(), 'max', arr.max())
write_tiff('/tmp/check.tif', arr, meta)
"
open /tmp/check.tif      # opens in Preview
```

**What you should see:** a normal-looking *negative* — orange-masked if colour
film, tonally inverted. It should **not** look near-black (that would mean gamma
didn't apply) and **not** look contrast-stretched (that would mean
`no_auto_bright` was dropped). Open it in Lightroom or Affinity too and confirm
it reports **ProPhoto RGB**, not sRGB or Untagged.

**Done when:** the eyeball check passes, all fixture tests pass locally, and CI
is green with fixtures absent.

---

### Chunk 3 — EXIF read and the film-date shift

**Branch:** `chunk-03-exif-datedshift`

**Goal:** Read capture metadata from NEF; write correct, semantically-right
metadata into the TIFF.

**The date rule.** You asked to replace the *date only* and keep time-of-day so
capture-order stays consistent. Naively swapping the date breaks exactly that
if a scanning session crosses midnight — two calendar days collapse onto one and
the order inverts. Verified:

```
session: 23:58 (Aug 27), 00:01 (Aug 28), 00:03 (Aug 28)
naive swap → 1998-07-04 23:58, 1998-07-04 00:01, 1998-07-04 00:03   ✗ out of order
day-delta  → 1998-07-04 23:58, 1998-07-05 00:01, 1998-07-05 00:03   ✓ ordered
```

So shift by whole days relative to the **first frame in the selection**:

```python
def shift_capture_date(frame_dt, anchor_dt, film_date):
    """Replace calendar date with film_date, preserving time-of-day AND ordering."""
    day_delta = (frame_dt.date() - anchor_dt.date()).days
    return datetime.combine(film_date + timedelta(days=day_delta), frame_dt.time())
```

For a normal same-day session this is identical to "just swap the date" —
`day_delta` is 0 for every frame. It only diverges when it needs to.

**EXIF semantics — write both fields, they mean different things:**

| Tag | Value | Why |
|-----|-------|-----|
| `DateTimeOriginal` | shifted (film date + original time-of-day) | when the *photograph* was taken — the film exposure |
| `DateTimeDigitized` | the NEF's real capture time, unmodified | when it was *digitised* — literally your scanning session |

This is correct EXIF usage rather than a workaround, and it means you keep both
facts instead of trading one for the other.

**Do:**

1. `core/exif.py` — read `DateTimeOriginal` (with `SubSecTimeOriginal` when
   present), `Make`, `Model`, `LensModel`, `ExposureTime`, `FNumber`, `ISO` via
   `exifread`.
2. Implement `shift_capture_date` exactly as above.
3. Extend the TIFF writer to emit: baseline `DateTime` (306), `Make` (271),
   `Model` (272), `Software` (305) = `"Scanny Boy <version>"`,
   `ImageDescription` (270) noting source filename and that this is an
   unstitched scan frame, plus an EXIF IFD carrying `DateTimeOriginal` (36867)
   and `DateTimeDigitized` (36868).

**Tests:**

- `shift_capture_date` — same-day case is exactly a date swap.
- **Cross-midnight case preserves ordering** (use the table above as the fixture).
- Sub-second precision survives when present.
- A frame *earlier* in the day than the anchor still orders correctly.
- Fixture-gated: real NEF → EXIF fields non-empty and `DateTimeOriginal` parses.
- Round-trip: write a TIFF, read tags back, assert both date fields present and
  distinct.

**Verify:**

```bash
cli/.venv/bin/python -c "
import tifffile
with tifffile.TiffFile('/tmp/check.tif') as t:
    for k,v in t.pages[0].tags.items(): print(v.name, '=', v.value)
"
```

**Done when:** both date fields are present, correct, and distinct; ordering
tests pass.

---

### Chunk 4 — Sorting, grouping, validation

**Branch:** `chunk-04-grouping`

**Goal:** `probe` works fully — turn a file selection into validated negative
groups.

**Do:**

`core/grouping.py`:

1. Sort by `DateTimeOriginal` (sub-second when available), tiebreak on natural
   filename order. Natural, not lexicographic — `DSC_9.NEF` sorts before
   `DSC_10.NEF`.
2. Chunk into groups of `per_negative`.
3. **Hard-fail** non-divisible selections with code `NOT_DIVISIBLE` and a
   message naming both numbers: `"14 files selected, 4 per negative — 14 is not
   divisible by 4 (2 files short of 16, or 2 too many for 12)"`. Say what's
   wrong *and* what would fix it.
4. Warn (don't fail) when a group's internal time gaps are wildly uneven — a
   likely sign a frame was missed. Emit as an event; the UI surfaces it.
5. Derive each group's output name from its first frame (D9).

**Tests:**

- Divisible selection groups correctly at N=2,3,4,6.
- Non-divisible raises `NOT_DIVISIBLE` with both counts in the message.
- Natural sort: `DSC_9` before `DSC_10`.
- **Filename rollover:** `DSC_9998, DSC_9999, DSC_0001, DSC_0002` with
  ascending EXIF times stays in capture order — proving EXIF beats filename.
- Missing EXIF falls back to filename without crashing.
- Empty selection raises `NO_FILES`.
- Output names are unique across groups.

**Verify:**

```bash
cli/.venv/bin/scanny-boy probe --input tests/fixtures/nef --per-negative 4
# expect: 2 groups, correct membership, sensible output names
cli/.venv/bin/scanny-boy probe --input tests/fixtures/nef --per-negative 3; echo "exit=$?"
# expect: exit 1, NOT_DIVISIBLE naming 8 and 3
```

**Done when:** both commands behave as above and `probe` writes nothing to disk
(assert via a read-only directory test).

---

### Chunk 5 — Batch orchestration, parallelism, cancellation

**Branch:** `chunk-05-orchestration`

**Goal:** `convert` runs a whole selection with real progress, real
parallelism, real cancellation, and bounded disk.

**Do:**

1. `core/pipeline.py` — orchestrate group-at-a-time (D3). Never hold more than
   one negative's intermediates.
2. `multiprocessing.Pool` sized to physical cores
   (`os.cpu_count()`, capped sensibly). **Workers return their own `index`** so
   progress attribution is correct out of completion order.
3. `--jobs 1` forces the serial path, bypassing the pool entirely — used by
   tests for determinism and by you for debugging.
4. Run directory under `NSTemporaryDirectory()` equivalent
   (`tempfile.mkdtemp(prefix="scannyboy-")`), removed on success, on failure,
   and on cancel.
5. `SIGTERM` handler per §3.4.
6. **Preflight before any work:** output dir writable, sufficient free disk
   (estimate ≈ 150 MB × file count and compare against `shutil.disk_usage`),
   every input readable. Fail fast with a clear code — not 40 minutes in.
7. A single failed file emits `item_failed` with `recoverable: true` and the run
   continues; the run reports partial success at the end.

**Tests:**

- Progress events are monotonic in count and every index 1..N appears exactly
  once, **under `--jobs 4`** (this is the test that catches mis-attribution).
- `--jobs 1` and `--jobs 4` produce byte-identical outputs.
- One unreadable input → `item_failed`, run completes, `finished.ok` reflects
  partial success, exit 1.
- SIGTERM mid-run → exit 130, temp dir gone, completed negatives retained.
- Temp dir is removed on every exit path (success, failure, cancel).
- Preflight rejects an unwritable output dir before emitting `started`.

**Verify:**

```bash
time cli/.venv/bin/scanny-boy convert --files tests/fixtures/nef/*.NEF \
     --out /tmp/sbout --film-date 1998-07-04 --per-negative 4
ls -lh /tmp/sbout                       # expect one .tif per source frame
du -sh /tmp/sbout

# cancellation
cli/.venv/bin/scanny-boy convert --files tests/fixtures/nef/*.NEF \
     --out /tmp/sbout2 --film-date 1998-07-04 --per-negative 4 & 
sleep 2; kill -TERM %1; wait; echo "exit=$?"   # expect 130
ls /tmp/scannyboy-* 2>/dev/null || echo "temp cleaned ✓"
```

Also compare `--jobs 1` wall time against default to confirm the speedup is
real. If it isn't roughly linear in cores, the pool isn't doing what you think.

**Done when:** all the above pass, and no `scannyboy-*` temp dir survives any
exit path.

---

### Chunk 6 — Freeze the CLI

**Branch:** `chunk-06-pyinstaller`

**Goal:** A standalone `scanny-boy` binary with no Python dependency.

**Do:**

1. Rewrite `cli/build/scanny_boy.spec` for the real dependency set. `rawpy`
   and `numpy` need explicit binary collection; PyInstaller's automatic hook
   detection misses LibRaw's dylib.
2. **Exclude OpenCV** from the Phase 1 freeze — it is an optional extra and
   would add ~46 MB for code nothing calls yet.
3. Bundle `ROMM RGB.icc` as a datafile so the binary doesn't depend on the
   system path, with the system path as fallback.
4. Update `scripts/build-cli.sh`: verify the binary actually runs after
   building (`--version` smoke test) rather than just checking it exists.

**Tests:** a shell test that runs the *frozen* binary through `probe` and
`convert` against fixtures and diffs the output against the venv build's
output. Freezing breaking `rawpy` at runtime is a classic, silent failure.

**Verify:**

```bash
./scripts/build-cli.sh
ls -lh mac/ScannyBoy/Resources/cli/scanny-boy
./mac/ScannyBoy/Resources/cli/scanny-boy probe --input tests/fixtures/nef --per-negative 4
# must produce byte-identical output to the venv build
```

**Done when:** frozen and venv builds agree byte-for-byte on the same inputs.

---

### Chunk 7 — XcodeGen project and CLI bridge

**Branch:** `chunk-07-xcodegen-bridge`

**Goal:** A buildable macOS app that can invoke the CLI and parse streaming
events. No real UI yet.

**Do:**

1. `mac/project.yml` — target `ScannyBoy`, deployment target macOS 14.0, Swift
   6, `ScannyBoyTests` using **Swift Testing** (available in Xcode 16). Source
   globs so new files need no project edit. App Sandbox **off** (D6).
2. Gitignore `mac/ScannyBoy.xcodeproj`; document `xcodegen generate` in the
   README as the step after any file addition.
3. Rewrite `CLIBridge/CLIRunner.swift` completely — the scaffold's version does
   `readDataToEndOfFile()`, which **blocks until exit and makes streaming
   progress impossible**. Replace with:
   - line-buffered async reads off `Pipe.fileHandleForReading`
   - an `AsyncStream<CLIEvent>` the UI consumes
   - `Codable` event types mirroring §3.2, decoded by `event` discriminator
   - `terminate()` sending SIGTERM
   - **dev/release binary resolution:** bundled `Resources/cli/scanny-boy` when
     present, else `cli/.venv/bin/scanny-boy` via a debug-only path, so you can
     iterate without freezing.
4. Delete `ScanResult.swift` if Chunk 0 somehow left it.

**Tests** (`ScannyBoyTests`):

- Event decoding for every event type in §3.2.
- Unknown event type decodes to a safe `.unknown` case rather than throwing —
  forward compatibility when Phase 2 adds stitch events.
- Partial-line buffering: a JSON object split across two reads decodes once
  reassembled. **This is the highest-value Swift test in the project** — it's
  the bug that will otherwise appear only under real load.
- A fake script emitting canned NDJSON drives the runner end-to-end without
  needing the real CLI.

**Verify:**

```bash
cd mac && xcodegen generate && xcodebuild -scheme ScannyBoy -destination 'platform=macOS' build test 2>&1 | tail -20
```

**Done when:** app builds, tests pass, `xcodegen generate` from a clean
checkout produces a working project. Remove `continue-on-error` from the CI
`swift` job in this PR.

---

### Chunk 8 — The UI

**Branch:** `chunk-08-ui`

**Goal:** The actual app: workflow steps 1–5 end to end.

**Do:**

Single-window SwiftUI app, top-to-bottom flow:

1. **Folder picker** — `NSOpenPanel`, directories only. Remember last used.
2. **File list** — all `.NEF` in the folder, sorted by the same rule the CLI
   uses. Multi-select supporting shift-click ranges (your selection is
   sequential, so range selection is the primary interaction, not an extra).
3. **Shots per negative** — stepper, default 4.
4. **Colour / B&W** — segmented control. Label it so it's clear this is
   metadata and does not reduce channels (D2), e.g. a footnote: *"Both modes
   write 16-bit RGB; this records film type for later processing."*
5. **Capture date** — `DatePicker`, date only, defaulting to today. Label it
   *"Date the film was shot"* to distinguish from the scan date.
6. **Output folder** — picker, must differ from input; validate and disable Run
   otherwise.
7. **Grouping preview** — call `probe` live on selection change and show
   "8 files → 2 negatives", or the `NOT_DIVISIBLE` error inline with Run
   disabled. Catching this before the run is most of the UI's value.
8. **Run / Cancel** — progress bar driven by `progress` events, current
   filename, count, elapsed and estimated remaining. Cancel sends SIGTERM.
9. **Completion** — succeeded/failed counts, any `item_failed` entries listed,
   and a "Reveal in Finder" button.

Keep view state in one `@Observable` model so it's testable without the view.

**Tests:** model-level — selection→grouping state transitions, Run enablement
rules (needs: selection non-empty, divisible, output set and distinct from
input), progress accumulation from a synthetic event stream, cancel state.

**Verify (manual, in the running app):**

- Point at your fixtures, select 8, set 4/negative, set a date, run → 8 TIFFs.
- Select 7 → inline divisibility error, Run disabled.
- Set output = input → blocked with a clear reason.
- Cancel mid-run → stops promptly, no temp dirs, partial outputs cleaned.
- Confirm progress advances *during* the run, not all at once at the end. If it
  jumps at the end, line-flushing (Chunk 1) or streaming reads (Chunk 7)
  regressed.

**Done when:** the full 1–5 workflow runs from the GUI against real fixtures.

---

### Chunk 9 — Docs and end-to-end sign-off

**Branch:** `chunk-09-docs`

**Do:**

1. Rewrite the root `README.md`: what it does, the 0.x prerequisites, clone →
   bootstrap → xcodegen → run, the dev-vs-frozen CLI distinction, how to run
   each test suite, and the fixtures convention (why they're gitignored and
   what to put there).
2. `CONTRIBUTING.md` — the chunk workflow, branch naming, the "paste real test
   output in the PR" rule.
3. `docs/DECISIONS.md` — lift §1 verbatim so future agents inherit the
   rationale rather than re-deriving it.
4. Delete `plan.md` from the repo root (superseded by this document).
5. Tag `v0.1.0`.

**Verify:** clone into a fresh directory and follow the README literally, with
nothing cached. Anything you have to improvise is a README bug.

---

## 5. Testing conventions

- **Python:** `pytest`, tests co-located as `*_test.py` next to the code under
  test (your stated preference).
- **Swift:** Swift Testing (`@Test`), in `ScannyBoyTests/`.
- **Fixture-gated tests** must `skipif` cleanly so CI passes without fixtures.
  A test that fails on the CI runner for lack of a NEF is a broken test.
- **No mocking of rawpy.** Either use a real fixture or use a real synthetic
  array. Mocked decode proves nothing about the thing most likely to break.

---

## 6. Risk register

| Risk | Severity | Mitigation |
|------|----------|-----------|
| LibRaw 0.22.1 mis-reads Z f NEFs | **High** — blocks everything | Chunk 2's eyeball check is the gate. If colours or dimensions are wrong, stop and report before building on it. |
| Gamma/ICC mismatch | Medium — silently wrong files | Pinned to `(1.8, 16)` + ROMM, asserted in Chunk 2. |
| Progress mis-attribution under parallelism | Medium | Workers return their own index; asserted under `--jobs 4` in Chunk 5. |
| Swift partial-line JSON parsing | Medium | Explicit split-line test in Chunk 7. |
| PyInstaller silently breaking rawpy | Medium | Frozen-vs-venv byte-diff in Chunk 6. |
| Disk exhaustion mid-roll | Low | Preflight estimate + per-negative streaming. |

---

## 7. Phase 2 preview — stitching

Not in scope. Recorded so Phase 1 doesn't foreclose it.

You confirmed: **copy stand, camera parallel to film, film moved by hand** — so
translation-dominant, but with inconsistent overlap and small unintended
rotations. That means:

- `cv2.Stitcher_create(cv2.Stitcher_SCANS)` is the right mode — its affine
  model absorbs the small rotations hand-feeding introduces, which a
  pure-translation model would not.
- ⚠️ Use `cv2.Stitcher_SCANS`, **not** `cv2.Stitcher.SCANS` — the latter does
  not exist in OpenCV 5 (verified).
- **Expect the default Stitcher to fail on negatives.** Low contrast, inverted
  tonality, and large uniform-density areas (skies) starve feature detection.
  Film grain is genuinely helpful high-frequency texture here — do not blur or
  aggressively downscale before feature detection.
- Plan for a **detection-only preprocessing pass**: invert and contrast-stretch
  a copy purely to find features, then apply the resulting transforms to the
  untouched linear-ish originals. Never stitch the stretched copy.
- Stitcher's default exposure compensation and seam blending will fight your
  consistent-exposure premise — expect to disable them.

Also deferred: re-enabling App Sandbox with security-scoped bookmarks (D6),
code signing, and notarisation.

---

## 8. Handoff template

When dispatching a chunk:

> Implement **Chunk N — <title>** from `docs/IMPLEMENTATION_PLAN.md`.
> Read §1 (locked decisions) first and do not re-litigate them.
> Work on branch `chunk-NN-<slug>`. Write the listed tests, run the
> verification block, and paste real terminal output into the PR body.
> If a locked decision appears to block you, stop and report rather than
> substituting your own choice.
