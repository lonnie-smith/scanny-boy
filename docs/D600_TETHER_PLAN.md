# Tethered capture on the Nikon D600

`docs/TETHER_PLAN.md` built tethered capture around one body: the Z f on
firmware V2.00. Every behaviour the camera layer relies on was measured on it
(§0.1 there), and §11 names the gap: *"a body with the operations and
different behaviour is not detected."* The D600 is that body. It is a 2012
DSLR from an older generation of Nikon's PTP firmware, so it probably
advertises most of the same vendor operations while behaving differently
underneath them.

On the D600 the user shoots **from live view**: it makes positioning the
negative and focusing practical on a copy stand, where the viewfinder is hard
to look through. That is decided, whatever it does or does not do for shutter
wear. The Z f should use its electronic shutter; that is a later chunk (D-7),
but the spike can record what it needs.

This plan follows TETHER_PLAN's conventions: numbered chunks, each
independently green; every constant in one module; and **no behaviour is
coded against until the D600 has been measured**. The "Expected" column below
is what libgphoto2's Nikon driver and older-body knowledge suggest. It is a
list of questions for the probe, not a set of facts.

**Givens from the user.** The negative does not move during an exposure.
Flicker is not expected to matter (LED light source). Scans are at base ISO.
The user says which camera is in use when tethering (§2.1). The user builds
new flat-field and calibration profiles for the D600 (§3).

**What this does not change.** The Z f path (until D-7), the sequence and its
timing rules, the stitch queue, the roll lock, deferred refresh, the per-frame
analysis, and the event protocol (still 22).

---

## 0. What has to be learned first

### 0.1 Live view and the shutter on the D600

The D600 has no electronic shutter for stills, and the plan takes that as
given; the spike does not test for one. Live view is used for what it gives
on this body:

- **Positioning and focusing.** The negative is framed and focused on the
  rear screen (or in the app, §0.3), not through the viewfinder.
- **The mirror** stays up for the whole session, so mirror slap never reaches
  an exposure. No exposure delay mode is needed.

It does not spare the shutter. In live view the D600 closes its open shutter,
opens it for the exposure, closes it, and reopens it for live view, so every
frame is still a mechanical actuation. For scale: 36 negatives shot 3×2 is
216 frames per roll, and Nikon's often-quoted design figure for this class of
body is about 150,000 actuations — roughly 700 rolls. That is a rough figure,
not a measurement. The Z f's electronic shutter (D-7) is where wear goes to
zero.

### 0.2 Other concerns with live view

These are the ones worth knowing about before building. Each is a D-0
question.

1. **Power.** A D600 does not charge or run from USB, and live view drains
   the battery quickly. A multi-hour session probably needs the AC adapter
   (EH-5b with the EP-5B coupler). If the battery dies mid-sequence, the
   camera drops off USB, which counts as a lost connection (§2.5 there).
2. **Live view ends on its own.** The body has a live view auto-off timer and
   a temperature shutdown. Either can end live view in the middle of a
   session. The sequence has to notice, stop cleanly, and restart live view,
   which flips the mirror down and up again between negatives. D-0 records
   the longest timer the body allows and whether an event announces the end.
3. **Sensor heat.** Hours of live view warm the sensor, which raises noise
   and can shift black levels. At base ISO and short exposures this is
   probably minor. D-0 compares a dark frame from a cold start with one after
   45 minutes of live view.
4. **Body controls under PTP.** Some Nikon bodies lock their buttons while a
   computer controls live view. If the zoom and focus-magnify buttons stop
   working when the app starts live view, focusing on the rear screen is lost
   and in-app live view (§0.3) stops being optional. D-0 tests live view
   started both from the body's LV button and over PTP.
5. **Release from live view over PTP.** Whether `InitiateCaptureRecInSdram`
   (`0x90C0`) is accepted in live view, whether live view resumes afterwards
   by itself, and how `DeviceReady` behaves around the close–open–close–open
   shutter cycle. The move cue and interval clock depend on the last of
   these.
6. **Exposure metering in live view** does not matter in Manual, but the
   D600's live view display may not preview the set exposure. The histogram
   and clipping check come from the downloaded frame (§6 there), not from the
   screen.
7. **USB contention.** Streaming live view frames to the app while a 25 MB
   NEF downloads over USB 2 slows both. If in-app live view is built, it
   pauses during downloads.
8. **Rolling readout and flicker** only matter with an electronic shutter,
   so not on the D600. For the Z f in D-7, see §0.4.
