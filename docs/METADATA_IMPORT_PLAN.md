# Frame log metadata import

Status: implemented (phases 0–6, §8). The Swift phases still need a build and test run on a Mac.

## 1. Problem

Rolls and frames are logged while shooting in a separate app, *Frame log*. That app exposes a read-only export API. This feature lets the user:

1. pick a Frame log roll and link it to a Scanny Boy roll;
2. fetch that roll's frames;
3. line the frames up with the roll's negatives in a dedicated sheet;
4. write the frames' metadata into the library database.

Export (§7) then embeds the metadata in the JPEG XL files. As everywhere else in the app, the metadata lives in the database and only export touches files (`ARCHITECTURE.md` §9.1).

The two sources rarely line up one-to-one:

- scans may omit frames the log has, and the log may omit frames that were scanned;
- some logged frames carry little or no metadata.

Both sources are in shooting order, though. **A valid alignment is always monotonic**: a later negative never pairs with an earlier frame. The UI and the validation both rely on that.

This doc uses "frame" for a Frame log frame and "negative" for a Scanny Boy negative. Remember that a Scanny Boy *frame* is a single capture, several of which stitch into one negative.

## 2. The Frame log API (summary)

- **Base path**: `<base-url>/api/framelogger/export/v1`. The default base URL is `https://lonnie-smith.com`.
- **Auth**: every request carries `Authorization: Bearer fl_<43 base64url chars>`.
- **Endpoints**: `GET` only, JSON responses.

**`GET /rolls?status=all|active|finished`** returns `{"rolls": [...]}`. Each roll has:

- `uid` (10 chars, `[0-9a-z]`), `rollNumber`
- `film`, `camera` (plain strings)
- `iso` (number or null)
- `createdAt`, `finishedAt`
- `frameCount`, `lastFrame`
- `firstCapturedAt`, `lastCapturedAt`

Active rolls come first, then rolls by number, descending.

**`GET /rolls/:ref`** takes a roll number or a uid and returns `{"apiVersion": 1, "roll": {...}, "frames": [...]}`.

- The roll has the fields above, except that `film` and `camera` are `{name, catalogId}` objects (`camera` may be null). It adds `missingFrameNumbers`: the numbers in `1..lastFrame` that have no logged frame.
- Frames come in `frameNumber` order. Each frame has:
  - `uid`, `frameNumber`
  - `capturedAt`: `{utc, local, offset, timeZone, timeZoneSource, precision}` or null. `precision` is `instant` or `date`. With `date`, `local` is just `YYYY-MM-DD` and `offset` is null.
  - `lens`: `{name, catalogId}` or null
  - `position`: `{latitude, longitude, accuracyM, source, approximate}` or null
  - `place`: `{locationId, locationName, locationTag, placeName, address}`. Always present, but every field may be null.
  - `notes`: string or null

**Statuses**: `401` (token missing, bad, revoked or unknown), `403`, `404` (`Roll not found`), and `429` with a `Retry-After` header.

New fields may be added within v1. Ignore any field this doc does not name.

## 3. Decisions

| Decision | Choice |
|---|---|
| Who does HTTP, mapping and default alignment | The Python CLI, via new `framelog` subcommands. Swift owns the UI and the token. |
| Token storage | The macOS Keychain, separate per `AppEnvironment` profile. Passed to the CLI only through the `SCANNY_BOY_FRAMELOG_TOKEN` environment variable, never in argv. |
| Conflicts | **Always overwrite.** An imported value replaces the existing one. An imported null or empty value leaves the existing value alone. |
| Unmatched negatives | Left untouched, except that their `framelog_frame_uid` is cleared when the user explicitly sets them to "no match" (§6.3). |
| `notes` | Overwrites the negative's `caption`. |
| City / state | Parsed from `place.address` when it is US-style (§5.2); otherwise not imported. |
| Default alignment | Negative at sequence rank N ↔ frame number N. A re-import of the linked roll reuses stored frame uids (§5.3). |
| Alignment UI | One row per negative. Changing a row's frame shifts every later matched row by the same amount. |

