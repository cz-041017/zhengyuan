from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from traffic_control.config_loader import load_config, validate_config
from traffic_control.degradation import (
    DegradationController,
    DegradationMode,
    conservative_oldest_wait,
)
from traffic_control.feedback import FeedbackMonitor, FeedbackStatus
from traffic_control.models import (
    ControllerState,
    LaneState,
    PhaseDecision,
    VehicleObservation,
)
from traffic_control.phasing import PhaseGroup
from traffic_control.queue_estimator import QueueEstimator
from traffic_control.runtime import RuntimeObjects, build_runtime
from traffic_control.scoring import ScoreConfig, aggregate_demand
from traffic_control.state_machine import MachineConfig, PhaseDemand


@dataclass(frozen=True)
class TunableParameters:
    """Every tunable knob of the decision layer, in one place.

    Phase timings (min/max green, yellow, all-red) are audited data and live on
    the phases, not here; everything an operator would tune for behaviour is
    collected in this object.
    """

    observation_timeout_s: float
    confirm_timeout_s: float
    queue_speed_threshold_mps: float
    vehicle_length_m: float
    stopped_gap_m: float
    stale_after_s: float
    score: ScoreConfig
    machine: MachineConfig

    @property
    def max_wait_target_s(self) -> float:
        """The P0 single-direction maximum wait target (seconds)."""
        return self.score.max_wait_target_s


@dataclass(frozen=True)
class LayerResult:
    """The full outcome of one decision tick."""

    now: float
    decision: PhaseDecision
    degradation: DegradationMode
    feedback: FeedbackStatus
    demands: Mapping[str, PhaseDemand]
    lane_states: Mapping[str, LaneState]
    downstream_blocked: Mapping[str, bool]