9. **The D600's shutter shedding** (the reason for Nikon's shutter service
   programme) put oil and dust spots on sensors. Spots on the sensor are in
   every frame, and a flat field built on another day will not remove them.
   If this body never had the shutter replaced, check the sensor before
   building its flat field.

### 0.3 Live view in the app

Focusing on the camera's rear screen needs no code. Streaming live view into
the Capture tab (`GetLiveViewImage`, with a magnifier) is a larger feature:
an image loop over PTP, a view, pausing during downloads, and focus-zoom
control. **It is not in this plan's chunks by default.** D-0 checks that the
stream works at all (Q21) so the option stays open. If §0.2 item 4 finds that
the body's buttons lock under PTP, it moves into D-3.

### 0.4 Questions for the probe

| # | Question | Z f (measured) | D600 (expected, unverified) | Why it matters |
|---|---|---|---|---|
| Q1 | Does `GetDeviceInfo` list `0x90C0`, `0x90C7`, `0x90C8`, `0x1008`, `0x1009`, `0x1015`? | Yes | Probably yes | Otherwise the body is `unsupported` |
| Q2 | Which **handle** does a buffer frame get? | `0x0B000001`, `0x0B000002`, … restarting when empty | Likely the fixed `0xFFFF0001` older bodies use (`PTP.sdramHandle`, unused today) | The buffer scan walks `0x0B000001…0x0B000010` only, so a D600 frame would never be found |
| Q3 | Does `ObjectAddedInSdram` (`0xC101`) arrive, and when? | Queued, unreliable | Queued; possibly more reliable | Whether the scan is the signal or a fallback |
| Q4 | Are events **pushed** through `ptpEventHandler` at all? | Yes, for card shots | Possibly queue-only | Card mode depends on pushed `ObjectAdded` |
| Q5 | Is `InitiateCaptureRecInMedia` (`0x9207`) supported? | Yes | Uncertain — it may postdate this body | Card mode's release; fallback is `InitiateCapture` (`0x100E`) |
| Q6 | Are `StartLiveView` (`0x9201`), `EndLiveView` (`0x9202`) and `GetLiveViewImage` (`0x9203`) listed, and does a live view status property exist (libgphoto2 names `0xD1A2`)? | n/a | Probably | Starting, stopping and observing live view |
| Q7 | After `StartLiveView`, how long until `DeviceReady` answers OK? | n/a | Some hundreds of ms (mirror up) | When the first release may fire |
| Q8 | Is `0x90C0` accepted **in live view**? Does live view resume after the frame without a new `StartLiveView`? | n/a | Probably accepted; resume unknown | The whole live view sequence |
| Q9 | In live view, how long is `DeviceReady` busy around a 1/2 s exposure, and does OK mark the end of the exposure or only the end of the shutter reopening? | 0.55 s (no LV) | Longer than the exposure | The move cue and interval clock |
| Q10 | After a release, how long is the rear screen blank before live view shows the negative again? | n/a | About a second | When the operator can see to position the next cell; may argue for a longer default interval |
| Q11 | Does the release shut down live view's display on the rear screen for review, and can image review be turned off? | n/a | Review can be disabled | A review image hides live view while positioning |
| Q12 | With live view started by **PTP**, do the body's zoom and focus buttons still work? And with live view started from the **LV button**, does a PTP release work? | n/a | Unknown | §0.2 item 4 |
| Q13 | Longest live view auto-off setting; does an event announce live view ending (timeout, temperature, button)? | n/a | Unknown | §0.2 item 2 |
| Q14 | Battery percentage (`BatteryLevel`, `0x5001`) over 30 min of live view with releases | n/a | Fast drain | §0.2 item 1 |
| Q15 | Dark frame at base ISO, cold vs after 45 min of live view: black level and noise per channel | n/a | Slight rise | §0.2 item 3 |
| Q16 | What does focus mode (`0x500A`) read with the lens switched to M, in and out of live view? | `1` (Manual) | Unknown | `runEnabled` requires `isManualFocus` |
| Q17 | Does a download clear the buffer (`GetObjectInfo` then fails)? | Yes | Probably | `confirmBufferCleared` |
| Q18 | Are undownloaded buffer frames kept across sessions, across live view restarts, across power-off? Does `DelImageSDRAM` (`0x90C3`) work? | Across sessions; rest untested | Unknown | Leftovers (§2.5 there) |
| Q19 | Release → file on disk in live view; download rate; NEF size and bit depth | 1.7 s; ≈45 MB/s; 25 MB | USB 2 native; similar size | Interval defaults |
| Q20 | Storage IDs of the two SD slots, and `ObjectAdded` under slot 2's role | `0x09`/`0x0A`, backup → two events | Unknown | Card mode |
| Q21 | Does `GetLiveViewImage` return a JPEG, at what size and rate, and does streaming it slow a concurrent download? | n/a | Small JPEG, a few fps | §0.3, §0.2 item 7 |
| Q22 | Does `0x500D` use the same shutter encoding (s × 10 000, `0xFFFFFFFF` bulb)? | Yes | Probably | `exposureTimeout` |
| Q23 | First-connection delay with a full card | 24 s behind a 73 s catalog | Unknown | The `preparing` message |

