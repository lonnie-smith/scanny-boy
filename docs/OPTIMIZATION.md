# Optimization plan: stop paying for the process

The Edit tab is laggy because every gesture constructs a Python process. This
plan removes that cost in four stages, in strict order, each shippable on its
own and each leaving the CLI a working CLI.

The headline measurement, taken on a real roll
(`Sep-6-2026-at-10-56-PM`, 10137x11705 and 10317x12086, 16-bit RGB, DEFLATE,
4 rows/strip, 559 MB and 591 MB):

| | wall | startup | work |
| --- | --- | --- | --- |
| `edit list-spots` (trivial, touches the DB) | 0.79 s | 0.74 s | 0.05 s |
| `edit render-region` (fast strip path) | 1.14 s | 0.74 s | 0.40 s |
| `edit render-preview` | 1.55 s | 0.74 s | 0.81 s |

**The tone and colour maths is under a millisecond.** A `cv2.LUT` over a
preview-sized array does not register on a 3-run best-of. Every millisecond
the user waits is process startup, TIFF decode, or PNG encode — none of it
the image processing this project exists to do. That is the whole argument
for the plan below: the algorithms are not the problem and must not be
touched.

This plan follows the conventions of `docs/MONOCHROME_PLAN.md` and
`docs/SPOTTING_PLAN.md`: every stage is measured before the next is
justified, no constant is pinned without a number behind it, and the
rejected alternatives are written down so a later reader does not re-propose
them.

---

## 0. Why this shape, and why in this order

### 0.1 Startup is imports, not `fork`

The obvious reading of the table above is "spawning processes is slow". It is
not. Measured on this machine:

```
bare interpreter boot                    0.02 s
import scanny_boy.cli                    0.70 s
packaged binary `--help`                 0.74 s
```

Process creation is 20 ms and is irrelevant. The 0.70 s is the import graph
of `cli.py:12-28`, which pulls the whole application eagerly:

```
1107 ms  scanny_boy.film_base    (655 ms of it cv2)
 607 ms  scanny_boy.calibration  (603 ms of it scipy.optimize)
 337 ms  scanny_boy.library.db   (335 ms of it alembic -> sqlalchemy)
```

This matters for sequencing. If the cost were `fork`/`exec`, only a resident
process could help and §1 would be pointless. Because it is imports, §1
recovers a real fraction of it for a few hours of work and no architectural
risk — and the same work later shortens the daemon's cold start, so it is not
throwaway.

### 0.2 Decode is the second term, and only a daemon can remove it

Work, isolated from startup:

```
full TIFF decode                              599 ms  -> 712 MB array
render_preview  total work                    736 ms  (599 decode + 22 PNG + ~115 transform)
render_region   fast strip path               270 ms
render_region   fine-rotation / repair path   772 ms  <- full decode fallback
cv2.imencode PNG, 1600x1200 crop               50 ms   (2.14 MB out of 5.76 MB raw)
cv2.imencode PNG, preview-sized (1024 edge)    22 ms   (1.54 MB out of 2.72 MB raw)
the tone/colour LUT itself                     <1 ms
```

Decode is 599 of `render_preview`'s 736 ms. A one-shot process cannot amortize
it: the array dies with the process, so the next slider nudge decodes the same
712 MB again. Holding decoded pixels across requests is the one thing a CLI
structurally cannot do at any speed, and it is why §3 exists and why it comes
last among the substantive stages — it is worthless until something is
resident to hold the cache.

### 0.3 The strip reader is already good; the fallback is the problem

`previews.render_region` (`previews.py:526`) reads only the strips overlapping
the requested rect. With 4 rows/strip a 1600x1200 crop touches roughly 300 of
2926 strips, and 270 ms reflects that. This code is not a target.

Its two documented fallbacks to a **full** decode are: a nonzero fine rotation
angle, and spot repair being on. Both replay the transform over the whole
image and slice the rect out, because the warp interpolates across the crop
boundary. That is 270 ms -> 772 ms, on every pan.

Spot-repair-on is not an edge case. `EditModel.swift:105` says it plainly —
the point of turning repair on is to look at the result. A user inspecting
repairs at 100% pays the fallback on every gesture. The numbers in §0 were
taken with no `rotate_fine` and no spots op recorded, so they are the
*optimistic* case for real editing.

