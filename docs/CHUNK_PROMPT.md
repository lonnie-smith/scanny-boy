# Chunk prompt header

Paste this in front of every implementation request, replacing `N` with the
chunk number. Run one chunk per session, in order.

---

You are implementing **Chunk N** of the Scanny Boy Phase 1 plan.

**Read first, in this order, before writing any code:**

1. `docs/IMPLEMENTATION_PLAN.md` sections 1–5 — goal, vocabulary, verified
   facts, locked decisions, and the command-line contract.
2. The Chunk N entry in section 6.
3. Section 7 (test rules), section 8 (human approval points), and appendix A
   (sample NEF facts).

**Ground rules:**

- Section 3 is locked. Do not change a decision there — parameters, tag
  mappings, error codes, exit statuses, licence text, folder rules — without
  stopping and asking me. If the plan looks wrong, say so and wait; do not
  "improve" it in passing.
- Section 2 lists facts already verified by running code. Trust them. Do not
  re-litigate library choices or re-benchmark what is already measured.
- Work only inside Chunk N's stated scope. If you find a problem belonging to
  another chunk, note it in the PR body and leave it alone.
- Start from an up-to-date `main`, work on the branch named in the chunk
  (`chunk-NN-...`), and open one pull request for it.
- `main` is protected: PR required, required checks must pass, branch must be
  up to date. Never force-push and never push straight to `main`.

**Sample files:**

- Six real Nikon Z f NEFs are at `tests/fixtures/nef/` — two negatives of three
  frames. They are ignored by Git and must stay ignored; the repository is
  public and they are about 190 MB. Never `git add` them, never commit their
  contents, never copy them into the repository elsewhere.
- Appendix A has their hashes, dimensions, timestamps, and settings. Use those
  values rather than re-deriving them.
- Tests that need them skip clearly and say what was not tested.

**Definition of done for the chunk:**

1. Every "Do" item in the chunk entry is implemented.
2. Every listed test exists and passes.
3. The full existing suite passes, not just the new tests:
   `cd cli && uv run ruff check . && uv run pytest`
   plus the chunk's own verification commands.
4. The PR body contains **actual pasted command output**, not a description of
   it. Include the test summary line and any measurement the chunk asks you to
   record.
5. Never hide a failing build by piping it through `tail` or `head`.

**Stop and report — do not work around — if:**

- a required sample file is missing;
- an earlier chunk is not merged;
- a human approval point in section 8 applies;
- a fact in section 2 turns out to be false on this machine;
- a required EXIF tag is absent from the real files (Chunk 2).

Report what is blocked, what you verified, and what you would do next.

**Finally:** end your work with a short summary of what changed, what the tests
prove, and anything you deliberately left out.