The Z f electronic shutter questions are in D-7 and can be run during the
same spike.

---

## 1. Latent issues found while reading the camera layer

These are not D600-specific, but the D600 is likely to trip them. Each needs
confirming, and the fixes land in D-1.

1. **The "new handle" rule breaks when a handle is reused.**
   `CaptureSessionModel.runSequence` keeps every downloaded handle in
   `handlesBefore` for the rest of the negative, and
   `TetherCamera.waitForFrame` looks only for handles *not* in that set. On a
   fixed-handle body (Q2) the second cell's frame has the same handle as the
   first, so it is never "new" and the cell times out. The Z f restarts
   numbering at `0x0B000001` once the buffer is empty (§0.1 there), so it
   looks exposed to the same failure; worth checking whether T-4's 36-release
   acceptance run actually passed this code. The fix is to compare against a
   scan taken immediately before *each* release. `TetherCamera.release()`
   already records `handlesBeforeRelease` and never uses it.
2. **The move cue plays at release, not at the end of the exposure.**
   `runSequence` calls `playMoveCue()` before `waitForExposureEnd()`, which is
   the opposite of TETHER_PLAN §0.5/§3.3.
3. **Queued `ObjectAdded` events are discarded during a card capture.**
   `drainCheckEvent` adds every `0x4002`/`0xC101` parameter to
   `ignoredHandles`, and `waitForFrame` calls it while polling. That is right
   for the pre-release drain of history and wrong afterwards: on a body that
   queues rather than pushes (Q4), the new frame's own event is ignored.
4. **`DeviceInfo.model` is decoded and dropped** (`_ = info` in
   `connectAfterOpen`). Nothing records which body is connected.

---

## 2. Design

### 2.1 The user picks the camera; a profile describes it

The Capture tab's setup gains a **Camera** field: Z f or D600, remembered in
`UserDefaults`. The choice selects a `TetherBodyProfile` from
`Capture/TetherBodyProfile.swift`, the only file that names a camera model.

```swift
struct TetherBodyProfile: Sendable, Equatable, Identifiable {
    let id: String                       // "z-f", "d600"
    let displayName: String              // "Nikon D600"
    let deviceInfoModel: String          // DeviceInfo.model, for the cross-check
    let bufferHandles: BufferHandleScheme
    let cardRelease: (opcode: UInt16, params: [UInt32])
    let eventSource: EventSource         // .pushed, .queued, .both
    let requiredOperations: Set<UInt16>
    let liveView: LiveViewPolicy         // .none (Z f), .required (D600)
    let exposureOverhead: Duration       // shutter cycle added to exposureTimeout
}

enum BufferHandleScheme { case sequential(first: UInt32, last: UInt32); case fixed(UInt32) }
```

