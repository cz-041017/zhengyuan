from traffic_control.degradation import (
    DataFreshness,
    DegradationController,
    DegradationMode,
    conservative_oldest_wait,
)


PHASES = {"P_N", "P_E", "P_S", "P_W"}


def test_freshness_fresh_within_timeout_and_never_seen_stale() -> None:
    tracker = DataFreshness(timeout_s=3.0)

    result = tracker.assess(10.0, PHASES, {"P_N": 9.0, "P_E": 8.0})

    assert "P_N" in result.fresh
    assert "P_E" in result.fresh
    # Never-seen phases are stale (unknown), never "empty".
    assert result.stale == frozenset({"P_S", "P_W"})


def test_freshness_stale_beyond_timeout() -> None:
    tracker = DataFreshness(timeout_s=3.0)

    result = tracker.assess(10.0, {"P_N"}, {"P_N": 6.0})  # 4 s old

    assert result.fresh == frozenset()
    assert result.stale == frozenset({"P_N"})


def test_freshness_future_timestamp_is_stale() -> None:
    tracker = DataFreshness(timeout_s=3.0)

    result = tracker.assess(10.0, {"P_N"}, {"P_N": 12.0})

    assert result.stale == frozenset({"P_N"})


def test_degradation_normal_partial_failsafe() -> None:
    controller = DegradationController(timeout_s=3.0)

    all_fresh = {"P_N": 9.0, "P_E": 9.0, "P_S": 9.0, "P_W": 9.0}
    assert controller.decide(10.0, PHASES, all_fresh) == DegradationMode.NORMAL

    some_stale = {"P_N": 9.0, "P_E": 9.0, "P_S": 9.0, "P_W": 5.0}
    assert (
        controller.decide(10.0, PHASES, some_stale) == DegradationMode.PARTIAL
    )

    all_stale = {"P_N": 5.0, "P_E": 5.0, "P_S": 5.0, "P_W": 5.0}
    assert (
        controller.decide(10.0, PHASES, all_stale) == DegradationMode.FAILSAFE
    )


def test_degradation_failsafe_when_no_data() -> None:
    controller = DegradationController(timeout_s=3.0)

    assert controller.decide(10.0, PHASES, {}) == DegradationMode.FAILSAFE


def test_conservative_wait_fresh_uses_observed() -> None:
    assert conservative_oldest_wait(10.0, 9.0, 30.0, 3.0) == 30.0


def test_conservative_wait_stale_assumes_since_last_seen() -> None:
    # Last seen 5 s ago (stale): wait is at least now - last_seen = 5 s.
    assert conservative_oldest_wait(10.0, 5.0, 2.0, 3.0) == 5.0
    # Never less than the observed wait either.
    assert conservative_oldest_wait(10.0, 5.0, 10.0, 3.0) == 10.0


def test_conservative_wait_never_seen_assumes_since_startup() -> None:
    assert conservative_oldest_wait(10.0, None, 0.0, 3.0) == 10.0