## 4. Storage (phase 1)

### 4.1 Database: migration `0022_framelog_import.py`

| Table | New column | Type |
|---|---|---|
| `rolls` | `framelog_roll_uid` | TEXT NULL |
| `rolls` | `framelog_roll_number` | INTEGER NULL |
| `negatives` | `framelog_frame_uid` | TEXT NULL |
| `negatives` | `latitude`, `longitude`, `gps_accuracy_m` | REAL NULL |
| `negatives` | `location_name`, `address` | TEXT NULL |

`capture_time` is already a JSON column on `negatives`, so the two new capture-time keys (§4.2) need no new columns. Older rows simply lack the keys, and they must read back as `None`.

### 4.2 Manifest (`roll_manifest.py`, `roll-manifest.schema.json`, Swift `RollManifest.swift`)

- **`RollMetadata`** gains `framelog_roll_uid: str | None` and `framelog_roll_number: int | None`.
- **`CaptureTime`** gains:
  - `logged_datetime_original: str | None`, a local wall time in `YYYY-MM-DDTHH:MM:SS` form
  - `logged_offset: str | None`, in `±HH:MM` form
- **`NegativeRecord`** gains:
  - `framelog_frame_uid: str | None`
  - `place: NegativePlace`, a new dataclass with `latitude`, `longitude`, `gps_accuracy_m`, `location_name` and `address`, all nullable.

  These fields belong to the negative only. They are **not** part of `NegativeMetadata`, because they have no roll-level fallback.

Every new key is required-nullable in the schema, as the existing metadata keys are. Swift decodes each one with `decodeIfPresent`, so a manifest from an older helper still decodes.

### 4.3 Capture time (`roll_sequence.py`)

`intended_times` changes as follows:

- A negative with `logged_datetime_original` uses that time, parsed with `datetime.fromisoformat`, as its intended time.
- Such a negative does **not** consume a rank on its date. Rank-based negatives on the same date rank among themselves only.
- Logged times are returned even when the roll has no `roll_capture_date`. Only the rank-based negatives need that date.

`sequence_negatives` is unchanged. Display order still comes from the source NEF capture time.

### 4.4 `metadata set` keys (`metadata_edit.py`)

**New roll keys**

- `framelog_roll_uid`: matches `^[0-9a-z]{10}$`
- `framelog_roll_number`: an integer ≥ 1. Reject a bool, even though it passes an `int` check.

**New negative keys**

| Key | Validation |
|---|---|
| `framelog_frame_uid` | `^[0-9a-z]{10}$` |
| `latitude` | a number in −90…90 |
| `longitude` | a number in −180…180 |
| `gps_accuracy_m` | a number ≥ 0 |
| `location_name`, `address` | strings, at most 500 chars |
| `logged_time` | `YYYY-MM-DDTHH:MM:SS`, and must parse as a date and time |
| `logged_offset` | `^[+-]\d{2}:\d{2}$` |

Most of these keys write to the field of the same name. `logged_time` and `logged_offset` write to `capture_time.logged_datetime_original` and `capture_time.logged_offset`.

The existing semantics hold: an absent key means unchanged, and `null` or `""` clears the field. **Cross-field rules** are checked on the merged result, and any violation fails the whole payload with `INVALID_METADATA`:

- `latitude` and `longitude` are either both set or both null.
- `logged_offset` requires `logged_time`.
- Setting a negative's `capture_date` (its date override) also clears its `logged_time` and `logged_offset`, unless the same payload sets them too. That keeps a manual date edit authoritative.
- Setting `logged_time` also clears the negative's `capture_date` override, unless the same payload sets it.
- A payload that carries `logged_time` (set or cleared) without `logged_offset` clears the stored offset. An offset belongs to the time it was logged with.

None of the new keys feed the metadata-values catalog. After writing, `apply_intended_times` runs, as it does today.

## 5. CLI (phase 2)

### 5.1 Modules

The package is flat, so these are plain modules beside `metadata_edit.py`, with colocated `*_test.py` tests:

