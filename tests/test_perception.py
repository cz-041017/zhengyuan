import pytest

from traffic_control.perception import (
    CANONICAL_LANE_IDS,
    DownstreamReport,
    RawDetection,
    downstream_occupancy,
    normalize_detections,
)


def test_canonical_lane_ids_cover_eight_movements() -> None:
    assert len(CANONICAL_LANE_IDS) == 8
    assert "north_through" in CANONICAL_LANE_IDS
    assert "south_left" in CANONICAL_LANE_IDS


def test_normalize_detections_converts_and_validates() -> None:
    detections = normalize_detections(
        [
            RawDetection(
                track_id=1,
                lane_id="north_through",
                observed_at=10.0,
                speed_mps=0.2,
            ),
            RawDetection(
                track_id=2,
                lane_id="east_left",
                observed_at=10.0,
                speed_mps=None,
            ),
        ]
    )

    assert len(detections) == 2
    assert detections[0].track_id == 1
    assert detections[0].speed_mps == 0.2
    assert detections[1].speed_mps is None


def test_normalize_detections_rejects_unknown_lane() -> None:
    with pytest.raises(ValueError, match="unknown lane_id"):
        normalize_detections(
            [
                RawDetection(
                    track_id=1, lane_id="north_right", observed_at=10.0
                ),
            ]
        )


def test_raw_detection_rejects_bad_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        RawDetection(
            track_id=1,
            lane_id="north_through",
            observed_at=1.0,
            confidence=2.0,
        )


def test_downstream_report_rejects_bad_occupancy() -> None:
    with pytest.raises(ValueError, match="occupancy"):
        DownstreamReport(lane_id="east_through", occupancy=1.5, observed_at=1.0)

    # None (unknown) is allowed and must not be coerced to a number.
    report = DownstreamReport(lane_id="east_through", occupancy=None, observed_at=1.0)
    assert report.occupancy is None


def test_downstream_occupancy_indexes_by_lane() -> None:
    indexed = downstream_occupancy(
        [
            DownstreamReport(lane_id="east_through", occupancy=0.9, observed_at=1.0),
            DownstreamReport(lane_id="west_through", occupancy=None, observed_at=1.0),
        ]
    )

    assert indexed == {"east_through": 0.9, "west_through": None}
