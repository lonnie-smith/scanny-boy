import numpy as np
import pytest

from scanny_boy.detection import DETECTION_LONG_EDGE, USE_CLAHE, build_detection_image
from scanny_boy.events import Code
from scanny_boy.linear import encode_from_linear
from scanny_boy.raw_decode import decode_raw
from scanny_boy.registration import detect_features, register_pair
from scanny_boy.sample_nef_support import FIXTURES_DIR
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene

_SCENE_SIZE = (1700, 3400)
_FRAME_SIZE = (1400, 2100)


def _build_pair_features(rotations_deg, *, overlap=0.25, seed=1):
    scene = synthetic_scene(*_SCENE_SIZE, seed=seed)
    frames, placements = cut_frames(
        scene,
        frame_size=_FRAME_SIZE,
        count=len(rotations_deg),
        overlap=overlap,
        rotations_deg=rotations_deg,
        seed=seed,
    )
    features = []
    for i, frame in enumerate(frames):
        linear_rgb = np.stack([frame, frame, frame], axis=-1)
        intermediate = encode_from_linear(linear_rgb)
        detection = build_detection_image(
            intermediate, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
        )
        features.append(detect_features(detection, name=f"frame{i}"))
    return features, placements


def _expected_relative_transform(placements, i, j):
    """`placements[k]` is `[R_k | t_k]`, frame k -> scene. Returns the
    expected `(rotation, translation)` mapping frame j into frame i, which
    is what `register_pair(features[i], features[j])` should recover."""
    rotation_i, translation_i = placements[i][:, :2], placements[i][:, 2]
    rotation_j, translation_j = placements[j][:, :2], placements[j][:, 2]
    rotation = rotation_i.T @ rotation_j
    translation = rotation_i.T @ (translation_j - translation_i)
    return rotation, translation


def _angle_deg(rotation):
    return np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0]))


def test_recovers_a_known_rotation_and_translation():
    features, placements = _build_pair_features([0.0, 5.0])
    result = register_pair(features[0], features[1])
    assert result.accepted

    expected_rotation, expected_translation = _expected_relative_transform(
        placements, 0, 1
    )
    recovered_angle = _angle_deg(result.transform[:, :2])
    expected_angle = _angle_deg(expected_rotation)

    assert abs(recovered_angle - expected_angle) < 0.1
    assert np.linalg.norm(result.transform[:, 2] - expected_translation) < 1.0
    assert result.scale_drift < 0.001


@pytest.mark.parametrize("angle_deg", [0, 1, 2, 3, 5, 8])
def test_recovers_across_the_rotation_range(angle_deg):
    features, placements = _build_pair_features([0.0, float(angle_deg)])
    result = register_pair(features[0], features[1])
    assert result.accepted

    expected_rotation, _ = _expected_relative_transform(placements, 0, 1)
    recovered_angle = _angle_deg(result.transform[:, :2])
    expected_angle = _angle_deg(expected_rotation)

    assert abs(recovered_angle - expected_angle) < 0.1


def test_recovers_at_minimum_overlap():
    features, placements = _build_pair_features([0.0, 0.0], overlap=0.20)
    result = register_pair(features[0], features[1])
    assert result.accepted

    _, expected_translation = _expected_relative_transform(placements, 0, 1)
    assert np.linalg.norm(result.transform[:, 2] - expected_translation) < 1.0


def test_reverse_pair_is_the_inverse_transform():
    features, _ = _build_pair_features([0.0, 4.0])
    forward = register_pair(features[0], features[1])
    backward = register_pair(features[1], features[0])
    assert forward.accepted
    assert backward.accepted

    rotation_ab, translation_ab = forward.transform[:, :2], forward.transform[:, 2]
    rotation_ba, translation_ba = backward.transform[:, :2], backward.transform[:, 2]

    composed_rotation = rotation_ba @ rotation_ab
    composed_translation = rotation_ba @ translation_ab + translation_ba

    assert np.allclose(composed_rotation, np.eye(2), atol=1e-3)
    assert np.linalg.norm(composed_translation) < 1.0


