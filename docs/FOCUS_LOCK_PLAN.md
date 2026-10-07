# Focus assist, part 2: a larger loupe, a lock, and check-shot history

`docs/FOCUS_ASSIST_PLAN.md` built the loupe, a peak-hold meter, and the check
shot, on the Z f. In use, three things fall short, worst on fine-grained film:

1. **The loupe is too small to judge grain.** It shows the 640×424 frame at
   one frame pixel per Retina pixel, about 2.5 inches across on a laptop.
2. **The meter never says "done".** It shows a percentage of the best score
   seen and turns green at a fixed 97%. It does not know how noisy the score
   is, so it cannot tell a real difference from jitter. One lucky frame can set
   a peak that the ring can never return to, and it cannot tell "not there yet"
   from "gone past it".
3. **Check shots are not compared with each other.** Each shows its 3×3 grid
   as a percentage of its own best region. That says whether the film is flat,
   not whether this shot is sharper than the last.

The operator focuses on the lens ring and confirms with a check shot (the
rail is used only for coarse focus, because gravity makes fine steps on it
unreliable). This plan improves both halves of that loop. It does not drive a
rail or change how focus is set.

It follows FOCUS_ASSIST_PLAN's conventions: numbered chunks, each
independently green; every constant in `FocusAssistTuning`; and no behaviour
coded against until it has been measured. Everything here is
body-independent, but only the Z f has in-app live view today. The D600 gets
it through `docs/D600_TETHER_PLAN.md` (D-0, D-2), and §7 lists what has to be
re-checked then.

---

## 0. What has to be learned first

### 0.1 Why measure

The lock (§2) rests on numbers nobody has: how much the score jitters while
the ring is still, how wide the peak is on fine grain, and how different a
still frame pair is from a turning one. FOCUS_ASSIST_PLAN's evidence is one
negative and two passes through focus, logged as half-second summaries. That
is enough for a peak-hold meter, not for a lock that claims "nothing left to
gain".

### 0.2 Recording sweeps

`TetherProbe.swift` gains `--lv-record SECONDS`: hold live view and write
every distinct `GetLiveViewImage` payload to `--out` as `frame-NNNNN.bin`,
with `frames.csv` listing index, monotonic time, and the header's area
fields. At 30 fps and ~60 KB a frame, a minute is about 100 MB.

Each recording follows a script, read out by the probe so the passes are
timestamped in the log:

| Pass | Operator does | What it measures |
|---|---|---|
| A | Hold the ring still near focus, 5 s | Score jitter at rest; the frame-to-frame difference at rest |
| B | Rack slowly through focus, one way, and stop well past it | Peak width and height at a slow turn |
| C | Rack back at normal speed | The same at normal speed; the difference while turning |
| D | Return to the peak by eye and hold, 5 s | Jitter at the peak; how close by-eye gets |
| E | Shift the ring by the smallest amount the hand can manage, hold, and repeat four times | Whether the smallest deliberate step is visible above the jitter |

Record at the focusing zoom (512 px on the Z f) on:

- a **fine-grained** negative (the case that fails today);
- a **coarse-grained** negative, for range;
- a **dense** and a **thin** area of the same fine-grained negative.

### 0.3 Check-shot repeatability

In the app as it is, take three check shots without touching anything, then
one after the smallest ring shift from pass E. This gives the spread of the
raw regional focus ratio between identical shots, and whether one hand-sized
step shows above it. §3 depends on both.

### 0.4 Fixtures

The full recordings live in `tests/fixtures/liveview/sweeps/`, ignored by
Git like the sample NEFs (add it to `.gitignore`). Two derivatives are
committed:

- **`sweep-<name>.csv`**: per frame, time, the current score, and the
  centre's mean absolute difference from the previous frame. These drive the
  lock tests (§6), which need no JPEGs.
- **A handful of frames** per recording (rest, peak, soft) as `.bin`, beside
  the existing `lv-watch-*` fixtures.

### 0.5 What gets decided from it