### 0.4 Keep stdio; a socket buys nothing here

The transport does not need to change. `CLISession`
(`mac/ScannyBoy/CLIBridge/CLISession.swift`) already streams newline-delimited
JSON with a `LineAssembler`, drains stdout and stderr on separate queues so
neither can stall the other, and finishes the stream exactly once — after the
process has terminated *and* both pipes have reached EOF. That is the
difficult part and it is done and tested.

The child currently gets `FileHandle.nullDevice` on stdin
(`CLISession.swift:73`) with the comment "the CLI reads no input". §2 deletes
that line and sends requests the other way up the same pipe. The event
contract (`shared/contract/CONTRACT.md:782`), `CLIEvent`, `LineAssembler` and
every decoder survive untouched.

A socket would add port allocation, an auth story, orphaned-daemon cleanup,
and sandbox and entitlement friction in a signed, notarized app. It buys
multiple concurrent clients and reconnect-after-crash. One app talking to its
own bundled helper needs neither. **Rejected.**

Also rejected: **XPC**. It is the Apple-native answer and it would work, but it
means a second bundled service target, a separate signing and entitlement
story, and abandoning the JSON event contract that both sides' tests are
written against. The pipe is already there.

### 0.5 Ordering is mandatory

§1 before §2, so the daemon's cold start is short and so the win from §2 is
measured against an already-trimmed baseline rather than credited with §1's
work. §2 before §3, because a cache needs a resident process to live in. §3
before §4, because §4's win (~50-84 ms of PNG encode) is invisible underneath
a 599 ms decode and cannot be honestly measured until §3 has removed it. §4 is
genuinely optional and is written last for that reason.

Every stage ends with the app fully working. Nothing here is a flag day.

---

## 1. Lazy-import the subcommand modules

**Target: 0.74 s -> ~0.45 s of startup on every `edit` command.**

`cli.py:12-28` imports every subcommand's implementation at module scope, so
`edit render-region` pays for `scipy.optimize` (via `calibration`) and
`alembic` (via `library.db`) that it never calls. Measured import cost of the
individual modules:

```
0.42 s  scanny_boy.edits
0.40 s  scanny_boy.previews
0.36 s  scanny_boy.film_base
0.56 s  scanny_boy.calibration
0.23 s  scanny_boy.library.db
```

### 1.1 What to do

Move each subcommand's implementation import inside the function that
dispatches it. `argparse` wiring stays at module scope — it must, to build the
parser — so only the `run_*` callables and the types they need move.

The awkward cases, all of which need care rather than cleverness:

- **`events`** stays eager. Every command emits, it is cheap, and the
  `Code` enum is referenced by the parser's error paths.
- **`library.db`'s 335 ms is alembic**, imported for migrations. Most commands
  open an already-migrated database and never need it. Defer the alembic
  import to the migration path specifically, not to `library.db` as a whole.
- **Dataclass annotations** that reference deferred types need
  `from __future__ import annotations` (already present) plus `TYPE_CHECKING`
  guards, the pattern `edits.py:28` already uses.

### 1.2 How to know it worked

Add a test that asserts the import graph, not the wall time — a timing
assertion is flaky on CI and worthless. Run the packaged binary with
`-X importtime`, or import `scanny_boy.cli` in a subprocess and assert that
`scipy` and `alembic` are absent from `sys.modules`. Pin the assertion to
those two module names; they are the two large leaves and they are what
regresses when someone adds a convenience import at the top of `cli.py`.

Re-measure the three commands in §0's table afterwards and record the numbers
here. Expect roughly 1.14 -> 0.85 s and 1.55 -> 1.26 s.

**Re-measured after §1** (same roll and negatives as §0's table, 3-run
best-of, this time through the venv console script rather than the packaged
binary — `uv run` adds ~0.3 s of its own and the packaged binary a little
more again, so the absolute numbers sit below §0's rather than beside them;
the delta is the honest part):

