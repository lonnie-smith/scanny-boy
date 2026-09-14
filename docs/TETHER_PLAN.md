# Tethered capture: shooting a roll from the copy stand

Today a roll starts as a folder of NEFs the user shot by hand and later
points Add Scans at. This plan puts the camera on a USB tether and turns
the copy stand into one continuous workflow:

1. **The Z f is driven from the app** over ImageCaptureCore's raw PTP
   passthrough. By default a frame goes into the camera's memory buffer, not
   onto a card, and lands on disk under a name the app gives it.
2. **One key press captures a whole negative.** The user sets the grid
   (Across × Down, from the same presets Add Scans uses) and a shot interval
   (a dropdown, default 4 s). Space starts the negative; the app fires
   `across × down` frames, one per interval, while the user moves the film.
   A mini-view of the grid fills in cell by cell.
3. **The roll's base frame is shot through the tether** and attached with
   the existing `roll set-base-frame` before the first negative
   (docs/REBATE_ANCHORING.md). This is the "base analysis image": it is
   already the roll's thin-end clipping reference (§6.2).
4. **Every frame is checked for clipping and focus** as it lands. The checks
   are advisory and never block a shot.
5. **A negative starts processing the moment its last frame lands**, in the
   background, while the user captures the next one. Its RAW decoding and an
   alignment check run straight away, alongside other negatives', so a
   negative that won't align is flagged while it is still on the stand. Its
   stitch waits for the roll, because stitches run strictly one at a time
   (§0.6), and the backlog is allowed to build (§4.6).
6. **Whole-roll analyses** that would otherwise churn on every negative are
   deferred to the end of the session and run once (§5).

This plan follows the conventions of `docs/REBATE_ANCHORING.md` and
`docs/ROLL_HIGHLIGHT_LOCK.md`: numbered chunks, each independently green;
every constant in exactly one module; and **no threshold pinned without a
measurement the user has approved** — with one exception the user chose: the
capture warnings ship with provisional thresholds, labelled unmeasured, and
are tuned by use (§6.5).

It changes the event protocol from version 21 to **22** (§7).

**What this does not change.** The `run`/`stitch` pipeline and everything it
publishes; the grid rules (`min(across, down) <= 2`, the 12-scan cap); the
roll invariants; the base-frame state machine; and Add Scans, which keeps
working for folders shot without a tether. A capture folder is an ordinary
input folder — Add Scans can open it too.

---

## 0. Why this shape

### 0.1 What the probe measured

Every design decision below rests on runs of `mac/Tools/TetherProbe.swift`
(commit `e7affbc`) against the user's Z f, firmware V2.00, on 2026-09-11. The
probe is a throwaway; its findings are the durable part.

| Question | Measured answer |
|---|---|
| Can ImageCaptureCore see and open the camera? | Yes over USB (`ICTransportTypeUSB`). In the camera's mass storage mode it appears as `ICTransportTypeMassStorage` and cannot be driven: PTP commands go unanswered. |
| Does `ICCameraDevice.requestTakePicture()` fire? | **No.** The user confirmed nothing happened. The camera advertises `canTakePicture`, yet `tetheredCaptureEnabled` reads `false`. |
| Does the raw PTP passthrough work? | Yes. `requestSendPTPCommand(_:outData:completion:)` answered `GetDeviceInfo` with `0x2001 OK` and a 577-byte dataset listing 118 operations, 26 events, 21 properties. |
| Which releases fire? | `InitiateCapture` (`0x100E`) and Nikon `InitiateCaptureRecInMedia` (`0x9207`, media 0) write to the cards. Nikon `InitiateCaptureRecInSdram` (`0x90C0`) writes **nothing to either card**. Every release command answered in 6–17 ms. |
| When is the exposure over? | Nikon `DeviceReady` (`0x90C8`) answers busy (`0x2019`) right after the release and OK again 0.55 s later at 1/2 s (EXIF-confirmed). On two earlier shots it was 0.28 s, with the camera last read at 1/8 s. It tracks the exposure. |
| How does a card shot announce itself? | Two `ObjectAdded` (`0x4002`) events, one per slot (the second slot is set to backup), then `CaptureComplete` (`0x400D`), pushed through `ptpEventHandler` about 1.3 s after the release. |
| How does a buffer shot announce itself? | Unreliably. `ObjectAddedInSdram` (`0xC101`) is **queued** in Nikon's `CheckEvent` (`0x90C7`) queue, never pushed, and no `CaptureComplete` follows. The queue was polled after two of the three buffer shots: once the event came at 1.1 s; once it did not come within 8 s and only turned up when a later session read the queue. |
| Can a buffer frame be found without the event? | Yes. Buffer frames take handles `0x0B000001`, `0x0B000002`, … Asking `GetObjectInfo` of each handle found the third shot's frame 1.14 s after release. Buffer frames never appear in `GetObjectHandles` or in ImageCaptureCore's catalog. |
| Download | `GetObject` (`0x1009`): 25 MB NEF in 0.55 s (≈45 MB/s — the link negotiated USB 2, 480 Mb/s). exiftool reads it as a valid lossless `NIKON Z f` NEF. |
| Release → file on disk | **1.7 s** from the buffer, **2.2 s** from a card, both at a 1/2 s exposure. |
| Does a download clear the buffer? | Yes: afterwards the handle answers `0x2009 Invalid object handle`, and numbering restarts at `0x0B000001` once the buffer is empty. |
| Are undownloaded buffer frames kept? | Yes, across sessions: a frame shot at 12:25:00 was still there and downloadable at 12:31. |
| What is a buffer frame called? | Always `DSC_0000.NEF`, on storage `0x00000000`. |
| Two slots | Each card shot is written to both. The two copies have **identical sensor data**; only the Nikon `MemoryCardNumber` tag differs. |
| Nikon's event queue | Read-once, and it holds every event since the camera was switched on, including `ObjectAdded` for frames downloaded long ago. |
| Can exposure settings be read? | Yes, through `GetDevicePropValue` (`0x1015`): program, shutter, aperture, ISO, WB, focus mode. A shutter-speed change was pushed as `DevicePropChanged` (`0x4006`) for `0x500D`. |
| Recording media (`0xD10B`) | Settable; allowed values `[0, 1, 2]`; reads 0 (card). A `DevicePropChanged` for it fires around **every** buffer capture — it is not a user settings change. |
| ImageCaptureCore's `didAdd` | Fires for card shots, but every new item reads `wasAddedAfterContentCatalogCompleted == false`. Never fires for buffer frames. |
| First connection after plugging in | The content catalog took 73 s for 2,343 card files, and the first PTP reply waited 24 s behind it. Later sessions: catalog ≈0.2 s, `GetDeviceInfo` 5 ms. |

### 0.2 Swift owns the camera; Python still owns every decision

ARCHITECTURE.md §3's rule is that Python owns every decision and Swift owns
no logic. That rule survives, with one boundary stated explicitly.

