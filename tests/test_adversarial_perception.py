"""回归测试：下游占用 lane_id 必须做规范校验（漏洞7，已修复）。

拼错的下游 lane_id 会静默进入 SpillbackMonitor 成为死键，导致 P1 溢流保护
静默失效。DownstreamReport 现在与 normalize_detections 一致地对非法 lane_id
fail-fast（ValueError）。
"""
from __future__ import annotations

import pytest

from traffic_control.perception import (
    DownstreamReport,
    RawDetection,
    downstream_occupancy,
    normalize_detections,
)


def test_normalize_detections_rejects_unknown_lane_id() -> None:
    """基线：车辆检测对非法 lane_id fail-fast。"""
    with pytest.raises(ValueError):
        normalize_detections(
            [RawDetection(track_id=1, lane_id="west_thru", observed_at=1.0)]
        )


def test_downstream_occupancy_rejects_unknown_lane_id() -> None:
    """downstream_occupancy 必须拒绝非法 lane_id。"""
    with pytest.raises(ValueError):
        downstream_occupancy(
            [DownstreamReport(lane_id="west_thru", occupancy=0.9, observed_at=1.0)]
        )
