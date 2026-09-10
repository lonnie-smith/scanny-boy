#!/usr/bin/env python3
"""Generate the fine-pitch ChArUco calibration target as a vector PDF.

    uv run --project cli cli/tools/generate_charuco_board.py

Writes `calibration/lens_calibration_targets.pdf`: a US Letter page carrying a
50 x 38 board of 2.0 mm squares with 1.5 mm markers, plus corner crop marks
for trimming the sheet down to a 5 13/16 x 3 11/16 inch card, and nothing
else. The board is 100 x 76 mm — a hair under 4 x 3 inches, the largest
2.0 mm grid that fits inside that envelope on a whole number of squares.

**The crop marks.** Four corner pairs, offset outside the trim line rather
than touching it, so nothing survives onto the cut card no matter which
side of the line the blade lands. They are drawn as thin filled rectangles
like everything else here — this writer emits no stroke state at all, which
keeps the content stream to one graphics operator and one fill.

**Why 2.0 mm.** At the scanning rig's magnification (~168 px/mm) one frame
covers roughly 36 x 24 mm of the target, so the existing 4.0 mm `6x9` board
puts at most 40 interior corners in a frame — the calibration measured a
median of 32. At 2.0 mm the same window holds 187. That does not by itself
make the acceptance gate pass (corner-localisation accuracy is what binds,
not corner count), but it tightens the fitted coefficient's spread from
about 13% to 6%.

**Why 4 x 3 inches, and why that size is what makes 2.0 mm possible.** The
board only has to be at least as large as a real 6x9 negative (~84 x 56 mm)
so that a frame is always looking at board rather than past its edge; 100 x
76 mm clears that by 8 mm and 10 mm a side. The size is also a hard
constraint on the pitch, because a ChArUco board needs one ArUco marker per
white square and the largest predefined dictionary holds 1024. At 2.0 mm a
5 x 3 inch board would need 1197 markers and be impossible; 4 x 3 needs
950, which fits DICT_4X4_1000. Going bigger costs the fine pitch, and the
fine pitch is the point.

**Why DICT_4X4_1000.** Two reasons, both about this board being fine.
A 4x4 marker is 6 modules across its 1.5 mm including the border, so a
module is 250 um — 17% larger than the 5x5 family's 214 um, and the whole
point of this target is dimensional accuracy at print scale. And the 4x4
patterns are disjoint from the DICT_5X5_* dictionaries the existing `35mm`
and `6x9` boards use, so `charuco.detect`'s format auto-detection cannot
confuse this board with either of them. (The predefined 5x5 dictionaries
are nested prefixes of one another, so a DICT_5X5_1000 board here *would*
share ids with the `6x9` board and make that detection ambiguous.)

**The constants here and `charuco.BOARD` must agree.** This tool draws the
artefact; `charuco.BOARD` is the transcription the detector uses, and
`charuco_test.test_board_constants_match_the_pdf` pins it. Change one and
change the other, then regenerate the PDF.

**Vector, not raster.** The target's accuracy is the thing being bought, so
the PDF carries filled rectangles at exact page coordinates rather than a
resampled bitmap. Print it at 100% scale with no fitting.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import cv2
import numpy as np

SQUARES_X = 50
SQUARES_Y = 38
SQUARE_MM = 2.0
MARKER_MM = 1.5
DICTIONARY = "DICT_4X4_1000"

PAGE_IN = (8.5, 11.0)  # US Letter, portrait; the board's long axis runs up it
# The card the sheet is trimmed down to, short edge first. Larger than the
# board on every side; the extra white is quiet zone the detector wants.
CARD_IN = (3.0 + 11.0 / 16.0, 5.0 + 13.0 / 16.0)
CROP_GAP_MM = 2.0  # bare paper between the trim corner and the mark
CROP_LEN_MM = 7.0
CROP_WEIGHT_MM = 0.15  # ~3.5 dots at 600 dpi: thin to cut to, thick to print
# One cell is the greatest common divisor of the square and marker pitches:
# a square is 8 cells, a marker 6, so the whole board rasterises exactly.
CELL_MM = SQUARE_MM / 8.0
MM_PER_IN = 25.4
PT_PER_IN = 72.0

OUT = Path(__file__).resolve().parents[2] / "calibration" / "lens_calibration_targets.pdf"


def board_cells() -> np.ndarray:
    """The board as a boolean cell grid, True where ink goes.

    `generateImage` at exactly 8 pixels per square is the board itself, not
    a rendering of it: every square boundary and every marker module lands
    on a cell edge, so this is lossless and there is no convention here for
    this tool to get wrong."""
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICTIONARY))
    board = cv2.aruco.CharucoBoard(
        (SQUARES_X, SQUARES_Y), SQUARE_MM, MARKER_MM, dictionary
    )
    size = (SQUARES_X * 8, SQUARES_Y * 8)
    image = board.generateImage(size, marginSize=0, borderBits=1)
    return image < 128


def runs(cells: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Merge horizontal runs of ink into `(x, y, width, height)` rectangles
    in cell units. Keeps the content stream to a few thousand operators
    instead of one per cell."""
    out = []
    height, width = cells.shape
    for y in range(height):
        row = cells[y]
        x = 0
        while x < width:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < width and row[x]:
                x += 1
            out.append((start, y, x - start, 1))
    return out