- **`framelog_client.py`**: built on `urllib.request`. Do not add `requests`, because the PyInstaller bundle must not grow.
  - `FrameLogClient(base_url, token, opener=urllib.request.urlopen)`, with methods `list_rolls(status)` and `get_roll(ref)`.
  - A 15 s timeout and an `Accept: application/json` header.
  - A failed request raises `FrameLogError(code, message)` with one of the codes in §5.5.
  - `get_roll` checks that `apiVersion == 1`.
- **`framelog_mapping.py`**: pure functions with no I/O.
  - `map_roll(api_roll, frames) -> dict` builds the `metadata set` roll payload (§5.2).
  - `map_frame(api_frame) -> dict` builds the negative payload (§5.2).
  - `parse_us_address(address) -> (city, state) | None`.
  - `display_frame(api_frame) -> dict` builds a `framelog_roll` frame entry (§5.4).
- **`framelog_align.py`**: `default_alignment(manifest, api_roll, frames) -> list[(negative_id, frame_number | None)]` (§5.3).
- **`framelog_import.py`**: the three command runners, wired into `cli.py` the way `metadata set` is.

The test fixtures (§9) live in `tests/fixtures/framelog/`.

### 5.2 Mapping

**`map_frame`**

| Frame log field | Negative key | Rule |
|---|---|---|
| `uid` | `framelog_frame_uid` | always |
| `capturedAt.local` + `offset`, when `precision == "instant"` | `logged_time`, `logged_offset` | `offset` may be null; then omit `logged_offset` |
| `capturedAt.local`, when `precision == "date"` | `capture_date` | `YYYY-MM-DD`; omit `logged_time` |
| `lens.name` | `lens` | |
| `notes` | `caption` | stripped |
| `position.latitude`, `longitude`, `accuracyM` | `latitude`, `longitude`, `gps_accuracy_m` | |
| `place.locationName`, else `place.placeName` | `location_name` | |
| `place.address` | `address` | |
| `parse_us_address(place.address)` | `city`, `state` | only when it parses |

A source value that is null, missing, or blank after stripping means the key is **absent** from the payload. Import never clears a field.

**`parse_us_address`** matches the last components of the address as `…, <City>, <ST> <ZIP5>[-<ZIP4>]`, optionally followed by `, USA` or `, United States`.

- `ST` is two uppercase letters, and the result is `(city, ST)`.
- Anything else returns `None`. That includes a missing street, a non-US address, or `IL` without a ZIP.

**`map_roll`**

| Roll payload key | Source |
|---|---|
| `film` | `film.name` |
| `camera` | `camera.name` |
| `iso` | `str(iso)` |
| `framelog_roll_uid` | `uid` |
| `framelog_roll_number` | `rollNumber` |
| `capture_date` | the date part of the first frame (in `frameNumber` order) whose `capturedAt` is not null |

The same absent-when-null rule applies.

### 5.3 Default alignment

Negatives are taken in `roll_sequence.sequence_negatives` order. A negative's rank is its 1-based index in that order.

1. Check whether the roll is linked to this Frame log roll (`framelog_roll_uid == api_roll.uid`) **and** at least one negative's `framelog_frame_uid` matches a frame of this roll. If both hold, use the stored pairing:
   - each negative whose uid matches gets that frame's `frameNumber`;
   - every other negative gets `None`;
   - then drop any stored pair that would break strict monotonic order, keeping the earlier one.
2. Otherwise, the negative at rank N gets frame number N when `1 <= N <= lastFrame`, else `None`.

   A number listed in `missingFrameNumbers` is still assigned. It shows as "no metadata", and it keeps the later rows positional. `apply` writes nothing for it (§5.4).

### 5.4 Commands

```
scanny-boy framelog rolls [--status all|active|finished] [--base-url URL]
scanny-boy framelog roll  --roll DIR --ref UID|NUMBER [--base-url URL]
scanny-boy framelog apply --roll DIR --ref UID --assignments JSON [--base-url URL]
```

All three read the token from `SCANNY_BOY_FRAMELOG_TOKEN`. They run one-shot and never through `serve`: the Swift side keeps them out of `routesThroughDaemon`.

