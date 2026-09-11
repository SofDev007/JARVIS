#!/usr/bin/env python3
"""Digital Twin AI Assistant — kernel entry point.

Wires the event bus, the module registry and the configured modules, then
runs until interrupted. Intentionally thin: everything interesting lives in
the ``digital_twin`` package.

Usage::

    python main.py                          # defaults + config/default_config.yaml
    python main.py --config my.yaml
    python main.py --context presentation   # starting intent context
    python main.py --no-gesture             # kernel without the camera module
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from digital_twin.configuration.settings import AppConfig, load_config
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.registry import ModuleRegistry
from digital_twin.reasoning.intent import IntentEngine
from digital_twin.utils.logging_setup import setup_logging

logger = logging.getLogger("digital_twin.main")

from digital_twin.paths import resolve_asset

DEFAULT_CONFIG = resolve_asset("config/default_config.yaml")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse the command-line interface."""
    parser = argparse.ArgumentParser(
        prog="digital-twin",
        description="Multimodal Digital Twin AI Assistant kernel.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to a YAML configuration file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="User profile to apply (overrides profiles.active in the config).",
    )
    parser.add_argument(
        "--context",
        type=str,
        default=None,
        help="Initial application context for the intent engine.",
    )
    parser.add_argument(
        "--no-gesture",
        action="store_true",
        help="Do not start the gesture perception module.",
    )
    return parser.parse_args(argv)


def _console_reporter(event: Event) -> None:
    """Human-readable event trace on stdout (the kernel's minimal 'UI')."""
    if event.topic == Topics.CHAT_RESPONSE:
        print(f"\nAssistant: {event.payload.get('text')}")
        reasoning = event.payload.get("reasoning")
        if reasoning:
            print(f"  (reasoning: {reasoning})")
        return
    if event.topic == Topics.CHAT:
        return  # the user just typed it; echoing is noise
    print(f"  {event}")


