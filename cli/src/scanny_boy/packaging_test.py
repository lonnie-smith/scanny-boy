"""Chunk 7: the packaged command-line program (section 5.2).

Every check here runs the frozen `ScannyBoyCLI.app`, never the development
import path, because the two failures section 5.2 warns about — `tifftools`
reading its own package metadata, and `imagecodecs` loading codecs through
delayed imports — produce a perfectly clean build and fail only at run time.
That is also why the conversions below decode real NEFs and write real
TIFFs instead of anything synthetic: the horizontal predictor is reached
only by a genuine Deflate write.

The packaged runs are module-scoped fixtures. Each one decodes three
24.5 MP frames, so re-running a conversion per assertion would cost minutes
for no extra coverage.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import plistlib
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import tifffile

from scanny_boy.icc_profile import PROFILE_SHA256
from scanny_boy.output_folder import STAGING_SUFFIX
from scanny_boy.packaged_app_support import (
    BUNDLE_EXECUTABLE,
    BUNDLE_PATH,
    DEV_VENV,
    requires_packaged_app,
    run_packaged,
)
from scanny_boy.pipeline import run_convert
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
    REAL_SAMPLE_FILES,
    requires_real_samples,
)
from scanny_boy.tiff_fingerprint_support import tiff_fingerprint

pytestmark = [
    requires_packaged_app,
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason="the packaged helper is a macOS .app bundle; nothing was tested",
    ),
]

VERSION = importlib.metadata.version("scanny-boy")
DEFLATE_COMPRESSION = 32946  # Adobe Deflate, per section 3.4
HORIZONTAL_PREDICTOR = 2

# System locations a bundled library is allowed to come from. Anything else
# must live inside the bundle itself.
SYSTEM_LIBRARY_PREFIXES = ("/usr/lib/", "/System/")


@dataclass(frozen=True)
class PackagedRun:
    out_dir: Path
    events: list[dict]
    stderr: str
    returncode: int


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _convert_packaged(
    out_dir: Path, jobs: int, env: dict[str, str] | None = None
) -> PackagedRun:
    result = run_packaged(
        "convert",
        "--input",
        str(FIXTURES_DIR),
        "--files",
        *NEGATIVE_1,
        "--out",
        str(out_dir),
        "--per-negative",
        "3",
        "--jobs",
        str(jobs),
        env=env,
    )
    return PackagedRun(
        out_dir=out_dir,
        events=_events(result.stdout),
        stderr=result.stderr,
        returncode=result.returncode,
    )


@pytest.fixture(scope="module")
def packaged_serial_run(tmp_path_factory) -> PackagedRun:
    """A packaged `--jobs 1` conversion of the first negative.

    `DYLD_PRINT_LIBRARIES` makes dyld name every library the process loads,
    which is how `test_no_library_is_loaded_from_the_development_venv`
    proves the bundle is self-contained at run time rather than by reading
    the build's file list.
    """
    out_dir = tmp_path_factory.mktemp("packaged-serial")
    return _convert_packaged(out_dir, jobs=1, env={"DYLD_PRINT_LIBRARIES": "1"})


@pytest.fixture(scope="module")
def packaged_threaded_run(tmp_path_factory) -> PackagedRun:
    out_dir = tmp_path_factory.mktemp("packaged-threaded")
    return _convert_packaged(out_dir, jobs=4)


@pytest.fixture(scope="module")
def development_run(tmp_path_factory) -> Path:
    """The same conversion through the development import path, for the
    comparison the chunk requires."""
    out_dir = tmp_path_factory.mktemp("development")
    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="development",
        jobs=1,
        emit=lambda event: None,
    )
    assert outcome.status == "complete"
    return out_dir


# -------------------------------------------------------------------------
# The bundle itself
# -------------------------------------------------------------------------


def test_codesign_verify_strict_succeeds_for_the_helper_bundle():
    """PyInstaller ad-hoc signs the bundle. Xcode's Code Sign On Copy phase
    in Chunk 8 re-signs it, and a bundle that fails here fails there."""
    result = subprocess.run(
        ["codesign", "--verify", "--strict", "--verbose=1", str(BUNDLE_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "valid on disk" in result.stderr


def test_helper_bundle_is_background_only_with_a_unique_identifier():
    """Section 5.2: a unique helper identifier and `LSBackgroundOnly`, so
    the helper never appears in the Dock."""
    plist = plistlib.loads((BUNDLE_PATH / "Contents" / "Info.plist").read_bytes())

    assert plist["CFBundleIdentifier"] == "com.lonniesmith.scanny-boy.cli"
    assert plist["LSBackgroundOnly"] is True
    assert plist["CFBundleExecutable"] == "scanny-boy"
    assert BUNDLE_EXECUTABLE.exists()


def test_bundle_carries_the_vetted_icc_profile_and_its_own_metadata():
    """The profile is ordinary package data and the two `copy_metadata`
    entries of section 5.2 are what keep `importlib.metadata` working in the
    frozen program."""
    profiles = list(BUNDLE_PATH.rglob("ScannyBoy-Linear-ProPhoto-v1.icc"))
    assert profiles, "the vetted ICC profile is missing from the bundle"

    for profile in profiles:
        assert hashlib.sha256(profile.read_bytes()).hexdigest() == PROFILE_SHA256

    names = {path.name for path in BUNDLE_PATH.rglob("*.dist-info")}
    assert any(name.startswith("tifftools-") for name in names), names
    assert any(name.startswith("scanny_boy-") for name in names), names


def test_bundle_carries_scipy_optimize_and_opencv_aruco():
    """Geometric calibration's two runtime dependencies, confirmed against
    the frozen bundle rather than the spec's hooks (docs/GEOMETRIC_PLAN.md
    sections 8–9): PyInstaller has a scipy hook, but a hook that silently
    stops collecting is exactly the failure mode this file exists to catch.
    The bundle cannot run arbitrary Python, so the check is on the shipped
    artefacts: scipy.optimize's compiled extension modules and OpenCV's
    native library, which is where cv2.aruco.CharucoDetector lives."""
    optimize_binaries = list(BUNDLE_PATH.rglob("scipy/optimize/*.so"))
    assert optimize_binaries, (
        "scipy.optimize's compiled modules are missing from the bundle"
    )
    # `least_squares` reaches MINPACK through these extensions; a hook that
    # collected only the pure-Python half would pass the glob above.
    assert any("minpack" in path.name.lower() for path in optimize_binaries), (
        [path.name for path in optimize_binaries]
    )

    cv2_binaries = list(BUNDLE_PATH.rglob("cv2/cv2.*so"))
    assert cv2_binaries, "the OpenCV native module is missing from the bundle"
    # aruco is compiled into the headless OpenCV module; the availability
    # symbols themselves are pinned in opencv_availability_test.py, which
    # runs against the development venv that built this bundle.


def test_bundle_links_only_bundled_or_system_libraries():
    """Inspect the real Mach-O dependencies rather than assuming no hook is
    missing (section 5.2). LibRaw in particular is expected to be collected
    without a hook, so its own dependencies must resolve inside the bundle."""
    libraw = next(BUNDLE_PATH.rglob("libraw_r.*.dylib"))
    for binary in (BUNDLE_EXECUTABLE, libraw):
        output = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # The first line is the file being inspected, not a dependency.
        for line in output.splitlines()[1:]:
            path = line.strip().split(" (")[0]
            if not path:
                continue
            assert path.startswith(
                ("@rpath/", "@loader_path/", "@executable_path/")
            ) or path.startswith(SYSTEM_LIBRARY_PREFIXES), (
                f"{binary.name} links {path}, which is neither bundled nor a "
                "system library"
            )


# -------------------------------------------------------------------------
# Packaged commands
# -------------------------------------------------------------------------


def test_packaged_version_prints_the_distribution_version():
    """The cheapest proof that the frozen program starts and can read its
    own package metadata."""
    result = run_packaged("--version", timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"scanny-boy {VERSION}"


@requires_real_samples
def test_packaged_probe_returns_the_catalogue_in_canonical_order():
    result = run_packaged("probe", "--input", str(FIXTURES_DIR), timeout=120)

    assert result.returncode == 0, result.stderr
    events = _events(result.stdout)
    probe_result = next(e for e in events if e["event"] == "probe_result")
    # A prefix, not the whole catalogue: the fixtures directory also holds
    # Phase 2's gate-B stitching scans, captured 27 days later.
    assert probe_result["catalogue"][: len(REAL_SAMPLE_FILES)] == REAL_SAMPLE_FILES
    assert probe_result["warnings"] == []
    assert events[-1]["event"] == "finished"
    assert events[-1]["exit_status"] == 0


@requires_real_samples
@pytest.mark.parametrize("fixture_name", ["packaged_serial_run", "packaged_threaded_run"])
def test_packaged_conversion_writes_real_tiffs(fixture_name, request):
    """`--jobs 1` and `--jobs 4`, packaged, each writing genuine Deflate
    TIFFs with the horizontal predictor. This is the check that catches the
    `imagecodecs` delayed-import failure: without the collected submodules
    the predictor is missing and the first write fails."""
    run: PackagedRun = request.getfixturevalue(fixture_name)

    assert run.returncode == 0, run.stderr[-4000:]
    assert run.events[-1]["event"] == "finished"
    assert run.events[-1]["status"] == "success"
    assert {e["event"] for e in run.events} >= {
        "started",
        "progress",
        "item_done",
        "group_done",
        "finished",
    }

    for name in NEGATIVE_1:
        output = run.out_dir / f"{Path(name).stem}.tif"
        assert output.exists()
        with tifffile.TiffFile(output) as handle:
            page = handle.pages[0]
            assert page.shape == (4040, 6064, 3)
            assert page.dtype == "uint16"
            assert page.tags["Compression"].value == DEFLATE_COMPRESSION
            assert page.tags["Predictor"].value == HORIZONTAL_PREDICTOR
            assert page.tags["Orientation"].value == 1
            assert page.tags["InterColorProfile"].value is not None

    assert (run.out_dir / "scanny-boy-manifest.json").exists()


@requires_real_samples
def test_packaged_program_runs_a_real_stitch(tmp_path):
    """Chunk P2-8: the frozen binary performs a complete `run` on the
    sample NEFs and the resulting stitched TIFF is opened and checked.

    An import check, a `--version` check, or a conversion-only check does
    not discharge this (section 4.2) — OpenCV, like `imagecodecs` before
    it, can fail only in the frozen bundle. This is the packaged
    equivalent of `run_pipeline_test.py`'s real-sample coverage: full
    RAW decode, registration, compositing, and the two-pass TIFF write,
    all inside the bundle.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # Section 5.4 decision 1: `run` publishes into a roll, and never creates
    # one. The roll is created through the packaged binary itself — its
    # record lands in the packaged process's own library database, so an
    # in-process write would not be visible to it.
    result = run_packaged(
        "roll",
        "init",
        "--library",
        str(tmp_path),
        "--name",
        "packaged",
        "--per-negative",
        "3",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out_dir = tmp_path / "packaged"

    result = run_packaged(
        "run",
        "--input",
        str(FIXTURES_DIR),
        "--files",
        *NEGATIVE_1,
        "--roll",
        str(out_dir),
        "--per-negative",
        "3",
        "--jobs",
        "3",
        "--work",
        str(work_dir),
        timeout=600,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    events = _events(result.stdout)
    assert events[-1]["event"] == "finished"
    assert events[-1]["status"] == "success"
    negative_done = [e for e in events if e["event"] == "negative_done"]
    assert len(negative_done) == 1
    assert negative_done[0]["output"] == "_DSC4638.tif"

    output = out_dir / "_DSC4638.tif"
    assert output.exists()
    with tifffile.TiffFile(output) as handle:
        page = handle.pages[0]
        assert page.shape[-1] == 3
        assert page.dtype == "uint16"
        assert page.tags["Compression"].value == DEFLATE_COMPRESSION
        assert page.tags["Predictor"].value == HORIZONTAL_PREDICTOR
        assert page.tags["Orientation"].value == 1
        assert page.tags["InterColorProfile"].value is not None
        assert page.tags["ImageDescription"].value == "_DSC4638.NEF+2: stitched scan"

    # The record lives in the packaged process's library database, so it is
    # read back through a packaged `roll info`.
    info = run_packaged("roll", "info", "--roll", str(out_dir), timeout=60)
    assert info.returncode == 0, info.stderr
    manifest = next(
        e for e in _events(info.stdout) if e["event"] == "roll_info"
    )["manifest"]
    assert_matches_roll_manifest_schema(manifest, load_roll_manifest_schema())
    # Section 3.3: a roll is additive, so the status belongs to the run.
    assert manifest["runs"][0]["status"] == "complete"
    assert manifest["negatives"][0]["status"] == "completed"

    # `--work` was supplied explicitly, so it survives a complete run
    # (section 3.5) — proving the packaged program's cleanup logic, not
    # just its pixel output.
    assert work_dir.exists()
    assert (work_dir / "scanny-boy-manifest.json").exists()


@requires_real_samples
def test_development_and_packaged_runs_produce_equal_pixels_and_metadata(
    development_run, packaged_serial_run, packaged_threaded_run
):
    """The point of the whole chunk: freezing the program changes nothing
    about the files it produces. Compared after the documented changing
    field (IFD0 `DateTime`) is ignored, per section 7."""
    for name in NEGATIVE_1:
        output = f"{Path(name).stem}.tif"
        expected = tiff_fingerprint(development_run / output)
        assert tiff_fingerprint(packaged_serial_run.out_dir / output) == expected
        assert tiff_fingerprint(packaged_threaded_run.out_dir / output) == expected


@requires_real_samples
def test_packaged_cancellation_keeps_completed_groups_and_exits_143(tmp_path):
    """SIGTERM after a negative has demonstrably been published — gated on
    the `group_done` event, not on a fixed sleep — leaves that negative in
    place, discards the negative in progress, and exits 143."""
    out_dir = tmp_path / "cancelled"
    out_dir.mkdir()

    process = subprocess.Popen(
        [
            str(BUNDLE_EXECUTABLE),
            "convert",
            "--input",
            str(FIXTURES_DIR),
            "--files",
            *REAL_SAMPLE_FILES,
            "--out",
            str(out_dir),
            "--per-negative",
            "3",
            "--jobs",
            "4",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None

    events: list[dict] = []
    try:
        for line in process.stdout:
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event)
            if event["event"] == "group_done":
                process.send_signal(signal.SIGTERM)
                break
        events.extend(_events(process.stdout.read()))
    finally:
        try:
            returncode = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        finally:
            process.stdout.close()

    assert returncode == 143
    assert events[-1]["event"] == "finished"
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["exit_status"] == 143

    for name in NEGATIVE_1:
        assert (out_dir / f"{Path(name).stem}.tif").exists()
    for name in NEGATIVE_2:
        assert not (out_dir / f"{Path(name).stem}.tif").exists()

    staging = [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)]
    assert staging == []

    manifest = json.loads((out_dir / "scanny-boy-manifest.json").read_text())
    assert manifest["status"] == "cancelled"


# -------------------------------------------------------------------------
# Isolation from the development environment
# -------------------------------------------------------------------------


@requires_real_samples
def test_no_library_is_loaded_from_the_development_venv(packaged_serial_run):
    """Every library the packaged conversion actually loads comes from
    inside the bundle or from the system — never from `cli/.venv` or the
    source checkout."""
    loaded = [
        line.split("> ", 1)[1]
        for line in packaged_serial_run.stderr.splitlines()
        if line.startswith("dyld[") and "> " in line
    ]
    assert loaded, "DYLD_PRINT_LIBRARIES produced no library list"
    # rawpy's LibRaw and an imagecodecs extension are the two that matter:
    # if either came from outside, the bundle is not self-contained.
    assert any("libraw_r" in path for path in loaded)
    assert any("imagecodecs" in path for path in loaded)

    bundle_prefix = str(BUNDLE_PATH.resolve())
    for path in loaded:
        assert not path.startswith(str(DEV_VENV)), f"loaded from the dev venv: {path}"
        assert path.startswith(bundle_prefix) or path.startswith(
            SYSTEM_LIBRARY_PREFIXES
        ), f"loaded from outside the bundle: {path}"


def test_packaged_program_ignores_python_modules_outside_the_bundle(tmp_path):
    """dyld only sees native libraries. This covers the pure-Python half:
    decoy modules on `PYTHONPATH` that raise on import are never reached,
    because the frozen interpreter imports only from the bundle."""
    import scanny_boy.cli  # noqa: F401 — imported for the assertion below

    shadowed = ["tifftools", "numpy", "tifffile"]
    # The premise: these really are imported while the CLI starts up, so a
    # decoy that shadowed one of them would break `--version`.
    for name in shadowed:
        assert name in sys.modules

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    for name in shadowed:
        (decoy / f"{name}.py").write_text(
            f'raise AssertionError("decoy {name} outside the bundle was imported")\n'
        )

    result = run_packaged("--version", env={"PYTHONPATH": str(decoy)}, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"scanny-boy {VERSION}"