**`framelog rolls`** emits `framelog_rolls`. It carries `rolls`, an array of these fields, each passed through from the API under a snake-case name:

- `uid`, `roll_number`, `film`, `camera`, `iso`
- `created_at`, `finished_at`
- `frame_count`, `last_frame`
- `first_captured_at`, `last_captured_at`

**`framelog roll`** emits `framelog_roll`, carrying:

- **`roll`**:
  - `{uid, roll_number, film, camera, iso, created_at, finished_at, frame_count, last_frame, missing_frame_numbers}`
  - `film` and `camera` are flattened to their `name`
  - `last_frame` is `0` when the API gives null
- **`frames`**, in frame order. Each entry is:
  - `uid`, `frame_number`
  - `captured_local`: string or null
  - `precision`: `instant`, `date` or null
  - `offset`, `lens`, `caption`
  - `location_name`, `address`, `city`, `state`
  - `has_position`: bool
- **`alignment`**: `[{negative_id, frame_number | null}]`, one per sequenced negative, in sequence order (§5.3).
- **`linked`**: true when the roll's `framelog_roll_uid` already equals this uid.

**`framelog apply`**

`--assignments` is a JSON object, `{negative_id: frame_number | null}`. It is validated before any fetch:

- every key is a negative of the roll; otherwise `NEGATIVE_NOT_FOUND`;
- every number is an int in `1..lastFrame`; otherwise `INVALID_METADATA` (checked after the fetch, because `lastFrame` comes from it);
- the non-null numbers strictly increase in sequence order, with no duplicates; otherwise `INVALID_METADATA`.

Then the command re-fetches `GET /rolls/<uid>` and builds **one** `metadata set` payload:

- `roll` is `map_roll(...)`.
- For each assigned negative whose number is a logged frame, the negative's payload is `map_frame(frame)`.
- For each negative assigned `null`, or a number with no logged frame (one in `missingFrameNumbers`), the payload is `{"framelog_frame_uid": null}`, and only when the negative currently has one.
- Negatives absent from `--assignments` are omitted.

It runs that payload through the `run_metadata_set` path, so the write is atomic, all-or-nothing. It emits the existing `metadata_updated` event carrying the manifest.

### 5.5 Error codes

The codes go in the stable error code table in `CONTRACT.md`. Every one is a fatal `error` event.

| Code | When |
|---|---|
| `FRAMELOG_NO_TOKEN` | The `SCANNY_BOY_FRAMELOG_TOKEN` environment variable is unset or empty |
| `FRAMELOG_UNAUTHORIZED` | HTTP 401 or 403 |
| `FRAMELOG_NOT_FOUND` | HTTP 404 |
| `FRAMELOG_RATE_LIMITED` | HTTP 429. The message includes the `Retry-After` seconds when present |
| `FRAMELOG_UNREACHABLE` | DNS, connection, TLS or timeout failure |
| `FRAMELOG_BAD_RESPONSE` | Any other status, a non-JSON body, a missing required field, or `apiVersion != 1` |

Other changes in this phase:

- `PROTOCOL_VERSION` goes from 28 to 29 (`events.py`), and Swift's expected version changes to match.
- `schema.json` gains the `framelog_rolls` and `framelog_roll` event schemas.

## 6. Swift (phases 4 and 5)

### 6.1 Credentials and settings (phase 4)

- **`mac/ScannyBoy/Model/FrameLogCredentials.swift`** defines a `FrameLogTokenStore` protocol with `token() -> String?`, `setToken(_:)` and `clear()`. It has two implementations:
  - **`KeychainFrameLogTokenStore`**:
    - a `kSecClassGenericPassword` item whose service is `com.lonniesmith.scanny-boy.framelog`, with a `.dev` suffix in the development profile
    - account `token`
    - accessibility `kSecAttrAccessibleAfterFirstUnlock`
  - **`InMemoryFrameLogTokenStore`**, used by tests and the `.test` profile.

  `AppEnvironment` picks the store for the active profile.
