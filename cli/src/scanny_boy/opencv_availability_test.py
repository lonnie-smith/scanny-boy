"""Pins the OpenCV 4.x line and the symbols this program depends on.
OpenCV 5.0 drops AKAZE, which is still a live detector candidate (see
cli/pyproject.toml)."""

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
        # Geometric calibration.
        "undistortPoints",
        "remap",
    ):
        assert hasattr(cv2, name), f"cv2.{name} is missing"

    assert hasattr(cv2, "aruco"), "cv2.aruco is missing"
    assert hasattr(cv2.aruco, "CharucoDetector"), "cv2.aruco.CharucoDetector is missing"
