# Focus assist: focusing from live view in the Capture tab

Getting focus right is the hardest part of scanning on the copy stand, and the
lens is manual: the operator turns the ring, and nothing in the app can. What
the app can do is show the film large and say, frame by frame, how close the
ring is to its sharpest point.

This plan follows `docs/TETHER_PLAN.md`'s conventions: numbered chunks, each
independently green; every constant in one module; and no behaviour coded
against until the Z f has been measured. The measurements are in
TETHER_PLAN §0.9.

---

## 0. Why this shape

### 0.1 What the probe showed

From TETHER_PLAN §0.9, the facts this design rests on:

- **Live view streams over the same PTP channel as capture.** `StartLiveView`
  answers OK in under 0.1 s, frames come back in about 5 ms, and 13,700
  fetches across two runs had no failures.
- **Every frame is a 640×424 JPEG** whatever the zoom, behind a 384-byte
  header that says which part of the sensor it shows.
- **The zoom is set on the body, not over PTP.** The Z f answers
  `0x200A` for `LiveViewImageZoomRatio` (`0xD1A3`), but its magnify button
  keeps working while the app runs live view, and the header reports the
  result.
- **At the third magnify step a frame shows 512 sensor pixels across**, 1.25
  frame pixels per sensor pixel. Grain is visible. The fourth step (256 px)
  only enlarges.
- **The camera refreshes at 30 fps.** Faster polling returns repeats.
- **A sharpness score finds the peak.** Mean squared Laplacian over the frame's
  centre: about 2.4 clearly out of focus, 15.5 and 16.1 on two passes through
  focus. The operator's final by-eye setting in that run scored 84% of the
  peak.

### 0.2 A meter, not autofocus

The lens has no motor, so the app never closes the loop. It shows the image
and a number, and the operator's hand does the rest.

The number is there because the eye has no memory. Above half its peak, the
score lasts about one second of normal turning; an operator who watches only
the image cannot tell whether the moment before was sharper. A peak-hold
meter answers exactly that question.

### 0.3 Live view for the ring, a real frame for the corners

Live view shows one small area at a time, as an in-camera JPEG. That suits the
act of turning the ring. It cannot say whether the film is flat or the camera
is parallel to it. A **check shot** does that: one release, scored on the raw
across the frame by §6.3 of TETHER_PLAN's analysis (§4 here).

### 0.4 Scores are relative

The score depends on the film, the zoom, the area shown, the lens, and the
camera's JPEG processing. It is only ever compared with other frames of the
same view, as a percentage of the best seen since the view last changed. It is
never compared with TETHER_PLAN §6.3's focus ratio, which is computed from the
raw and remains the authority on captured frames.

---

## 1. A focus pass, end to end

```
session open, sequence idle or paused
  F ──► focus assist opens; StartLiveView; frames stream into the loupe
  operator magnifies 3 steps on the body ──► loupe reads "512 px, 1.25×"
  operator racks through focus ──► meter climbs, peaks, falls; peak mark holds
  operator returns to the peak mark ──► meter near 100%
  C (optional) ──► check shot: live view pauses, one release, 3×3 focus map
  F or Esc ──► EndLiveView; focus assist closes
```

Focus assist is a panel in the Capture tab, above the mini-view. It is
available only while the sequence is idle or paused (§3.2), and closes itself
if a negative starts.

---

## 2. The live view layer (Swift)

### 2.1 Primitives

`CameraControlling` gains three requirements, implemented in `TetherCamera`
and the fake:

| Requirement | PTP | Notes |
|---|---|---|
| `startLiveView()` | `0x9201`, then `DeviceReady` (`0x90C8`) until not busy | Throws `liveViewRefused(code)` on anything but OK |
| `endLiveView()` | `0x9202` | Always sent when focus assist closes, the session closes, or the connection drops |
| `liveViewFrame() -> LiveViewFrame` | `0x9203` | Retries `0x2019` (busy) up to `LIVE_VIEW_BUSY_RETRIES` |

`startLiveView` is allowed only in `ready`. The one refusal seen (`0xA004`)
came while the first connection after plugging in was still cataloguing the
card, when the state is `preparing` anyway. If `0xA004` is seen in `ready`,
the panel shows "The camera refused live view — close any menu or playback on
the camera" and offers Retry; nothing retries on its own.

`GetLiveViewImageEx` (`0x9428`) is not used: it returns the same frame behind
a larger header (§7).

