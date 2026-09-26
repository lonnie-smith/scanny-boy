#!/usr/bin/env python3
"""Generate the film-curve test-roll shooting instructions as a PDF.

    uv run --no-project --with reportlab cli/tools/generate_film_curve_instructions.py

Writes `calibration/film_curve_test_roll.pdf`: the instructions a user follows
to shoot one test roll per film stock, which `film-curve create` then measures
into a per-channel shape correction (`docs/FILM_CURVE_PLAN.md`).

**Why the exposure table is left blank.** The series has to span ten stops,
from -4 to +6, and every rig reaches that span differently: the aperture range
of the lens plus the power range of the flash must add to at least ten, and
which control supplies which stop depends on both. So the PDF teaches the
arithmetic, shows one worked example, and hands over a worksheet to fill in
rather than a table to follow.

**Why reportlab, run outside the project.** The CLI does not depend on
reportlab and should not: this is a document generator run by hand when the
instructions change, not part of any pipeline. `--no-project` keeps it out of
the CLI's lockfile. (The ChArUco target generator next door writes its PDF by
hand instead, because it needs exact print geometry; this one needs flowing
text and tables, which is reportlab's job.)
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

OUT = str(
    Path(__file__).resolve().parents[2] / "calibration" / "film_curve_test_roll.pdf"
)

INK = colors.HexColor("#111111")
INK2 = colors.HexColor("#4a4a4a")
RULE = colors.HexColor("#b8b8b8")
LIGHT = colors.HexColor("#f0efec")
ACCENT = colors.HexColor("#2a78d6")

ss = getSampleStyleSheet()
H1 = ParagraphStyle(
    "H1",
    parent=ss["Title"],
    fontName="Helvetica-Bold",
    fontSize=19,
    leading=23,
    alignment=TA_LEFT,
    textColor=INK,
    spaceAfter=2,
)
SUB = ParagraphStyle(
    "SUB",
    parent=ss["Normal"],
    fontName="Helvetica",
    fontSize=10,
    leading=14,
    textColor=INK2,
    spaceAfter=12,
)
H2 = ParagraphStyle(
    "H2",
    parent=ss["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=12.5,
    leading=15,
    textColor=INK,
    spaceBefore=14,
    spaceAfter=5,
)
H3 = ParagraphStyle(
    "H3",
    parent=ss["Heading3"],
    fontName="Helvetica-Bold",
    fontSize=10.5,
    leading=13,
    textColor=INK,
    spaceBefore=9,
    spaceAfter=3,
)
BODY = ParagraphStyle(
    "BODY",
    parent=ss["Normal"],
    fontName="Helvetica",
    fontSize=9.6,
    leading=13.4,
    textColor=INK,
    spaceAfter=6,
)
SMALL = ParagraphStyle(
    "SMALL",
    parent=BODY,
    fontSize=8.7,
    leading=11.8,
    textColor=INK2,
)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=9.2, leading=12, spaceAfter=0)
CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")


def bullets(items, style=BODY):
    return ListFlowable(
        [
            ListItem(Paragraph(t, style), leftIndent=14, value="bulletchar")
            for t in items
        ],
        bulletType="bullet",
        bulletFontSize=6,
        bulletOffsetY=-2,
        leftIndent=12,
        spaceAfter=6,
    )


def note(text):
    t = Table([[Paragraph(text, SMALL)]], colWidths=[6.6 * inch])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, ACCENT),
            ]
        )
    )
    return t


def planning_table(rows, widths=(1.5 * inch, 1.9 * inch, 1.9 * inch), head=None, pad=5):
    head = head or ["Exposure offset", "Aperture", "Flash power"]
    data = [[Paragraph(h, CELLB) for h in head]]
    for r in rows:
        data.append([Paragraph(c, CELL) for c in r])
    t = Table(data, colWidths=list(widths), repeatRows=1)
    style = [
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 1.0, INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    data_rows = len(rows)
    for i in range(1, data_rows + 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f7f5")))
    t.setStyle(TableStyle(style))
    return t


story = []
story.append(Paragraph("Shooting a film-curve test roll", H1))
story.append(
    Paragraph(
        "One roll per film stock. Produces the per-channel curve correction for that stock on your rig. "
        "See docs/FILM_CURVE_PLAN.md for what the data is used for.",
        SUB,
    )
)

story.append(Paragraph("What the roll has to contain", H2))
story.append(
    Paragraph(
        "A series of frames of one evenly lit neutral surface, each a stop apart, covering everything from "
        "nearly clear film to the densest part of the film's range. Nothing else about the frames matters: "
        "not the subject, not the exact exposure, not the colour of the light. The measurement compares the "
        "three colour channels <i>against each other</i> within each frame, so the only requirements are that "
        "the surface is neutral and evenly lit, and that the series covers the whole range.",
        BODY,
    )
)

story.append(Paragraph("Equipment", H2))
story.append(
    bullets(
        [
            (
                "<b>A neutral surface</b> larger than the frame: matte white paint, a photographic grey card, or "
                "brightener-free paper or board. Matte, not glossy."
            ),
            "<b>A manual flash</b> with switchable power. Consistency between frames matters more than absolute output.",
            "<b>A tripod</b>, and a film camera whose aperture you set by hand.",
            "<b>Black tape</b>, to mark an L in a corner of the target area.",
            "<b>A digital camera</b> (optional but recommended) to rehearse the setup.",
            "<b>A grey card</b> (optional), for a neutrality cross-check.",
        ]
    )
)

story.append(Paragraph("Setting up", H2))
story.append(
    bullets(
        [
            (
                "<b>Dark room.</b> Room light that reaches the target is additive and won't reproduce frame to frame. "
                "Turn everything off and close the blinds."
            ),
            (
                "<b>Light the target evenly.</b> Bounce the flash into an umbrella or fire it through a diffuser, or "
                "just move it further back. Uneven brightness across the target is tolerable — up to about a stop — "
                "but it must not change between frames."
            ),
            (
                "<b>Keep the camera's shadow out of the frame,</b> or at least out of one clean area. Raising the "
                "flash, or shooting a lit diffuser instead of a reflective surface, removes the shadow entirely."
            ),
            "<b>Fill the frame and defocus.</b> Focus at infinity from close range, so the surface's texture blurs away.",
            (
                "<b>Tape an L</b> at the edge of a clean, unshadowed part of the target. It marks the usable area and "
                "gives the analysis something to find."
            ),
            (
                "<b>Check the fabric for brighteners</b> if you're using cloth or paper. Shine a UV flashlight on it in "
                "the dark: if it glows blue-white, tape a UV filter over the flash head or put clear acrylic in the light path."
            ),
        ]
    )
)

story.append(
    note(
        "<b>Rehearse with a digital camera if you can.</b> Shoot the setup at the film's ISO and check four things: "
        "a frame with the flash off comes out black (no room light); the clean area is even; five identical frames "
        "match within a few percent; and the colour doesn't shift between the power settings you plan to use "
        "(compare the red/green and blue/green ratios at each power, adjusting the aperture to keep the level similar). "
        "A shift of about 1% is fine. A large shift at the lowest powers means you should avoid those settings."
    )
)

story.append(PageBreak())

story.append(Paragraph("Planning your series", H2))
story.append(
    Paragraph(
        "You need <b>11 frames, one stop apart, from −4 to +6</b> relative to a normally exposed frame. "
        "That's a ten-stop span, and you build it out of two controls: the lens aperture and the flash power. "
        "Shutter speed is not one of them — the flash is far brighter than the room, so the shutter sets "
        "nothing. Pick one shutter speed that syncs (1/60 or 1/125 is safe) and leave it there. Leave the ISO "
        "alone too.",
        BODY,
    )
)

story.append(Paragraph("Work out your own table", H3))
story.append(
    bullets(
        [
            "<b>Count your aperture stops.</b> From widest to smallest: f/8 to f/32 is 4 stops, f/4 to f/22 is 5, and so on.",
            "<b>Count your flash's power stops.</b> Full to 1/64 is 6 stops; full to 1/128 is 7.",
            (
                "<b>Add them.</b> The total must be at least 10. If it isn't, add the difference by moving the flash "
                "further from the target: doubling the distance is about 2 stops less light."
            ),
            (
                "<b>Fix the ends first.</b> +6 has to be your widest aperture at full power, and −4 your smallest "
                "aperture at the lowest power you'll use — assuming the two ranges add up to exactly 10, with nothing to spare."
            ),
            (
                "<b>Fill in between.</b> Hold the aperture wide and step the power down for the upper half; then hold "
                "the power and close the aperture for the lower half. Each row must be one stop below the one above it: "
                "halving the power is one stop down, and closing the aperture one click is one stop down."
            ),
            (
                "<b>Favour the higher power settings.</b> At its lowest settings a flash fires very briefly, and film "
                "can respond differently to very brief flashes. Where a row can be reached two ways, take the one with more power."
            ),
        ]
    )
)

story.append(Paragraph('Setting the anchor: which exposure is "0"', H3))
story.append(
    Paragraph(
        "0 should be a normal exposure of the target. Don't point a reflected-light meter at a white surface — "
        "it will try to render it mid-grey and underexpose by a couple of stops. Use an incident meter, meter off "
        "a grey card, or use a digital camera at the film's ISO and pick the combination that puts the target at "
        "roughly 10–20% of the raw maximum. If in doubt, err bright: negative film tolerates overexposure far "
        "better than underexposure.",
        BODY,
    )
)

story.append(
    KeepTogether(
        [
            Paragraph("A worked example", H3),
            Paragraph(
                "A lens stopping from f/8 to f/32 (4 stops) with a flash going from full to 1/64 (6 stops): 10 stops "
                "in total, exactly enough. Note how the lower half switches to 1/32 power so that only two frames "
                "sit at 1/64.",
                BODY,
            ),
            planning_table(
                [
                    ("+6", "f/8", "1/1 (full)"),
                    ("+5", "f/8", "1/2"),
                    ("+4", "f/8", "1/4"),
                    ("+3", "f/8", "1/8"),
                    ("+2", "f/8", "1/16"),
                    ("+1", "f/8", "1/32"),
                    ("0", "f/8", "1/64"),
                    ("\u22121", "f/16", "1/32"),
                    ("\u22122", "f/22", "1/32"),
                    ("\u22123", "f/32", "1/32"),
                    ("\u22124", "f/32", "1/64"),
                ],
                pad=3,
            ),
        ]
    )
)

story.append(PageBreak())

story.append(Paragraph("Your shooting worksheet", H1))
story.append(
    Paragraph(
        "Fill in the aperture and power columns before you load the film. Write each frame number in as you "
        "shoot. See \u201cThe shot list\u201d for what the overlap and duplicate rows are for.",
        SUB,
    )
)


def off(o):
    return "0" if o == 0 else (f"+{o}" if o > 0 else f"\u2212{-o}")


ws_rows = [
    ["Blank (cap on)", "\u2014", "\u2014", ""],
    ["Blank (cap on)", "\u2014", "\u2014", ""],
]
ws_rows += [["Series", off(o), "", ""] for o in range(6, -5, -1)]
ws_rows += [["Overlap", "", "", ""] for _ in range(3)]
ws_rows += [["Duplicate / grey card", "", "", ""] for _ in range(5)]
story.append(
    planning_table(
        ws_rows,
        head=["Frame", "Offset", "Aperture &amp; flash power", "Frame no."],
        widths=(1.75 * inch, 0.85 * inch, 2.7 * inch, 1.3 * inch),
        pad=6,
    )
)

story.append(PageBreak())

story.append(Paragraph("The shot list", H2))
story.append(
    Paragraph(
        "Beyond the 11 frames, a handful of extras make the roll self-checking. Log the frame number of every "
        "shot against its row — frame counters and scan order rarely line up by themselves.",
        BODY,
    )
)

story.append(Paragraph("1. Two blank frames", H3))
story.append(
    Paragraph(
        "Lens cap on, shoot two frames. These record the film's own base density, which everything else is "
        "measured against.",
        BODY,
    )
)

story.append(Paragraph("2. The series", H3))
story.append(
    Paragraph(
        "All 11 rows of your worksheet, in order. Change only the aperture ring and the power dial. Don't touch "
        "the focus, the camera, the flash or the target. At full power, wait 5–10 seconds after the ready "
        "light before firing — output right after recycling is unreliable.",
        BODY,
    )
)

story.append(Paragraph("3. Two or three overlap frames", H3))
story.append(
    Paragraph(
        "Shoot the same exposure a second way, using a different combination of aperture and power. In the "
        "worked example, 0 is f/8 at 1/64, so an overlap would be f/11 at 1/32. Plan them on the worksheet. "
        "These prove that the power steps and the brief flashes behave — if a pair doesn't match on the "
        "developed film, you'll know.",
        BODY,
    )
)

story.append(Paragraph("4. Duplicates, if you have frames to spare", H3))
story.append(Paragraph("In order of value:", BODY))
story.append(
    bullets(
        [
            (
                "<b>0, shot again as the very last frame of the roll.</b> Catches any drift across the session — "
                "batteries, a nudged target. The single most useful extra frame."
            ),
            "<b>+6 and +5.</b> The dense end carries most of the information, and full power is where output varies most.",
            "<b>+3</b>, then <b>−4</b>, then anything else. Shoot each duplicate right after its original.",
        ]
    )
)

story.append(Paragraph("5. Grey-card frames (optional)", H3))
story.append(
    Paragraph(
        "Tape a grey card to the target, in frame alongside the main surface, and shoot two or three "
        "exposures. This confirms your target is really neutral. Do these last, since taping the card "
        "disturbs the setup.",
        BODY,
    )
)

story.append(Paragraph("Developing", H2))
story.append(
    Paragraph(
        "Process normally. The measurement barely cares how hard the film was developed, because it compares "
        "channels within each frame. What it <i>does</i> care about is silver left behind in the film: that "
        "adds grey density on top of the dyes, and it corrupts the result.",
        BODY,
    )
)
story.append(
    bullets(
        [
            (
                "Use fresh chemistry, and give <b>bleach and fix 50–100% more time</b> than usual. Unlike development, "
                "those steps run to completion, so extra time costs nothing."
            ),
            "Agitate the bleach well so it stays aerated.",
            (
                "If in doubt, cut off one dense frame and run it through fresh bleach and fix again. If its density "
                "drops, the roll needs rebleaching before it's measured."
            ),
        ]
    )
)

story.append(
    KeepTogether(
        [
            Paragraph("Scanning", H2),
            bullets(
                [
                    "Scan on your normal rig, with identical settings for every frame.",
                    "<b>One capture per frame</b> — these aren't tiled scans and never go through stitch.",
                    "Include the blank frames in the scans.",
                    "If you shoot a bare-light reference, <b>check it isn't clipped</b> — it's useless if any channel hits maximum.",
                    "Keep your frame log with the files.",
                ]
            ),
        ]
    )
)

story.append(
    KeepTogether(
        [
            Paragraph("Before you start: a last check", H2),
            bullets(
                [
                    "Room dark, flash manual, ISO and shutter fixed.",
                    "Camera, flash and target locked down; only the aperture ring and power dial move.",
                    "Tape L in frame, inside an unshadowed area.",
                    "Worksheet filled in, both ends of the series checked.",
                    (
                        "One roll, one film stock, and enough frames: 11 for the series, plus 2 blanks, 3 overlaps and "
                        "any duplicates — around 20."
                    ),
                ]
            ),
        ]
    )
)

doc = SimpleDocTemplate(
    OUT,
    pagesize=letter,
    leftMargin=0.95 * inch,
    rightMargin=0.95 * inch,
    topMargin=0.85 * inch,
    bottomMargin=0.85 * inch,
    title="Shooting a film-curve test roll",
    author="scanny-boy",
)


def footer(canvas, doc_):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(INK2)
    canvas.drawString(0.95 * inch, 0.55 * inch, "scanny-boy — film-curve test roll")
    canvas.drawRightString(letter[0] - 0.95 * inch, 0.55 * inch, f"{doc_.page}")
    canvas.restoreState()


doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
