# Phase 2 chunk prompt header

Paste this in front of every Phase 2 implementation request, replacing `N`
with the chunk number. Run one chunk per session, in order.

For Phase 1 chunks, use [`CHUNK_PROMPT.md`](CHUNK_PROMPT.md) instead.

## Which model to run each chunk with

Phase 2 is written to be executed rather than interpreted — every module,
signature, constant, and test name is specified — so **most chunks run well
on Sonnet 5**. Two do not, for reasons that are about blast radius rather
than difficulty.

| Chunk | Model | Why |
| --- | --- | --- |
| P2-0 Contract and protocol v2 | **Sonnet 5** | Wide but shallow; every edit is written out |
| P2-1 Registration spike | **Opus 5** | Sets every threshold the rest of the plan depends on, and must recognise when a measurement invalidates the design rather than reporting it as a number |
| P2-2 Colour and detection | **Sonnet 5** | Two small modules, formulas given |
| P2-3 Pairwise registration | **Sonnet 5** | Algorithm fully specified, ground-truth tests |
| P2-4 Layout solve | **Sonnet 5** | The linear algebra is written out in the plan; do not let it be re-derived |
| P2-5 Compositing | **Sonnet 5** | Longest chunk, but every step is enumerated |
| P2-6 `stitch` command | **Opus 5** | Refactors shared Phase 1 code that Phase 1's own tests must keep passing — the highest regression risk in Phase 2 |
| P2-7 `run` command | **Sonnet 5** | Orchestration plus a cleanup table with no inference in it |
| P2-8 Packaging | **Sonnet 5**, escalate to **Opus 5** on failure | Ordinary until PyInstaller emits an opaque error, then it is debugging with poor signal |
| P2-9 App run flow | **Sonnet 5** | SwiftUI against established patterns |
| P2-10 App re-stitch | **Sonnet 5** | Reuses P2-9's UI |
| P2-11 Documentation | **Sonnet 5** | Prose; use Opus 5 if you want the README to read better |

**Haiku 4.5 is not recommended for any Phase 2 chunk.** Even the mechanical
ones touch numerically sensitive code where a plausible-looking wrong line
passes review.

---

You are implementing **Chunk P2-N** of the Scanny Boy **Phase 2** plan.

**Read first, in this order, before writing any code:**

1. `docs/PHASE2_IMPLEMENTATION_PLAN.md` sections 0–4 — the scope correction,
   goal, vocabulary, verified facts, locked decisions, and build rules.
   Section 0 matters: Phase 1 did **not** implement stitching, whatever
   older notes may say.
2. Section 5.1 — the rule that makes this plan safe to execute — and then
   the Chunk P2-N entry in section 5.
3. Section 3.12 (measured constants). If it still says *unset* and your
   chunk needs a value from it, stop.
4. Sections 6 (test rules) and 7 (approval and pause points), and appendices
   B and C if they have been written yet.
5. `docs/IMPLEMENTATION_PLAN.md` sections 3 and 4, and
   `shared/contract/CONTRACT.md`. Phase 1's decisions are still in force;
   Phase 2 amends exactly two of them and says so where it does.

**The most important rule.** Your chunk entry names every module, class,
function, field, constant, error code, and test you need to write. **If you
find yourself inventing one, you have left the plan — stop and report.**
Concretely, stop rather than deciding, if you would have to:

- name anything not written down in the plan;
- choose a threshold, tolerance, or magic number not in section 3.12;
- change a signature the plan gives;
- modify Phase 1 code beyond the two changes the plan names explicitly
  (the `output_folder.py` rules parameter in P2-6, and the
  `_ProgressReporter` offset in P2-7);
- relax or delete a test assertion to make something pass.

Report what you needed, what you would have chosen, and why. A stopped chunk
costs one message. A chunk that invented an API costs a rewrite of
everything built on top of it. **Guessing is the expensive option here, not
the fast one.**

**Other ground rules:**

- **Phase 2 section 3 is locked**, exactly as Phase 1's section 3 is. If the
  plan looks wrong, say so and wait; do not "improve" it in passing.
- **Phase 2 section 2 lists facts already established by running code.**
  Trust them. Do not re-benchmark OpenCV, re-derive the ROMM curve, or
  re-litigate the rigid model.
- **Phase 1 code is not yours to rewrite.** `convert` keeps its exact
  meaning and its tests must keep passing untouched. A chunk that finds
  itself editing Phase 1's conversion tests has gone wrong — stop and
  report.
- Work only inside Chunk P2-N's stated scope. Note anything you find that
  belongs to another chunk in the PR body and leave it alone.
- Start from an up-to-date `main`, work on the branch named in the chunk
  entry, and open one pull request for it.
- `main` is protected: PR required, checks must pass, branch must be up to
  date. Never force-push, never push straight to `main`.

**Sample files:**

- Real Nikon Z f NEFs live at `tests/fixtures/nef/`. They are ignored by Git
  and must stay ignored — the repository is public and the files are large.
  Never `git add` them, never commit their contents, never copy them
  elsewhere in the repository.
- Phase 1's appendix A covers the original six frames. Phase 2's appendix C
  covers the stitching scans from user gate B. Use those recorded values
  rather than re-deriving them.
- Tests that need them skip clearly and say what was not tested.

**Testing rules that bite in Phase 2:**

- Synthetic images must be film-like — gradients, blobs, light grain — from
  the shared `synthetic_scene_support.py` generator. Never pure noise: it
  breaks Deflate *and* it tells you nothing about feature detection.
- Every registration test needs ground truth. Generate the fixture from a
  known transform and assert the recovered transform against it. "It
  produced a picture" is not a test.
- Test the rejections, not just the successes. A gate that has never
  refused anything has not been shown to work.
- Never assert an exact pixel hash of stitched output; it depends on the
  OpenCV build.
- Never allocate a full-size canvas in a test. Stub the canvas size for the
  size- and memory-guard tests.

**Definition of done for the chunk:**

1. Every "Do" item and every named file in the chunk entry exists.
2. Every test named in the chunk entry exists, with that name, and passes.
3. The full existing suite passes, not just the new tests:
   `cd cli && uv run ruff check . && uv run pytest`
   plus `cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'`
   for chunks touching Swift, plus the chunk's own verification commands.
4. The PR body contains **actual pasted command output**, not a description
   of it — the test summary line and every measurement the chunk asks you to
   record.
5. Never hide a failing build by piping it through `tail` or `head`.

**Stop and report — do not work around — if:**

- user gate B (sample scans) or user gate C (approved thresholds) has not
  been passed and your chunk needs it;
- section 3.12 still reads *unset* for a constant you need;
- an earlier chunk is not merged;
- an approval point in section 7.1 applies;
- a fact in section 2 turns out to be false on this machine;
- Chunk P2-1 finds that rawpy's gamma is not the ROMM curve — that
  invalidates section 3.3 and needs the user's decision;
- a Phase 1 test would have to change.

Report what is blocked, what you verified, and what you would do next.

**Finally:** end with a short summary of what changed, what the tests prove,
and anything you deliberately left out.
