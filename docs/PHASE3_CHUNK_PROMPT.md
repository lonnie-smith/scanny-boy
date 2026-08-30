# Phase 3 chunk prompt header

Paste this in front of every Phase 3 implementation request, replacing `N`
with the chunk number. For Phase 1 chunks use [`CHUNK_PROMPT.md`](CHUNK_PROMPT.md);
for Phase 2, [`PHASE2_CHUNK_PROMPT.md`](PHASE2_CHUNK_PROMPT.md).

## Which model to run each chunk with

| Chunk | Model | Auto-advance | Why |
| --- | --- | --- | --- |
| P3-0 Contract and protocol v3 | **Sonnet 5** | yes | Wide but shallow; every edit written out |
| P3-1 ICC profile matching LibRaw's curve | **Sonnet 5** | **no** — next is Opus | Self-contained; every parameter and byte rule given in plan §3.13 |
| P3-2 Roll manifest v2 | **Opus 5** | yes | Rewrites a module Phase 2's tests cover; the highest regression risk in Phase 3 |
| P3-3 Roll-aware output folder and `probe --roll` | **Opus 5** | **no** — next is Sonnet | Refactors shared Phase 1/2 code that must keep passing |
| P3-4 Library, slugs, roll commands | **Sonnet 5** | yes | New modules with given signatures |
| P3-5 `run`/`stitch` against a roll | **Sonnet 5** | yes | Orchestration; every flag and removal enumerated |
| P3-6 Sequencing and capture times | **Sonnet 5** | yes | One small pure module, formula given |
| P3-7 `apply-metadata` | **Sonnet 5** | yes | Follows `tiff_exif.py`'s existing two-pass write |
| P3-8 Re-apply after re-stitch | **Sonnet 5** | yes | Small; P3-7 built the machinery |
| P3-9 Package and verify | **Sonnet 5**, escalate to **Opus 5** on failure | **no** — possible model change | Ordinary until PyInstaller emits an opaque error |
| P3-10 App: library sidebar and roll CRUD | **Sonnet 5** | **no** — approval point 6.1 | New IA; look at it before building on it |
| P3-11 App: Add Scans and overlap sheet | **Sonnet 5** | yes | Reworks existing views |
| P3-12 App: Edit stage | **Sonnet 5** | yes | Largest Swift chunk; every control specified |
| P3-13 Documentation and v0.3 sign-off | **Sonnet 5** | — | Prose |

**Haiku 4.5 is not recommended for any Phase 3 chunk.**

This table duplicates the plan's §5.3. If the two ever disagree, **§5.3
wins** and the disagreement is itself a stop-and-report.

---

You are implementing **Chunk P3-N** of the Scanny Boy **Phase 3** plan.

**Read first, in this order, before writing any code:**

1. `docs/PHASE3_IMPLEMENTATION_PLAN.md` sections 0–4 — what is breaking and
   why, the goal, the amendments to Phases 1 and 2, the locked decisions,
   and the test rules. **Section 0 matters:** Phase 3 is a deliberate
   protocol and manifest break with no migration path.
2. Section 5.1, then the Chunk P3-N entry in section 5.
3. Section 6 (approval and pause points).
4. `shared/contract/CONTRACT.md`, and `docs/PHASE2_IMPLEMENTATION_PLAN.md`
   sections 2, 3.2–3.5, 3.8, 3.11, and 3.12 — all still authoritative and
   unchanged. Phase 3 amends only what its section 2 table names.
5. `docs/IMPLEMENTATION_PLAN.md` sections 3 and 4 for Phase 1's rules, which
   remain in force except as that same table amends them.

**The most important rule.** Your chunk entry names every module, class,
function, field, constant, error code, and test you need. **If you find
yourself inventing one, you have left the plan — stop and report.**
Concretely, stop rather than deciding, if you would have to:

- name anything not written down in the plan;
- choose a threshold, tolerance, or magic number not written down;
- change a signature the plan gives;
- modify Phase 1 or Phase 2 code beyond what your chunk's file table names;
- relax or delete a test assertion to make something pass.

Report what you needed, what you would have chosen, and why. A stopped chunk
costs one message. A chunk that invented an API costs a rewrite of
everything built on top of it. **Guessing is the expensive option here.**