The camera is reachable only through ImageCaptureCore, a macOS framework the
frozen CLI cannot load. So **device I/O is Swift's**: discovering the camera,
sending PTP, finding and downloading the frame, writing the file, and timing
the sequence. Everything that *judges* stays in the CLI: grouping and
validation (`probe`, `run`), the base frame's gates (`roll set-base-frame`),
clipping and focus verdicts (`capture analyze`, §6), stitching, and every roll
record write.

Two things Swift does decide, because it must:

- **The file name.** The camera names every buffer frame `DSC_0000.NEF`, so
  someone has to; the rule lives in exactly one Swift type (§2.6).
- **The number of shots**, `across × down`. `ConfigurationModel` already
  computes that product; its validity still comes back from the CLI
  (`grid create`, `probe`, and `run` all refuse `INVALID_GRID`).

### 0.3 ImageCaptureCore and raw PTP

libgphoto2 was the other serious route. It would put capture in the CLI, but
it `dlopen`s its camera and port drivers from build-time paths — the hardest
thing to carry through PyInstaller and ad-hoc signing — and it has to fight
macOS's `ptpcamerad` for the device. The probe removed its one advantage:
ImageCaptureCore's PTP passthrough reads and sets properties and sends Nikon's
vendor release operations, which is everything libgphoto2 offered that this
feature needs. Nikon's own SDK is registration-gated C++ for the same
capability. Both are rejected (§10).

### 0.4 The camera's buffer by default

A buffer capture writes nothing to the cards. At 25 MB per frame written to
both slots, a 36-negative roll shot 3×2 puts about 10.8 GB on the cards for
nothing; the buffer route avoids that and lands the file 0.5 s sooner.

The cost is the card as a safety net: if USB drops between a release and its
download, that frame exists only in the camera's memory. The design absorbs
it rather than hides it — **a cell fills only when its file is on disk**, a
leftover buffer frame is offered back on reconnect (§2.5), and retaking a cell
is an ordinary action (§3.2). Card recording stays available as a setting
(§2.3) for anyone who wants the second copy.

### 0.5 A timed sequence per negative

The user presses Space once per negative; the app fires every cell. The
operator's only job is positioning the film, so the app has to say two things
clearly: **when it is safe to move** (the exposure is over) and **when to hold
still** (a release is coming). §3.2 and §3.3 define both.

The interval is measured **from the end of the exposure**, not from the
release. The operator's time to move starts when the shutter closes; measuring
from the release would let a long exposure eat into it (at 1/2 s, a "4 s"
interval would leave 3.5 s). The exposure's end is observable: `DeviceReady`
returning OK (§0.1).

### 0.6 Decode in parallel, stitch one at a time

`run` is two stages, and they differ in the one way that matters here.
`prepare` decodes a negative's RAWs, applies the flat field and writes
intermediates into its own work folder; it never reads or writes the roll
record. `stitch --work` registers, composites and publishes into the roll, and
writes the record. So the queue splits the same way (§4.1): a negative's
`prepare` starts the moment its last frame lands, alongside any others still
preparing, and its `stitch` runs when the roll is free.

Stitches of one roll never overlap, for reasons in the code, not preference:

- **The record would be overwritten, repeatedly.** `run_stitch` loads the roll
  once when it starts (`plan.existing_manifest`) and writes that whole copy
  back after appending its run, after every negative it publishes, and at the
  end. `library/repo.save_roll` deletes every run and negative row missing
  from the copy it is given, so each write by one stitch deletes whatever the
  other published since it started — and cascades away those negatives' edits.
- **Cleanup assumes a single stitch.** The roll's staging rule treats the
  record's last run (`runs[-1]`) as the owner of staging directories; a second
  stitch planning mid-way through the first would see the first's unfinished
  negatives as leftovers for `apply_recovery_cleanup` to delete.
- **Names, negative ids and the clamp's reference negatives** all come from
  the start-of-stitch copy.
- **Memory is budgeted per process**: each sizes itself to half of physical
  RAM without knowing about the others.

So stitches are serial (§4.1), nothing else writes that roll while they run
(§4.2), and the CLI enforces it with a lock instead of trusting the app
(§4.3). A serial stitch stage can fall behind capture, and that is accepted:
the user does not mind stitching finishing after scanning does. What the user
does mind is finding out late that a negative cannot be stitched, so the
alignment gates run early instead, outside the queue (§4.1).

### 0.7 Whole-roll analyses

Most of the roll-level state already behaves well when negatives arrive one
at a time: the film kind is fixed at `roll init`, the base frame locks at the
first publish, the camera colour matrix is seeded once, and the sequence is
recomputed on every record write. One does not: the **highlight lock** is
recomputed after every stitch and, whenever it changes, forces a re-render of
**every** completed negative's preview. Stitching per negative would turn one
render pass into roughly n²/2 renders over a session, with earlier negatives
visibly changing colour mid-session. §5 has the full inventory; §4.4 defers
that one refresh to the end of the session.

### 0.8 Ordering

- **T-2 (the roll lock) and T-3 (deferred refresh) land before T-5 (the
  queue)**, because the queue is only safe with both.
- **T-1 (camera layer) lands before T-4 (the Capture tab).**

### 0.9 Live view, measured

Runs of `TetherProbe.swift --liveview` against the same Z f on 2026-09-13,
for `docs/FOCUS_ASSIST_PLAN.md`. Nothing above depends on them.

| Question | Measured answer |
|---|---|
| Does `StartLiveView` (`0x9201`) work? | Yes: OK in under 0.1 s, `DeviceReady` OK at once, `LiveViewStatus` (`0xD1A2`) 0 → 1, `LiveViewProhibitCondition` (`0xD1A4`) 0. One attempt answered `0xA004` (Invalid status) after waiting 25 s behind the first connection's card catalog; it did not recur in three later runs. |
| Can the zoom be set? | **No.** `LiveViewImageZoomRatio` (`0xD1A3`) answers `0x200A` (property not supported) to get, set and describe. No property in `0xD1A0`–`0xD1FF` is advertised in DeviceInfo, though `0xD1A2` and `0xD1A4` read fine. |
| Do the body's controls work while PTP runs live view? | Yes: the magnify button stepped the zoom during a run, and the frames followed. |
| What does `GetLiveViewImage` (`0x9203`) return? | A 384-byte header, then a **640×424 JPEG at every zoom step**, 49–72 KB. Median fetch 4.8 ms; no failed fetch in about 13,700. |
| What is in the header? | Big-endian: bytes 4–7 JPEG length, 8–11 frame size (640, 424), 12–15 sensor size (6048, 4032), 16–19 the **sensor area shown**, 20–23 its centre (3024, 2016 unmoved). Bytes 24–31 are non-zero only unmagnified; not decoded. |
| What does each magnify step show? | 6048, 2048, 1024, 512 and 256 sensor px across: 0.11, 0.31, 0.63, **1.25** and 2.5 frame px per sensor px. Grain is visible at 512 and 256; 256 only enlarges it, and its JPEG shrinks to 49 KB. |
| How fast does the camera refresh? | **30 fps**: 1,200 distinct frames in 40 s out of 6,631 fetches; the rest were repeats. |
| Does `GetLiveViewImageEx` (`0x9428`) give more? | No: the same 640×424 JPEG behind a 1024-byte header, with the same fields at other offsets (12–15 JPEG length, 16–19 sensor, 20–23 area, 24–27 centre, 28–31 frame size). |
| Does a sharpness score find focus? | Yes, at the 512 px step: mean squared Laplacian over the centre half of the grey frame read about 2.4 clearly out of focus, and peaked at 15.5 and 16.1 on two passes through focus. It stays above half its peak for about 1 s of normal turning. The operator's final by-eye setting scored 84% of the peak. |

