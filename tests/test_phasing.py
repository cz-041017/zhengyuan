import pytest

from traffic_control.phasing import (
    APPROACH_MOVEMENTS,
    ConflictMatrix,
    LayoutMode,
    PhaseGroup,
    phase_groups,
)


def test_minor_only_builds_four_merged_approaches() -> None:
    groups = phase_groups(LayoutMode.MINOR_ONLY)

    assert [g.phase_id for g in groups] == ["P_N", "P_E", "P_S", "P_W"]
    for group in groups:
        # Each approach merges through + left, none is green-wave eligible.
        assert group.movement_ids == APPROACH_MOVEMENTS[
            group.phase_id[-1]
        ]
        assert group.green_wave_eligible is False


def test_main_road_ns_builds_green_wave_groups() -> None:
    groups = phase_groups(LayoutMode.MAIN_ROAD, "NS")

    assert [g.phase_id for g in groups] == ["P_MT", "P_ML", "P_E", "P_W"]

    by_id = {g.phase_id: g for g in groups}
    assert by_id["P_MT"].movement_ids == frozenset(
        {"north_through", "south_through"}
    )
    assert by_id["P_MT"].green_wave_eligible is True
    assert by_id["P_ML"].movement_ids == frozenset(
        {"north_left", "south_left"}
    )
    assert by_id["P_ML"].green_wave_eligible is False
    assert by_id["P_E"].movement_ids == APPROACH_MOVEMENTS["E"]
    assert by_id["P_W"].movement_ids == APPROACH_MOVEMENTS["W"]


def test_main_road_ew_swaps_axes() -> None:
    groups = phase_groups(LayoutMode.MAIN_ROAD, "EW")

    by_id = {g.phase_id: g for g in groups}
    assert by_id["P_MT"].movement_ids == frozenset(
        {"east_through", "west_through"}
    )
    assert by_id["P_ML"].movement_ids == frozenset(
        {"east_left", "west_left"}
    )
    assert by_id["P_N"].movement_ids == APPROACH_MOVEMENTS["N"]
    assert by_id["P_S"].movement_ids == APPROACH_MOVEMENTS["S"]


def test_main_road_requires_direction() -> None:
    with pytest.raises(ValueError, match="main_road_direction"):
        phase_groups(LayoutMode.MAIN_ROAD, None)


def test_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown layout mode"):
        phase_groups("ring")


def test_phase_group_rejects_empty_movements() -> None:
    with pytest.raises(ValueError, match="at least one movement"):
        PhaseGroup("X", frozenset())


def test_conflict_matrix_symmetric() -> None:
    matrix = ConflictMatrix(
        {
            "P_N": frozenset({"P_S"}),
            "P_S": frozenset({"P_N"}),
        }
    )
    assert matrix.are_conflicting("P_N", "P_S") is True
    assert matrix.are_conflicting("P_S", "P_N") is True
    assert matrix.are_conflicting("P_N", "P_N") is False
    assert matrix.conflicts_of("P_N") == frozenset({"P_S"})


def test_conflict_matrix_rejects_asymmetric_edge() -> None:
    with pytest.raises(ValueError, match="not symmetric"):
        ConflictMatrix(
            {
                "P_N": frozenset({"P_S"}),
                "P_S": frozenset(),
            }
        )


def test_conflict_matrix_rejects_self_conflict() -> None:
    with pytest.raises(ValueError, match="conflicts with itself"):
        ConflictMatrix({"P_N": frozenset({"P_N"})})


def test_conflict_matrix_rejects_unknown_reference() -> None:
    with pytest.raises(ValueError, match="unknown phase"):
        ConflictMatrix({"P_N": frozenset({"P_X"})})