### 2.2 The header

`PTP.LiveViewHeader` decodes the 384-byte header (big-endian; TETHER_PLAN
§0.9):

| Field | Bytes |
|---|---|
| `jpegLength` | 4–7 |
| `frameSize` | 8–11 |
| `sensorSize` | 12–15 |
| `areaSize` | 16–19 |
| `areaCentre` | 20–23 |

`LiveViewFrame` carries the header and the JPEG bytes, located by the header
length rather than an SOI scan. The decoder rejects a payload whose
`jpegLength` plus the header length disagrees with its size, so a body or
firmware with a different layout fails loudly instead of reporting a wrong
zoom.

### 2.3 The frame loop

`FocusAssistModel` owns one task while the panel is open:

1. Fetch a frame. If the JPEG bytes equal the previous frame's, wait
   `LIVE_VIEW_POLL_INTERVAL` (15 ms) and fetch again. This keeps up with the
   camera's 30 fps without the probe's ~170 requests a second.
2. Decode the JPEG and score it (§3) off the main actor.
3. Publish the image, the header, and the meter state to the view.

The loop never overlaps a release or a download: the PTP channel runs one
transaction at a time (TETHER_PLAN §3.2), and focus assist exists only when
the sequence is idle or paused.

---

## 3. The meter

### 3.1 The score

The probe's measure, unchanged: draw the frame into 8-bit grey, and take the
mean squared 4-neighbour Laplacian over the centre half. Grain dominates it,
which is what focus is judged on. At 640×424 it costs well under a
millisecond per frame.

### 3.2 Peak and percentage

- **Per frame, never averaged over time.** The probe's half-second averages
  blunted the peaks: at t = 2.5 s the average read 8.5 while single frames
  reached 12.5.
- **One outlier frame must not set the peak.** The value shown and held is the
  median of the last `SCORE_MEDIAN_FRAMES` (3) scores, 100 ms at 30 fps.
- **The peak resets** when `areaSize` or `areaCentre` changes (a new zoom or a
  moved area), and on R. Moving the film is not detectable from the header,
  hence R.
- **Below `SCORE_MIN_TEXTURE`** the meter reads "not enough detail here" instead
  of a percentage, so bare rebate or clear film never shows a meaningless peak.
  The threshold is picked in F-2 from frames of rebate and of out-of-focus
  grain; 2.4 was the out-of-focus floor on one negative, so the threshold sits
  well below that.

### 3.3 What the panel shows

- **The loupe**: the frame at 2× on a Retina display (one frame pixel per
  screen point), nearest-neighbour. A larger scale adds no detail.
- **The zoom**: "512 of 6048 px · 1.25×", from the header. At any other step, a
  hint: "Magnify 3 steps on the camera for focusing."
- **Where**: a small sensor outline with the shown area drawn in it, from
  `areaSize` and `areaCentre`.
- **The meter**: a horizontal bar of the current percentage, a tick at 100%
  that stays put, and the percentage as text. The bar turns green at or above
  `PEAK_BAND` (97%).
- **R** resets the peak; **C** takes a check shot (§4).

No sound. The operator focusing is looking at the screen.

---

## 4. The check shot

One release to the buffer, the same path as a sequence cell, then
`capture analyze` (TETHER_PLAN §6.1) on the frame without a baseline:

1. Pause the frame loop and `EndLiveView`. Whether `0x90C0` fires from live
   view on the Z f is not measured; ending live view first needs no answer.
2. Release, wait for the exposure, download to the session's work folder
   under a `focus-check-` name (`CaptureNaming`), never into a negative.
3. Run the analysis. Show its 3×3 regional focus ratios as a grid, each region
   as a percentage of the grid's best, with regions below
   `FOCUS_MIN_TEXTURE` greyed out.
4. `StartLiveView` again and resume the loop.

A falling edge (a whole row or column low) says the film or the camera is not
parallel; one low corner says the film is not flat there. The panel states
which, in those words, and leaves the fix to the operator.

The check shot needs TETHER_PLAN's T-6 (`capture analyze`). Until then F-3
cannot land.

---

## 5. Files