def test_non_overlapping_pair_is_rejected():
    features = []
    for i, seed in enumerate([10, 20]):
        scene = synthetic_scene(*_FRAME_SIZE, seed=seed)
        linear_rgb = np.stack([scene, scene, scene], axis=-1)
        intermediate = encode_from_linear(linear_rgb)
        detection = build_detection_image(
            intermediate, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
        )
        features.append(detect_features(detection, name=f"independent{i}"))

    result = register_pair(features[0], features[1])
    assert result.accepted is False
    assert result.reject_code is Code.STITCH_INSUFFICIENT_MATCHES


def test_rigid_fit_never_returns_a_scale_other_than_one():
    features, _ = _build_pair_features([0.0, 3.0])
    result = register_pair(features[0], features[1])
    assert result.accepted

    rotation = result.transform[:, :2]
    scale = np.hypot(rotation[0, 0], rotation[1, 0])
    assert abs(scale - 1.0) < 1e-9


# Appendix C: which pairs of each gate-B negative genuinely share film, and
# which end-to-end pairs do not despite both frames belonging to the same
# negative. `mismatch`'s three frames share no film with each other at all.
_GATE_B_NEGATIVES = {
    "normal": {
        "frames": ["normal_1.NEF", "normal_2.NEF", "normal_3.NEF"],
        "overlapping": [(1, 2), (2, 3)],
        "sharing_no_film": [(1, 3)],
    },
    "wonky": {
        "frames": ["wonky_1.NEF", "wonky_2.NEF", "wonky_3.NEF"],
        "overlapping": [(1, 2), (2, 3), (1, 3)],
        "sharing_no_film": [],
    },
    "order": {
        "frames": ["order_1.NEF", "order_2.NEF", "order_3.NEF"],
        "overlapping": [(1, 2), (2, 3), (1, 3)],
        "sharing_no_film": [],
    },
    "tight": {
        "frames": ["tight_1.NEF", "tight_2.NEF", "tight_3.NEF"],
        "overlapping": [(1, 3), (2, 3)],
        "sharing_no_film": [(1, 2)],
    },
    "mismatch": {
        "frames": ["mismatch_1.NEF", "mismatch_2.NEF", "mismatch_3.NEF"],
        "overlapping": [],
        "sharing_no_film": [(1, 2), (1, 3), (2, 3)],
    },
}

_gate_b_missing = sorted(
    {
        name
        for negative in _GATE_B_NEGATIVES.values()
        for name in negative["frames"]
        if not (FIXTURES_DIR / name).exists()
    }
)

requires_gate_b_samples = pytest.mark.skipif(
    bool(_gate_b_missing),
    reason=(
        "gate-B sample NEFs not present at tests/fixtures/nef/ (see "
        "docs/PHASE2_IMPLEMENTATION_PLAN.md appendix C); missing: "
        f"{_gate_b_missing}"
    ),
)


@requires_gate_b_samples
def test_real_sample_pairs_meet_their_gates():
    for negative_name, info in _GATE_B_NEGATIVES.items():
        features_by_index = {}
        for index, filename in enumerate(info["frames"], start=1):
            decoded = decode_raw(FIXTURES_DIR / filename)
            detection = build_detection_image(
                decoded.pixels, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
            )
            features_by_index[index] = detect_features(detection, name=filename)

        for i, j in info["overlapping"]:
            result = register_pair(features_by_index[i], features_by_index[j])
            assert result.accepted, (
                f"{negative_name} {i}-{j} should be accepted: "
                f"{result.reject_message}"
            )

        for i, j in info["sharing_no_film"]:
            result = register_pair(features_by_index[i], features_by_index[j])
            assert not result.accepted, (
                f"{negative_name} {i}-{j} should be rejected"
            )
