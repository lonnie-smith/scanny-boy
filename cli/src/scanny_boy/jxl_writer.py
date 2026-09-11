"""Lossless 16-bit JPEG XL writing with an embedded ICC profile.

`imagecodecs.jpegxl_encode` cannot embed an ICC profile — its colour
options are CICP enum codes, and Adobe RGB is not expressible as one —
so this module drives the bundled libjxl directly through ctypes.
The library is *not* located by path: importing
`imagecodecs._jpegxl` makes the dynamic linker load libjxl (and its
thread runner and CMS) into the process, after which the symbols resolve
through the global namespace — true in a checkout and in the PyInstaller
bundle alike, and independent of the libjxl version.

The encode sequence below is the one proven against the bundled libjxl.
Four footguns are called out at their
sites; they cost real hours when rediscovered.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import numpy as np

JXL_EFFORT = 7  # libjxl's default; 3 is ~30% larger, 9 is much slower
JXL_BITS_PER_SAMPLE = 16
JXL_SUFFIX = ".jxl"


class JxlEncoderUnavailable(Exception):
    """libjxl could not be reached through the process's symbol namespace.

    A packaging failure, not a user error: `imagecodecs` ships libjxl and
    always has, so reaching this means the bundle or wheel is broken. The
    exporter surfaces it as `Code.JXL_ENCODER_UNAVAILABLE`.
    """

    code = "JXL_ENCODER_UNAVAILABLE"


# --- libjxl constants (from the 0.12 headers, which this file's ABI
# --- mirrors; see the struct definitions below) ---------------------------

JXL_TRUE = 1
JXL_FALSE = 0

# `JxlDataType` is NOT densely numbered: FLOAT=0, UINT8=2, UINT16=3,
# FLOAT16=5. Value 1 is unassigned and produces a `JXL_ENC_ERR_GENERIC`
# from `JxlEncoderProcessOutput` — *not* from `JxlEncoderAddImageFrame`,
# which returns success. Guessing 1 for uint16 costs an hour.
JXL_TYPE_FLOAT = 0
JXL_TYPE_UINT8 = 2
JXL_TYPE_UINT16 = 3
JXL_TYPE_FLOAT16 = 5

JXL_NATIVE_ENDIAN = 0

# `JxlEncoderFrameSettingId`: EFFORT is enum member 0 in encode.h.
JXL_ENC_FRAME_SETTING_EFFORT = 0

JXL_ENC_SUCCESS = 0
JXL_ENC_NEED_MORE_OUTPUT = 2
JXL_ENC_ERROR = 1

# libjxl header sizes the output buffer in growing chunks; 4 MiB is well
# above what a single chunk ever needs at these image sizes, but the loop
# below doubles on `NEED_MORE_OUTPUT` regardless.
_OUTPUT_CHUNK = 4 * 1024 * 1024


# --- ctypes mirrors of libjxl's ABI ---------------------------------------

class JxlPreviewHeader(ctypes.Structure):
    _fields_ = [
        ("xsize", ctypes.c_uint32),
        ("ysize", ctypes.c_uint32),
    ]


class JxlAnimationHeader(ctypes.Structure):
    _fields_ = [
        ("tps_numerator", ctypes.c_uint32),
        ("tps_denominator", ctypes.c_uint32),
        ("num_loops", ctypes.c_uint32),
        ("have_timecodes", ctypes.c_int32),  # JXL_BOOL
    ]


class JxlBasicInfo(ctypes.Structure):
    """Field-for-field with libjxl 0.12's `JxlBasicInfo`
    (codestream_header.h). `jxl_writer_test` asserts the documented
    defaults come back from `JxlEncoderInitBasicInfo` and that
    `sizeof == 204`; that test is the tripwire for an ABI change —
    without it, a struct-layout drift shows up as silent pixel
    corruption.
    """

    _fields_ = [
        ("have_container", ctypes.c_int32),  # JXL_BOOL
        ("xsize", ctypes.c_uint32),
        ("ysize", ctypes.c_uint32),
        ("bits_per_sample", ctypes.c_uint32),
        ("exponent_bits_per_sample", ctypes.c_uint32),
        ("intensity_target", ctypes.c_float),
        ("min_nits", ctypes.c_float),
        ("relative_to_max_display", ctypes.c_int32),  # JXL_BOOL
        ("linear_below", ctypes.c_float),
        ("uses_original_profile", ctypes.c_int32),  # JXL_BOOL
        ("have_preview", ctypes.c_int32),  # JXL_BOOL
        ("have_animation", ctypes.c_int32),  # JXL_BOOL
        ("orientation", ctypes.c_int32),  # JxlOrientation
        ("num_color_channels", ctypes.c_uint32),
        ("num_extra_channels", ctypes.c_uint32),
        ("alpha_bits", ctypes.c_uint32),
        ("alpha_exponent_bits", ctypes.c_uint32),
        ("alpha_premultiplied", ctypes.c_int32),  # JXL_BOOL
        ("preview", JxlPreviewHeader),
        ("animation", JxlAnimationHeader),
        ("intrinsic_xsize", ctypes.c_uint32),
        ("intrinsic_ysize", ctypes.c_uint32),
        ("padding", ctypes.c_uint8 * 100),
    ]


class JxlPixelFormat(ctypes.Structure):
    """`JxlPixelFormat` from types.h. `align` is `size_t` — 8 bytes — so
    this struct is 24 bytes, not 16."""

    _fields_ = [
        ("num_channels", ctypes.c_uint32),
        ("data_type", ctypes.c_int32),  # JxlDataType
        ("endianness", ctypes.c_int32),  # JxlEndianness
        ("align", ctypes.c_size_t),
    ]


class JxlCmsInterface(ctypes.Structure):
    """`JxlCmsInterface` from cms_interface.h — seven pointers.
    `JxlEncoderSetCms` takes it *by value*, so the ctypes call passes
    `contents`."""

    _fields_ = [
        ("set_fields_data", ctypes.c_void_p),
        ("set_fields_from_icc", ctypes.c_void_p),
        ("init_data", ctypes.c_void_p),
        ("init", ctypes.c_void_p),
        ("get_src_buf", ctypes.c_void_p),
        ("get_dst_buf", ctypes.c_void_p),
        ("run", ctypes.c_void_p),
        ("destroy", ctypes.c_void_p),
    ]


JXL_PARALLEL_RUNNER = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
)


class _JxlApi:
    """The libjxl symbols this writer needs, with `argtypes` set on every
    one. An unprototyped call passes the encoder handle as a 32-bit
    `int`, truncating the pointer, and segfaults the interpreter — so
    *every* function gets `argtypes`, including the two easiest to skip
    because their return values look uninteresting (`JxlEncoderGetError`,
    `JxlEncoderDestroy`)."""

    def __init__(self) -> None:
        # Linux dlopens an extension's dependency libraries with the
        # import's own flags, and the default flags lack RTLD_GLOBAL — so
        # without this the libjxl symbols never reach the global namespace
        # `CDLL(None)` searches (macOS loads dependencies globally either
        # way, which is why the failure only showed on Linux).
        dlopen_flags = sys.getdlopenflags()
        try:
            # Loads libjxl, libjxl_threads and libjxl_cms into the process.
            sys.setdlopenflags(dlopen_flags | getattr(os, "RTLD_GLOBAL", 0))
            import imagecodecs._jpegxl  # noqa: F401
        except Exception as exc:
            raise JxlEncoderUnavailable(
                "could not import imagecodecs._jpegxl; the bundled libjxl "
                f"is missing from this build: {exc}"
            ) from exc
        finally:
            sys.setdlopenflags(dlopen_flags)
        # The global symbol namespace — path-independent and
        # version-agnostic, which is what makes this work frozen.
        lib = ctypes.CDLL(None)

        def need(name: str) -> ctypes._FuncPtr:
            try:
                return getattr(lib, name)
            except AttributeError as exc:
                raise JxlEncoderUnavailable(
                    f"libjxl symbol {name!r} did not resolve; this build's "
                    "libjxl is too old or incompletely collected"
                ) from exc

        self.JxlEncoderCreate = need("JxlEncoderCreate")
        self.JxlEncoderCreate.argtypes = [ctypes.c_void_p]
        self.JxlEncoderCreate.restype = ctypes.c_void_p

        self.JxlEncoderDestroy = need("JxlEncoderDestroy")
        self.JxlEncoderDestroy.argtypes = [ctypes.c_void_p]
        self.JxlEncoderDestroy.restype = None

        self.JxlEncoderGetError = need("JxlEncoderGetError")
        self.JxlEncoderGetError.argtypes = [ctypes.c_void_p]
        self.JxlEncoderGetError.restype = ctypes.c_int

        self.JxlEncoderSetCms = need("JxlEncoderSetCms")
        self.JxlEncoderSetCms.argtypes = [ctypes.c_void_p, JxlCmsInterface]
        self.JxlEncoderSetCms.restype = None

        self.JxlGetDefaultCms = need("JxlGetDefaultCms")
        self.JxlGetDefaultCms.argtypes = []
        self.JxlGetDefaultCms.restype = ctypes.POINTER(JxlCmsInterface)

        self.JxlEncoderSetParallelRunner = need("JxlEncoderSetParallelRunner")
        self.JxlEncoderSetParallelRunner.argtypes = [
            ctypes.c_void_p,
            JXL_PARALLEL_RUNNER,
            ctypes.c_void_p,
        ]
        self.JxlEncoderSetParallelRunner.restype = ctypes.c_int

        self.JxlThreadParallelRunner = need("JxlThreadParallelRunner")
        self.JxlThreadParallelRunner.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.JxlThreadParallelRunner.restype = ctypes.c_int

        self.JxlThreadParallelRunnerCreate = need("JxlThreadParallelRunnerCreate")
        self.JxlThreadParallelRunnerCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.JxlThreadParallelRunnerCreate.restype = ctypes.c_void_p

        self.JxlThreadParallelRunnerDestroy = need("JxlThreadParallelRunnerDestroy")
        self.JxlThreadParallelRunnerDestroy.argtypes = [ctypes.c_void_p]
        self.JxlThreadParallelRunnerDestroy.restype = None

        # NOT 0 — 0 means zero worker threads, not "one per core". The
        # default query returns one worker per core (10 on the machine
        # this was written on).
        self.default_num_threads = int(
            need("JxlThreadParallelRunnerDefaultNumWorkerThreads")()
        )

        self.JxlEncoderUseContainer = need("JxlEncoderUseContainer")
        self.JxlEncoderUseContainer.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.JxlEncoderUseContainer.restype = ctypes.c_int

        self.JxlEncoderUseBoxes = need("JxlEncoderUseBoxes")
        self.JxlEncoderUseBoxes.argtypes = [ctypes.c_void_p]
        self.JxlEncoderUseBoxes.restype = ctypes.c_int

        self.JxlEncoderCloseBoxes = need("JxlEncoderCloseBoxes")
        self.JxlEncoderCloseBoxes.argtypes = [ctypes.c_void_p]
        self.JxlEncoderCloseBoxes.restype = None

        self.JxlEncoderInitBasicInfo = need("JxlEncoderInitBasicInfo")
        self.JxlEncoderInitBasicInfo.argtypes = [
            ctypes.POINTER(JxlBasicInfo)
        ]
        self.JxlEncoderInitBasicInfo.restype = None

        self.JxlEncoderSetBasicInfo = need("JxlEncoderSetBasicInfo")
        self.JxlEncoderSetBasicInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(JxlBasicInfo),
        ]
        self.JxlEncoderSetBasicInfo.restype = ctypes.c_int

        self.JxlEncoderSetICCProfile = need("JxlEncoderSetICCProfile")
        self.JxlEncoderSetICCProfile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        self.JxlEncoderSetICCProfile.restype = ctypes.c_int

        self.JxlEncoderAddBox = need("JxlEncoderAddBox")
        self.JxlEncoderAddBox.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char * 4,  # JxlBoxType
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_int,  # JXL_BOOL
        ]
        self.JxlEncoderAddBox.restype = ctypes.c_int

        self.JxlEncoderFrameSettingsCreate = need("JxlEncoderFrameSettingsCreate")
        self.JxlEncoderFrameSettingsCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.JxlEncoderFrameSettingsCreate.restype = ctypes.c_void_p

        self.JxlEncoderFrameSettingsSetOption = need(
            "JxlEncoderFrameSettingsSetOption"
        )
        self.JxlEncoderFrameSettingsSetOption.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int64,
        ]
        self.JxlEncoderFrameSettingsSetOption.restype = ctypes.c_int

        self.JxlEncoderSetFrameLossless = need("JxlEncoderSetFrameLossless")
        self.JxlEncoderSetFrameLossless.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.JxlEncoderSetFrameLossless.restype = ctypes.c_int

        self.JxlEncoderSetFrameDistance = need("JxlEncoderSetFrameDistance")
        self.JxlEncoderSetFrameDistance.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float,
        ]
        self.JxlEncoderSetFrameDistance.restype = ctypes.c_int

        self.JxlEncoderAddImageFrame = need("JxlEncoderAddImageFrame")
        self.JxlEncoderAddImageFrame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(JxlPixelFormat),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        self.JxlEncoderAddImageFrame.restype = ctypes.c_int

        self.JxlEncoderCloseInput = need("JxlEncoderCloseInput")
        self.JxlEncoderCloseInput.argtypes = [ctypes.c_void_p]
        self.JxlEncoderCloseInput.restype = None

        self.JxlEncoderProcessOutput = need("JxlEncoderProcessOutput")
        self.JxlEncoderProcessOutput.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.JxlEncoderProcessOutput.restype = ctypes.c_int


_api: _JxlApi | None = None
JXL_ENCODE_THREADS: int | None = None


def _load_api() -> _JxlApi:
    global _api, JXL_ENCODE_THREADS
    if _api is None:
        _api = _JxlApi()
        JXL_ENCODE_THREADS = _api.default_num_threads
    return _api


class _EncodeError(RuntimeError):
    def __init__(self, call: str, error: int) -> None:
        super().__init__(f"libjxl {call} failed (JxlEncoderError {error})")


def _check(api: _JxlApi, enc: ctypes.c_void_p, call: str, status: int) -> None:
    if status != JXL_ENC_SUCCESS:
        raise _EncodeError(call, api.JxlEncoderGetError(enc))


def encode_jxl(
    pixels: np.ndarray,
    *,
    icc_profile: bytes,
    exif: bytes | None = None,
    xmp: bytes | None = None,
) -> bytes:
    """Encodes one 16-bit lossless JPEG XL codestream in memory.

    `pixels` is `(H, W)` or `(H, W, 3)`, uint16. `icc_profile` is
    required — an untagged file is never written. `exif` is raw TIFF
    bytes (the 4-byte box offset prefix is added here); `xmp` is the
    packet bytes.
    """
    if pixels.ndim == 2:
        height, width = pixels.shape
        channels = 1
    elif pixels.ndim == 3 and pixels.shape[2] in (1, 3):
        height, width, channels = pixels.shape
        if channels == 1:
            pixels = pixels[:, :, 0]
    else:
        raise ValueError(
            "jxl_writer needs a (H, W) or (H, W, 3) uint16 image; got "
            f"shape {pixels.shape} dtype {pixels.dtype}"
        )
    if pixels.dtype != np.uint16:
        raise ValueError(
            f"jxl_writer needs uint16 pixels; got dtype {pixels.dtype} for "
            f"shape {pixels.shape}"
        )
    if not icc_profile:
        raise ValueError("refusing to write a JPEG XL file without an ICC profile")

    api = _load_api()
    data = np.ascontiguousarray(pixels)
    n_channels = 1 if data.ndim == 2 else 3

    runner = api.JxlThreadParallelRunnerCreate(None, api.default_num_threads)
    enc = None
    try:
        enc = api.JxlEncoderCreate(None)
        parallel_runner = JXL_PARALLEL_RUNNER(api.JxlThreadParallelRunner)
        _check(
            api,
            enc,
            "SetParallelRunner",
            api.JxlEncoderSetParallelRunner(enc, parallel_runner, runner),
        )
        # `JxlGetDefaultCms` returns a pointer; `JxlEncoderSetCms` takes
        # the interface struct by value.
        cms = api.JxlGetDefaultCms()
        api.JxlEncoderSetCms(enc, cms.contents)

        with_metadata = exif is not None or xmp is not None
        _check(api, enc, "UseContainer", api.JxlEncoderUseContainer(enc, JXL_TRUE))
        if with_metadata:
            _check(api, enc, "UseBoxes", api.JxlEncoderUseBoxes(enc))

        info = JxlBasicInfo()
        api.JxlEncoderInitBasicInfo(ctypes.byref(info))
        info.xsize = width
        info.ysize = height
        info.bits_per_sample = JXL_BITS_PER_SAMPLE
        # 0 exponent bits: unsigned integer samples, not float.
        info.exponent_bits_per_sample = 0
        # Required for true lossless: the codestream keeps the original
        # (untouched) samples.
        info.uses_original_profile = JXL_TRUE
        info.num_color_channels = n_channels
        info.num_extra_channels = 0
        info.alpha_bits = 0
        _check(api, enc, "SetBasicInfo", api.JxlEncoderSetBasicInfo(enc, ctypes.byref(info)))
        _check(
            api,
            enc,
            "SetICCProfile",
            api.JxlEncoderSetICCProfile(enc, icc_profile, len(icc_profile)),
        )

        if exif is not None:
            # libjxl expects a 4-byte big-endian offset to the TIFF
            # header before the payload; 0 puts it at the start.
            payload = b"\x00\x00\x00\x00" + bytes(exif)
            _check(
                api,
                enc,
                "AddBox(Exif)",
                api.JxlEncoderAddBox(
                    enc,
                    (ctypes.c_char * 4)(*b"Exif"),
                    payload,
                    len(payload),
                    JXL_FALSE,
                ),
            )
        if xmp is not None:
            _check(
                api,
                enc,
                "AddBox(xml )",
                api.JxlEncoderAddBox(
                    enc,
                    (ctypes.c_char * 4)(*b"xml "),
                    xmp,
                    len(xmp),
                    JXL_FALSE,
                ),
            )
        if with_metadata:
            api.JxlEncoderCloseBoxes(enc)

        frame_settings = api.JxlEncoderFrameSettingsCreate(enc, None)
        _check(
            api,
            enc,
            "FrameSettingsSetOption(EFFORT)",
            api.JxlEncoderFrameSettingsSetOption(
                frame_settings, JXL_ENC_FRAME_SETTING_EFFORT, JXL_EFFORT
            ),
        )
        _check(
            api,
            enc,
            "SetFrameLossless",
            api.JxlEncoderSetFrameLossless(frame_settings, JXL_TRUE),
        )
        # Belt and braces alongside `SetFrameLossless`: distance 0.0 is
        # the lossless distance, but the lossless *flag* is the only one
        # of the two that actually selects the lossless path.
        _check(
            api,
            enc,
            "SetFrameDistance",
            api.JxlEncoderSetFrameDistance(frame_settings, 0.0),
        )

        fmt = JxlPixelFormat(
            num_channels=n_channels,
            data_type=JXL_TYPE_UINT16,
            endianness=JXL_NATIVE_ENDIAN,
            align=0,
        )
        nbytes = data.nbytes
        _check(
            api,
            enc,
            "AddImageFrame",
            api.JxlEncoderAddImageFrame(
                frame_settings,
                ctypes.byref(fmt),
                data.ctypes.data_as(ctypes.c_char_p),
                nbytes,
            ),
        )
        api.JxlEncoderCloseInput(enc)

        out = bytearray()
        buf = ctypes.create_string_buffer(_OUTPUT_CHUNK)
        while True:
            next_out = ctypes.cast(buf, ctypes.c_char_p)
            avail = ctypes.c_size_t(_OUTPUT_CHUNK)
            status = api.JxlEncoderProcessOutput(enc, ctypes.byref(next_out), ctypes.byref(avail))
            if status == JXL_ENC_ERROR:
                raise _EncodeError("ProcessOutput", api.JxlEncoderGetError(enc))
            produced = _OUTPUT_CHUNK - avail.value
            out += buf.raw[:produced]
            if status == JXL_ENC_SUCCESS:
                break
        return bytes(out)
    finally:
        if enc is not None:
            api.JxlEncoderDestroy(enc)
        api.JxlThreadParallelRunnerDestroy(runner)


def write_jxl(
    path: Path,
    pixels: np.ndarray,
    *,
    icc_profile: bytes,
    exif: bytes | None = None,
    xmp: bytes | None = None,
) -> None:
    """Writes `pixels` as a 16-bit lossless JPEG XL file at `path`.

    Writes to a `.tmp` sibling and `replace()`s on success, unlinking the
    tmp on any `BaseException` — the same discipline the exporter's base
    write already follows. Refuses an empty `icc_profile` with a
    `ValueError`, mirroring `tiff_writer.write_base_tiff`'s "never
    silently write untagged data" guard.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        encoded = encode_jxl(
            pixels, icc_profile=icc_profile, exif=exif, xmp=xmp
        )
        tmp_path.write_bytes(encoded)
        tmp_path.replace(path)
    except BaseException:
        # A failed write must not leave a partial .tmp file behind in the
        # user's output folder.
        tmp_path.unlink(missing_ok=True)
        raise