def build_registry(config: AppConfig, bus: EventBus, with_gesture: bool) -> ModuleRegistry:
    """Register the configured modules onto a fresh registry."""
    registry = ModuleRegistry(bus)
    if config.memory.enabled:
        from digital_twin.memory.module import MemoryModule

        registry.register(MemoryModule(config.memory))
    registry.register(IntentEngine(config.intent))
    if config.context_perception.enabled:
        from digital_twin.perception.context.module import ContextPerceptionModule

        registry.register(ContextPerceptionModule(config.context_perception))
    dispatcher = None
    web_confirmation = None
    frame_hub = None
    if config.dashboard.enabled:
        from digital_twin.dashboard.frames import FrameHub

        frame_hub = FrameHub()
    if config.automation.enabled:
        from digital_twin.automation.dispatcher import build_action_dispatcher

        if config.security.confirmation == "web":
            from digital_twin.dashboard.web_confirmation import WebConfirmation

            web_confirmation = WebConfirmation()
        dispatcher = build_action_dispatcher(
            config.automation, config.security,
            confirmation=web_confirmation,
        )
        registry.register(dispatcher)
    if config.screen_reading.enabled:
        from digital_twin.perception.screen.module import ScreenReadingModule

        screen_reader = ScreenReadingModule(config.screen_reading)
        registry.register(screen_reader)
        if dispatcher is not None:
            from digital_twin.perception.screen.actions import (
                register_screen_actions,
            )

            # The reasoner's action catalog is late-bound (M11), so
            # registration order no longer affects what the LLM sees.
            register_screen_actions(dispatcher.registry, screen_reader)
    if config.files.enabled and dispatcher is not None:
        from digital_twin.automation.file_actions import register_file_actions

        register_file_actions(dispatcher.registry, config.files)
    from digital_twin.security.secrets import SecretsError, create_secret_store

    try:
        secret_store = create_secret_store(config.secrets)
    except SecretsError as exc:
        secret_store = None
        logger.warning("Secret store unavailable: %s", exc)
    if config.browser.enabled and dispatcher is not None:
        from digital_twin.browser.actions import register_browser_actions
        from digital_twin.browser.driver import DriverHolder, create_browser_driver

        register_browser_actions(
            dispatcher.registry,
            DriverHolder(lambda: create_browser_driver(config.browser)),
            secret_store,
            config.browser,
        )
    knowledge_pair = None
    if config.knowledge.enabled:
        from digital_twin.knowledge.embedding import create_embedder
        from digital_twin.knowledge.store import KnowledgeStore

        knowledge_store = KnowledgeStore(
            config.knowledge.db_path,
            create_embedder(config.knowledge.embedder,
                            config.knowledge.embedding_dim),
            chunk_chars=config.knowledge.chunk_chars,
            chunk_overlap=config.knowledge.chunk_overlap,
        )
        knowledge_pair = (knowledge_store, config.knowledge)
        if dispatcher is not None:
            from digital_twin.knowledge.actions import register_knowledge_actions

            register_knowledge_actions(
                dispatcher.registry, knowledge_store,
                config.knowledge, config.files,
            )
        if config.knowledge.watch_paths:
            from digital_twin.knowledge.watch import KnowledgeWatchModule

            registry.register(KnowledgeWatchModule(
                config.knowledge, config.files, knowledge_store))
    plugin_reports = []
    if config.plugins.enabled and config.plugins.paths:
        from digital_twin.plugins.loader import load_plugins

        plugin_reports = load_plugins(
            config.plugins,
            dispatcher.registry if dispatcher is not None else None,
            registry,
            secrets=secret_store,
        )
        for plugin in plugin_reports:
            if plugin.ok:
                logger.info(
                    "Plugin %s %s active (actions: %s)",
                    plugin.name, plugin.version,
                    ", ".join(plugin.actions) or "-",
                )
            else:
                logger.error("Plugin %s DISABLED: %s", plugin.name, plugin.error)
    if config.dashboard.enabled:
        from digital_twin.dashboard.module import DashboardModule

        dash_memory = None
        if config.memory.enabled:
            try:
                dash_memory = registry.get("memory")
            except Exception:
                dash_memory = None

        # M18 Phase 3: Device registry and device confirmation for mTLS
        device_registry = None
        device_confirmation = None
        if config.automation.enabled:
            from digital_twin.security.audit import build_audit_log
            from digital_twin.security.device_identity import DeviceRegistry
            audit = build_audit_log(config.security)
            device_registry = DeviceRegistry(config.security.devices_dir, audit=audit)

            # Check if any devices are enrolled
            active_devices = device_registry.active_devices()
            if active_devices:
                logger.info("Device identity enabled: %d device(s) enrolled",
                            len(active_devices))
                # Use device confirmation for DANGEROUS actions
                from digital_twin.security.device_confirmation import (
                    DeviceConfirmationProvider,
                )
                device_confirmation = DeviceConfirmationProvider(
                    device_registry,
                    timeout_s=config.security.confirmation_timeout_s,
                )
                logger.info(
                    "Device confirmation enabled — DANGEROUS actions require "
                    "second-device approval")
            else:
                logger.info(
                    "No devices enrolled — device identity disabled. Enroll with: "
                    "python -m digital_twin.security.device_cli enroll --label 'device'")

        registry.register(DashboardModule(
            config.dashboard,
            registry,
            confirmations=(web_confirmation
                           if config.automation.enabled else None),
            dispatcher=dispatcher,
            memory_store=dash_memory,
            knowledge_store=(knowledge_pair[0] if knowledge_pair else None),
            plugin_reports=plugin_reports,
            app_config=config,
            frame_hub=frame_hub,
            device_registry=device_registry,
            device_confirmation=device_confirmation,
            security_config=config.security,
        ))
        logger.info("Dashboard will listen on http://%s:%s",
                    config.dashboard.host, config.dashboard.port)
    if config.planner.enabled and config.automation.enabled:
        from digital_twin.planner.module import PlannerModule

        planner_memory = None
        if config.memory.enabled:
            try:
                planner_memory = registry.get("memory")
            except KeyError:
                planner_memory = None
        registry.register(PlannerModule(config.planner, memory=planner_memory))
    if config.llm.enabled and config.chat.enabled:
        from digital_twin.perception.chat.module import ChatPerceptionModule
        from digital_twin.reasoning.chat_reasoner import ChatReasoner
        from digital_twin.reasoning.llm import create_language_model

        memory_module = None
        if config.memory.enabled:
            try:
                memory_module = registry.get("memory")
            except KeyError:
                memory_module = None
        registry.register(ChatReasoner(
            config.llm,
            model=lambda: create_language_model(config.llm,
                                                secrets=secret_store),
            allowed_intents=tuple(config.automation.intent_bindings),
            memory=memory_module,
            knowledge=knowledge_pair,
            action_catalog=(dispatcher.actions_catalog
                            if dispatcher is not None else ()),
        ))
        registry.register(ChatPerceptionModule(config.chat))
    if config.voice.enabled:
        from digital_twin.voice.actions import register_voice_actions
        from digital_twin.voice.module import VoicePerceptionModule
        from digital_twin.voice.synthesis import SpeechSynthesizer

        synthesizer = SpeechSynthesizer(
            backend=config.voice.tts_backend,
            rate_wpm=config.voice.tts_rate_wpm,
            piper_voice=config.voice.piper_voice,
            piper_data_dir=config.voice.piper_data_dir,
            jarvis_reference_wav=config.voice.jarvis_reference_wav,
            jarvis_precache_dir=config.voice.jarvis_precache_dir,
            precached_phrases=list(config.voice.precached_phrases),
        )
        if dispatcher is not None:
            register_voice_actions(dispatcher.registry, synthesizer)
        registry.register(
            VoicePerceptionModule(config.voice, synthesizer=synthesizer)
        )
        if config.voice.wake_word.strip():
            from digital_twin.voice.wake import WakeWordModule

            registry.register(WakeWordModule(config.voice, synthesizer=synthesizer))
            logger.info("Wake word enabled: %r", config.voice.wake_word)
    if with_gesture:
        # Imported here so the kernel starts even without cv2/mediapipe.
        from digital_twin.perception.gesture.module import GesturePerceptionModule

        registry.register(GesturePerceptionModule(
            config.gesture,
            frame_sink=(frame_hub.sink("gesture") if frame_hub else None),
        ))
    return registry


