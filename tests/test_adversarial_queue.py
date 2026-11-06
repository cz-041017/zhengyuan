"""回归测试：等待时间代理口径一致（漏洞6，已修复）。

oldest_wait_s 现在只统计停等车辆（speed <= 阈值），行驶中的车辆不计入等待
时间，避免虚高等待误触发 P0 抢占。
"""
from __future__ import annotations

from traffic_control.models import VehicleObservation
from traffic_control.queue_estimator import QueueEstimator


def test_oldest_wait_excludes_moving_vehicles() -> None:
    """行驶中的车辆（speed > 阈值）不应计入等待时间。"""
    est = QueueEstimator(
        queue_speed_threshold_mps=0.5,
        vehicle_length_m=5.0,
        stopped_gap_m=2.0,
        stale_after_s=3.0,
    )
    est.update(
        [
            VehicleObservation(
                track_id=1, lane_id="east_through", observed_at=0.0, speed_mps=5.0
            )
        ],
        now=0.0,
    )
    states = est.update(
        [
            VehicleObservation(
                track_id=1, lane_id="east_through", observed_at=2.0, speed_mps=5.0
            )
        ],
        now=2.0,
    )

    lane = states["east_through"]
    # 车辆一直在行驶（5 m/s > 0.5 m/s），等待时间应为 0。
    assert lane.oldest_wait_s == 0.0
    # 对照：排队长度正确地只统计停等车辆。
    assert lane.queue_length_m == 0.0
