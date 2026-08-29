"""Pins section 2.1 of the Phase 2 plan: the OpenCV 4.x line and the symbols
Phase 2 depends on. OpenCV 5.0 drops AKAZE, which is still a live detector
candidate until Chunk P2-1 decides otherwise (see cli/pyproject.toml)."""

import cv2


def test_opencv_version_and_symbols():
    assert cv2.__version__.startswith("4.")

    for name in (
        "SIFT_create",
        "ORB_create",
        "AKAZE_create",
        "estimateAffinePartial2D",
        "distanceTransform",
        "warpAffine",
        "createCLAHE",
    ):
        assert hasattr(cv2, name), f"cv2.{name} is missing"