| Constant | Decided from |
|---|---|
| `STILL_DIFF` (frame difference below which the ring counts as still) | Passes A and D against B and C |
| `STILL_FRAMES` (how long still before a settled reading) | The same, plus how long a hand takes to come off the ring |
| `LOCK_Z` (how many jitter widths below the peak still counts as at it) | Pass D's jitter, and how far a peak taken while turning sits above a still reading of the same point |
| `BRACKET_DROP` (how far past the peak the score must fall to count as passed) | Pass B/C peak shapes against pass A jitter |
| `RESTART_SKIP_FRAMES` (frames ignored after live view restarts) | The frames after a check shot's restart |
| `CHECK_SHOT_NOISE` (raw ratio spread between identical check shots) | §0.3 |
| Whether `FocusScore` changes | §2.1 |

The findings go into a table in §0 of this plan, in the style of
TETHER_PLAN §0.9, and every later chunk is revised against them before it
starts.

---

## 1. The loupe

### 1.1 Size

A larger loupe adds no detail, as FOCUS_ASSIST_PLAN §3.3 says, but it makes
the detail visible. A Retina pixel on a laptop is about 0.1 mm, which is at
the limit of the eye's acuity at normal viewing distance. Grain drawn one
pixel wide is there but hard to see. Drawn two or three pixels wide, it is
easy.

- **Three sizes**, each a whole number of screen pixels per frame pixel, so
  nearest-neighbour drawing never resamples: **S** (1 px, as today), **M**
  (2 px), **L** (3 px). On a Retina laptop that is 320, 640 and 960 points
  across.
- **The scale comes from the display**, through `@Environment(\.displayScale)`,
  not a fixed `scale: 2`. Today a non-Retina external display draws the
  640-pixel frame into 320 pixels with `.interpolation(.none)`, which drops
  every other pixel. That removes exactly the grain being focused on.
- **The loupe gets its own full-width row** at M and L, with the zoom, area
  outline and meter beneath it. When the Form is narrower than the chosen
  size, the loupe drops to the largest whole-pixel size that fits, never a
  fractional one.
- **L** cycles the size. The size is remembered in `UserDefaults`, a sticky
  user setting.

### 1.2 Grain view

A display-only toggle, **G**, remembered in `UserDefaults`:

- grey, inverted (the negative shown as a positive), and levels-stretched
  between the 1st and 99th percentiles of the centre's luminance;
- **the levels are taken once per view and held**, from the first frames
  after an area change or an R. Re-stretching every frame would pump the
  brightness while the ring turns, and a change in brightness reads to the
  eye as a change in sharpness;
- the score always uses the untouched frame.

The grey buffer `FocusScore` already draws is the input, so this adds one
lookup-table pass per frame, off the main actor.

---

## 2. The lock

### 2.1 The score

`FocusScore` stays the mean squared Laplacian over the centre half unless the
sweeps show something clearly better. The test is **the peak's height above
the out-of-focus floor, divided by the jitter at rest**, from passes A–D. That
ratio decides how sure the lock can be. A score's absolute scale does not
matter.

Candidates, each cheap at 640×424:

| Candidate | Why it might win on fine grain |
|---|---|
| Laplacian (today) | Baseline |
| Laplacian, median over a 4×4 tile grid | A dust speck, hair or scratch cannot dominate. Dust sits on the film surface, not in the emulsion, so it can pull the peak |
| Difference of Gaussians, one band below the frame's Nyquist | Less weight on JPEG block edges and in-camera sharpening, which are highest-frequency and do not follow focus |
| High/mid band ratio (as `capture_analysis.py` uses on the raw) | Does not jump with density, so a moved area reads the same scale |

Change `FocusScore` only if a candidate beats the Laplacian's ratio by at
least 1.5× on the fine-grained recordings and puts its peak at the same
frame. Otherwise keep it, and record the numbers in the rejected
alternatives.

### 2.2 Stillness

The ring is **still** when the centre's mean absolute difference from the
previous distinct frame stays below `STILL_DIFF` for `STILL_FRAMES` frames.
Turning a macro lens's focus ring changes the magnification slightly, so the
image breathes and the difference is large while turning. Pass A against
pass C sets the threshold.

After live view restarts (a check shot, a Retry), the first
`RESTART_SKIP_FRAMES` frames are not scored and do not count towards
stillness. Live view may ramp exposure as it comes back.