| File | Role |
|---|---|
| `Capture/PTP.swift` | `LiveViewHeader` decoder |
| `Capture/CameraControlling.swift` | The three live view requirements, `LiveViewFrame`, `liveViewRefused` |
| `Capture/TetherCamera.swift` | Their PTP implementation |
| `Capture/FakeTetherCamera.swift` | Replays recorded frames from fixtures |
| `Capture/FocusAssistTuning.swift` | Every constant in this plan |
| `Model/FocusScore.swift` | The score (§3.1); pure, no camera |
| `Model/FocusAssistModel.swift` | The frame loop, peak and median, zoom text, check shot |
| `Views/FocusAssistPanel.swift` | Loupe, zoom and area, meter, check-shot grid |

Changes: `CaptureStageView` (the panel and the F, R, C keys),
`CaptureSessionModel` (closing focus assist when a negative starts, refusing
Space while it is open).

Keys: F, R and C must be checked against `AppKeyboard.swift` and every view's
`keyboardShortcut`, as Space was (TETHER_PLAN §3.2).

---

## 6. Tests

No test touches a camera.

- **`LiveViewHeader`** decodes the probe's recorded payloads, committed as
  fixtures: one at each zoom step (`lv-watch-t00`, `t05`, `t09`, `t13`, `t17`
  from the 2026-09-13 run), asserting `areaSize` 6048, 2048, 1024, 512, 256.
  A truncated payload and one with a wrong `jpegLength` are rejected.
- **`FocusScore`** on fixture JPEGs: the recorded peak frame outscores the
  recorded out-of-focus frames at the same zoom, and a flat grey frame scores
  below `SCORE_MIN_TEXTURE`.
- **`FocusAssistModel`** against the fake replaying a scripted score sequence:
  a single outlier does not set the peak; a zoom change resets it; identical
  JPEGs are not rescored; the loop stops and `endLiveView` is sent when a
  negative starts, the session closes, or the connection drops.

---

## 7. Rejected alternatives

- **`GetLiveViewImageEx` (`0x9428`) for bigger frames.** On the Z f it returns
  the same 640×424 JPEG with a 1024-byte header.
- **Setting the zoom from the app.** `0xD1A3` is unsupported on the Z f, and
  whatever Z bodies use instead is unknown. The body's button works and the
  header reports it, so nothing is lost but a click.
- **Averaging scores over time.** Blunts the peak the meter exists to catch
  (§3.2).
- **An audible "at peak" tone.** The operator is watching the screen while
  focusing, and a tone lagging a fast turn would point past the peak.
- **Focus peaking overlays on the loupe.** Useful on a whole scene; on a
  magnified grain field the whole frame is edges. Could be revisited for the
  unmagnified view.
- **Scoring the NEF's embedded preview for the check shot.** It is downscaled
  and in-camera sharpened; grain is gone. TETHER_PLAN §6.3 measures on the raw
  for this reason.

---

## 8. Chunks

### F-1 — the live view layer

`LiveViewHeader`, the three `CameraControlling` requirements in `TetherCamera`
and the fake, `FocusAssistTuning`, fixtures from the probe runs. No UI.

### F-2 — the loupe and meter

`FocusScore`, `FocusAssistModel`, `FocusAssistPanel`, the keys. Picks
`SCORE_MIN_TEXTURE` (§3.2). Requires F-1 and TETHER_PLAN T-4.

Done when, on the Z f, a negative focused to the meter's 100% and one focused
by eye alone are both captured, and the difference is judged on the stitched
results. That settles whether the probe's 84% by-eye setting was a visible
loss (§9).

### F-3 — the check shot

The release-and-analyze path and the 3×3 grid. Requires F-2 and TETHER_PLAN
T-6.

---

## 9. Risks and open questions

- **Whether 16% below the peak is visible in a scan is unknown.** One run is
  the evidence for the meter beating the eye. F-2's done criterion measures it.
- **The score reflects the camera's JPEG processing.** Picture Control
  sharpening changes the scale, which is harmless, and could shift the apparent
  peak slightly, which is not. A check shot against the raw would show it.
- **The header layout is measured on one Z f.** Other bodies and firmware may
  differ; §2.2's length check refuses rather than misreads.
- **The median over 3 frames adds 100 ms of lag** to the displayed value. At
  normal turning speed the peak is about 1 s wide above half its height, so
  this is tolerable; if a fast turn overshoots, `SCORE_MEDIAN_FRAMES` drops
  to 1.
- **Heat and battery from long live view on the Z f** are unmeasured. Focus
  assist runs for minutes per roll, not for the session.