| | before §1 | after §1 |
| --- | --- | --- |
| `edit list-spots` | 0.79 s | 0.39 s |
| `edit render-region` | 1.14 s | 0.75 s |
| `edit render-preview` | 1.55 s | 1.23 s |

A bare `import scanny_boy.cli` went from ~0.70 s to ~0.05 s (warm, measured
with `-X importtime`). What remains on the render commands is cv2, rawpy and
SQLAlchemy, all reached through `edits` -> `previews` / `repo` at dispatch
time — the plan's ~0.45 s estimate did not account for those, and deferring
them further would mean import surgery inside `previews`/`repo`, which §6's
scope rules counsel against. §2 makes the remainder a once-per-process cost
instead.

### 1.3 Why this is not the fix

It leaves ~0.45 s of startup on every gesture, and it does nothing at all
about the 599 ms decode. It is worth doing because it is cheap, low-risk, and
permanently useful — including to §2's cold start. It is not a substitute for
§2.

---

## 2. `scanny-boy serve`: one resident process over stdin

**Target: startup amortized to zero. Region 1.14 -> 0.40 s, preview
1.55 -> 0.81 s, slider round trip ~2.5 s -> ~1.0 s.**

### 2.1 The request channel

A new subcommand, `scanny-boy serve`, reads newline-delimited JSON **requests**
on stdin and writes the existing newline-delimited JSON **events** on stdout.
One request object per line:

```json
{"request_id": "uuid", "command": ["edit", "render-region", "--roll", "...", ...]}
```

`command` is the argv the one-shot CLI would have received. That is deliberate:
`serve` re-enters the same `argparse` parser and the same `run_*` functions, so
there is exactly one implementation of every command and the one-shot path
cannot drift from the served path.

Every event emitted while a request is in flight gains a `request_id` field.
That is the only addition to the event schema. Bump `PROTOCOL_VERSION`
(`events.py:112`) and add `request_id` to `shared/contract/schema.json` as
optional — one-shot invocations continue to emit events without it, and the
Swift decoder must treat it as optional for exactly that reason.

Each request ends with a terminal event carrying its `request_id` and the exit
status the one-shot CLI would have returned, so `CLIOutcome`
(`CLISession.swift`) maps over unchanged.

### 2.2 Cancellation, which is the part that actually changes

Today cancellation is SIGTERM to the child
(`CLISession.requestCancellation()`), caught by `sigterm_cancellation`
(`cancellation.py:65`), which sets a `CancellationToken` the pipeline polls.

With a resident process, SIGTERM kills every in-flight request, not the one
the user cancelled. So:

- A `{"request_id": "...", "cancel": true}` request cancels one request by
  setting **that request's** token.
- `CancellationToken` (`cancellation.py:46`) is already the right abstraction
  and does not change. Only its trigger does: an in-band message instead of a
  signal. `sigterm_cancellation` stays, for the one-shot path.
- SIGTERM to the daemon keeps its current meaning — shut down — and should
  cancel every live token, then drain.

This is the single largest correctness risk in the plan. The existing
cancellation tests must be duplicated against `serve` before anything else in
§2 is considered done.

### 2.3 Concurrency: serialize first

Run one request at a time, on a worker thread, with the reader loop free to
accept a `cancel` for the running request. Do **not** build a worker pool in
this stage.

Serializing is defensible because the app already assumes supersession
everywhere: `EditModel`'s debounce and commit-task cancellation
(`EditModel.swift:310-346`), and `PreviewZoomModel.fetchCrop`'s
`inFlightKey` and `requestGeneration` guards
(`PreviewZoomModel.swift:350`). Both already drop superseded work. A queue
depth greater than one is a thing to add when a measurement demands it.

The one case to watch is a long `run`/`stitch` blocking Edit-tab queries. If
that bites, the answer is a second daemon for long jobs, not a thread pool
inside one — the long-job path already has its own progress and cancellation
semantics and does not want to share a process with interactive queries.

### 2.4 Lifecycle, on the Swift side

`CLIRunner.session(for:)` (`CLIRunner.swift:701`) is the single chokepoint —
every command in the app is built as a `CLICommand` and handed to it. That is
where the daemon goes, and it is why this stage does not touch call sites.