def main(argv: list[str] | None = None) -> int:
    """Program entry point; returns a process exit code."""
    args = parse_args(argv)
    try:
        config = load_config(args.config, profile=args.profile)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.logging)
    logger.info("Digital Twin kernel starting (config: %s)", args.config)

    # Harden the at-rest state directories (data/, logs/) and warn loudly if
    # any broad principal can still read them — checked before a single secret
    # is written, so a regenerated directory is caught in time. Never fatal.
    from digital_twin.security.fsacl import secure_and_verify_state

    secure_and_verify_state(config)

    bus = EventBus(
        max_queue_size=config.bus.max_queue_size,
        slow_handler_warn_ms=config.bus.slow_handler_warn_ms,
    )
    bus.start()
    bus.subscribe("*", _console_reporter, name="console")

    with_gesture = config.gesture.enabled and not args.no_gesture
    try:
        registry = build_registry(config, bus, with_gesture)
    except ImportError as exc:
        logger.error("Missing dependency: %s", exc)
        print(
            f"\nA dependency is missing: {exc}\n"
            "Install requirements first:  pip install -r requirements.txt\n",
            file=sys.stderr,
        )
        bus.stop()
        return 1

    registry.start_all()
    if args.context:
        bus.publish(Event(Topics.CONTEXT, "cli", {"context": args.context}))

    running = [s for s in registry.statuses() if s.state.value == "running"]
    print(
        f"\nDigital Twin kernel running — modules: "
        f"{', '.join(s.name for s in running) or 'none'} (Ctrl+C to stop)\n"
    )

    exit_code = 0
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nShutting down…")
    except Exception:
        logger.exception("Kernel crashed")
        exit_code = 1
    finally:
        registry.stop_all()
        bus.stop()
        logger.info("Digital Twin kernel stopped")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