**Auto-advance.** Your chunk entry carries an `Auto-advance` line.

- **`yes`**: open the pull request, wait for CI to go green, merge it with
  `gh pr merge --squash`, then begin the next chunk in the same session
  using this same prompt with N incremented. Report a one-paragraph summary
  per chunk as you go.
- **`no`**: open the pull request and **stop**. Report and wait.

Regardless of the marker, **stop** if: CI fails twice on the same cause; a
Phase 1 or Phase 2 test would have to change beyond what your file table
names; section 5.1 applies; or an approval point in section 6.1 applies.
Never merge a red PR, never force-push, never push straight to `main`.

**Other ground rules:**

- **Section 3 is locked.** If it looks wrong, say so and wait; do not
  improve it in passing.
- **Phase 2's registration, colour, gate, and memory work is settled.** Do
  not re-benchmark OpenCV, re-derive the ROMM curve, re-litigate the rigid
  model, or touch the section 3.12 constants.
- **P3-1 changes no pixel value.** It replaces the embedded ICC profile,
  not the encoding. A failing pixel-data assertion means the chunk has gone
  wrong — stop and report rather than updating the expectation.
- **There is no migration.** Never write code that reads
  `manifest_format_version: 1` of the roll manifest, and never add a
  compatibility shim for protocol version 2.
- Work only inside Chunk P3-N's stated scope. Note anything you find that
  belongs to another chunk in the PR body and leave it alone.
- Start from an up-to-date `main`, work on the branch named in the chunk
  entry, and open one pull request for it. `main` is protected.

**Sample files:**

- Real Nikon Z f NEFs live at `tests/fixtures/nef/`. They are ignored by Git
  and must stay ignored — the repository is public and the files are large.
  Never `git add` them, never copy them elsewhere in the repository.
- Phase 1's appendix A and Phase 2's appendix C record their groupings and
  settings. Use those values rather than re-deriving them.
- Tests that need them skip clearly and say what was not tested.

**Testing rules that bite in Phase 3:**

- **Never touch the real `~/Pictures`.** Every library test injects a
  temporary base directory. A test reaching for
  `FileManager.default.urls(for: .picturesDirectory, …)` has gone wrong.
- **Cross-run behaviour needs two real runs.** Overlap detection,
  sequencing, output-name collisions, and supersession are cross-run
  properties; a hand-edited manifest that merely looks like a second run
  happened proves nothing. The manifest must come from the real writing
  path — before P3-4 that means a second `stitch`, since `run --roll` does
  not exist yet.
- **Replace really deletes.** An unskipped overlap supersedes the negative
  it covers and removes that TIFF (plan §3.4). Assert the whole rule: the
  replacement publishes under its own name, the predecessor is marked and
  desequenced, its file is gone, its name stays claimed, and its neighbours
  keep their sequence positions.
- **Apply tests assert the round trip and the non-changes**: the tag reads
  back, and the ICC profile, the other curated tags, and the pixel data are
  all unchanged. An apply that quietly rewrites pixels is exactly what this
  catches.
- **Test the skips**, not just the successes: an externally-modified TIFF is
  skipped and named, and the negatives around it still apply.
- Phase 2's rules still hold: film-like synthetic images from
  `synthetic_scene_support.py`, ground truth for every registration test,
  never an exact pixel hash of stitched output, never a full-size canvas in
  a test.

**Definition of done for the chunk:**

1. Every "Do" item and every named file in the chunk entry exists.
2. Every test named in the chunk entry exists, with that name, and passes.
3. The full existing suite passes, not just the new tests:
   `cd cli && uv run ruff check . && uv run pytest`
   plus `cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'`
   for chunks touching Swift, plus the chunk's own verification commands.
4. The PR body contains **actual pasted command output**, not a description
   of it.
5. Never hide a failing build by piping it through `tail` or `head`.

**Stop and report — do not work around — if:**

- an earlier chunk is not merged;
- an approval point in section 6.1 applies;
- a Phase 1 or Phase 2 test's *meaning* would have to change;
- the plan does not name something you need.

**Finally:** end with a short summary of what changed, what the tests prove,
and anything you deliberately left out.