- At connect, `DeviceInfo.model` is compared with the chosen profile. A
  mismatch is a setup error ("The connected camera is a Z f, but Capture is
  set to D600"), and Space stays disabled. Asking the user makes the choice
  explicit; the check stops a wrong choice from being driven with the wrong
  behaviour.
- `TetherCamera` stores the profile. The buffer scan, card release, event
  handling, live view, and exposure timeout read from it instead of
  `TetherTiming`'s Z f constants. `bufferScanFirst/Last` move into the Z f
  profile; genuinely shared timings stay in `TetherTiming`.
- `CameraControlling` gains `var body: TetherBodyProfile? { get set }`.
- `PTP.requiredOperations` becomes what every profile needs; the rest live on
  the profile.

### 2.2 Finding a frame on a fixed-handle body

With `.fixed(h)`:

- **Before a release**, `GetObjectInfo(h)` must fail. If it answers, it is a
  leftover (§2.5 there).
- **After the release**, poll `CheckEvent` and `GetObjectInfo(h)`; the first
  successful answer is the frame.
- **After the download**, `GetObjectInfo(h)` must fail again (Q17).

This is the same rule as the Z f path once §1.1 is fixed — "a handle that
answers now and did not answer immediately before this release" — so one
implementation serves both schemes.

### 2.3 Live view in the sequence (D600)

Subject to D-0's answers:

- **Session start**: when the session opens, `StartLiveView`, then wait for
  `DeviceReady` (Q7). The Capture tab shows "Live view on — focus on the
  camera's screen."
- **Each release** fires from live view with `0x90C0`. The end of the
  exposure comes from whatever D-0 finds `DeviceReady` actually marks (Q9).
  If it marks only the shutter reopening, the move cue is a little late,
  which is safe.
- **Live view dropped** (event or status property, Q13) between cells: the
  sequence pauses after the in-flight cell, like an exposure change (§2.4
  there), and resuming restarts live view first. A drop *during* a cell fails
  that cell, and it is retaken.
- **Session end**: `EndLiveView`.
- **Battery**: `BatteryLevel` is read at connect and between negatives; below
  a threshold the tab warns. The threshold is picked in D-3 from Q14's drain
  rate.
- **Camera settings the user must set on the body** (live view auto-off at
  its longest, AF/M switch on M) are listed in the setup checks where they
  can be read over PTP, and in a one-line reminder where they cannot.

### 2.4 Roll and camera consistency

A roll's `camera_color` is seeded by its first run, and the CLI already warns
when a base frame's camera differs from the roll's
(`stitch_pipeline.py`, `cli.py:_camera_model_from_source`). The Capture tab
compares the chosen camera with the roll's `camera_color.camera_model` (from
`roll info`) and warns before Space. It is advisory, like the CLI's; the CLI
stays the authority.

---

## 3. The CLI side

The user builds D600 flat-field and calibration profiles, so the plan does not
cover making them. What it covers is that the right profile is used and that
the pipeline handles the body's files:

- **Profile selection.** The Capture tab's flat-field picker defaults to the
  last profile used *with the chosen camera*, not the last one used at all.
  A first read of `flatfield.py` found no check that a profile's camera
  matches the frames it corrects. D-4 confirms what happens when a Z f
  profile meets a D600 frame. A shape error is acceptable. A silently
  resampled or misregistered correction is not; that would need a CLI guard,
  a contract change, decided with the user before it is built.
- **Decoding**: LibRaw's D600 support, 6016×4016, the bit depth and
  compression the body is set to (and whether 12-bit or lossy NEFs are
  refused).
- **Base-frame gates and clipping**: `FILM_BASE_CLIPPED` and
  `SCAN_CLIP_LEVEL` against the D600's white level.
- **Stitching**: one 3×2 negative through `prepare` → `capture check` →
  `stitch`, which exercises the solve's constants on a second sensor.
- **Capture analysis**: `capture analyze` on D600 frames. The provisional
  thresholds stay as they are, but a D600 frame must not error.

---

## 4. Chunks

### D-0 — the spike (measurement, no product code)

Extend `mac/Tools/TetherProbe.swift`:

- add `0xFFFF0001` to the buffer scan and log which handle answers;
- log whether each event arrived pushed or queued;
- `--live-view start|stop|status` and `--release` from live view, polling
  `DeviceReady` every 50 ms from release and logging each busy/OK transition
  with a timestamp;
- `--live-view-image N`: fetch N `GetLiveViewImage` frames, log size and rate,
  and fetch them during a download to see the contention;
- `--soak MINUTES`: hold live view, release every 30 s, log battery level,
  live view status and every event, and stop when live view ends;
- try `0x90C3` on a buffer frame.

Run it for Q1–Q23, and by hand:

- time the screen blackout after each release, with image review on and
  off (Q10, Q11);
- body buttons under PTP live view, and PTP release under button live view
  (Q12);
- dark frames cold and after the soak (Q15).

Save the raw DeviceInfo, a CheckEvent payload, the live view status reply,
and ObjectInfo for a buffer and a card frame as test fixtures. Write the
findings as a table in the style of TETHER_PLAN §0.1, and **revise every later
chunk against them before it starts**. If the Z f electronic shutter
questions (D-7) are cheap to run in the same sitting, run them too.

### D-1 — body profiles, camera choice, and the latent fixes

`TetherBodyProfile` with the Z f profile only, the Camera setup field and the
model cross-check, the §1 fixes, and `CameraControlling.body`.
`FakeTetherCamera` gains a handle scheme, an event source and a live view
state. Tests, against the fake and fixtures:

- a sequential-handle body whose numbering restarts completes a 3×2
  negative (fails today if §1.1 is right);
- the move cue fires after `waitForExposureEnd`, not after `release`;
- a queued `ObjectAdded` arriving after the release is used in card mode;
- a connected model that differs from the chosen camera disables Space.

No behaviour change on the Z f other than the fixes. Re-run a real Z f
sequence before merging.