def cells_to_points(rects, cell_pt, x0_pt, y0_pt) -> list[tuple[float, ...]]:
    """Cell-grid rectangles into page points. PDF's origin is bottom left
    and the cell grid's is top left, so rows are flipped here."""
    height_cells = max(y for _, y, _, _ in rects) + 1
    return [
        (x0_pt + x * cell_pt,
         y0_pt + (height_cells - y - h) * cell_pt,
         w * cell_pt,
         h * cell_pt)
        for x, y, w, h in rects
    ]


def crop_marks(card_pt, page_pt) -> list[tuple[float, ...]]:
    """Eight marks — two per corner — bracketing the trim box without
    touching it. Each is a filled rectangle `CROP_WEIGHT_MM` thick."""
    gap = CROP_GAP_MM / MM_PER_IN * PT_PER_IN
    length = CROP_LEN_MM / MM_PER_IN * PT_PER_IN
    weight = CROP_WEIGHT_MM / MM_PER_IN * PT_PER_IN
    x0 = (page_pt[0] - card_pt[0]) / 2
    y0 = (page_pt[1] - card_pt[1]) / 2
    x1, y1 = x0 + card_pt[0], y0 + card_pt[1]

    out = []
    for x in (x0, x1):
        for y in (y0, y1):
            # horizontal mark, running outward from the trim edge in x
            hx = x - gap - length if x == x0 else x + gap
            out.append((hx, y - weight / 2, length, weight))
            # vertical mark, running outward from the trim edge in y
            vy = y - gap - length if y == y0 else y + gap
            out.append((x - weight / 2, vy, weight, length))
    return out


def content_stream(rects) -> bytes:
    """PDF content: one fill for every rectangle on the page, board and
    crop marks alike."""
    parts = ["0 g"]
    for px, py, pw, ph in rects:
        parts.append(f"{px:.4f} {py:.4f} {pw:.4f} {ph:.4f} re")
    parts.append("f")
    return "\n".join(parts).encode("ascii")


def write_pdf(path: Path, stream: bytes, page_pt: tuple[float, float]) -> None:
    """A minimal single-page PDF 1.4 with one Flate-compressed content
    stream. No fonts, no images, no metadata — the board and nothing else."""
    compressed = zlib.compress(stream, 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox "
            f"[0 0 {page_pt[0]:.4f} {page_pt[1]:.4f}] "
            f"/Contents 4 0 R /Resources << >> >>"
        ).encode("ascii"),
        (
            f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
        ).encode("ascii")
        + compressed
        + b"\nendstream",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode("ascii") + payload + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode("ascii")
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    path.write_bytes(body)


def main() -> None:
    cells = np.rot90(board_cells())  # long axis up the portrait page
    rects = runs(cells)

    cell_pt = CELL_MM / MM_PER_IN * PT_PER_IN
    page_pt = (PAGE_IN[0] * PT_PER_IN, PAGE_IN[1] * PT_PER_IN)
    card_pt = (CARD_IN[0] * PT_PER_IN, CARD_IN[1] * PT_PER_IN)
    board_pt = (cells.shape[1] * cell_pt, cells.shape[0] * cell_pt)
    if board_pt[0] > card_pt[0] or board_pt[1] > card_pt[1]:
        raise SystemExit("board does not fit the card")
    margin = CROP_GAP_MM + CROP_LEN_MM
    if (card_pt[0] + 2 * margin / MM_PER_IN * PT_PER_IN > page_pt[0]
            or card_pt[1] + 2 * margin / MM_PER_IN * PT_PER_IN > page_pt[1]):
        raise SystemExit("card plus crop marks does not fit the page")

    # board and card share the page's centre, so trimming to the marks
    # leaves the board centred on the card
    x0 = (page_pt[0] - board_pt[0]) / 2
    y0 = (page_pt[1] - board_pt[1]) / 2
    page_rects = cells_to_points(rects, cell_pt, x0, y0) + crop_marks(card_pt, page_pt)

    write_pdf(OUT, content_stream(page_rects), page_pt)

    markers = -(-SQUARES_X * SQUARES_Y // 2)
    print(f"wrote {OUT}")
    print(f"  page          {PAGE_IN[0]}in x {PAGE_IN[1]}in (US Letter)")
    print(f"  board         {SQUARES_X}x{SQUARES_Y} squares of {SQUARE_MM}mm "
          f"= {SQUARES_Y * SQUARE_MM:.0f} x {SQUARES_X * SQUARE_MM:.0f} mm")
    print(f"  board         {SQUARES_Y*SQUARE_MM/MM_PER_IN:.2f}in x "
          f"{SQUARES_X*SQUARE_MM/MM_PER_IN:.2f}in")
    print(f"  card at marks {CARD_IN[0]:.4f}in x {CARD_IN[1]:.4f}in "
          f"= {CARD_IN[0]*MM_PER_IN:.2f} x {CARD_IN[1]*MM_PER_IN:.2f} mm")
    print(f"  board margin  {(CARD_IN[0]*MM_PER_IN - SQUARES_Y*SQUARE_MM)/2:.2f} mm short "
          f"edge, {(CARD_IN[1]*MM_PER_IN - SQUARES_X*SQUARE_MM)/2:.2f} mm long edge")
    print(f"  markers       {markers} of {DICTIONARY}'s 1000")
    print(f"  corners       {(SQUARES_X-1)*(SQUARES_Y-1)} interior")
    print(f"  marker module {MARKER_MM/6*1000:.0f} um "
          f"({MARKER_MM/6/MM_PER_IN*600:.1f} dots at 600dpi)")
    print(f"  rectangles    {len(page_rects)} ({len(page_rects)-len(rects)} crop marks), "
          f"{OUT.stat().st_size/1024:.0f} kB")


if __name__ == "__main__":
    main()