- **`FrameLogSettings`** holds `baseURL`, stored in `AppEnvironment.defaults` under the key `framelogBaseURL`, with the default `https://lonnie-smith.com`.
- **`SettingsView`** gets a new "Frame log" section containing:
  - a base URL text field;
  - a token `SecureField`. Once a token is saved, the section shows only its `fl_` + 8-char prefix and a Remove button;
  - a "Test connection" button. It runs `framelog rolls --status active` and shows either "Connected — N rolls" or the error's message.
- **`CLIRunner.swift`** gets:
  - the `CLICommand.framelogRolls(status:baseURL:)`, `.framelogRoll(roll:ref:baseURL:)` and `.framelogApply(roll:ref:assignments:baseURL:)` builders;
  - a way to give a single one-shot command extra environment variables, if `CLISession` cannot already. The token goes there.

  The new commands must not match `routesThroughDaemon`.
- **`CLIEvent.swift`** decodes the `framelog_rolls` and `framelog_roll` events.

### 6.2 Import model (phase 5): `mac/ScannyBoy/Model/FrameLogImportModel.swift`

This is an `@Observable` model that holds:

- the roll list, the selected roll ref, and the fetched `framelog_roll` payload;
- `rows: [Row]`. Each row has `negativeID` and `frameNumber: Int?`, and the rows are in sequence order;
- the phase (`choosingRoll`, `loading`, `aligning`, `applying`, or `failed(message)`).

The **alignment operations** are pure. They live in a value type, `FrameLogAlignment`, which the unit tests exercise directly. Its invariant: **the non-nil `frameNumber`s strictly increase down the rows, and each lies in `1...lastFrame`.**

- **`options(forRow r)`**: `(p+1)...lastFrame`, where `p` is the nearest non-nil frame number above row `r` (0 if there is none). The picker offers exactly these numbers, plus "No match".
- **`setFrame(row r, to f)`**:
  - If the row already had a number `old`, every later non-nil row shifts by `f - old`.
  - If the row was nil, it is set to `f`. Then, walking down the later rows, any non-nil value that is ≤ the previous non-nil value becomes previous + 1. This pushes later rows forward only as far as needed.
  - In both cases, a later value that ends up above `lastFrame` becomes nil.
- **`setNoMatch(row r)`**: sets the row to nil. Other rows are unchanged.
- **`reset()`**: restores the CLI's `alignment`.
- **Derived values** for display:
  - `skipped(beforeRow r)`, the count of frame numbers jumped since the previous non-nil row;
  - the set of frame numbers no row uses.

**Import** builds `{negativeID: frameNumber | null}` covering every row and runs `framelog apply`. On `metadata_updated`, it hands the manifest to `EditModel` the same way `enqueueMetadataSet` does, then dismisses the sheet.

### 6.3 The sheet (phase 5): `mac/ScannyBoy/Views/FrameLogImportSheet.swift`

It follows the `NewRollSheet` structure: `@Environment(\.dismiss)`, Cancel, and a default-action button. Minimum size is about 720×520.

**Step 1, choose a roll**

- A list of rolls from `framelog rolls`. Each row shows:
  - `#rollNumber`
  - film and camera
  - the first to last capture date range
  - the frame count, with an "active" badge on active rolls
