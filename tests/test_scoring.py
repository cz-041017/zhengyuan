import pytest

from traffic_control.scoring import (
    ScoreConfig,
    Scorer,
    SpillbackMonitor,
    aggregate_demand,
)


def make_scorer() -> Scorer:
    return Scorer(ScoreConfig())


def test_demand_score_bounds_and_weights() -> None:
    scorer = make_scorer()

    # Empty direction scores zero.
    assert scorer.demand_score(0, 0.0) == pytest.approx(0.0)

    # Full vehicle queue and full wait saturate to 1.0.
    assert scorer.demand_score(20, 120.0) == pytest.approx(1.0)

    # Half queue (10/20) and half wait (60/120) -> 0.6*0.5 + 0.4*0.5 = 0.5
    assert scorer.demand_score(10, 60.0) == pytest.approx(0.5)


def test_demand_score_clips_above_saturation() -> None:
    scorer = make_scorer()
    assert scorer.demand_score(1000, 100000.0) == pytest.approx(1.0)


def test_green_wave_bonus() -> None:
    scorer = make_scorer()
    assert scorer.green_wave_score(True) == pytest.approx(2.0)
    assert scorer.green_wave_score(False) == pytest.approx(0.0)


def test_wait_urgency_ramp() -> None:
    scorer = make_scorer()

    assert scorer.wait_urgency_score(79.0) == pytest.approx(0.0)
    assert scorer.wait_urgency_score(80.0) == pytest.approx(0.0)
    # At 100s, x = 0.5, quadratic -> 5.0 * 0.25 = 1.25
    assert scorer.wait_urgency_score(100.0) == pytest.approx(1.25)
    # At 120s, x = 1.0 -> 5.0
    assert scorer.wait_urgency_score(120.0) == pytest.approx(5.0)
    # Clips past the deadline.
    assert scorer.wait_urgency_score(200.0) == pytest.approx(5.0)


def test_spillback_penalty() -> None:
    scorer = make_scorer()
    assert scorer.spillback_score(False) == pytest.approx(0.0)
    assert scorer.spillback_score(True) == pytest.approx(-1000.0)


def test_phase_score_composition_and_priority_order() -> None:
    scorer = make_scorer()

    # A blocked direction is always below any unblocked one (P1 dominates).
    blocked = scorer.phase_score(20, 0.0, downstream_blocked=True)
    unblocked = scorer.phase_score(0, 0.0)
    assert blocked < unblocked

    # Green wave outranks pure demand (P2 > P3): max demand (1.0) < +2.0.
    # Use a wait below the urgency threshold so only P3 contributes.
    demand_max = scorer.phase_score(20, 0.0)
    green_wave = scorer.phase_score(0, 0.0, green_wave_active=True)
    assert green_wave > demand_max

    # Wait urgency near deadline outranks green wave (P0 > P2):
    # 120s wait gives 5.0 urgency, exceeding +2.0.
    starving = scorer.phase_score(0, 120.0)
    assert starving > green_wave


def test_aggregate_demand_sums_and_maxes() -> None:
    assert aggregate_demand([3, 5, 2], [10.0, 40.0, 25.0]) == (10, 40.0)
    assert aggregate_demand([], []) == (0, 0.0)


def test_aggregate_demand_requires_aligned_inputs() -> None:
    with pytest.raises(ValueError, match="align"):
        aggregate_demand([1], [])


def test_score_config_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="o_low"):
        ScoreConfig(o_high=0.5, o_low=0.8)

    with pytest.raises(ValueError, match="spillback_penalty"):
        ScoreConfig(spillback_penalty=0.0)

    with pytest.raises(ValueError, match="wait_urgency_start_s"):
        ScoreConfig(wait_urgency_start_s=130.0)


def test_spillback_hysteresis() -> None:
    monitor = SpillbackMonitor(o_high=0.8, o_low=0.5)

    # Rises above high -> blocked.
    assert monitor.update("E", 0.9) is True
    assert monitor.is_blocked("E") is True

    # Stays blocked between low and high (hysteresis).
    assert monitor.update("E", 0.6) is True
    assert monitor.is_blocked("E") is True

    # Falls to low -> released.
    assert monitor.update("E", 0.5) is False
    assert monitor.is_blocked("E") is False


def test_spillback_first_low_occupancy_is_unblocked() -> None:
    monitor = SpillbackMonitor(o_high=0.8, o_low=0.5)

    # A direction first observed below o_high must be reported unblocked,
    # not raise KeyError (regression).
    assert monitor.update("E", 0.4) is False
    assert monitor.is_blocked("E") is False


def test_spillback_unknown_occupancy_treated_as_blocked() -> None:
    monitor = SpillbackMonitor(o_high=0.8, o_low=0.5)

    assert monitor.update("E", None) is True
    assert monitor.is_blocked("E") is True


def test_spillback_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="o_low"):
        SpillbackMonitor(o_high=0.5, o_low=0.6)