### 2.3 Jitter

While still, the model keeps the standard deviation of single-frame scores
over the still window. Nothing measures it while turning, so the latest still
value is used, and until the first still window of a view, `LOCK_Z` applies
to a default jitter taken from the sweeps as a fraction of the score.

### 2.4 The settled reading

Once still for `STILL_FRAMES`, the model takes the **mean of the single-frame
scores** over the still window. Its jitter is the single-frame jitter divided
by √N.

It is a mean of scores, not the score of a mean frame. Averaging frames
removes noise, and noise contributes Laplacian energy, so an averaged frame
scores lower than any single frame and could not be compared with a peak
taken from single frames.

A settled reading above the held peak replaces it: it is the better estimate
of that point.

### 2.5 States

`FocusLock` is a pure value type: it takes `(time, score, frameDifference)`
for each frame and returns a state. `FocusAssistModel` feeds it and shows
the result. No camera, no clock of its own.

| State | When | Panel says |
|---|---|---|
| `searching` | Fewer than `STILL_FRAMES` scores since the view changed, or no rise seen yet | "Turn slowly through focus" |
| `rising` | The median score is climbing and below the peak band | "Getting sharper" |
| `pastPeak` | A peak was seen after a rise, and the score has since fallen below the peak by more than `BRACKET_DROP` | "Past the peak — turn back" |
| `nearPeak` | Within the peak band (peak − `LOCK_Z`·jitter) but not still, or not yet past the peak on both sides | "Near the peak — hold still" |
| `locked` | Still; the settled reading is within the peak band; and the peak was approached from below and overshot by more than `BRACKET_DROP` | **LOCKED** |

`locked` is left on any frame that is not still, or a settled reading that
falls out of the band. An area change or R returns to `searching`, as it
resets the peak today.

The approach-and-overshoot condition is what makes the lock trustworthy: it
says the held peak is a summit with lower ground on both sides, not the top
of a climb that has not finished.

### 2.6 What the panel shows

- **The state** as a large label, in text as well as colour. LOCKED is a
  filled green badge.
- **A trace**: the last `TRACE_SECONDS` (6 s) of scores as a line, scaled to
  the held peak, with the peak as a horizontal line and the peak band shaded.
  It shows the hump, which side of it the ring is on, and how big the jitter
  is.
- **The meter bar** as today, with the fixed 97% green replaced by the peak
  band.
- **A chime** on entering `locked`, once, and not again until the lock has
  been lost and regained. It is a single tone from `CaptureCues`, distinct
  from the countdown tones. On by default, with a toggle in the panel
  remembered in `UserDefaults`.

FOCUS_ASSIST_PLAN §7 rejected an "at peak" tone because a tone lagging a fast
turn would point past the peak. The lock chime is different: it plays only
once the ring is still, so it cannot lag a turn, and it tells an operator
whose eyes are on the lens that they can stop. That rejection stays for a
tone that tracks the score.

---

## 3. Check-shot history

### 3.1 What is kept

Each check shot appends an entry to a list in `FocusAssistModel`:

- its nine regional focus ratios, as today;
- **the loupe cell**: the grid cell holding the live view area's centre at the
  moment of release, from `areaCentre` and the sensor size;
- the live view state at release: the area key, the latest settled reading if
  still, and the lock state.

No CLI change: the ratios already arrive in `frame_analyzed`'s
`focus_regions`. Passing earlier check shots as `capture analyze` baselines
would re-decode each of their raws on every shot, for a number the app can
compute from what it already has.

### 3.2 What is shown

- **A row per shot**, newest first: shot number, the loupe cell's ratio as a
  percentage of the best shot's ratio for that cell, and the lock state at
  release. The best shot is marked.
- **Whether the difference is real**: when two shots differ by less than
  `CHECK_SHOT_NOISE` (§0.3), the row says "same as #n", not a percentage.
- **The best shot's live view reading** as a tick on the meter ("best check
  shot"), shown only while the area key is unchanged since that shot. It is
  a target to turn back to after a check shot shows an earlier setting was
  better.
- The 3×3 grid and the flatness diagnosis stay as they are, for the newest
  shot.