- `CLIRunner` gains a lazily-started shared daemon session and a request
  registry keyed by `request_id`.
- `session(for:)` keeps its signature and its return type. It submits a
  request and returns an `AsyncStream<CLISessionOutput>` filtered to that
  `request_id`. Callers — `EditModel.renderRegion` (`EditModel.swift:851`),
  `renderPreview` (`:896`), and every other `for await line in ... .start()`
  loop — are unchanged.
- **Not every command goes to the daemon.** Route `edit *`, `roll info`,
  `roll list` and the other interactive queries there. Leave `run`, `stitch`,
  `export` and `flatfield create` on one-shot processes: they are long,
  they already have their own cancellation and progress semantics, and a crash
  in one must not take down the interactive session.
- If the daemon fails to start or dies, fall back to one-shot for that
  request and restart the daemon behind it. The app must never become unusable
  because a helper died. Log it to stderr; do not surface it.
- The daemon must die with the app. It inherits the pipe, so closing stdin is
  the ordinary path; keep the existing `onTermination` SIGTERM as the backstop
  it already is.

### 2.5 Testing

`CLISessionTests` and `CLIIntegrationTests` gain a served variant. The
existing one-shot tests stay exactly as they are — they are the proof that
`serve` did not fork the implementation. Add, specifically:

- Two overlapping requests: both complete, events are correctly partitioned by
  `request_id`, and neither sees the other's.
- Cancel request A while B runs: A reports cancelled, B completes untouched.
- Kill the daemon mid-request: the app falls back and stays usable.
- A `run` still goes one-shot and still cancels by SIGTERM.

**Re-measured after §2** (the same roll and negative as §0's table, served
through the rebuilt packaged helper, per-request wall from the request line
to its `finished`; before §3, so decode is still in every number):

| | wall |
| --- | --- |
| first served `render-region` (cold daemon) | 1.62 s |
| second served `render-region` | 0.42 s |
| one-shot `render-preview`, same negative | 2.74 s |

The warm region lands on §2's 0.40 s target. One deviation from the letter
of §2.5 worth recording: the served routing is opt-in on `CLIRunner`
(`daemonRouting:`), default off, because dozens of existing suites drive
fake one-shot executables for `roll list` and `edit *`, and this plan's own
rule is that the existing one-shot tests stay exactly as they are. The app's
single runner (`ScannyBoyApp`) is built with routing on; the served variants
live in `CLIDaemonTests` (fake `serve` executable, fast tier) and
`CLIIntegrationTests` (the real helper).

---

## 3. Cache decoded pixels in the daemon

**Target: preview 0.81 -> ~0.14 s; the repair/fine-rotation region fallback
0.77 -> ~0.05 s of work. Slider round trip well under 200 ms.**

This is the stage the user actually feels, and it is impossible before §2.

### 3.1 Cache the preview-resolution array, not the full one

A decoded negative is **712 MB**. An LRU of two is 1.4 GB, which is a real
cost to impose on a machine that is also holding the app's own image buffers.

Cache instead at preview resolution — `PREVIEW_MAX_EDGE` is 1024
(`previews.py:62`), so a negative's display array is 887x1024, about **5.4 MB**
at 16-bit — and keep 100% zoom on the existing strip reader. That is most of
the win for well under 1% of the RAM, because:

- The fit view is where sliders live, and it only ever needs preview
  resolution. `render_preview` (`previews.py:299`) decodes 119 MP and
  immediately throws almost all of it away.
- The 100% region path already reads only the strips it needs (§0.3) and is
  270 ms, which is tolerable for a deliberate pan.

Keyed on `(tiff_path, mtime, quarter_turns, flipped, fine_angle)` — the
geometry, not the tone. Tone and colour are LUTs applied *after* the cached
array (<1 ms, §0), so a slider drag must hit the cache, and it will.

Bound the cache by total bytes, not entry count, and make the bound one
constant in one module per this project's convention.

**Re-measured after §3.1** (the same roll and negative as §0's table, served
through the rebuilt packaged helper, per-request wall from the request line
to its `finished`):

