from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# ---------------------------------------------------------------------------
# Movement identifiers. A "phase" is a set of movements that may run
# simultaneously and that have passed the audited conflict review.
# ---------------------------------------------------------------------------

NORTH_THROUGH = "north_through"
NORTH_LEFT = "north_left"
EAST_THROUGH = "east_through"
EAST_LEFT = "east_left"
SOUTH_THROUGH = "south_through"
SOUTH_LEFT = "south_left"
WEST_THROUGH = "west_through"
WEST_LEFT = "west_left"

APPROACH_MOVEMENTS = {
    "N": frozenset({NORTH_THROUGH, NORTH_LEFT}),
    "E": frozenset({EAST_THROUGH, EAST_LEFT}),
    "S": frozenset({SOUTH_THROUGH, SOUTH_LEFT}),
    "W": frozenset({WEST_THROUGH, WEST_LEFT}),
}

THROUGH_MOVEMENTS = {
    "N": NORTH_THROUGH,
    "E": EAST_THROUGH,
    "S": SOUTH_THROUGH,
    "W": WEST_THROUGH,
}

LEFT_MOVEMENTS = {
    "N": NORTH_LEFT,
    "E": EAST_LEFT,
    "S": SOUTH_LEFT,
    "W": WEST_LEFT,
}

# ---------------------------------------------------------------------------
# Pedestrian crossings. Each crossing is named by the side of the intersection
# it lies on. A phase that releases vehicles may only release the crossing(s)
# that do not conflict with the released movements.
# ---------------------------------------------------------------------------

CROSSINGS = {
    "N": "north_crossing",
    "E": "east_crossing",
    "S": "south_crossing",
    "W": "west_crossing",
}

# Ordinary (non green-wave) junction: the crossing adjacent to each approach
# in clockwise order (N->E, E->S, S->W, W->N). This is the side a left-turning
# vehicle exits away from, so through + left movements stay conflict-free.
NEXT_SIDE_CROSSING = {"N": "E", "E": "S", "S": "W", "W": "N"}


class LayoutMode:
    """Supported intersection layouts."""

    MINOR_ONLY = "minor_only"
    MAIN_ROAD = "main_road"


@dataclass(frozen=True)
class PhaseGroup:
    """A configured group of movements that form one signal phase.

    ``green_wave_eligible`` marks the main-road through phase that may
    receive the P2 green-wave bonus; at most one group per layout.
    """

    phase_id: str
    movement_ids: frozenset[str]
    green_wave_eligible: bool = False
    crosswalk_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.phase_id.strip():
            raise ValueError("phase_id must not be empty")
        if not self.movement_ids:
            raise ValueError("a phase group must contain at least one movement")


def phase_groups(
    mode: str, main_road_direction: str | None = None
) -> tuple[PhaseGroup, ...]:
    """Build the phase groups for a layout.

    mode A (``minor_only``): each approach merges through + left into one
        group, producing four groups compared purely by score.

    mode B (``main_road``): the main road's bidirectional through movements
        form the green-wave group; the main road's bidirectional left turns
        form one group; each minor approach merges through + left.
    """
    if mode == LayoutMode.MINOR_ONLY:
        return tuple(
            PhaseGroup(
                f"P_{approach}",
                APPROACH_MOVEMENTS[approach],
                crosswalk_ids=frozenset(
                    {CROSSINGS[NEXT_SIDE_CROSSING[approach]]}
                ),
            )
            for approach in ("N", "E", "S", "W")
        )

    if mode == LayoutMode.MAIN_ROAD:
        if main_road_direction not in ("NS", "EW"):
            raise ValueError(
                "main_road_direction must be 'NS' or 'EW' in main_road mode"
            )
        if main_road_direction == "NS":
            main_through = frozenset({NORTH_THROUGH, SOUTH_THROUGH})
            main_left = frozenset({NORTH_LEFT, SOUTH_LEFT})
            minor_first = APPROACH_MOVEMENTS["E"]
            minor_second = APPROACH_MOVEMENTS["W"]
            minor_first_id, minor_second_id = "P_E", "P_W"
        else:  # EW
            main_through = frozenset({EAST_THROUGH, WEST_THROUGH})
            main_left = frozenset({EAST_LEFT, WEST_LEFT})
            minor_first = APPROACH_MOVEMENTS["N"]
            minor_second = APPROACH_MOVEMENTS["S"]
            minor_first_id, minor_second_id = "P_N", "P_S"

        # Green-wave rules (project decision): the main-road through phase
        # releases both crossings alongside the main road; the main-road left
        # turn releases none (all-red for pedestrians); each minor approach
        # releases one crossing.
        if main_road_direction == "NS":
            mt_crossings = frozenset({CROSSINGS["E"], CROSSINGS["W"]})
            first_crossing = frozenset({CROSSINGS["N"]})  # east approach -> N
            second_crossing = frozenset({CROSSINGS["S"]})  # west approach -> S
        else:  # EW
            mt_crossings = frozenset({CROSSINGS["N"], CROSSINGS["S"]})
            # The crossing on the *left-turn exit* side is the one the left
            # turn would drive through; pick the opposite side so through and
            # left are both conflict-free (mirrors the NS rule).
            first_crossing = frozenset({CROSSINGS["W"]})  # north approach -> W
            second_crossing = frozenset({CROSSINGS["E"]})  # south approach -> E

        return (
            PhaseGroup(
                "P_MT",
                main_through,
                green_wave_eligible=True,
                crosswalk_ids=mt_crossings,
            ),
            PhaseGroup("P_ML", main_left, crosswalk_ids=frozenset()),
            PhaseGroup(minor_first_id, minor_first, crosswalk_ids=first_crossing),
            PhaseGroup(minor_second_id, minor_second, crosswalk_ids=second_crossing),
        )

    raise ValueError(f"unknown layout mode: {mode!r}")


@dataclass(frozen=True)
class ConflictMatrix:
    """Audited phase-conflict relations.

    Conflict data must come from the reviewed junction configuration
    (GB 25280 + local conflict table); it is never inferred from movement
    overlap alone. The matrix is validated to be symmetric and to reference
    only known phases.
    """

    conflicts: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        for phase_id, others in self.conflicts.items():
            if phase_id in others:
                raise ValueError(
                    f"phase {phase_id!r} conflicts with itself"
                )
            for other in others:
                if other not in self.conflicts:
                    raise ValueError(
                        f"conflict references unknown phase {other!r}"
                    )
                if phase_id not in self.conflicts[other]:
                    raise ValueError(
                        f"conflict matrix is not symmetric: "
                        f"{phase_id!r} -> {other!r} has no reverse edge"
                    )

    def conflicts_of(self, phase_id: str) -> frozenset[str]:
        return self.conflicts.get(phase_id, frozenset())

    def are_conflicting(self, first: str, second: str) -> bool:
        if first == second:
            return False
        return second in self.conflicts.get(first, frozenset())