- The linked roll (the manifest's `framelog_roll_uid`) is preselected and marked "Linked".
- Rolls whose `frame_count` is within ±3 of the negative count get a subtle "likely match" mark.
- **Next** fetches `framelog roll`.

**Step 2, align**

The header shows the roll summary and two counts: negatives matched, and frames unused. Below it, one row per negative:

- the thumbnail, reusing the source `MetadataStageView`'s filmstrip thumbnails;
- the sequence number;
- a frame `Picker`, offering `options(forRow:)` plus "No match";
- the incoming summary: local time or date, lens, location name, caption snippet, and a GPS glyph. Rows with no data show "No metadata in log", and unmatched rows show "No match — left unchanged";
- a "⇣ n skipped" marker when `skipped(beforeRow:) > 0`.

There is also a **Reset alignment** button, and a **Back** button that returns to step 1.

**Import** is the default action. A footer note reads: "Imported values replace existing ones."

There are two **entry points**:

- an "Import from Frame log…" button in `MetadataStageView`'s roll section;
- the same item in the File menu, through a new `Notification.Name.scannyBoyRequestFrameLogImport`.

Both are enabled only when a roll is open and a token is saved. `ContentView` presents the sheet from a `ViewModifier`, because `ContentView.body` is at the type checker's limit.

**Read-only display in `MetadataStageView`** (phase 5, small):

- When the selected negative has a `logged_datetime_original`, show it, with its offset, under the date picker.
- When it has a `place`, show the location name, the address, and a GPS glyph that opens Maps with the coordinates. Leave the existing city and state fields as they are.

## 7. Export (phase 3): `export_metadata.py`

**`ExportMetadata` gains:**

- `offset_time_original`, from `capture_time.logged_offset`, used only when `logged_datetime_original` is set;
- `latitude`, `longitude`, `gps_accuracy_m`;
- `location_name`, `address`;
- `framelog_roll_uid`, `framelog_frame_uid`.

`DateTimeOriginal` already comes from `intended_datetime_original`, which equals the logged time after §4.3, so it needs no change.

**`build_exif`:**

- Write `OffsetTimeOriginal` (tag 36881, ASCII `±HH:MM`) when it is set.
- When `latitude` and `longitude` are set, write a GPS IFD (pointer tag 34853) containing:
  - `GPSVersionID` `2.3.0.0`
  - `GPSLatitudeRef` `N` or `S`, and `GPSLatitude` as three RATIONALs (degrees, minutes, seconds)
  - `GPSLongitudeRef` `E` or `W`, and `GPSLongitude`, in the same form
  - `GPSHPositioningError` (RATIONAL), when the accuracy is set

  Express seconds with a denominator of 10000 so precision survives.

**`build_xmp`:**

- `Iptc4xmpCore:Location`, from `location_name`;
- `scannyboy:address`, `scannyboy:framelogRollUid` and `scannyboy:framelogFrameUid`;
- `exif:GPSLatitude` and `exif:GPSLongitude`, in the XMP `DDD,MM.mmmmk` form.

Declare the `Iptc4xmpCore` namespace `http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/` and the `exif` namespace `http://ns.adobe.com/exif/1.0/`.

## 8. Phases and delegation

| # | Phase | Implementer | Gate |
|---|---|---|---|
| 0 | This doc, plus the contract additions | Opus | — |
| 1 | §4 storage and Swift model decoding | Sonnet | `uv run pytest` |
| 2a | §9 fixtures | Haiku | — |
| 2b | §5 CLI | Sonnet | `uv run pytest` |
| 3 | §7 export | Sonnet | `uv run pytest --slow` |
| 4 | §6.1 credentials, settings and CLI bridge | Haiku | Swift: `./scripts/test-mac.sh` on the Mac |
| 5 | §6.2 and §6.3 import model and sheet | Sonnet | Swift: `./scripts/test-mac.sh` on the Mac |
| 6 | Review, then `ARCHITECTURE.md` §9.1 and §13 | Opus reviews, Haiku writes | Full fast suites |

## 9. Test fixtures: `tests/fixtures/framelog/`

- **`rolls.json`**: a `GET /rolls` response with three rolls. One is active with `finishedAt: null`, one is finished, and one has `camera: null`, `iso: null` and `lastFrame: null`.
- **`roll_42.json`**: the API doc's example roll, extended to six frames numbered 1, 2, 3, 5, 6 and 7, with `missingFrameNumbers: [4]` and `lastFrame: 7`. It covers:
  - an `instant` frame with full GPS and a US address;
  - a `date`-precision frame;
  - a frame with `capturedAt: null`, `position: null` and an all-null `place`;
  - a frame with a non-US address (`12 Rue de Rivoli, 75004 Paris, France`);
  - a frame whose position comes from `source: "location"` and `approximate: true`;
  - a frame with blank `notes` (`"   "`).
- **`roll_empty.json`**: a roll with no frames, `frameCount: 0`, `lastFrame: null` and `missingFrameNumbers: []`.