---

## 1. A session, end to end

```
Capture tab, roll selected
  │
  ├─ setup: film kind (from roll) · rig profile (optional) · grid preset
  │         · interval (default 4 s) · destination (buffer | card, Settings)
  │
  ├─ connect ── TetherCamera: find → open → GetDeviceInfo → required ops present?
  │             → drain CheckEvent → scan the buffer (leftovers? §2.5)
  │             → snapshot exposure (program, shutter, aperture, ISO, WB, focus)
  │
  ├─ bare-light reference (only if the roll has none attached)
  │     film out, light only → "Shoot bare-light reference"
  │     → release → download → roll set-flatfield-reference --frame FILE [--rig ID]
  │     → same f-stop as the scans; shutter/ISO may differ
  │
  ├─ base frame (only if the roll has none attached)
  │     user frames a piece of leader → "Shoot base frame"
  │     → release → download → roll set-base-frame --frame FILE
  │     → gates pass: attached   |   gates fail: message, shoot again
  │
  ├─ negative loop ──────────────────────────────────────────────────────────┐
  │     Space → sequence of across×down cells (§3.2)                         │
  │       each cell: hold cue → release → exposure over → move cue           │
  │                 → frame found → downloaded → file on disk → cell fills   │
  │                 → capture analyze (async, §6) → badges                   │
  │     last cell lands → negative complete → prepare → check (§4.1)         │
  │     user advances the film ──────────────────────────────────────────────┘
  │
  │   meanwhile, StitchQueueModel: prepares and checks in parallel,
  │   one `stitch --defer-roll-refresh` at a time, in capture order
  │
  └─ End session → wait for the queue → roll refresh (§4.4) → session summary
```

---

## 2. The camera layer (Swift)

### 2.1 Connection states

`TetherCamera` is an actor wrapping `ICDeviceBrowser` and one
`ICCameraDevice`. Its state is what the Capture tab shows:

| State | Meaning | Shown to the user |
|---|---|---|
| `absent` | Browsing not started | "Plug the camera in over USB, switch it on, then click Connect." |
| `searching` | User clicked Connect; browser running, no device yet | "Waiting for the camera…" |
| `massStorage` | Found as `ICTransportTypeMassStorage` | "The camera is connected as a disk. Set its USB mode to PTP." |
| `unavailable` | The session would not open | "Another app is using the camera. Quit Photos or Image Capture." |
| `preparing` | Session open, waiting on the first PTP reply | "Preparing the camera — the first connection after plugging in can take a minute." |
| `unsupported` | `GetDeviceInfo` lacks an operation this plan needs | "This camera doesn't support tethered capture." (v1 is tested on the Z f only) |
| `ready` | Idle, able to release | "Ready." |
| `busy` | A release or download is in flight | "Busy." |
| `lost` | The device disappeared mid-session | §2.5 |

Connection is **manual**: the user plugs in and powers on the camera, then
clicks **Connect** in the Camera section. `ICDeviceBrowser` does not start at
launch. After Connect, the browser stays running so unplug/replug can recover
without another click; **Retry** appears on error states, **Disconnect** while
a session is open.

`CaptureSessionModel` installs a handler on `TetherCamera` so state and exposure
updates reach the UI as the async connect completes — a one-shot snapshot at
tab appear is not enough.

`preparing` exists because of §0.1's first-connection figure: the first PTP
reply waited 24 s behind the card catalog. Space is disabled until
`GetDeviceInfo` has answered.

The required operations are `0x1008`, `0x1009`, `0x1015`, `0x90C7`, `0x90C8`,
and the destination's release (`0x90C0` for the buffer, `0x9207` for card).

### 2.2 PTP primitives

`PTP.swift` holds the container framing and dataset decoders the probe proved
out. Everything goes through `requestSendPTPCommand(_:outData:completion:)`,
one transaction at a time.

| Opcode | Name | Used for |
|---|---|---|
| `0x1001` | GetDeviceInfo | Required-operation check at connect |
| `0x1004` / `0x1005` | GetStorageIDs / GetStorageInfo | Card mode: which storage is slot 1 |
| `0x1008` | GetObjectInfo | Buffer handle scan; file size and capture date before download |
| `0x1009` | GetObject | Download |
| `0x1015` | GetDevicePropValue | Exposure snapshot |
| `0x90C7` | Nikon CheckEvent | Draining the queue; a hint that a buffer frame exists |
| `0x90C8` | Nikon DeviceReady | End of exposure |
| `0x90C0` | Nikon InitiateCaptureRecInSdram | Buffer release, param `0xFFFFFFFF` (no autofocus) |
| `0x9207` | Nikon InitiateCaptureRecInMedia | Card release, params `0xFFFFFFFF, 0` |
| `0x90C3` | Nikon DelImageSDRAM | Discarding a leftover buffer frame the user rejected (§2.5) |

`0x90C3` is the only opcode here the probe did not exercise; T-1 verifies it
before anything calls it.

### 2.3 Releasing and finding one frame

**Buffer mode** (the default), in order:

1. **Before the first release of a negative**, drain `CheckEvent` until it
   returns no events. The queue is read-once and holds history (§0.1).
2. **Scan the buffer**: `GetObjectInfo` on `0x0B000001` through
   `BUFFER_SCAN_LAST` (`0x0B000010`). Before a release the buffer must be
   empty; a leftover is handled by §2.5, never assigned to a cell silently.
3. **Release** with `0x90C0`.
4. **Poll `DeviceReady`** every `READY_POLL_INTERVAL` (50 ms) until it has
   answered busy and then OK. That moment is the end of the exposure: play the
   move cue (§3.3) and start the interval clock (§3.2). If it never returns OK
   within `EXPOSURE_TIMEOUT` (exposure + 10 s), the cell fails.
5. **Poll for the frame** every `FRAME_POLL_INTERVAL` (100 ms): drain
   `CheckEvent` and rescan the buffer. A handle that was not there before the
   release is the frame; `ObjectAddedInSdram` is only a hint that the scan is
   worth doing. If nothing appears within `FRAME_ARRIVAL_TIMEOUT` (10 s), the
   cell fails.
6. **Download**: `GetObjectInfo` for the size, `GetObject`, and check the byte
   count against it. Write to `<name>.NEF.partial`, `fsync`, rename into place.
7. **Confirm the buffer let go**: `GetObjectInfo` on the handle must now fail.
   If it still answers, the cell's file is kept but the sequence pauses — the
   camera is not behaving as measured.
8. The cell fills.

