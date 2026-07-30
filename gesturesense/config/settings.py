"""Typed application configuration with YAML overrides.

Every tunable in the application lives here — no magic constants scattered
through modules. Defaults are defined in the dataclasses; a YAML file (see
``config/default_config.yaml``) overrides any subset of them, and unknown
keys are reported instead of silently ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraConfig:
    """Webcam capture settings."""

    index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    mirror: bool = True
    reconnect_delay_s: float = 2.0


@dataclass(frozen=True)
class TrackingConfig:
    """MediaPipe Hands parameters and landmark smoothing."""

    max_hands: int = 2
    model_complexity: int = 1
    detection_confidence: float = 0.6
    tracking_confidence: float = 0.6
    landmark_smoothing: float = 0.55
    """EMA responsiveness in ``(0, 1]``: 1 disables smoothing entirely."""
    model_path: str = "models/hand_landmarker.task"
    """Hand-landmarker model for the MediaPipe Tasks backend (mediapipe
    ≥ 0.10.15, where the legacy Solutions API was removed). Relative paths
    resolve against the project root; the file is downloaded automatically
    on first run if missing. Ignored by the legacy backend, which ships
    its models inside the wheel."""


@dataclass(frozen=True)
class GestureConfig:
    """Gesture engine thresholds and temporal stabilisation."""

    min_confidence: float = 0.55
    history: int = 7
    min_votes: int = 4


@dataclass(frozen=True)
class UiConfig:
    """Window, theme and HUD settings."""

    window_title: str = "GestureSense"
    theme: str = "dark"
    fullscreen: bool = False
    show_fps: bool = True
    show_bounding_box: bool = True
    show_landmarks: bool = True
    font_scale: float = 0.55
    notification_ttl_s: float = 2.5
    render_fps_limit: int = 60


@dataclass(frozen=True)
class DebugConfig:
    """Optional developer overlay."""

    enabled: bool = False
    show_finger_states: bool = True
    show_timings: bool = True
    show_system_usage: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    """Structured logging destinations."""

    enabled: bool = True
    level: str = "INFO"
    directory: str = "logs"
    fps_report_interval_s: float = 30.0


@dataclass(frozen=True)
class AppConfig:
    """Root configuration object handed to every subsystem."""

    camera: CameraConfig = field(default_factory=CameraConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _merge(instance: Any, overrides: dict[str, Any], path: str = "") -> Any:
    """Recursively apply a dict of overrides onto a (nested) dataclass."""
    valid = {f.name: f for f in fields(instance)}
    updates: dict[str, Any] = {}
    for key, value in overrides.items():
        where = f"{path}.{key}" if path else key
        if key not in valid:
            logger.warning("Unknown config key ignored: %s", where)
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            updates[key] = _merge(current, value, where)
        else:
            updates[key] = value
    return replace(instance, **updates)


def _validate(config: AppConfig) -> AppConfig:
    """Fail fast on out-of-range values instead of misbehaving at runtime."""
    checks: list[tuple[bool, str]] = [
        (config.camera.width > 0 and config.camera.height > 0, "camera resolution must be positive"),
        (0 < config.tracking.detection_confidence <= 1, "tracking.detection_confidence must be in (0, 1]"),
        (0 < config.tracking.tracking_confidence <= 1, "tracking.tracking_confidence must be in (0, 1]"),
        (1 <= config.tracking.max_hands <= 4, "tracking.max_hands must be within 1..4"),
        (config.tracking.model_complexity in (0, 1), "tracking.model_complexity must be 0 or 1"),
        (0 < config.tracking.landmark_smoothing <= 1, "tracking.landmark_smoothing must be in (0, 1]"),
        (0 < config.gestures.min_confidence < 1, "gestures.min_confidence must be in (0, 1)"),
        (config.gestures.min_votes <= config.gestures.history, "gestures.min_votes cannot exceed gestures.history"),
        (config.ui.theme in ("dark", "light"), "ui.theme must be 'dark' or 'light'"),
        (config.ui.render_fps_limit >= 1, "ui.render_fps_limit must be >= 1"),
    ]
    problems = [message for ok, message in checks if not ok]
    if problems:
        raise ValueError("Invalid configuration: " + "; ".join(problems))
    return config


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration, applying YAML overrides from ``path`` if given.

    A missing file falls back to defaults with a warning rather than
    crashing — the application must always be able to start.
    """
    config = AppConfig()
    if path is None:
        return _validate(config)

    file_path = Path(path)
    if not file_path.exists():
        logger.warning("Config file not found, using defaults: %s", file_path)
        return _validate(config)

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("Failed to parse config %s: %s — using defaults", file_path, exc)
        return _validate(config)

    if not isinstance(raw, dict):
        logger.error("Config root must be a mapping, got %s — using defaults", type(raw).__name__)
        return _validate(config)

    return _validate(_merge(config, raw))
