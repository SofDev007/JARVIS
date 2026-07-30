"""Config-driven custom gesture registration.

Users extend the recognizer without touching any core code: point
``gesture.custom_gesture_modules`` at Python modules or ``.py`` files whose
import registers :class:`gesturesense.gesture.base.GestureRule` subclasses
via ``@register_gesture``. Semantic ids for unknown display names are
slugified automatically by :mod:`digital_twin.perception.gesture.semantics`,
so custom gestures flow through events, intent mappings and profiles
immediately.

Failures degrade gracefully: a typo'd path or a broken rule module is
logged and reported in the module's health metrics, but never takes the
gesture module (let alone the assistant) down. Loading is idempotent —
module restarts and pause/resume cycles do not re-execute rule modules,
which would trip the library's duplicate-name guard.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

#: Specs already imported in this process (idempotency across restarts).
_loaded_specs: set[str] = set()


@dataclass(frozen=True)
class CustomGestureReport:
    """Outcome of loading the configured custom gesture modules."""

    loaded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def load_custom_gesture_modules(specs: Iterable[str]) -> CustomGestureReport:
    """Import every spec (dotted module path or ``.py`` file), best-effort.

    Returns which specs loaded and which failed; failures are logged with
    tracebacks. Already-loaded specs count as loaded without re-execution.
    """
    loaded: list[str] = []
    failed: list[str] = []
    for spec in specs:
        if spec in _loaded_specs:
            loaded.append(spec)
            continue
        try:
            _import_spec(spec)
        except Exception:
            logger.exception("Failed to load custom gesture module %r", spec)
            failed.append(spec)
        else:
            _loaded_specs.add(spec)
            loaded.append(spec)
            logger.info("Loaded custom gesture module %r", spec)
    return CustomGestureReport(loaded=tuple(loaded), failed=tuple(failed))


def _import_spec(spec: str) -> None:
    path = Path(spec)
    if path.suffix == ".py":
        if not path.exists():
            raise FileNotFoundError(f"Custom gesture file not found: {spec}")
        resolved = path.resolve()
        module_name = f"_dt_custom_gestures_{abs(hash(str(resolved)))}"
        if module_name in sys.modules:  # same file under a different spec string
            return
        module_spec = importlib.util.spec_from_file_location(module_name, resolved)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"Cannot create import spec for {spec}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        try:
            module_spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)  # don't cache a broken module
            raise
    else:
        importlib.import_module(spec)