**Card mode**: release with `0x9207` (media 0). Collect `ObjectAdded` pushed
through `ptpEventHandler`, ignoring any handle seen in step 1's drain; wait
for `CaptureComplete`; fetch only the object on the lowest storage ID (slot 1)
— the other slot's copy has identical sensor data (§0.1). Steps 4, 6 and 8
are unchanged.

All the constants above live in `TetherTiming.swift`.

### 2.4 Exposure watch

At connect, `TetherCamera` reads program, shutter, aperture, ISO, WB and focus
mode. The Capture tab shows them, and flags two conditions before Space is
enabled: an exposure program other than Manual, and a focus mode other than
manual focus. The release itself never autofocuses (param `0xFFFFFFFF`), but
an AF body setting can still refocus between shots when touched.

During a session, a `DevicePropChanged` for shutter, aperture, ISO or WB
**pauses the sequence** after the in-flight shot and shows old → new. Resuming
is an explicit choice. `0xD10B` (recording media) and every other vendor
property are ignored — `0xD10B` changes around every buffer capture.

This is a guard for the operator, not the authority. The authority remains the
CLI's: `consistency.py` refuses mixed exposure within a negative's run, and
`FILM_BASE_EXPOSURE_MISMATCH` flags a negative whose exposure differs from the
base frame's. Per-negative runs lose the cross-negative consistency a whole
folder used to get from one `run` (§5); this watch is what gives it back at
capture time.

### 2.5 A dropped connection, and leftovers

If the device disappears mid-sequence, the sequence stops. Cells whose files
are on disk stay filled; the in-flight cell is empty.

On reconnect, and at every connect, the buffer scan (§2.3 step 2) may find
leftovers. Buffer frames survive across sessions (§0.1), so a leftover is
usually the frame a drop cut off, but that cannot be proven. The app shows
each leftover's embedded preview and capture time and offers:

- **Use for cell *k*** — only when exactly one cell of an open negative is
  empty;
- **Save to `_unclaimed/`** — downloaded, kept, attached to nothing;
- **Discard** — `0x90C3`, removing it from the camera's memory. The user
  chooses this; the app never discards on its own.

As built: the app downloads each leftover into `_unclaimed/` as soon as it
finds one, at connect or before a release. Downloading is the only proven way
to read the embedded preview, and it clears the frame from the camera, so the
frame is safe on disk before the user chooses. **Save** leaves that file
where it is, **Use for cell *k*** moves it to the cell's name, and **Discard**
deletes it. Start and Resume stay disabled until every leftover is resolved.

### 2.6 Files on disk

**Where.** A capture base folder, set in Settings, default
`~/Pictures/Scanny Boy Captures/`, holding one folder per roll named after
the roll's folder. Not inside the roll folder, which holds only published
TIFFs (ARCHITECTURE.md §9). The roll record stores sources by absolute path
and SHA-256, so renaming a roll later does not orphan anything.

**The name.** `<yyyyMMdd-HHmmss>_<cc>.NEF`: the local time of the negative's
first release, then the shot's number within the negative, `01` upward.

- It needs no enumeration of the folder to stay unique.
- It sorts in the order the negatives were shot, which is also the CLI's
  canonical order (capture time, then filename).
- If a name already exists (two negatives started in one second), the file is
  created with `O_EXCL` and the suffix `-2`, `-3`, … is appended to the stamp.

One consequence, accepted by the user: `roll_manifest.allocate_output_name`
names a published TIFF after its first member's stem, so tethered negatives
publish as `20260911-123325_01.tif`. No separate naming rule for tethered
negatives.

`CaptureNaming.swift` is the only place a capture name is chosen.

---

## 3. The sequence (Swift)

### 3.1 Setup

| Field | Source | Notes |
|---|---|---|
| Roll | Sidebar selection | Locked while a session is open or its queue is non-empty (§4.2) |
| Film kind | `probe --roll` | Required, as for Add Scans |
| Base frame | `probe --roll`'s `film_base` | Required before the first negative; shot here if absent (§1) |
| Bare-light reference | `roll info`'s `flat_field` | Required before the first negative; shot here if absent (§1) |
| Rig profile | `RigModel` | Optional geometry/CA; defaults to the last used, as on Add Scans |
| Grid | `GridModel` presets or Across/Down | The same controls and clamp as Add Scans |
| Interval | Dropdown: 2, 3, 4, 5, 6, 8, 10 s | Default 4 s; last used remembered in `UserDefaults` |
| Destination | Settings: camera buffer or card | Default buffer — nothing is written to the cards |
| Cues | Settings: sound on/off | Default on |

Space is enabled only when the camera is `ready` (§2.1), the exposure checks
pass (§2.4), and every required field is set — the same completeness rule as
`ConfigurationModel.runEnabled`.

### 3.2 The timeline of one negative

With a 1/2 s exposure and the default 4 s interval, from §0.1's measurements:

```
t = 0.00  user starts negative ── initial interval clock starts
t ≈ 3.00  hold cue (HOLD_CUE_LEAD = 1.0 s before release)
t ≈ 4.00  release (cell 1)
t ≈ 4.55  exposure over (DeviceReady OK) ── move cue, interval clock starts
t ≈ 5.1   frame found in the buffer
t ≈ 5.7   file on disk ─────────────────── cell 1 fills
t ≈ 7.55  hold cue
t ≈ 8.55  release (cell 2)
…
last cell's file on disk ── negative complete, enqueued for stitching
```

Rules:

- An **initial interval** of the same duration runs before cell 1's release.
  If the operator pauses during it and cell 1 has not fired yet, the initial
  interval runs again on resume.
- Inter-shot intervals start at the end of the exposure (§0.5).
- **The next release waits for the previous frame's download.** The PTP
  channel runs one transaction at a time and a 25 MB `GetObject` takes 0.55 s;
  a release must never queue behind one. If a slow download pushes past the
  interval, the countdown shows "waiting for download" rather than firing late
  without saying so.
- The last cell has no interval after it.

Keys, active only when the Capture stage has focus and no text field does:

| Key | Idle | During a sequence | Paused |
|---|---|---|---|
| Space | Start the next negative | Pause after the in-flight shot | Resume (hold cue first) |
| Delete | — | — | Retake the last filled cell (replaces its file in place) |
| Esc | — | Stop the negative | Stop the negative |

