from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from traffic_control.config_loader import load_config, validate_config
from traffic_control.degradation import (
    DegradationMode,
    conservative_oldest_wait,
)
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    LaneState,
    PhaseDecision,
    VehicleObservation,
)
from traffic_control.phasing import PhaseGroup
from traffic_control.queue_estimator import QueueEstimator
from traffic_control.runtime import RuntimeObjects, build_runtime
from traffic_control.scoring import ScoreConfig, aggregate_demand
from traffic_control.startup import StartupSequence, StartupStage
from traffic_control.state_machine import MachineConfig, PhaseDemand
from traffic_control.telemetry import (
    AlarmEvent,
    AlarmLevel,
    AlarmType,
    NullTelemetrySink,
    TelemetrySink,
)


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
    # Pedestrian guarantee knobs (exposed for the interface layer: it drives
    # the pedestrian lamps and the 3 s pedestrian-yellow from these values).
    pedestrian_max_red_s: float = 150.0
    pedestrian_min_green_s: float = 12.0
    pedestrian_yellow_s: float = 3.0

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
    startup: StartupStage | None = None
    # Crosswalks released by the current green phase (empty when silent).
    # The interface layer drives pedestrian lamps from this set.
    active_crosswalks: frozenset[str] = frozenset()
    # Remaining seconds of the pedestrian-yellow window (0 = not yellow).
    # Derived from the phase's minimum green: the last ``pedestrian.yellow_s``
    # seconds of green forbid new pedestrians from entering.
    pedestrian_yellow_remaining_s: float = 0.0


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

    Control-room takeover (single junction)::

        layer.takeover(now)   # silent; emits no proposals
        layer.release(now)    # in-memory reset + 5 s flash / 10 s all-red

    Telemetry: pass a ``TelemetrySink`` to receive alarms/logs. The default is
    a local-logging ``NullTelemetrySink``; no network code lives here.
    """

    def __init__(
        self,
        config: dict[str, Any],
        sink: TelemetrySink | None = None,
    ) -> None:
        validate_config(config)
        self._config = config
        self._sink: TelemetrySink = sink or NullTelemetrySink()
        self._runtime: RuntimeObjects = build_runtime(config)
        queue_config = config["queue_estimation"]

        self._queue = QueueEstimator(
            queue_speed_threshold_mps=queue_config["queue_speed_threshold_mps"],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
        )
        self._spillback = self._runtime.spillback_monitor
        self._degradation = self._runtime.degradation
        self._feedback = self._runtime.feedback_monitor
        self._machine = self._runtime.state_machine
        self._groups: tuple[PhaseGroup, ...] = self._runtime.phase_groups

        self._parameters = TunableParameters(
            observation_timeout_s=config["runtime"]["observation_timeout_s"],
            confirm_timeout_s=config["runtime"]["confirm_timeout_s"],
            queue_speed_threshold_mps=queue_config[
                "queue_speed_threshold_mps"
            ],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
            score=self._runtime.score_config,
            machine=self._runtime.machine_config,
            pedestrian_max_red_s=(config.get("pedestrian") or {}).get("max_red_s", 150.0),
            pedestrian_min_green_s=(config.get("pedestrian") or {}).get("min_green_s", 12.0),
            pedestrian_yellow_s=(config.get("pedestrian") or {}).get("yellow_s", 3.0),
        )

        movements: set[str] = set()
        for group in self._groups:
            movements.update(group.movement_ids)
        self._movements = movements
        # Memory of the last tick each movement reported anything, so a dead
        # feed becomes "stale" (conservative wait) instead of "empty".
        self._last_seen: dict[str, float] = {}
        # Manual override / startup gating.
        self._manual_override = False
        self._startup: StartupSequence | None = None
        self._last_mode: DegradationMode | None = None

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        sink: TelemetrySink | None = None,
    ) -> DecisionLayer:
        """Build the layer from a YAML config file."""
        return cls(load_config(path), sink=sink)

    @property
    def parameters(self) -> TunableParameters:
        return self._parameters

    @property
    def runtime(self) -> RuntimeObjects:
        return self._runtime

    @property
    def manual_override(self) -> bool:
        return self._manual_override

    @property
    def startup(self) -> StartupSequence | None:
        return self._startup

    # -- control-room takeover ---------------------------------------------
    def takeover(self, now: float) -> None:
        """Enter manual override: this junction stops emitting proposals."""
        if self._manual_override:
            return
        self._manual_override = True
        self._startup = None
        self._machine.reset_transient()
        self._sink.report_alarm(
            AlarmEvent(
                alarm_type=AlarmType.TAKEOVER,
                junction_id=None,
                now=now,
                level=AlarmLevel.WARNING,
            )
        )

    def release(self, now: float) -> None:
        """Leave manual override: reset in-memory state and start the sequence."""
        if not self._manual_override:
            return
        self._manual_override = False
        self._reset_state()
        self._startup = StartupSequence(started_at=now)
        self._sink.report_alarm(
            AlarmEvent(
                alarm_type=AlarmType.RELEASE,
                junction_id=None,
                now=now,
                level=AlarmLevel.INFO,
            )
        )
        self._sink.report_alarm(
            AlarmEvent(
                alarm_type=AlarmType.STARTUP_SEQUENCE,
                junction_id=None,
                now=now,
                level=AlarmLevel.INFO,
                detail={
                    "flash_s": self._startup.flash_s,
                    "all_red_s": self._startup.all_red_s,
                },
            )
        )

    def _reset_state(self) -> None:
        """In-memory reset (no process restart): rebuild runtime + queues."""
        self._runtime = build_runtime(self._config)
        queue_config = self._config["queue_estimation"]
        self._queue = QueueEstimator(
            queue_speed_threshold_mps=queue_config["queue_speed_threshold_mps"],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
        )
        self._spillback = self._runtime.spillback_monitor
        self._degradation = self._runtime.degradation
        self._feedback = self._runtime.feedback_monitor
        self._machine = self._runtime.state_machine
        self._groups = self._runtime.phase_groups
        movements: set[str] = set()
        for group in self._groups:
            movements.update(group.movement_ids)
        self._movements = movements
        self._parameters = TunableParameters(
            observation_timeout_s=self._config["runtime"]["observation_timeout_s"],
            confirm_timeout_s=self._config["runtime"]["confirm_timeout_s"],
            queue_speed_threshold_mps=queue_config["queue_speed_threshold_mps"],
            vehicle_length_m=queue_config["vehicle_length_m"],
            stopped_gap_m=queue_config["stopped_gap_m"],
            stale_after_s=queue_config["stale_after_s"],
            score=self._runtime.score_config,
            machine=self._runtime.machine_config,
            pedestrian_max_red_s=(self._config.get("pedestrian") or {}).get("max_red_s", 150.0),
            pedestrian_min_green_s=(self._config.get("pedestrian") or {}).get("min_green_s", 12.0),
            pedestrian_yellow_s=(self._config.get("pedestrian") or {}).get("yellow_s", 3.0),
        )
        self._last_seen = {}
        self._last_mode = None

    def tick(
        self,
        now: float,
        state: ControllerState,
        observations: Sequence[VehicleObservation] = (),
        downstream_occupancy: Mapping[str, float | None] | None = None,
        green_wave_active: Mapping[str, bool] | None = None,
    ) -> LayerResult:
        """Produce one decision from one perception frame."""
        # 1) Manual override: fully silent, no proposal at all.
        if self._manual_override or state.mode == ControllerMode.MANUAL_OVERRIDE:
            self._feedback.reset()
            return self._silent(now, "manual_override", DegradationMode.NORMAL)

        # 2) Startup sequence in progress (after a release / restart).
        if self._startup is not None:
            stage = self._startup.stage(now)
            if stage is StartupStage.DONE:
                self._startup = None
            else:
                return self._silent(
                    now, "startup_sequence", DegradationMode.NORMAL, stage
                )

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

        # 3) Data watchdog: FAILSAFE hands control to the signal controller.
        # Diagnostics (demands / lane_states) are kept so operators can see why.
        if mode is DegradationMode.FAILSAFE:
            self._report_mode_change(now, mode)
            # Do not carry a stale countdown or pending switch across the gap.
            self._machine.reset_transient()
            self._feedback.reset()
            return LayerResult(
                now=now,
                decision=PhaseDecision(None, "failsafe_hold", feasible=False),
                degradation=mode,
                feedback=FeedbackStatus.IDLE,
                demands=demands,
                lane_states=lane_states,
                downstream_blocked=blocked_by_phase,
            )
        self._report_mode_change(now, mode)

        decision = self._machine.tick(state, now, demands)
        feedback = self._feedback.observe(decision, state, now)

        current_phase = self._runtime.phases.get(
            state.current_phase_id or ""
        )
        active_crosswalks = (
            current_phase.crosswalk_ids
            if current_phase is not None
            else frozenset()
        )
        yellow_remaining = 0.0
        if current_phase is not None:
            yellow = self._parameters.pedestrian_yellow_s
            green_min_end = state.phase_started_at + current_phase.min_green_s
            if now >= green_min_end - yellow:
                yellow_remaining = max(0.0, green_min_end - now)

        # Surface hard infeasibilities as alarms (never silently swallow).
        if not decision.feasible:
            self._sink.report_alarm(
                AlarmEvent(
                    alarm_type=AlarmType.NO_FEASIBLE_PHASE,
                    junction_id=None,
                    now=now,
                    level=AlarmLevel.WARNING,
                    detail={"reason": decision.reason},
                )
            )

        return LayerResult(
            now=now,
            decision=decision,
            degradation=mode,
            feedback=feedback,
            demands=demands,
            lane_states=lane_states,
            downstream_blocked=blocked_by_phase,
            active_crosswalks=active_crosswalks,
            pedestrian_yellow_remaining_s=yellow_remaining,
        )

    # -- internals ---------------------------------------------------------
    def _silent(
        self,
        now: float,
        reason: str,
        mode: DegradationMode,
        stage: StartupStage | None = None,
        *,
        feasible: bool = True,
    ) -> LayerResult:
        """A result that deliberately proposes nothing."""
        return LayerResult(
            now=now,
            decision=PhaseDecision(
                None,
                reason,
                feasible=feasible,
            ),
            degradation=mode,
            feedback=FeedbackStatus.IDLE,
            demands={},
            lane_states={},
            downstream_blocked={},
            startup=stage,
        )

    def _report_mode_change(
        self, now: float, mode: DegradationMode
    ) -> None:
        """Emit an alarm only when the degradation mode changes."""
        if mode is self._last_mode:
            return
        previous = self._last_mode
        self._last_mode = mode
        if mode is DegradationMode.FAILSAFE:
            self._sink.report_alarm(
                AlarmEvent(
                    alarm_type=AlarmType.FAILSAFE,
                    junction_id=None,
                    now=now,
                    level=AlarmLevel.CRITICAL,
                    detail={
                        "previous": previous.value if previous else None
                    },
                )
            )
        elif mode is DegradationMode.PARTIAL:
            self._sink.report_alarm(
                AlarmEvent(
                    alarm_type=AlarmType.PARTIAL,
                    junction_id=None,
                    now=now,
                    level=AlarmLevel.WARNING,
                    detail={
                        "previous": previous.value if previous else None
                    },
                )
            )

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
            if phase_id in freshness.stale or phase_id in freshness.dead:
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