### 3.3 When it clears

Comparing check shots only means something while the film and the area under
test have not moved. Film movement cannot be detected, so the history clears:

- on **Clear** in the panel;
- when a negative starts (the film is about to move);
- when the session closes.

It survives closing and reopening focus assist, so an operator can close it,
adjust the rail, reopen and compare.

### 3.4 The lock across a check shot

A check shot ends and restarts live view (FOCUS_ASSIST_PLAN §4). The ring does
not move, so the peak, jitter and lock state carry across it. The first
frames after the restart are skipped (§2.2). If the area key differs after
the restart, the peak resets as usual.

---

## 4. Files

| File | Change |
|---|---|
| `mac/Tools/TetherProbe.swift` | `--lv-record` (§0.2) |
| `Capture/FocusAssistTuning.swift` | Every new constant |
| `Model/FocusScore.swift` | Exposes the grey buffer; the chosen score (§2.1); the centre difference |
| `Model/FocusLock.swift` | New: the state machine (§2.5), pure |
| `Model/FocusAssistModel.swift` | Feeds `FocusLock`; the trace buffer; grain-view levels; check-shot history |
| `Views/FocusAssistPanel.swift` | Loupe sizes and layout; grain view; state label, trace, band, chime toggle; history rows |
| `Views/CaptureStageView.swift` | The L and G keys |
| `Capture/CaptureCues.swift` | The lock tone |
| `Model/CaptureSessionModel.swift` | Clears check-shot history when a negative starts |
| `.gitignore` | `tests/fixtures/liveview/sweeps/` |

Keys: L and G must be checked against `AppKeyboard.swift` and every view's
`keyboardShortcut`, as F, R and C were. The panel's hint line becomes
"F or Esc closes · R resets peak · C check shot · L loupe size · G grain view".

---

## 5. Settings

All per user, in `UserDefaults`, defaulted on first use:

| Setting | Default |
|---|---|
| Loupe size | M |
| Grain view | Off |
| Lock chime | On |

---

## 6. Tests

No test touches a camera.

- **Loupe**: the chosen size maps to whole screen pixels per frame pixel at
  display scales 1, 2 and 3; a width that does not fit L steps down to M,
  not to a fractional scale.
- **Grain view**: the levels are taken from the first frames of a view and
  held while later frames differ; R or an area change retakes them; the score
  is identical with grain view on and off.
- **`FocusLock`**, driven by the committed `sweep-*.csv` series and by
  synthetic ones:
  - a slow rack through focus and back, ending still at the peak, reaches
    `locked`;
  - the same series stopping still *before* the peak (no overshoot) never
    reaches `locked`, only `nearPeak`;
  - one outlier frame does not set an unreachable peak;
  - any frame above `STILL_DIFF` leaves `locked`;
  - a settled reading above the peak replaces it;
  - an area change returns to `searching`.
- **Chime**: plays once on entering `locked`, not on staying there, and
  again only after the lock is lost and regained. The cue is injected, so
  the test hears nothing.
- **Check-shot history**, with a stub runner emitting `frame_analyzed`:
  - percentages are relative to the best shot's loupe cell;
  - two shots within `CHECK_SHOT_NOISE` read "same as";
  - the loupe cell follows `areaCentre`;
  - the target tick shows only while the area key is unchanged;
  - starting a negative clears the history, and closing focus assist does
    not;
  - the lock state survives a check shot's live view restart.
- **Score choice (slow tier)**: a `SCANNY_BOY_SLOW_TESTS` test that reads the
  ignored sweep recordings and asserts the chosen score's peak-over-jitter
  ratio and peak frame, so a later change to `FocusScore` is checked against
  real frames.

---

## 7. The D600

Nothing here names a body, but the constants come from the Z f. When the D600
has in-app live view (D600_TETHER_PLAN D-0 and D-2):

- record the §0.2 sweeps on it and re-check `STILL_DIFF`, `STILL_FRAMES` and
  `RESTART_SKIP_FRAMES`. The jitter is measured live, so `LOCK_Z` and
  `BRACKET_DROP` should transfer, but check them;
