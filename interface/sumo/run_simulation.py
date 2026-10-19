#!/usr/bin/env python3
"""SUMO TraCI driver: run the decision layer against a 4-way intersection.

This is the seam between the decision layer and a (simulated) signal controller:

  * perception adapter: reads vehicles per approach lane (perception stand-in);
  * controller: turns ``DecisionLayer.tick``'s ``PhaseDecision`` into a SOFT
    phase request, executed as yellow -> all-red -> green using the audited
    clearance timings from config;
  * the light is still the (simulated) controller's to switch: this driver only
    calls ``setRedYellowGreenState``, mirroring the Hikvision model where the
    centre only INDICATES a phase and the cabinet enforces lamp safety.

Run (Ubuntu 24.04, SUMO 1.28.0):
    export SUMO_HOME=/usr/share/sumo
    ./build.sh
    python run_simulation.py          # headless
    python run_simulation.py --gui    # graphical
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# SUMO ships ``traci`` under $SUMO_HOME/tools.
if "SUMO_HOME" in os.environ:
    sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))

import traci  # noqa: E402

# interface/sumo -> repo root is two directories up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from traffic_control.decision_layer import DecisionLayer  # noqa: E402
from traffic_control.models import (  # noqa: E402
    ControllerMode,
    ControllerState,
    VehicleObservation,
)

# Canonical movement id -> SUMO (edge, lane index).
MOVEMENT_TO_LANE = {
    "north_through": "N2C_0",
    "north_left": "N2C_1",
    "east_through": "E2C_0",
    "east_left": "E2C_1",
    "south_through": "S2C_0",
    "south_left": "S2C_1",
    "west_through": "W2C_0",
    "west_left": "W2C_1",
}

_track_counter = 0
_track_ids: dict[str, int] = {}


def _track_id(vehicle_id: str) -> int:
    """Stable int track id for a SUMO vehicle id (perception stand-in)."""
    global _track_counter
    if vehicle_id not in _track_ids:
        _track_ids[vehicle_id] = _track_counter
        _track_counter += 1
    return _track_ids[vehicle_id]


class SumoSignalController:
    """Applies a PhaseDecision as a soft phase request with clearance timing."""

    def __init__(self, layer: DecisionLayer, tls_id: str = "center") -> None:
        self.layer = layer
        self.tls_id = tls_id
        self.phases = layer.runtime.phases
        self.groups = layer.runtime.phase_groups

        self.controlled_lanes = list(
            traci.trafficlight.getControlledLanes(tls_id)
        )
        self.lane_to_movement = {
            lane: movement for movement, lane in MOVEMENT_TO_LANE.items()
        }
        missing = [
            lane for lane in self.controlled_lanes if lane not in self.lane_to_movement
        ]
        if missing:
            raise RuntimeError(f"unmapped controlled lanes: {missing}")

        self.current_phase_id = self.groups[0].phase_id
        self.green_started_at = 0.0
        self.last_reason = "initial"
        self._transition: tuple | None = None

        self._set_green(self.current_phase_id)

    # -- state-string helpers ----------------------------------------------
    def _state(self, phase_id: str, char: str) -> str:
        movements = self.phases[phase_id].movement_ids
        return "".join(
            char if self.lane_to_movement[lane] in movements else "r"
            for lane in self.controlled_lanes
        )

    def _set_green(self, phase_id: str) -> None:
        traci.trafficlight.setRedYellowGreenState(
            self.tls_id, self._state(phase_id, "G")
        )

    def _set_yellow(self, phase_id: str) -> None:
        traci.trafficlight.setRedYellowGreenState(
            self.tls_id, self._state(phase_id, "y")
        )

    def _set_all_red(self) -> None:
        traci.trafficlight.setRedYellowGreenState(
            self.tls_id, "r" * len(self.controlled_lanes)
        )

    # -- one control step --------------------------------------------------
    def step(self, now: float) -> None:
        if self._transition is not None:
            self._advance_transition(now)
            return

        observations = self._collect_observations(now)
        state = ControllerState(
            mode=ControllerMode.NORMAL,
            current_phase_id=self.current_phase_id,
            phase_started_at=self.green_started_at,
        )
        result = self.layer.tick(
            now, state, observations, downstream_occupancy=None
        )

        target = result.decision.selected_phase_id
        if target is not None and target != self.current_phase_id:
            self.last_reason = result.decision.reason
            self._begin_switch(target, now)

    def _collect_observations(self, now: float) -> list[VehicleObservation]:
        observations: list[VehicleObservation] = []
        for movement, lane_id in MOVEMENT_TO_LANE.items():
            for vehicle_id in traci.lane.getLastStepVehicleIDs(lane_id):
                observations.append(
                    VehicleObservation(
                        track_id=_track_id(vehicle_id),
                        lane_id=movement,
                        observed_at=now,
                        speed_mps=traci.vehicle.getSpeed(vehicle_id),
                    )
                )
        return observations

    def _begin_switch(self, target: str, now: float) -> None:
        yellow_s = self.phases[self.current_phase_id].yellow_s
        all_red_s = self.phases[self.current_phase_id].all_red_s
        self._set_yellow(self.current_phase_id)
        # (stage, target, until, all_red_s)
        self._transition = ("yellow", target, now + yellow_s, all_red_s)

    def _advance_transition(self, now: float) -> None:
        stage, target, until, all_red_s = self._transition
        if now < until:
            return
        if stage == "yellow":
            self._set_all_red()
            self._transition = ("all_red", target, now + all_red_s, all_red_s)
        else:  # all_red elapsed -> new green
            self._set_green(target)
            self.current_phase_id = target
            self.green_started_at = now
            self._transition = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true", help="run in sumo-gui")
    parser.add_argument(
        "--config",
        default=str(_PROJECT_ROOT / "configs" / "simulation.yaml"),
        help="decision-layer YAML config",
    )
    args = parser.parse_args()

    layer = DecisionLayer.from_yaml(args.config)

    binary = "sumo-gui" if args.gui else "sumo"
    cfg = str(Path(__file__).with_name("intersection.sumocfg"))
    traci.start([binary, "-c", cfg])

    controller = SumoSignalController(layer)
    end_time = traci.simulation.getEndTime()

    print(f"{'t':>6}  {'phase':>6}  {'reason':<22}  {'vehicles':>9}  {'stopped':>8}")
    try:
        step = 0
        while (
            traci.simulation.getMinExpectedNumber() > 0
            and traci.simulation.getTime() < end_time
        ):
            traci.simulationStep()
            now = traci.simulation.getTime()
            controller.step(now)
            step += 1

            if step % 60 == 0 or now >= end_time:
                vehicles = traci.vehicle.getIDCount()
                stopped = sum(
                    1
                    for v in traci.vehicle.getIDList()
                    if traci.vehicle.getSpeed(v) < 0.1
                )
                print(
                    f"{now:6.0f}  {controller.current_phase_id:>6}  "
                    f"{controller.last_reason:<22}  {vehicles:9d}  {stopped:8d}"
                )
    finally:
        traci.close()

    print("simulation finished")


if __name__ == "__main__":
    main()