| | wall |
| --- | --- |
| first served `render-preview` (cold daemon: imports + decode) | 3.45 s |
| second served `render-preview` (cache hit) | 0.054 s |
| mode change, same negative (cache hit) | 0.038 s |
| one-shot `render-preview`, same negative | 2.74 s |

The slider round trip is 38–54 ms — well under the 200 ms gate, so §4 stays
unbuilt (§4.2's own stop-here rule). Note the packaged helper's one-shot is
slower than §0's table (2.74 s against 1.55 s): PyInstaller's frozen imports
cost more than the venv's, which is exactly the cost §2 amortizes.

The key gained a spot term beyond the list above: a live spot set's repair
is the first step of the display replay, so it changes decoded pixels and
belongs in the key (§3.3). It rides as a hash of the set's canonical JSON;
Swift's counterpart (`EditModel.spotsTerm`) is a coarser summary of the same
rule, and both sites are commented at each other because they cannot share
one definition.

### 3.2 The full-resolution cache is deferred, not refused

Caching the full 712 MB array would additionally fix the repair/fine-rotation
fallback (§0.3) at 100% zoom. It is the right answer for a user sitting on one
negative inspecting repairs, and the wrong answer for anyone stepping through
a roll.

Do not build it in this stage. Ship §3.1, then measure how long users actually
sit on one negative at 100% with repair on. If that turns out to be the real
inspection workflow, add a single-entry full-resolution cache — one negative,
dropped the moment the selection changes — as a follow-up with its own
measurement. -> `punchlist.md`.

### 3.3 Invalidation

The cache key must include everything that changes the decoded pixels before
the LUT. Getting this wrong shows the user a stale image, which is worse than
being slow.

Swift already computes exactly this token: `EditModel.renderGeneration`
(`EditModel.swift:947`) folds rotation, flip, tone, colour and the spots term
into one string, and the region and preview file caches are keyed on it. The
daemon's key is the same idea minus the tone and colour terms, since those are
now applied after the cache. Derive both from one shared definition if
practical; if not, the divergence must be commented at both sites.

A re-stitch rewrites the published TIFF, which is why `mtime` is in the key.

---

## 4. Raw bytes instead of PNG

**Target: another 22-50 ms per render, plus whatever Swift spends decoding
the PNG back.**

Deliberately last, and genuinely optional.

### 4.1 What is being paid now

Each render encodes a PNG (`previews.py:213` and `:345`, `cv2.imencode`),
writes it to a cache file, and Swift reads it back and decodes it
(`EditModel.fullResolutionImage`, `EditModel.swift:1000`). Measured on real
pixels with the encoder the code actually uses:

```
cv2.imencode PNG, 1600x1200 crop            50 ms   (2.14 MB out of 5.76 MB raw)
cv2.imencode PNG, preview-sized (1024 edge)  22 ms   (1.54 MB out of 2.72 MB raw)
write to disk                                ~1 ms
```

Note that OpenCV's default PNG compression level is fast. An earlier
measurement of this project used `imagecodecs.png_encode` at its default level
and got 799 ms; that is not what this code does, and any future measurement
here must use `cv2.imencode` or it is measuring a different program.

### 4.2 Why it is last, and why it is the weakest stage

Today the PNG encode is 22 ms of `render_preview`'s 736 ms — 3%, invisible next
to the 599 ms decode. It only becomes a meaningful share once §3 has removed
that decode: preview work drops to roughly 137 ms, of which 22 ms is encode, so
this stage would be about 16% of what remains.

It is a better deal on the **region** path, where 50 ms sits inside 270 ms of
work — 18% today, and a larger share after §3 — and where the user is panning,
which is the gesture most sensitive to latency.

Be honest that this is tens of milliseconds either way. It is worth doing only
if §3 lands and the remaining latency is still short of feeling immediate. If
the slider round trip is already under 200 ms after §3, **stop here** and spend
the effort elsewhere. This stage exists in the plan so the option is written
down with its real numbers, not because it is committed to.

### 4.3 The transport

Raw pixels cannot go inline in a JSON event line — a 5.76 MB crop
base64-encodes to 7.7 MB of line-oriented text per pan, and the whole event
stream is line-assembled and UTF-8 decoded. **Rejected.**

Use POSIX shared memory. The daemon writes the raw buffer into a `shm_unlink`ed
segment and the `region_rendered` / `preview_rendered` event carries the
segment name plus `width`, `height`, `stride` and a pixel format, in place of
`path`. Swift maps it and wraps it in a `CGDataProvider` with no copy and no
decode. The app is already sandboxed with the helper as a child, so the
segment is shareable without additional entitlements — **verify this before
committing to the design**, because if it turns out to need one, the fallback
below is the answer instead.

The fallback, if shared memory proves awkward: keep the cache file but write
raw bytes with a tiny fixed header instead of PNG, and `mmap` it from Swift.
That drops the encode and the decode while keeping the existing
file-handoff shape and needing no new entitlement. It costs a 5.76 MB write per
render instead of a 2.14 MB one, which at ~1 ms measured is not a real cost.

Either way, both events keep `path` as an alternative so the one-shot CLI —
which has no shared segment to hand back and no daemon lifetime to scope it
to — keeps working unchanged. This is another `oneOf` in
`shared/contract/schema.json` and another `PROTOCOL_VERSION` bump.

### 4.4 The lifetime problem

A shared segment must outlive the event and die when Swift is done. Scope it to
the request: the daemon unlinks immediately after creating it, so the segment
vanishes when the last descriptor closes, and Swift closes its mapping when the
`Thumbnail` is released. A daemon crash then leaks nothing.

If that ownership cannot be made airtight, ship §4.3's `mmap`ed-file fallback
instead. A leaked 5.76 MB segment per pan is a worse bug than 50 ms of PNG
encode is a problem.

---

## 5. Work order

1. **§1** — lazy imports, plus the `sys.modules` assertion test. Re-measure and
   record. Ship it.
2. **§2.1-2.2** — `serve`, the request envelope, the `request_id` field, the
   protocol bump, and in-band cancellation with its tests. No Swift changes
   yet; drive it from a test harness.
3. **§2.4** — the Swift side, behind the `CLIRunner.session(for:)` chokepoint,
   routing only `edit *` and the cheap `roll` queries. Measure. Ship it.
4. **§3.1** — the preview-resolution cache and its invalidation. Measure the
   slider round trip specifically. Ship it.
5. **Measure, then decide.** §3.2 (full-resolution cache) and §4 (raw bytes)
   are both conditional on what the numbers say after step 4. Neither is
   committed to by this document.
6. **`DECISIONS.md`** — record the stdio-over-socket choice (§0.4), the
   startup-is-imports finding (§0.1), and the preview-resolution-over-full
   cache choice (§3.1). All three are things a later reader will otherwise
   re-litigate.

Run `uv run pytest --slow` at steps **2 and 4**: step 2 changes cancellation,
which the slow run exercises through real conversions; step 4 changes what
`render_preview` returns and the slow tier is what validates rendered output.
Steps 1 and 3 are covered by the fast tier plus
`SCANNY_BOY_SLOW_TESTS=1 ./scripts/test-mac.sh` for the Swift side of step 3.

---

## 6. Explicitly out of scope

- **Touching the tone, colour, or spot maths.** It is under a millisecond
  (§0). There is nothing to win and a great deal to break.
- **Touching `render_region`'s strip reader.** Already good (§0.3); the
  problem is its fallback, and §3 addresses that by caching rather than by
  rewriting the reader.
- **Rewriting the transport as a socket or XPC.** Refused with reasons in
  §0.4.
- **A worker pool in the daemon.** Serialize first (§2.3); add concurrency
  when a measurement asks for it.
- **Moving long jobs (`run`, `stitch`, `export`) into the daemon.** They stay
  one-shot (§2.4). A separate daemon for long jobs is a possible future, not
  part of this.
- **GPU or Metal rendering of the display encode.** A different project, and
  premature while 0.74 s of startup is still on the table.
- **Making the published TIFF cheaper to decode** (tiled instead of stripped,
  or a pyramid/sub-resolution page). It would cut the 599 ms directly and it
  would help the one-shot path too — but it changes the published artifact
  format, which is a decision with archival consequences and needs its own
  plan. -> `punchlist.md`.