- the loupe cell needs the D600's sensor size. `FocusAssistTuning.sensorWidth`
  and `sensorHeight` become fields of the body profile (D600_TETHER_PLAN
  §2.1), and so does the 512 px focusing-zoom hint;
- D-0 should record whether the D600's top live view zoom steps are true
  sensor pixels (from the header) and whether `LiveViewImageZoomRatio`
  (`0xD1A3`) and `ChangeAfArea` (`0x9205`) work. Neither changes this plan,
  but the second would allow click-to-zoom later.

---

## 8. Chunks

### FL-0 — the sweeps (measurement, no product code)

`--lv-record`, the §0.2 recordings on the Z f, the §0.3 check shots, the
committed CSVs and frames, and the findings table in §0. Revise §2 and §3
against it.

### FL-1 — the loupe

§1: sizes, the display-scale fix, the full-width row, grain view, the L and G
keys, and their tests. Needs nothing from FL-0 and can land first.

### FL-2 — the score

§2.1: evaluate the candidates on the recordings, keep or change `FocusScore`,
and add the slow-tier test. Requires FL-0.

### FL-3 — the lock

§2.2–2.6: stillness, jitter, the settled reading, `FocusLock`, the state
label, trace, band and chime. Requires FL-2.

Done when, on the Z f with a fine-grained negative, focusing to LOCKED and
focusing by eye are each followed by a check shot, and the locked setting's
loupe-cell ratio is at least as good as by-eye, by more than
`CHECK_SHOT_NOISE` or within it. Repeat three times. This also answers
FOCUS_ASSIST_PLAN §9's open question about the 84% by-eye setting.

### FL-4 — check-shot history

§3. Requires FL-0 for `CHECK_SHOT_NOISE`, and FL-3 for the lock state it
records. Without FL-3 it can land recording only the area key and the latest
score.

### FL-5 — documents

FOCUS_ASSIST_PLAN gains a pointer to this plan where §3.3 and §7 are
superseded; README and ARCHITECTURE.md describe the lock and the new keys;
DECISIONS.md records the lock chime and why it does not contradict the
rejected tracking tone.

**Ordering:** FL-0 and FL-1 in either order. FL-2 after FL-0. FL-3 after
FL-2. FL-4 after FL-3, or after FL-0 in its reduced form. FL-5 last.

---

## 9. Rejected alternatives

- **Scoring an average of the still frames.** Lower noise, but it scores
  below any single frame, so it cannot be compared with a peak taken while
  turning (§2.4).
- **A fixed percentage band.** What 97% means depends on the film, the area
  and the noise. A band measured in jitter widths means the same thing on
  every negative.
- **A tone that tracks the score.** Lags a fast turn (FOCUS_ASSIST_PLAN §7).
  The lock chime plays only when still.
- **Detecting film movement to clear the history.** Movement and focusing
  both change the frame. Clearing on a negative start and on Clear covers the
  real cases.
- **A true-pixel crop of the check shot's raw in the panel.** It would show
  grain exactly as it will scan, and is worth doing, but it needs the CLI to
  render a crop: a new event or command, so a contract change, to be decided
  with the user on its own.
- **Fractional loupe scales or a resizable loupe.** Any non-integer scale
  resamples the frame and softens exactly what is being judged.
- **Driving a motorised rail for automatic bracketing.** A purchase and a
  protocol question, and only worth it if the ring proves too coarse.

---

## 10. Risks and open questions

- **The jitter may be too large on fine grain** for any lock to be confident,
  even with the best score. Then FL-0 shows it, and the plan falls back to the
  trace and check-shot history, which are useful either way.
- **Stillness may be hard to separate from a very slow turn.** A slow enough
  turn looks still. A settled reading taken mid-turn would be slightly low,
  and the lock would then not trigger, which is safe. FL-0's pass E says how
  slow is too slow.
- **The live view JPEG's peak may not be the raw's peak** (in-camera
  sharpening, FOCUS_ASSIST_PLAN §9). FL-3's done criterion measures it on
  the raw.
- **The smallest hand-sized ring step may be below the check shot's noise.**
  Then check-shot history can say "same as", but not choose between two hand
  positions. §0.3 says so before FL-4 is built.