Nothing in the app binds a plain Space today (checked: `AppKeyboard.swift` and
every view's `keyboardShortcut`).

A stopped negative keeps its files and is not enqueued. The mini-view offers
**Resume from cell *k*** and **Discard negative**, which moves the files to the
Trash with `NSWorkspace.recycle` — recoverable, like deleting a roll.

### 3.3 Cues

The operator is looking at the film, not the screen, so the cues are audible
first:

- **Move**: a short tick when the exposure ends.
- **Hold**: a distinct tone `HOLD_CUE_LEAD` before the next release.
- A large countdown on screen for a glance from the stand.

Both sounds are system sounds (`NSSound`); nothing new is bundled.

### 3.4 The mini-view

A grid of `across × down` cells. Cell states:
empty, next, exposing, downloading, filled, failed. A filled cell shows the
NEF's embedded preview through `ThumbnailLoader`'s ImageIO path (no demosaic),
and once §6's analysis answers, a clipping and a focus badge.

Below the grid, a list of this session's negatives, one row each: queued
(with a progress bar and elapsed time for the active step), stitching (with
the stitch's progress), published (the stitched TIFF thumbnail), or failed
(the CLI's message, and Reshoot, §4.5).

---

## 4. Background stitching

### 4.1 The queue: prepare, check, stitch

`StitchQueueModel` takes each completed negative through three steps. The
first two never touch the roll and run as soon as there is a slot; the third
writes the roll and runs one at a time.

**Prepare.** The moment a negative's last frame lands:

```
prepare --input <capture folder> --files <the negative's frames>
        --out <capture folder>/.work/<negative stamp>
        --grid AxD [--rig <profile id>]
```

**Check**, straight after its prepare, in the same slot:

```
capture check --work <capture folder>/.work/<negative stamp>
              [--rig <profile id>]
```

`capture check` runs the stitch's own solve phase — detection, pair matching,
the rig-tilt rectification fit, the layout solve, and the CLAHE retry — by
calling `stitch_pipeline._solve_negative`, the function `run_stitch` calls. It
reads the grid from the work folder's manifest, as `stitch` does, composites
nothing, writes nothing, and reports `capture_checked` (§7.2). So its verdict
is the stitch's verdict for every gate the solve decides:

- `STITCH_UNDERCONSTRAINED` — too little overlap to connect the frames;
- `STITCH_RESIDUAL_TOO_HIGH` — a layout that does not fit its pairs;
- `STITCH_LAYOUT_UNEXPECTED` — a slid, duplicated or missing cell;
- `STITCH_SCALE_DRIFT` — the film's height changed between frames.

A negative that fails is flagged on its tile at once, while it is still on the
stand, and the queue does not stitch it (§4.5).

The solve takes the work folder, the grid and the optional rig profile, and a
first read of `_solve_negative` and `_attempt_solve` found no access to the
roll. Their `_SolvedNegative` entry does carry a `NegativeRecord`, which
`run_stitch` builds from the roll; the check builds a throwaway one, and T-5
confirms nothing in the solve depends on a real record.

**What the check cannot decide** is the overlap check (`MAX_OVERLAP_MAD`),
which measures whether pixels line up after compositing and fails as
`STITCH_RESIDUAL_TOO_HIGH` inside `_composite_and_publish`. That failure can
still arrive late, from the stitch. The user accepts reshooting in that case.

Up to `MAX_PARALLEL_PREPARES` negatives prepare and check at once, each step
in its own `CLISession`: 2 on this Mac. Each `prepare` already caps itself at
4 decode workers, so two use 8 of the 10 cores and leave room for the stitch
and the frame analysis. The cap only paces decoding. It does **not** limit how
many negatives are prepared ahead of the stitcher: checked negatives wait for
their stitch in any number (§4.6). Work folders live in the capture folder,
never in the roll folder.

**Stitch, one at a time, in capture order:**

```
stitch --work <capture folder>/.work/<negative stamp> --roll <roll>
       [--rig <profile id>] --defer-roll-refresh
```

A negative's stitch waits for every earlier negative's stitch, even if its own
check finished first, so the clamp's reference negatives are exactly what one
run over the whole roll would have used (§5). A negative that failed its
prepare or its check is skipped (§4.5).

The stitch repeats the solve rather than trusting the check's result, so
`stitch` itself stays unchanged; the repeat is a detection pass on
2000-pixel images and a solve, not a composite.

This is what `run` does internally — `run_pipeline.run_full` calls
`run_convert`, then `run_stitch` — so a capture folder behaves like any other
input, including roll-overlap detection and in-place replacement if the same
frames are ever stitched again. The app removes a work folder once its
negative publishes; §4.5 covers the ones it keeps.

### 4.2 Relaxing "one helper at a time"

`AppActivity.isBusy` includes `run.isActive` today, and any run disables the
sidebar, the tab picker and every stage's controls. That would freeze the
Capture tab for the whole of every stitch. The rule becomes:

- **Roll-writing work is serial per roll.** While a roll's queue is non-empty
  or a session on it is open, every other control that writes that roll is
  disabled: Edit's ops, negative delete, Apply, Re-stitch, the base-frame
  field, roll rename and delete, Add Scans' Convert, and Export (§4.3).
- **The Capture stage stays live**, and so do its analysis daemon (§6.1) and
  the prepares and checks (§4.1), none of which writes the roll.
- The sidebar stays on the capturing roll while its session is open.

DECISIONS.md gets this as an amendment to the Phase 3 app rule.

### 4.3 The CLI enforces it: a roll lock

§0.6's hazard is silent data loss, so the app's discipline is not the only
guard. Every command that writes a roll takes an exclusive advisory lock
(`fcntl.flock`, non-blocking) on
`~/Library/Application Support/ScannyBoy/locks/<roll_id>.lock`, beside the
library database. A second writer fails immediately with the new error
`ROLL_BUSY`, before any work.

- **Exclusive**: `run`, `stitch`, every `edit` command that records an op or
  deletes, `metadata set`, `apply-metadata`, `roll set-base-frame`,
  `roll rename`, `roll delete`, and `roll refresh` (§4.4).
- **Shared**: `export`, which reads published TIFFs a stitch may be replacing.
- **None**: `prepare` and `capture check` (neither touches the roll),
  `roll list`, `roll info`, `probe`, `edit list-*`, `edit render-*`.
- `serve` takes the lock per request, not for the daemon's lifetime.

The lock lives in one new module, `roll_lock.py`.

### 4.4 Deferring the roll refresh

`stitch_pipeline.run_stitch` ends by recomputing the highlight lock and, when
it changed, calling `previews.sync_previews(..., force=True)` over every
completed negative. At stitch time nothing else reads the lock — it is read
only at render and edit time (`color.read_metering` from `previews.py` and
`exporter.py`; the auto-density, auto-grade and auto-cast solves in
`edits.py`). So deferring it changes no published pixel and no seeded op.

- **`run` / `stitch --defer-roll-refresh`** skips the recompute and the forced
  preview pass, renders this run's own new previews under the roll's current
  lock, and sets the roll's new `refresh_pending` flag. The removal path
  inside the same run (`_remove_covered_negatives`) is covered by the same
  skip.
- **`roll refresh --roll DIR`** recomputes the lock with
  `highlight_lock.compute_roll_highlight_lock` (still its only writer), writes
  the record, runs `sync_previews(force=lock_changed)`, clears
  `refresh_pending`, and emits `roll_refreshed`.
- **`roll info`** reports `refresh_pending`.
- **The app runs `roll refresh`** when a session ends and its queue has
  drained, and whenever it opens the Edit or Export tab on a roll with
  `refresh_pending` set — so a crash mid-session never leaves stale colour.
- **An auto solve on a refresh-pending roll** (`edit tone --auto-grade`,
  `--auto-density`, `edit color --auto-cast`) warns `ROLL_REFRESH_PENDING`: it
  would read a lock the roll's newest negatives have not contributed to yet.

`edits.run_edit_delete` keeps recomputing the lock immediately; a delete is
never part of a capture session.

### 4.5 Failures and reshoots

- **A negative whose `prepare` fails** — a frame the decoder refuses, mixed
  exposure within the negative — never reaches the roll, because `prepare`
  records nothing there. Its tile shows the CLI's message; its frames stay,
  and it can be prepared again or reshot.
- **A negative that fails its check** never reaches the roll either. Its tile
  names the failed gate while the film is still at hand, and the next step is
  Reshoot. The queue does not stitch it: that would only record the same
  failure in the roll.
- **A negative whose stitch fails** — having passed its check, that means the
  overlap check — is recorded `failed` with its reason (`stitch_pipeline` sets
  the status) and its tile says why. Its frames **and
  its work folder** stay, so once the queue has drained, File > Re-stitch… can
  run it again without decoding the RAWs a second time.
- A work folder is removed when its negative publishes, is reshot, or is
  discarded.
- **A prepare or stitch refused for disk space** (`INSUFFICIENT_DISK`) is not a
  failed negative. `prepare` checks its own volume before writing anything;
  `run_stitch` checks the roll's volume after solving and raises before its
  first record write, so the roll is untouched. The queue holds the negative as
  waiting for space and stops starting prepares. If the waiting stitch still
  cannot fit, the queue deletes the work folders of the most recently prepared
  negatives, newest first — intermediates their NEFs can always rebuild —
  retrying the stitch after each, and prepares those negatives again later.
  Only if nothing is left to delete does the tile ask the user to free space,
  quoting the CLI's figure.
- **Reshoot** captures the negative again. New files are new sources, and
  overlap is keyed by source SHA-256, so the reshoot publishes as a **new**
  negative rather than adopting the failed one. When it publishes, the app
  offers to delete the failed record with `edit delete`, which accepts a
  negative of any status.
- A negative can also simply be run again from Add Scans against its capture
  folder; that is today's overlap-and-replace path, unchanged.

### 4.6 The backlog

Stitching may finish long after scanning does, and that is fine: nothing
waits on it, and the early check (§4.1) keeps the backlog from hiding
problems. How long a stitch takes is not a design input, so it is not
measured.

A long backlog needs three things from the app:

- **The Mac stays awake** while the queue has work. `StitchQueueModel` holds
  a `ProcessInfo` activity with `.idleSystemSleepDisabled` from the first
  queued negative until the queue drains; the display may still sleep.
- **The queue survives quitting the app.** `StitchQueueModel` records each
  negative's frames, work folder and step in a small state file in
  Application Support as it goes. On the next launch, a negative whose check
  passed goes straight to its stitch; any other goes back to `prepare`, which
  recovers or rebuilds its work folder.
- **Disk space.** Every negative prepared ahead of the stitcher holds its
  intermediates. The CLI's conservative estimate — uncompressed 16-bit RGB,
  though they are written Deflate-compressed — is about 147 MB per frame, so
  roughly 0.9 GB per 3×2 negative, and a whole roll's backlog can reach tens
  of GB on the capture folder's volume. There is no limit on it; running short
  is handled as waiting, never as a failed negative (§4.5).

Memory needs nothing. The composite's check compares its estimate with half
of physical RAM, not with free memory, so prepares running beside a stitch can
slow it but cannot make it fail `INSUFFICIENT_MEMORY`.

---

## 5. Whole-roll analyses

| Analysis | Computed today | Under per-negative background stitching | Plan |
|---|---|---|---|
| Film kind | Fixed at `roll init` | Unchanged | Nothing |
| Film base | `roll set-base-frame`; locked at first publish | Must be attached before the first negative's stitch | §1 shoots it first; Space waits for it |
| Camera colour | Seeded by the first run | Unchanged | Nothing |
| `clamp_bounds` | Per negative, against already-composited negatives on the roll (`_reference_bounds`) | Same as one big run in capture order — its first negative also clamps against nothing | Nothing; order dependence is existing behaviour (§11) |
| Highlight lock | Recomputed after every stitch; forces every preview to re-render when it changes | Recomputed after every negative: ≈n²/2 renders, earlier negatives change colour mid-session | Deferred to the end of the session (§4.4) |
| Sequence | Recomputed on every record write | Unchanged | Nothing |
| Auto tone / auto cast | At edit time, reading the lock | Would read a lock that lags the newest negatives | Warn until refreshed (§4.4) |
| Cross-negative exposure consistency | One `run` over a whole folder checked every frame together | Each run checks only its own negative | Exposure watch at capture time (§2.4); per-negative `FILM_BASE_EXPOSURE_MISMATCH`; the session summary lists mismatches (§6.4) |
| Focus drift across the roll | Not measured | New, incremental | §6.3 |
| Clipping across the roll | Per frame at prepare (`SCAN_CLIPPED`) | New per-frame check at capture time, incremental | §6.2 |

---

## 6. Per-frame analysis (CLI)

### 6.1 The command

```
scanny-boy capture analyze --frame FILE [--baseline FILE ...] --log FILE
```

emits one `frame_analyzed` event. The app sends it through a **dedicated
`serve` instance** as each file lands — resident, so a frame does not pay a
process launch — and never waits on the answer: a badge appears when it
arrives, and a slow analysis never delays a release. `serve` answers one
request at a time, so if analysis is slower than the interval, requests queue
and badges arrive late.

All constants live in the new module `capture_analysis.py`.

### 6.2 Clipping

**The thin end is already covered by the base frame.** Nothing on a negative
is thinner than its base (docs/REBATE_ANCHORING.md §0.4), and the base frame
is shot at the scans' exposure (§1.1 there). A base frame that clears
`FILM_BASE_CLIPPED` therefore means no picture content at that exposure clips
at the sensor's white point. The per-frame check is a guard against what the
base frame cannot see: an exposure change mid-roll, or bare light in the frame.

- **White point.** The fraction of pixels at or above
  `normalization.SCAN_CLIP_LEVEL`, per channel — the same measure the prepare
  stage records per frame (`measure_clip_fractions`) and warns on above
  `SCAN_CLIP_WARN`. At capture time it runs on a half-size decode for speed.
  Whether half-size preserves the fraction is not assumed: if LibRaw averages
  a block's two green sites, a half-clipped block reads below the clip level
  and green under-reports. T-6 compares the two fractions on real frames and
  records the agreement; if green diverges, the check reads the CFA's green
  sites directly instead. Bare light and sprocket holes clip legitimately; the
  badge says so.
- **Dense end.** The densest content of a negative (the scene's highlights)
  sits closest to the sensor's black level. The metric: each channel's 0.5th
  percentile, in stops above the black level, warned on below a provisional
  threshold (§6.5).

### 6.3 Focus

- **Measured on the green sites of the raw**, never on a demosaiced image —
  interpolation invents high-frequency detail.
- **On a regional grid** (`FOCUS_GRID`, 3×3 over the frame), so that film not
  lying flat or a camera not parallel to the film shows up as a pattern, not a
  single diluted number.
- **The metric is a ratio**: energy in a high spatial-frequency band over
  energy in a mid band. The ratio cancels local contrast, which is what makes
  grain-dominated regions comparable from frame to frame. Regions with too
  little texture (rebate, bare light) fall below `FOCUS_MIN_TEXTURE` and are
  excluded.
- **The baseline is the session's first negative**: the per-region median over
  its frames, passed as `--baseline`. Not the base frame, which is mostly
  featureless rebate.
- **Reported per frame**: the median region ratio relative to the baseline,
  and the spread across regions.

The honest limit: absolute scores do not compare across films. Drift within a
session — "has the focus wandered" — is what this measures, and it is the
question the user asked.

### 6.4 Where results go

The results shape no pixel, so they stay out of the roll record. `capture
analyze` appends each result to `--log`, `capture-log.jsonl` in the capture
folder — the CLI owns the format. At the end of a session,

```
scanny-boy capture summary --log FILE
```

emits `capture_summary`: frames analysed, frames over each threshold, the
focus trend across negatives, and the negatives whose EXIF exposure differs
from the base frame's.

### 6.5 Provisional thresholds

The user chose to ship the capture warnings without a measurement session and
to tune them by use. Every threshold lives in `capture_analysis.py`, marked
**provisional and unmeasured** the way ARCHITECTURE.md §8.1 marks the stitch's
unmeasured constants:

| Warning | Fires when | Constant |
|---|---|---|
| `CAPTURE_CLIPPED` | A channel's white-point fraction exceeds the prepare stage's own limit | `normalization.SCAN_CLIP_WARN`, imported, not redeclared |
| `CAPTURE_DENSE_END_LOW` | A channel's dense end sits fewer stops above black than the threshold | `DENSE_END_WARN_STOPS` |
| `CAPTURE_FOCUS_DRIFT` | The frame's median region ratio falls further below the baseline than the threshold | `FOCUS_DRIFT_WARN` |
| `CAPTURE_FOCUS_TILT` | The spread of ratios across regions exceeds the threshold | `FOCUS_TILT_WARN` |

T-6 picks the starting values and writes its reasoning beside each one. They
change only by editing that module.

**The accepted risk.** Using the app reveals a threshold that is too sensitive:
badges on frames that are fine. It cannot reveal one that is too loose. A soft
frame simply gets no badge, and the softness is found later, in the Edit tab
or on a print. This matters most for the focus thresholds, and the alignment
check (§4.1) does not stand in for them: it detects on images downsampled about
3×, where mild softness largely disappears.

---

## 7. CLI, contract, schema (protocol version 22)

### 7.1 Commands

```
scanny-boy capture analyze --frame FILE [--baseline FILE ...] --log FILE
scanny-boy capture summary --log FILE
scanny-boy capture check   --work DIR [--rig PROFILE_ID]
scanny-boy roll refresh    --roll DIR
scanny-boy run    ... [--defer-roll-refresh]
scanny-boy stitch ... [--defer-roll-refresh]
```

### 7.2 Events

| Event | Fields |
|---|---|
| `frame_analyzed` | `frame`; `clip_fractions` (per channel); `dense_end_stops` (per channel); `focus_regions` (the grid's ratios, `null` where excluded); `focus_relative` (`null` without a baseline); `focus_spread`; `warnings` (codes, §6.5) |
| `capture_checked` | `passed`; `code` and `message` (`null` when passed); `global_rms_px` (`null` when no layout solved); `used_clahe_fallback` |

`capture check` also emits `progress` events (stage: `stitch`) during the solve phase, the same as the standalone `stitch` command.
| `capture_summary` | `frames`; per-code counts; `focus_trend` (one value per negative); `exposure_mismatches` |
| `roll_refreshed` | `lock_changed`; `previews_regenerated` |

`roll info` gains `refresh_pending` on the roll.

### 7.3 Codes

| Code | Kind | When |
|---|---|---|
| `ROLL_BUSY` | error | A roll writer found the roll lock held (§4.3) |
| `ROLL_REFRESH_PENDING` | warning | An auto solve on a roll whose refresh is pending (§4.4) |
| `CAPTURE_CLIPPED` | warning | A channel clips beyond `SCAN_CLIP_WARN` (§6.5) |
| `CAPTURE_DENSE_END_LOW` | warning | A channel's dense end is too close to black (§6.5) |
| `CAPTURE_FOCUS_DRIFT` | warning | Focus has fallen below the session's baseline (§6.5) |
| `CAPTURE_FOCUS_TILT` | warning | Focus varies too much across the frame (§6.5) |

A frame that cannot be decoded reports the raw decoder's existing codes.
`capture check` reports its verdict with the stitch's own codes (§4.1) and adds
none.

### 7.4 Records and documents

- A library migration adds a nullable `refresh_pending` column to the rolls
  table, following `0014_highlight_lock.py`'s pattern.
- `CONTRACT.md`, `schema.json` and `roll-manifest.schema.json` gain the above.
- ARCHITECTURE.md gets the Capture tab (§13), the camera layer, the roll lock
  (§11), and the protocol-22 line (§3). DECISIONS.md gets this feature's
  decisions and the §4.2 amendment.

---

## 8. The Mac app

### 8.1 A Capture tab

`AppWorkspaceTab` gains `.capture`, first in the picker, before Add Scans.

### 8.2 Files

| File | Role |
|---|---|
| `Capture/TetherCamera.swift` | The actor: browser, session, connection states, PTP transactions, event handling, release, buffer scan, download |
| `Capture/PTP.swift` | Container framing and dataset decoders (DeviceInfo, ObjectInfo, StorageInfo, events, CheckEvent) |
| `Capture/TetherTiming.swift` | Every timing constant in §2–§3 |
| `Capture/CaptureNaming.swift` | The only place a capture file name is chosen |
| `Model/CaptureSessionModel.swift` | Setup, the sequence state machine, the interval clock, cues, keys |
| `Model/StitchQueueModel.swift` | The queue: `prepare` then `capture check` under `MAX_PARALLEL_PREPARES`, serial `stitch` in capture order, waiting for disk, work folders, the sleep assertion, the state file, `roll refresh` at the end |
| `Views/CaptureStageView.swift` | The tab |
| `Views/CaptureMiniView.swift` | The grid |
| `Views/CaptureQueueList.swift` | The session's negatives: one row per negative with status, progress bar, elapsed time, and stitched-TIFF thumbnail |

Changes: `AppActivity` (§4.2), `SettingsView` (capture folder, destination,
cues), `CLIRunner` (the new commands and a second daemon).

### 8.3 Tests

No test touches a camera; CI has none.

- **`PTP.swift`** decodes bytes recorded from the probe runs, committed as
  fixtures: the 577-byte DeviceInfo, event containers, a CheckEvent payload,
  and ObjectInfo for a buffer frame and a card frame.
- **`TetherCamera`** sits behind a `CameraControlling` protocol. A fake
  replays §0.1's behaviours: the late `ObjectAddedInSdram`, a dropped device,
  leftovers in the buffer, a buffer that fails to clear.
- **`CaptureSessionModel`** runs against the fake with an injectable clock:
  interval timing, the download-wait rule, pause and retake, stop and resume.
- **`StitchQueueModel`** runs against fake CLI sessions: prepares never exceed
  the cap; stitches never overlap and run in capture order even when checks
  finish out of order; a failed prepare or check is never stitched; an
  `INSUFFICIENT_DISK` refusal waits and frees prepared work folders newest
  first; the state file restores the queue; refresh at drain.

---

## 9. Chunks

### T-1 — the camera layer

`PTP.swift`, `TetherCamera`, `TetherTiming`, the `CameraControlling` protocol
and its fake, with fixture tests. Verifies `0x90C3` on the Z f and that
ImageCaptureCore works inside the ad-hoc-signed app bundle (the probe ran from
Terminal). No UI.

### T-2 — the roll lock

`roll_lock.py`, taken by every writer in §4.3, and `ROLL_BUSY`. Independent of
tethering and protective of existing users, so it can land first. Fast-tier
tests hold the lock in one process and assert the second writer fails before
touching anything.

### T-3 — deferred refresh

`--defer-roll-refresh`, `roll refresh`, the migration, `refresh_pending` in
`roll info`, `ROLL_REFRESH_PENDING`. The protocol bump to 22 lands here, with
the contract and schema. Tests: a deferred run leaves the lock and older
previews untouched and sets the flag; `roll refresh` produces exactly what an
undeferred run would have.

### T-4 — the Capture tab

Setup, connection states, the base-frame capture into `roll set-base-frame`,
the sequence engine, cues, keys, the mini-view, naming and the capture folder.
Files land on disk; nothing stitches yet — Add Scans can open the capture
folder, which is a usable feature on its own.

Done when a 36-release sequence on the Z f at the default interval lands every
frame without a retake.

### T-5 — the stitch queue

`capture check` in the CLI, confirming the solve needs no real roll record,
with `capture_checked` joining protocol 22's contract changes (§7).
`StitchQueueModel` with all three steps — `prepare` then `capture check` under
the cap, serial `stitch` in capture order — plus work-folder cleanup, waiting
for disk, the sleep assertion and the state file; the `AppActivity`
amendment; the session strip; reshoot and failed-record deletion; and
`roll refresh` at session end and on opening Edit or Export. Requires T-2 and
T-3.

### T-6 — per-frame analysis and warnings

`capture_analysis.py`, `capture analyze`, `capture summary`, the dedicated
`serve` instance, the badges, the capture log. Verifies half-size clip
fractions against `measure_clip_fractions`. Picks the provisional thresholds
(§6.5) and turns the `CAPTURE_*` warnings on.

### T-7 — resilience and card mode

The exposure watch's pause (§2.4), dropped-connection recovery and leftovers
(§2.5), and card mode with slot-1 dedup (§2.3). Split from T-4 to keep that
chunk reviewable; it must land before the feature is called done.

---

## 10. Rejected alternatives

- **`requestTakePicture()`.** It does nothing on the Z f.
- **libgphoto2.** Its runtime-loaded drivers are the hardest thing to package
  and sign, it fights `ptpcamerad` for the device, and ImageCaptureCore's PTP
  passthrough covers everything it offered here.
- **Nikon's SDK.** Registration-gated C++ with a bridging layer, for no
  capability the passthrough lacks.
- **ImageCaptureCore's `didAdd` as the arrival signal.** New items read
  `wasAddedAfterContentCatalogCompleted == false`, and buffer frames never
  arrive through it.
- **Waiting on `ObjectAddedInSdram` alone.** On one of the two shots where the
  queue was polled, it did not arrive within 8 s.
- **Overlapping stitches, as the code stands.** They overwrite each other's
  record writes and clean up each other's staging (§0.6).
- **Several negatives composited inside one process.** "Parallelism never
  spans negatives" is a memory decision (ARCHITECTURE.md §11): NumPy does not
  return freed memory to the OS, and the 3.5× safety factor was measured one
  negative at a time. Separate processes give their memory back when they
  exit.
- **One `run` for the whole roll at the end.** It defeats the point: a failure
  would surface after the roll is shot and the film is put away.
- **A parallel stitch stage.** Computing against a read-only copy and
  committing under the lock would let stitches overlap, at the price of a
  `stitch_pipeline` refactor and a clamp whose reference negatives depend on
  timing. The user does not need stitching to keep up with capture, so the
  backlog is accepted instead (§4.6).
- **Limiting how many negatives are prepared ahead of the stitcher.** It would
  bound the backlog's disk use; the user prefers no limit, and running short
  is handled as waiting (§4.5).
- **A low-resolution trial composite to predict the overlap check.** The user
  would rather reshoot the occasional negative that passes its check and fails
  the overlap check than add an approximate gate.
- **Measuring stitch time before building.** It mattered only while the design
  depended on the queue keeping up.
- **A measurement session before turning the capture warnings on.** The user
  chose to tune the thresholds by use instead, accepting that a too-loose
  focus threshold does not reveal itself (§6.5).
- **Recording to card by default.** Two copies of every frame on the cards,
  and 0.5 s slower. Kept as a setting.
- **The interval measured from the release.** A long exposure would eat the
  operator's time to move.
- **Sequential `NNN` file names.** Choosing the next number means enumerating
  the capture folder; the timestamp needs nothing.
- **The base frame as the focus baseline.** It is mostly featureless rebate.
- **Analysis in Swift.** It would split a judgement across two
  implementations and could not share `SCAN_CLIP_LEVEL`.

---

## 11. Risks and open questions

- **A negative can pass its check and still fail the overlap check** in its
  stitch, late. Accepted: it is reshot (§4.5). How often that happens is
  unknown until real sessions show it.
- **A whole roll's backlog can hold tens of GB of intermediates** (§4.6). A
  volume too small for it pauses the queue and frees rebuildable work folders
  rather than failing a negative (§4.5).
- **A focus threshold that is too loose goes unnoticed** (§6.5). Soft frames
  get no badge, and nothing in normal use says so.
- **Only the Z f on firmware V2.00 is tested.** `unsupported` (§2.1) refuses a
  body missing a required operation, but a body with the operations and
  different behaviour is not detected.
- **The buffer scan assumes handles restart at `0x0B000001`** once the buffer
  empties, as observed. Frames piling up past `BUFFER_SCAN_LAST` cannot happen
  while every release waits for the previous download; if it ever does, the
  sequence stops.
- **Buffer capacity, and what power-off does to an undownloaded frame**, are
  both untested. The download-wait rule keeps the first from arising; §2.5's
  leftover handling covers what survives the second.
- **The link negotiated USB 2.** Download is 0.55 s of the 1.7 s; a USB 3
  cable shortens it and changes nothing else.
- **ImageCaptureCore's privacy prompts inside the app bundle are unverified.**
  T-1 checks them.
- **`clamp_bounds` depends on order**: a roll's first negative clamps against
  nothing. That is true of every roll today and is not made worse.
- **A pending refresh after a crash** is covered by `refresh_pending` (§4.4).
- **Focusing is left to the operator's eye.** Live view in the app, with a
  sharpness meter and a check shot for the corners, is planned separately in
  `docs/FOCUS_ASSIST_PLAN.md` on the measurements in §0.9.