class DecisionLayer:
    """The decision layer as a ready-to-use package.

    One ``tick`` turns raw perception output into a phase proposal. It wires
    together the parts a deployment or a simulator normally has to assemble by
    hand:

        queue estimation -> freshness / degradation -> conservative waits for
        stale directions -> spillback hysteresis -> phase state machine ->
        execution feedback

    Usage::

        layer = DecisionLayer.from_yaml("configs/simulation.yaml")
        result = layer.tick(
            now=12.0,
            state=controller_state,
            observations=detections,
            downstream_occupancy={"east_through": 0.9},
        )
        result.decision.selected_phase_id, result.decision.reason

    Downstream occupancy: report every movement you have data for. Pass an
    explicit ``None`` for a movement whose downstream state is unknown; that
    is treated as congested. A movement omitted from the mapping is simply not
    evaluated this tick.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        validate_config(config)
        runtime: RuntimeObjects = build_runtime(config)
        queue_config = config["queue_estimation"]

        self._runtime = runtime
        self._queue = QueueEstimator(
            queue_speed_threshold_mps=queue_config["queue_speed_threshold_mps"],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
        )
        self._spillback = runtime.spillback_monitor
        self._degradation = runtime.degradation
        self._feedback = runtime.feedback_monitor
        self._machine = runtime.state_machine
        self._groups: tuple[PhaseGroup, ...] = runtime.phase_groups

        self._parameters = TunableParameters(
            observation_timeout_s=config["runtime"]["observation_timeout_s"],
            confirm_timeout_s=config["runtime"]["confirm_timeout_s"],
            queue_speed_threshold_mps=queue_config[
                "queue_speed_threshold_mps"
            ],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
            score=runtime.score_config,
            machine=runtime.machine_config,
        )

        movements: set[str] = set()
        for group in self._groups:
            movements.update(group.movement_ids)
        self._movements = movements
        # Memory of the last tick each movement reported anything, so a dead
        # feed becomes "stale" (conservative wait) instead of "empty".
        self._last_seen: dict[str, float] = {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> DecisionLayer:
        """Build the layer from a YAML config file."""
        return cls(load_config(path))

    @property
    def parameters(self) -> TunableParameters:
        return self._parameters

    @property
    def runtime(self) -> RuntimeObjects:
        return self._runtime

    def tick(
        self,
        now: float,
        state: ControllerState,
        observations: Sequence[VehicleObservation] = (),
        downstream_occupancy: Mapping[str, float | None] | None = None,
        green_wave_active: Mapping[str, bool] | None = None,
    ) -> LayerResult:
        """Produce one decision from one perception frame."""
        occupancy = downstream_occupancy or {}
        green_wave = green_wave_active or {}

        blocked_movements: set[str] = set()
        for movement, value in occupancy.items():
            if self._spillback.update(movement, value):
                blocked_movements.add(movement)

        lane_states = self._queue.update(
            list(observations), now, blocked_movements
        )

        for movement in lane_states:
            if movement in self._movements:
                self._last_seen[movement] = now

        demands, last_seen, blocked_by_phase = self._aggregate(
            lane_states, blocked_movements, green_wave
        )

        phase_ids = [group.phase_id for group in self._groups]
        mode = self._degradation.decide(now, phase_ids, last_seen)
        demands = self._apply_conservative_waits(demands, last_seen, now)

        decision = self._machine.tick(state, now, demands)
        feedback = self._feedback.observe(decision, state, now)

        return LayerResult(
            now=now,
            decision=decision,
            degradation=mode,
            feedback=feedback,
            demands=demands,
            lane_states=lane_states,
            downstream_blocked=blocked_by_phase,
        )

    # -- internals ---------------------------------------------------------
    def _aggregate(
        self,
        lane_states: Mapping[str, LaneState],
        blocked_movements: set[str],
        green_wave: Mapping[str, bool],
    ) -> tuple[
        dict[str, PhaseDemand], dict[str, float | None], dict[str, bool]
    ]:
        demands: dict[str, PhaseDemand] = {}
        last_seen: dict[str, float | None] = {}
        blocked_by_phase: dict[str, bool] = {}

        for group in self._groups:
            counts: list[int] = []
            waits: list[float] = []
            seen: list[float] = []
            blocked = any(
                movement in blocked_movements
                for movement in group.movement_ids
            )
            for movement in sorted(group.movement_ids):
                lane = lane_states.get(movement)
                if lane is not None:
                    counts.append(lane.vehicle_count)
                    waits.append(lane.oldest_wait_s)
                    seen.append(lane.last_updated_at)
                remembered = self._last_seen.get(movement)
                if remembered is not None:
                    seen.append(remembered)

            vehicle_count, oldest_wait = aggregate_demand(counts, waits)
            demands[group.phase_id] = PhaseDemand(
                vehicle_count=vehicle_count,
                oldest_wait_s=oldest_wait,
                downstream_blocked=blocked,
                green_wave_active=green_wave.get(group.phase_id, False),
            )
            last_seen[group.phase_id] = max(seen) if seen else None
            blocked_by_phase[group.phase_id] = blocked

        return demands, last_seen, blocked_by_phase

    def _apply_conservative_waits(
        self,
        demands: Mapping[str, PhaseDemand],
        last_seen: Mapping[str, float | None],
        now: float,
    ) -> dict[str, PhaseDemand]:
        freshness = self._degradation.assess(now, demands.keys(), last_seen)
        timeout = self._parameters.observation_timeout_s
        resolved: dict[str, PhaseDemand] = {}

        for phase_id, demand in demands.items():
            if phase_id in freshness.stale:
                wait = conservative_oldest_wait(
                    now,
                    last_seen.get(phase_id),
                    demand.oldest_wait_s,
                    timeout,
                )
                demand = PhaseDemand(
                    vehicle_count=demand.vehicle_count,
                    oldest_wait_s=wait,
                    downstream_blocked=demand.downstream_blocked,
                    green_wave_active=demand.green_wave_active,
                )
            resolved[phase_id] = demand

        return resolved