### D-2 — the D600 profile and live view sequence

The D600 profile from D-0's numbers, the fixed-handle scan (§2.2), live view
start/stop/drop handling (§2.3), the card release D-0 found, the exposure
overhead, and the D600 fixtures in `PTPTests`. The fake replays D-0's D600
behaviours, including live view ending mid-negative. Done when a 36-release
live view buffer sequence on the D600 lands every frame without a retake.

### D-3 — setup checks and warnings in the Capture tab

The chosen body and its firmware; live view status; battery level and its
warning; readable body settings (auto-off, focus mode per Q16); the
roll/camera mismatch warning (§2.4); the flat-field picker's per-camera
default (§3). In-app live view (§0.3) moves in here only if Q12 says the
body's buttons lock under PTP.

### D-4 — the pipeline on D600 NEFs

§3's checks, with D600 sample NEFs added to `tests/fixtures/nef/` (ignored
by Git, staged the way `stage_samples` stages the Z f's) and a `slow` test
that runs one D600 negative end to end, using the user's D600 flat field.

### D-5 — card mode and resilience on the D600

Card mode with D-0's slot and event findings, dropped-connection recovery
(including a battery that dies), and leftovers on a fixed handle (only one
can exist at a time, which simplifies §2.5 there). Mirrors TETHER_PLAN's T-7.

### D-6 — documents

README (supported bodies, the AC adapter recommendation, live view settings),
ARCHITECTURE.md's camera layer section, DECISIONS.md (user-chosen camera with
a model cross-check; live view on the D600 for positioning and focusing), and a
pointer from TETHER_PLAN §11 to this plan.

### D-7 — electronic shutter on the Z f (later)

The Z f always shoots "from live view", so this is only the shutter. Spike
questions:

- Can silent photography / the electronic shutter be read and set over PTP,
  or only on the body? (If only on the body, the app reads it and shows a
  setup check.)
- Does `0x90C0` work with it on, and what does `DeviceReady` do? (There is no
  shutter movement to wait for.)
- Is the NEF identical in bit depth and compression? Some bodies restrict
  RAW options with an electronic shutter.
- **Banding**: an LED that does not visibly flicker can still be driven by
  PWM, and a rolling electronic readout turns PWM into horizontal bands at
  short exposures. At a 1/2 s exposure it is very unlikely to show. Shoot a
  bare-light frame at the actual scan shutter speed with electronic and
  mechanical shutters and compare row means. A few minutes, and it settles
  the question for the stand's light.
- Does the flat field built with the mechanical shutter still apply? It
  should, since the optics are unchanged, but the bare-light comparison above
  answers that too.

Then a setup check in the Z f profile, and a Z f sequence run with the
electronic shutter on.

**Ordering:** D-0 before everything. D-1 before D-2. D-2 before D-3 and D-5.
D-4 is independent of the Swift chunks and can run in parallel with D-1.
D-7 after D-1.

---

## 5. Rejected alternatives

- **Detecting the body only from the operations it lists.** That is how the
  D600 would get through today and time out on its first cell. Operations say
  what a body accepts, not how it behaves.
- **Detecting the body only from `DeviceInfo.model`, without asking.** Asking
  is acceptable to the user and makes the choice visible. The model string is
  still used to check the answer.
- **Scanning both handle ranges on every body.** Seventeen `GetObjectInfo`
  calls per poll, and it hides which scheme a body uses.
- **Shooting through the viewfinder**, with exposure delay mode for the
  mirror. The viewfinder is hard to use on a copy stand for positioning and
  focusing, which is why live view was chosen.
- **`requestTakePicture()` on the D600.** No buffer capture, no `DeviceReady`
  timing, and a second release path to maintain.

## 6. Risks and open questions

- **Firmware.** Only the firmware D-0 is run on is tested. Record it.
- **Live view may not survive a whole roll** (auto-off, temperature, battery).
  §2.3 turns that into a pause, not a failed negative, but a session with
  frequent drops would be tiresome. The soak test sizes the problem.
- **Q9 may show `DeviceReady` does not mark the end of the exposure** in live
  view. Then the end has to be inferred from the release time and shutter
  speed, which is a timing rule, not a measurement, and needs the user's
  approval.
- **Body buttons may lock under PTP live view** (Q12), which would make
  in-app live view necessary for focusing.
- **The §1.1 bug may already affect the Z f.** If confirmed, fix it on this
  branch independently of D600 work.
- **Flat-field mismatches** across bodies may be silent today (§3).
- **Sensor spots** from the D600's known shutter shedding, if the body was
  never serviced (§0.2 item 9).
