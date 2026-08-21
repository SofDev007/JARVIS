"""Typed application configuration with YAML overrides.

Every tunable in the assistant lives here — no magic constants scattered
through modules. Defaults are defined in dataclasses; a YAML file overrides
any subset. Unknown keys are reported instead of silently ignored, and
out-of-range values fail fast at startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoggingConfig:
    """Structured logging destinations."""

    enabled: bool = True
    level: str = "INFO"
    directory: str = "logs"


@dataclass(frozen=True)
class EventBusConfig:
    """Event bus sizing and diagnostics."""

    max_queue_size: int = 1024
    slow_handler_warn_ms: float = 50.0


@dataclass(frozen=True)
class GestureModuleConfig:
    """Gesture perception module (wraps the GestureSense library)."""

    enabled: bool = True

    # Camera
    camera_index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    camera_fps: int = 30
    mirror: bool = True

    # Hand tracking
    max_hands: int = 2
    detection_confidence: float = 0.6
    tracking_confidence: float = 0.6
    landmark_smoothing: float = 0.55
    model_complexity: int = 1
    model_path: str = "models/hand_landmarker.task"

    # Gesture engine
    min_confidence: float = 0.55
    history: int = 7
    min_votes: int = 4

    # Event behaviour
    poll_interval_s: float = 0.02
    """How often the module polls the inference worker for new results."""
    repeat_interval_s: float = 0.0
    """Re-publish a *held* gesture every N seconds (0 = edge-triggered only)."""

    # Calibration / per-user tuning
    gesture_thresholds: dict[str, float] = field(default_factory=dict)
    """Per-gesture minimum confidence, keyed by semantic id (e.g.
    ``{"thumbs_up": 0.75}``). Detections below the threshold are treated as
    no gesture. Gestures without an entry use the engine's global
    ``min_confidence``."""
    disabled_gestures: list[str] = field(default_factory=list)
    """Semantic ids this module must never publish (e.g. ``[finger_gun]``)."""

    # Extensibility & tooling
    custom_gesture_modules: list[str] = field(default_factory=list)
    """Python module paths or ``.py`` files that register additional
    :class:`gesturesense.gesture.base.GestureRule` classes at startup."""
    debug_window: bool = False
    """Show a live OpenCV window with skeleton + gesture overlays. Fails
    soft (self-disables) in headless environments."""


@dataclass(frozen=True)
class IntentConfig:
    """Context-aware gesture → intent mapping."""

    default_context: str = "desktop"
    mappings: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "presentation": {
                "thumbs_up": "next_slide",
                "thumbs_down": "previous_slide",
                "closed_fist": "end_presentation",
            },
            "media": {
                "peace": "play_pause",
                "thumbs_up": "like",
                "pointing_right": "seek_forward",
                "pointing_left": "seek_backward",
            },
            "coding": {
                "thumbs_up": "accept_suggestion",
                "thumbs_down": "reject_suggestion",
            },
            "desktop": {},
            "*": {
                "open_palm": "assistant_attention",
            },
        }
    )
    """``context -> gesture -> intent``. The ``"*"`` context is the
    fallback consulted when the active context has no mapping. The gesture
    module never sees this table — context interpretation is reasoning-side
    by design."""


@dataclass(frozen=True)
class ContextPerceptionConfig:
    """Screen-context perception: active window → application context."""

    enabled: bool = True
    poll_interval_s: float = 1.0
    """How often the active window is sampled."""
    publish_window_info: bool = False
    """Include window title/process in ``context.changed`` payloads.
    Off by default: window titles routinely contain document names, chat
    partners and other sensitive detail that has no business on the bus
    unless explicitly wanted."""
    fallback_context: str = "desktop"
    """Context published when no rule matches the active window."""
    rules: list[dict] = field(
        default_factory=lambda: [
            {
                "context": "presentation",
                "any": [
                    "powerpoint", "impress", "google slides", "keynote",
                    ".pptx",
                ],
            },
            {
                "context": "media",
                "any": [
                    "youtube", "vlc", "spotify", "netflix", "media player",
                ],
            },
            {
                "context": "coding",
                "any": [
                    "visual studio code", "intellij", "pycharm", "vim",
                    "terminal", "konsole",
                ],
            },
        ]
    )
    """Ordered classification rules: first rule whose ``any`` substrings
    (case-insensitive) appear in the window title or process name wins."""


@dataclass(frozen=True)
class ScreenReadingConfig:
    """On-demand screen OCR: what the user can *see*, as text.

    Deliberately not a polling sensor: the screen is captured **only**
    when the gated ``read_screen`` action executes — screen content is
    the most sensitive perception surface there is, so every single
    capture passes the permission/confirmation/audit pipeline, and the
    screenshot file is deleted the moment OCR finishes.
    """

    enabled: bool = True
    capture_backend: str = "auto"
    """``auto`` picks the first available: ``scrot`` | ``imagemagick`` |
    ``gnome-screenshot`` (Linux/X11), ``screencapture`` (macOS),
    ``powershell`` (Windows)."""
    ocr_language: str = "eng"
    """Tesseract language code(s), e.g. ``eng`` or ``eng+deu``."""
    max_chars: int = 4000
    """Upper bound on OCR text published per capture (longer output is
    truncated and flagged)."""
    capture_timeout_s: float = 10.0
    ocr_timeout_s: float = 15.0


@dataclass(frozen=True)
class FilesConfig:
    """File-system intelligence: contained, gated file operations.

    Every file action is confined to ``allowed_roots`` (checked *after*
    symlink resolution, at validation time *and* again at execution).
    The default is an **empty allow-list** — like ``open_application``,
    the capability exists but can touch nothing until the user names the
    directories it may work in. Destructive operations are DANGEROUS
    (code-clamped to confirmation) and ``delete_file`` moves to a trash
    directory instead of unlinking, so a confirmed mistake is still
    recoverable.
    """

    enabled: bool = True
    allowed_roots: list[str] = field(default_factory=list)
    """Directories file actions may operate in (e.g. ``[~/Documents]``).
    Empty = every file action fails validation with guidance."""
    max_read_chars: int = 4000
    """Upper bound for one ``read_text_file`` action."""
    max_write_chars: int = 20000
    """Upper bound for one ``write_text_file`` action."""
    max_list_entries: int = 100
    """Cap on entries returned by ``list_files`` / ``search_files``."""
    max_scan_files: int = 2000
    """Cap on files hashed by one ``find_duplicates`` action."""
    trash_dir_name: str = ".digital_twin_trash"
    """Per-root trash directory ``delete_file`` moves into (never shown
    in listings; restore by moving files back out)."""


@dataclass(frozen=True)
class PluginsConfig:
    """Third-party plugin discovery and capability scoping.

    Loading a plugin is an explicit act of trust: only directories the
    user names in ``paths`` are scanned, each plugin ships a
    ``plugin.yaml`` manifest **declaring** every action it registers (and
    the minimum risk of each), and the loader enforces that contract —
    undeclared actions are refused, declared risk is a floor the plugin
    cannot register below, and everything a plugin provides is namespaced
    ``<plugin>.<name>`` so built-ins can never be shadowed. Runtime
    execution still passes the full dispatcher gate pipeline. (In-process
    Python cannot be truly sandboxed; OS-level isolation is tracked in
    REMAINING_WORK §2.11.)
    """

    enabled: bool = True
    paths: list[str] = field(default_factory=list)
    """Directories scanned for plugins (each plugin = a subdirectory
    containing ``plugin.yaml``). Empty = nothing is ever loaded."""


@dataclass(frozen=True)
class SecretsConfig:
    """Secret storage: values by name, never by value.

    Actions and events reference secrets **by name only**; the value is
    resolved inside a handler at the last moment and never appears in
    params, results, the audit log, or the LLM prompt. Backends: the OS
    keyring (``keyring`` package) when available, or a Fernet-encrypted
    local file — no silent downgrade, same policy as memory encryption.
    """

    backend: str = "auto"
    """``auto`` (keyring if importable, else encrypted file) |
    ``keyring`` | ``file``."""
    file_path: str = "data/secrets.enc"
    """Encrypted secrets file (``file`` backend)."""
    key_path: str = "data/secrets.key"
    """Fernet key file, created ``0600`` on first use (``file`` backend)."""
    index_path: str = "data/secrets.index"
    """Names-only index for the keyring backend (the OS keyring cannot
    enumerate; names are metadata, values stay in the keyring)."""


@dataclass(frozen=True)
class BrowserConfig:
    """Browser automation behind a replaceable driver.

    The reference driver is Playwright/Chromium (lazy import — the kernel
    starts without it and a triggered browser action fails audited with
    install guidance). ``allowed_domains`` restricts navigation when
    non-empty, and is **mandatory** for ``browser_fill_secret``: secrets
    are never typed into a page unless its host is explicitly
    allow-listed (phishing containment).
    """

    enabled: bool = True
    headless: bool = True
    allowed_domains: list[str] = field(default_factory=list)
    """Hosts navigation may target (exact or subdomain match), e.g.
    ``[github.com]``. Empty = any http(s) URL may be *opened*, but
    ``browser_fill_secret`` refuses to run at all."""
    max_extract_chars: int = 4000
    """Upper bound for one ``browser_extract_text`` action."""
    navigation_timeout_s: float = 20.0
    """Per-navigation/click timeout."""


@dataclass(frozen=True)
class DashboardConfig:
    """Local web dashboard: live status, events, chat and confirmations.

    **Loopback only, off by default.** The server binds ``127.0.0.1`` and
    refuses other hosts unless ``allow_remote`` is explicitly set (at
    which point securing the network is the operator's problem — stated,
    not hidden). All state-changing endpoints require a per-session token
    embedded in the served page, so a malicious website open in the same
    browser cannot forge approval clicks (CSRF containment). With
    ``security.confirmation: web``, confirmations move from the terminal
    to the dashboard — resolving the long-tracked stdin conflict between
    console chat and console confirmation prompts.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    """TCP port (0 = ephemeral, mainly for tests)."""
    allow_remote: bool = False
    """Permit binding non-loopback hosts. Off by default on purpose."""
    recent_events: int = 200
    """Ring-buffer size for the live event feed."""


@dataclass(frozen=True)
class KnowledgeConfig:
    """The knowledge engine: local document ingestion + vector recall.

    Everything stays on the machine: chunking, embedding (a
    dependency-free hashing embedder by default — honest lexical
    similarity; semantic models are drop-in) and ranking all run locally
    against a SQLite store. The reasoner injects the best-matching chunks
    into its prompt each message (RAG); ingestion goes through gated
    actions and file ingestion is confined to ``files.allowed_roots``.
    """

    enabled: bool = True
    db_path: str = "data/knowledge.db"
    embedder: str = "hashing"
    """``hashing`` (built-in, offline, lexical) — semantic embedders
    plug in via the Embedder interface."""
    embedding_dim: int = 256
    chunk_chars: int = 800
    """Maximum characters per chunk (paragraph-first packing)."""
    chunk_overlap: int = 100
    """Tail characters carried into the next chunk."""
    top_k: int = 3
    """Chunks retrieved per query."""
    min_score: float = 0.12
    """Cosine floor below which a chunk is noise, not knowledge."""
    prompt_max_chars: int = 2000
    """Upper bound for the knowledge section injected into the prompt."""
    watch_paths: list[str] = field(default_factory=list)
    """Directories polled for documents to auto-ingest. Each must resolve
    inside ``files.allowed_roots`` (empty = no watching). New and changed
    supported files are ingested; content-hash idempotence means a poll
    that finds nothing new does nothing."""
    watch_interval_s: float = 30.0
    """Seconds between folder scans."""


@dataclass(frozen=True)
class ChatConfig:
    """Chat perception module (typed text input)."""

    enabled: bool = True
    console: bool = True
    """Read lines from the kernel terminal when it is an interactive TTY.
    With console chat active, prefer ``security.permissions`` rules or
    ``auto_deny`` over interactive console confirmations (shared stdin)."""


@dataclass(frozen=True)
class LLMConfig:
    """Language-model backend for the chat reasoner."""

    enabled: bool = True
    provider: str = "gemini"
    """``gemini`` (default — genuine free tier), ``anthropic``, or
    ``ollama`` (local, no key)."""
    persona: str = (
        "You are KNOWA (Knowledge-driven Neural Operations, Workflow & "
        "Automation), the Mark I AI assistant. You turn information into "
        "understanding, simplify complexity, and solve problems with "
        "precision and reliability.\n\n"
        "You speak directly to your one user, who you address as \"Boss\". "
        "Your replies are converted to speech, so keep them spoken-length: "
        "short, natural sentences. No lists, no headers, no markdown.\n\n"
        "CORE PERSONALITY\n"
        "- Dry, understated wit — closer to a sharp, competent butler than "
        "a comedian. Clever, not silly.\n"
        "- Confident. You know what you're doing and it shows in how briefly "
        "you say things, not in how much you explain.\n"
        "- Genuinely on Boss's side. The wit never undermines that — tease, "
        "don't insult.\n"
        "- You are NOT a generic assistant. Never say \"As an AI,\" "
        "\"I'm here to help!\", \"Is there anything else I can help with?\", "
        "or similar stock phrasing. Never hedge more than once in a reply. "
        "Never apologize unless something actually went wrong.\n"
        "- Accuracy before speed: never fabricate, separate facts from "
        "assumptions, admit uncertainty, be concise for simple questions and "
        "thorough for complex ones, and if you err, acknowledge and correct "
        "it plainly.\n\n"
        "HOW YOU VARY YOUR TONE\n"
        "Most replies are just direct and efficient — answer the question, "
        "confirm the action, move on. Do not force a joke, compliment, or "
        "jab into every single reply. Let tone shifts arise from what's "
        "actually happening:\n"
        "- Most of the time: plain, competent, brief.\n"
        "- Sometimes, when Boss does something well or clever: acknowledge "
        "it with dry approval, not gushing praise.\n"
        "- Sometimes, when Boss makes an obvious mistake or repeats one: "
        "call it out lightly, never harshly.\n"
        "- If nothing notable happened, just answer.\n\n"
        "STYLE RULES\n"
        "- Keep replies short — this is spoken aloud, not read on a screen.\n"
        "- Stay in character always. Never break to discuss being a language "
        "model or an AI system.\n"
        "- No emoji. No markdown. No em-dash lists.\n"
        "- Say \"Boss\" naturally, once or twice per reply, never overusing "
        "it, and never by a real name unless asked.\n\n"
        "WAKE WORD GREETINGS\n"
        "If the user's whole message is just the wake word \"KNOWA\", reply "
        "with a varied, time-aware greeting that always includes \"Boss\". "
        "Base the time-of-day portion (morning/afternoon/evening) on the "
        "current time provided in context. Examples:\n"
        "\"Good morning, Boss. KNOWA online — what's the plan?\" | "
        "\"Good afternoon, Boss. Systems nominal. How can I help?\" | "
        "\"Good evening, Boss. Ready when you are.\" | "
        "\"KNOWA activated, Boss. Standing by.\" | "
        "\"Morning, Boss. Coffee's on me — what do you need?\"\n"
        "Vary the phrasing each time; never repeat the exact same line "
        "twice in a row."
    )
    """The assistant's identity and voice, injected at the top of every
    reasoning prompt. Edit to reshape who the assistant is — its name,
    how it addresses you, its tone — without touching code."""
    model: str = "gemini-3.6-flash"
    api_key_env: str = "GEMINI_API_KEY"
    api_key_secret: str = "gemini_api_key"
    """Secret-store name checked *before* the environment variable
    (store one with the secrets CLI); empty disables the lookup."""
    """Environment variable holding the API key. Keys are never read
    from configuration files."""
    ollama_url: str = "http://localhost:11434"
    max_tokens: int = 512
    temperature: float = 0.3
    timeout_s: float = 30.0
    history_turns: int = 8
    """Conversation turns kept in the prompt window."""
    memory_results: int = 5
    """Ranked memories injected into each prompt (0 disables recall)."""


@dataclass(frozen=True)
class MemoryConfig:
    """Persistent and working memory."""

    enabled: bool = True
    db_path: str = "data/memory.db"
    encryption: bool = False
    """Encrypt memory content at rest (requires the ``cryptography``
    package). Enabling without the package installed fails startup —
    encryption never silently downgrades to plaintext."""
    key_path: str = "data/memory.key"
    working_capacity: int = 200
    working_window_s: float = 3600.0
    episodic_max_records: int = 5000
    retention_days: float = 90.0
    """Episodic records older than this are pruned. Semantic facts are
    user-curated and never auto-pruned."""
    prune_interval_s: float = 300.0
    search_half_life_days: float = 7.0
    """Recency decay half-life used by search ranking."""


@dataclass(frozen=True)
class SecurityConfig:
    """Permission, confirmation and audit settings for the action pipeline."""

    risk_defaults: dict[str, str] = field(
        default_factory=lambda: {
            "safe": "allow",
            "sensitive": "confirm",
            "dangerous": "deny",
        }
    )
    """Default decision per risk level. Note: ``dangerous`` actions have a
    hard floor of ``confirm`` enforced in code — configuration can only
    tighten their handling, never silently allow them."""
    permissions: dict[str, str] = field(default_factory=dict)
    """Per-action overrides, e.g. ``{open_url: allow}``."""
    confirmation: str = "console"
    """Confirmation provider: ``console`` (interactive y/N with timeout,
    fail-closed without a TTY) or ``auto_deny`` (headless default)."""
    confirmation_timeout_s: float = 30.0
    audit_file: str = "logs/audit.jsonl"
    audit_max_bytes: int = 5_000_000
    audit_anchor_file: str = "data/audit.anchor"
    """DPAPI-protected tail anchor for the audit chain (Windows). Lives under
    ``data/`` — owner-only and *outside* ``logs/`` — so tail mutation and
    truncation are detectable. Empty disables anchoring."""
    devices_dir: str = "data/devices"
    """Directory holding the enrolled-device registry and each device's
    DPAPI-protected private key. Owner-only (under ``data/``)."""


@dataclass(frozen=True)
class AutomationConfig:
    """Action dispatcher behaviour and intent → action bindings."""

    enabled: bool = True
    max_queue_size: int = 64
    action_timeout_s: float = 30.0
    input_backend: str = "auto"
    """Input synthesis backend: ``auto`` (first available), ``xdotool``
    (Linux/X11) or ``pynput`` (cross-platform, optional dependency)."""
    max_type_text_chars: int = 500
    """Hard cap for a single ``type_text`` action."""
    applications: dict[str, str] = field(default_factory=dict)
    """Allow-list for ``open_application``: name → command line. There is
    deliberately no way to run a command that is not listed here."""
    intent_bindings: dict[str, dict] = field(
        default_factory=lambda: {
            "assistant_attention": {
                "action": "notify",
                "params": {"title": "Digital Twin", "message": "At your service."},
            },
            "like": {
                "action": "log_message",
                "params": {"message": "Liked the current item."},
            },
            "next_slide": {"action": "nav_key", "params": {"key": "right"}},
            "previous_slide": {"action": "nav_key", "params": {"key": "left"}},
            "seek_forward": {"action": "nav_key", "params": {"key": "right"}},
            "seek_backward": {"action": "nav_key", "params": {"key": "left"}},
            "play_pause": {"action": "media_key", "params": {"key": "play_pause"}},
            "end_presentation": {
                "action": "press_keys",
                "params": {"keys": ["escape"]},
            },
            "read_screen": {"action": "read_screen", "params": {}},
        }
    )
    """``intent -> {action, params}``. Intents without a binding are audited
    as ``unbound`` and otherwise ignored (not every meaning has an effect
    yet — presentation control needs input synthesis, a later milestone)."""


@dataclass(frozen=True)
class VoiceConfig:
    """Voice perception (offline STT) and spoken replies."""

    enabled: bool = True
    mode: str = "push_to_talk"
    """``push_to_talk``: the microphone opens only while a session is
    active (one utterance per trigger). ``continuous``: always listening
    while the module runs."""
    engine: str = "vosk"
    """``vosk`` (streaming, needs ``model_path``) | ``whisper`` (batch —
    transcribes on flush, no partials, needs ``whisper_model``)."""
    model_path: str = "models/vosk-model-small-en-us-0.15"
    """Local Vosk model directory (https://alphacephei.com/vosk/models);
    audio never leaves the machine. Unused when ``engine`` is ``whisper``."""
    whisper_model: str = "tiny"
    """Whisper model size (``tiny``/``base``/``small``/...); used only
    when ``engine`` is ``whisper``. Downloaded to ``~/.cache/whisper`` on
    first use unless already cached."""
    sample_rate: int = 16000
    block_ms: int = 30
    max_utterance_s: float = 30.0
    """Safety timeout: an open microphone session self-closes."""
    listen_intents: list[str] = field(
        default_factory=lambda: ["assistant_attention"]
    )
    """Intents that toggle listening — the open-palm gesture by default,
    so a raised hand is the push-to-talk button."""
    speak_replies: bool = True
    """Voice assistant replies via the gated ``speak`` action (mute with
    ``security.permissions: {speak: deny}``)."""
    tts_backend: str = "piper"
    """``piper`` (default) | ``jarvis`` | ``auto`` | ``espeak-ng`` | ``espeak``
    | ``say`` | ``powershell``. Piper is fast local TTS; jarvis uses
    pre-cached XTTS-v2 for system phrases only."""
    tts_rate_wpm: int = 175
    piper_voice: str = "en_GB-alan-low"
    """Piper voice ID for live synthesis. Downloaded on first use from
    HuggingFace (rhasspy/piper-voices). Common options: en_GB-alan-low,
    en_US-lessac-low, en_US-amy-low."""
    piper_data_dir: str = "models/piper"
    """Where Piper stores downloaded voice models."""
    jarvis_reference_wav: str = "voices/reference_voice.wav"
    """Reference clip for XTTS-v2 voice cloning (6-20s, clean, mono, British
    male narrator for JARVIS style). Only used for pre-cached phrases."""
    jarvis_precache_dir: str = "voices/precache"
    """Pre-generated .wav files for system phrases (JARVIS-cloned voice)."""
    precached_phrases: list[str] = field(
        default_factory=lambda: [
            "I'm still working on your previous request — give me a moment.",
            "Something went wrong while thinking about that; the details are in my logs.",
            "Cancelled that.",
        ]
    )
    """System phrases pre-synthesized with JARVIS voice. Only these exact
    strings get the premium voice; all LLM output uses Piper."""
    wake_word: str = "knowa"
    """Spoken phrase that starts a listening session in ``push_to_talk``
    mode. Defaults to ``knowa`` — the assistant's name — which is the one
    place a more-exposing default is justified: a hands-free assistant is
    expected to answer to its name. NOTE: a non-empty wake word implies
    *continuous microphone capture* by the always-on detector. Set to ``''``
    to disable it (triggers then stay gesture/event/API only). Detection
    reacts *only* to this phrase; utterances are not published — the wake
    word merely presses the push-to-talk button."""
    wake_backend: str = "auto"
    """``auto`` (reuse the STT transcriber) | ``scripted`` (tests)."""


@dataclass(frozen=True)
class PlannerConfig:
    """Multi-step plan execution (each step gated individually)."""

    enabled: bool = True
    step_timeout_s: float = 60.0
    """Max wait for one stage's results (confirmation time included)."""
    max_steps: int = 20
    accept_llm_plans: bool = True
    """Whether plans proposed by the chat reasoner may run. Every step
    still passes validation, permissions and confirmation individually."""
    intent_triggers: dict[str, str] = field(default_factory=dict)
    """``intent -> plan name``: lets a gesture start a routine."""
    plans: dict[str, dict] = field(default_factory=dict)
    """Named routines: ``{name: {description, on_error, steps: [...]}}``;
    a step is ``{action, params, label}`` or ``{parallel: [steps]}``."""


@dataclass(frozen=True)
class ProfilesConfig:
    """Named per-user profiles overriding gesture/intent settings.

    A profile is a partial override of the ``gesture`` and ``intent``
    sections only — thresholds, disabled gestures, repeat behaviour and
    intent mappings are per-user concerns; bus sizing and logging are not.
    The active profile is chosen here or with ``--profile`` on the CLI.
    """

    active: str = "default"
    available: dict[str, dict] = field(
        default_factory=lambda: {"default": {}}
    )


@dataclass(frozen=True)
class AppConfig:
    """Root configuration handed to the kernel."""

    logging: LoggingConfig = field(default_factory=LoggingConfig)
    bus: EventBusConfig = field(default_factory=EventBusConfig)
    gesture: GestureModuleConfig = field(default_factory=GestureModuleConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)
    context_perception: ContextPerceptionConfig = field(
        default_factory=ContextPerceptionConfig
    )
    screen_reading: ScreenReadingConfig = field(
        default_factory=ScreenReadingConfig
    )
    files: FilesConfig = field(default_factory=FilesConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    profiles: ProfilesConfig = field(default_factory=ProfilesConfig)


# ---------------------------------------------------------------------------
# Loading / merging / validation
# ---------------------------------------------------------------------------
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
    gesture = config.gesture
    intent = config.intent
    security = config.security
    automation = config.automation
    context = config.context_perception
    llm = config.llm
    planner = config.planner
    voice = config.voice
    memory = config.memory
    screen = config.screen_reading
    files = config.files
    plugins = config.plugins
    secrets = config.secrets
    browser = config.browser
    dashboard = config.dashboard
    knowledge = config.knowledge
    _DECISIONS = {"allow", "confirm", "deny"}
    _RISKS = {"safe", "sensitive", "dangerous"}
    _OCR_LANG_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-")
    checks: list[tuple[bool, str]] = [
        (config.bus.max_queue_size >= 1, "bus.max_queue_size must be >= 1"),
        (config.bus.slow_handler_warn_ms > 0, "bus.slow_handler_warn_ms must be > 0"),
        (gesture.frame_width > 0 and gesture.frame_height > 0,
         "gesture frame resolution must be positive"),
        (0 < gesture.detection_confidence <= 1,
         "gesture.detection_confidence must be in (0, 1]"),
        (0 < gesture.tracking_confidence <= 1,
         "gesture.tracking_confidence must be in (0, 1]"),
        (1 <= gesture.max_hands <= 4, "gesture.max_hands must be within 1..4"),
        (gesture.model_complexity in (0, 1),
         "gesture.model_complexity must be 0 or 1"),
        (0 < gesture.landmark_smoothing <= 1,
         "gesture.landmark_smoothing must be in (0, 1]"),
        (0 < gesture.min_confidence < 1,
         "gesture.min_confidence must be in (0, 1)"),
        (gesture.min_votes <= gesture.history,
         "gesture.min_votes cannot exceed gesture.history"),
        (gesture.poll_interval_s > 0, "gesture.poll_interval_s must be > 0"),
        (gesture.repeat_interval_s >= 0,
         "gesture.repeat_interval_s must be >= 0"),
        (isinstance(gesture.gesture_thresholds, dict)
         and all(
             isinstance(k, str) and isinstance(v, (int, float)) and 0 < v <= 1
             for k, v in gesture.gesture_thresholds.items()
         ),
         "gesture.gesture_thresholds must map semantic ids to values in (0, 1]"),
        (isinstance(gesture.disabled_gestures, list)
         and all(isinstance(g, str) and g for g in gesture.disabled_gestures),
         "gesture.disabled_gestures must be a list of semantic ids"),
        (isinstance(gesture.custom_gesture_modules, list)
         and all(isinstance(m, str) and m for m in gesture.custom_gesture_modules),
         "gesture.custom_gesture_modules must be a list of module paths"),
        (isinstance(intent.mappings, dict)
         and all(
             isinstance(ctx, str)
             and isinstance(table, dict)
             and all(isinstance(k, str) and isinstance(v, str) for k, v in table.items())
             for ctx, table in intent.mappings.items()
         ),
         "intent.mappings must be {context: {gesture: intent}} of strings"),
        (bool(intent.default_context), "intent.default_context must be non-empty"),
        (bool(memory.db_path), "memory.db_path must be non-empty"),
        (bool(memory.key_path), "memory.key_path must be non-empty"),
        (memory.working_capacity >= 1,
         "memory.working_capacity must be >= 1"),
        (memory.working_window_s > 0,
         "memory.working_window_s must be > 0"),
        (memory.episodic_max_records >= 10,
         "memory.episodic_max_records must be >= 10"),
        (memory.retention_days > 0, "memory.retention_days must be > 0"),
        (memory.prune_interval_s >= 1,
         "memory.prune_interval_s must be >= 1"),
        (memory.search_half_life_days > 0,
         "memory.search_half_life_days must be > 0"),
        (voice.mode in ("push_to_talk", "continuous"),
         "voice.mode must be push_to_talk or continuous"),
        (voice.engine in ("vosk", "whisper"),
         "voice.engine must be 'vosk' or 'whisper'"),
        (voice.engine != "vosk" or bool(voice.model_path),
         "voice.model_path must be non-empty (engine: vosk)"),
        (voice.engine != "whisper" or bool(voice.whisper_model),
         "voice.whisper_model must be non-empty (engine: whisper)"),
        (voice.sample_rate in (8000, 16000, 22050, 44100, 48000),
         "voice.sample_rate must be a standard rate"),
        (1 <= voice.block_ms <= 500, "voice.block_ms must be within 1..500"),
        (voice.max_utterance_s > 0, "voice.max_utterance_s must be > 0"),
        (isinstance(voice.listen_intents, list)
         and all(isinstance(i, str) and i for i in voice.listen_intents),
         "voice.listen_intents must be a list of intent names"),
        (isinstance(voice.wake_word, str),
         "voice.wake_word must be a string ('' disables it)"),
        (voice.wake_backend in ("auto", "scripted"),
         "voice.wake_backend must be 'auto' or 'scripted'"),
        (voice.tts_backend in ("auto", "piper", "jarvis", "espeak-ng", "espeak",
                               "say", "powershell"),
         "voice.tts_backend must be auto/piper/jarvis/espeak-ng/espeak/say/powershell"),
        (50 <= voice.tts_rate_wpm <= 400,
         "voice.tts_rate_wpm must be within 50..400"),
        (planner.step_timeout_s > 0, "planner.step_timeout_s must be > 0"),
        (1 <= planner.max_steps <= 100,
         "planner.max_steps must be within 1..100"),
        (isinstance(planner.intent_triggers, dict)
         and all(isinstance(k, str) and k and isinstance(v, str) and v
                 for k, v in planner.intent_triggers.items()),
         "planner.intent_triggers must map intent names to plan names"),
        (all(trigger_plan in planner.plans
             for trigger_plan in planner.intent_triggers.values()),
         "planner.intent_triggers reference unknown plans"),
        (isinstance(planner.plans, dict)
         and all(isinstance(k, str) and k and isinstance(v, dict)
                 for k, v in planner.plans.items()),
         "planner.plans must map names to plan definitions"),
        (llm.provider in ("gemini", "anthropic", "ollama"),
         "llm.provider must be 'gemini', 'anthropic' or 'ollama'"),
        (bool(llm.model), "llm.model must be non-empty"),
        (bool(llm.api_key_env), "llm.api_key_env must be non-empty"),
        (llm.max_tokens >= 16, "llm.max_tokens must be >= 16"),
        (0.0 <= llm.temperature <= 1.0, "llm.temperature must be in [0, 1]"),
        (llm.timeout_s > 0, "llm.timeout_s must be > 0"),
        (llm.history_turns >= 1, "llm.history_turns must be >= 1"),
        (llm.memory_results >= 0, "llm.memory_results must be >= 0"),
        (context.poll_interval_s > 0,
         "context_perception.poll_interval_s must be > 0"),
        (bool(context.fallback_context),
         "context_perception.fallback_context must be non-empty"),
        (isinstance(context.rules, list)
         and all(
             isinstance(rule, dict)
             and isinstance(rule.get("context"), str) and rule.get("context")
             and isinstance(rule.get("any"), list) and rule.get("any")
             and all(isinstance(s, str) and s for s in rule["any"])
             for rule in context.rules
         ),
         "context_perception.rules must be a list of "
         "{context: str, any: [substr, ...]}"),
        (screen.capture_backend in ("auto", "scrot", "imagemagick",
                                    "gnome-screenshot", "screencapture",
                                    "powershell"),
         "screen_reading.capture_backend must be auto/scrot/imagemagick/"
         "gnome-screenshot/screencapture/powershell"),
        (bool(screen.ocr_language)
         and set(screen.ocr_language) <= _OCR_LANG_CHARS,
         "screen_reading.ocr_language must be a tesseract language code "
         "(e.g. 'eng' or 'eng+deu')"),
        (isinstance(screen.max_chars, int)
         and 100 <= screen.max_chars <= 100_000,
         "screen_reading.max_chars must be in 100..100000"),
        (screen.capture_timeout_s > 0,
         "screen_reading.capture_timeout_s must be > 0"),
        (screen.ocr_timeout_s > 0, "screen_reading.ocr_timeout_s must be > 0"),
        (isinstance(files.allowed_roots, list)
         and all(isinstance(r, str) and r.strip() for r in files.allowed_roots),
         "files.allowed_roots must be a list of directory paths"),
        (isinstance(files.max_read_chars, int)
         and 1 <= files.max_read_chars <= 100_000,
         "files.max_read_chars must be in 1..100000"),
        (isinstance(files.max_write_chars, int)
         and 1 <= files.max_write_chars <= 1_000_000,
         "files.max_write_chars must be in 1..1000000"),
        (isinstance(files.max_list_entries, int)
         and 1 <= files.max_list_entries <= 5000,
         "files.max_list_entries must be in 1..5000"),
        (isinstance(files.max_scan_files, int)
         and 1 <= files.max_scan_files <= 100_000,
         "files.max_scan_files must be in 1..100000"),
        (bool(files.trash_dir_name)
         and files.trash_dir_name not in (".", "..")
         and "/" not in files.trash_dir_name
         and "\\" not in files.trash_dir_name,
         "files.trash_dir_name must be a plain directory name"),
        (isinstance(plugins.paths, list)
         and all(isinstance(p, str) and p.strip() for p in plugins.paths),
         "plugins.paths must be a list of directory paths"),
        (secrets.backend in ("auto", "keyring", "file"),
         "secrets.backend must be auto/keyring/file"),
        (bool(str(secrets.file_path).strip()),
         "secrets.file_path must be a file path"),
        (bool(str(secrets.key_path).strip()),
         "secrets.key_path must be a file path"),
        (bool(str(secrets.index_path).strip()),
         "secrets.index_path must be a file path"),
        (isinstance(browser.allowed_domains, list)
         and all(isinstance(d, str) and d.strip() and "/" not in d
                 and "://" not in d
                 for d in browser.allowed_domains),
         "browser.allowed_domains must be bare hostnames (no scheme/path)"),
        (isinstance(browser.max_extract_chars, int)
         and 100 <= browser.max_extract_chars <= 100_000,
         "browser.max_extract_chars must be in 100..100000"),
        (browser.navigation_timeout_s > 0,
         "browser.navigation_timeout_s must be > 0"),
        (isinstance(dashboard.port, int) and 0 <= dashboard.port <= 65535,
         "dashboard.port must be 0..65535"),
        (bool(str(dashboard.host).strip()),
         "dashboard.host must be a hostname or address"),
        (dashboard.allow_remote
         or str(dashboard.host) in ("127.0.0.1", "localhost", "::1"),
         "dashboard.host must be loopback unless dashboard.allow_remote is "
         "true"),
        (isinstance(dashboard.recent_events, int)
         and 10 <= dashboard.recent_events <= 5000,
         "dashboard.recent_events must be in 10..5000"),
        (not (security.confirmation == "web" and not dashboard.enabled),
         "security.confirmation: web requires dashboard.enabled: true"),
        (bool(str(knowledge.db_path).strip()),
         "knowledge.db_path must be a file path"),
        (knowledge.embedder in ("hashing", "semantic"),
         "knowledge.embedder must be \'hashing\' or \'semantic\'"
         "via the Embedder interface)"),
        (isinstance(knowledge.embedding_dim, int)
         and 16 <= knowledge.embedding_dim <= 4096,
         "knowledge.embedding_dim must be in 16..4096"),
        (isinstance(knowledge.chunk_chars, int)
         and 100 <= knowledge.chunk_chars <= 10_000,
         "knowledge.chunk_chars must be in 100..10000"),
        (isinstance(knowledge.chunk_overlap, int)
         and 0 <= knowledge.chunk_overlap < knowledge.chunk_chars,
         "knowledge.chunk_overlap must be >= 0 and < chunk_chars"),
        (isinstance(knowledge.top_k, int) and 1 <= knowledge.top_k <= 20,
         "knowledge.top_k must be in 1..20"),
        (isinstance(knowledge.watch_paths, list)
         and all(isinstance(p, str) and p.strip()
                 for p in knowledge.watch_paths),
         "knowledge.watch_paths must be a list of directory paths"),
        (knowledge.watch_interval_s >= 1.0,
         "knowledge.watch_interval_s must be >= 1.0"),
        (0.0 <= knowledge.min_score <= 1.0,
         "knowledge.min_score must be in 0..1"),
        (isinstance(knowledge.prompt_max_chars, int)
         and 200 <= knowledge.prompt_max_chars <= 20_000,
         "knowledge.prompt_max_chars must be in 200..20000"),
        (isinstance(security.risk_defaults, dict)
         and set(security.risk_defaults) <= _RISKS
         and all(v in _DECISIONS for v in security.risk_defaults.values()),
         "security.risk_defaults must map safe/sensitive/dangerous to "
         "allow/confirm/deny"),
        (isinstance(security.permissions, dict)
         and all(isinstance(k, str) and k and v in _DECISIONS
                 for k, v in security.permissions.items()),
         "security.permissions must map action names to allow/confirm/deny"),
        (security.confirmation in ("console", "auto_deny", "web"),
         "security.confirmation must be 'console', 'auto_deny' or 'web'"),
        (security.confirmation_timeout_s > 0,
         "security.confirmation_timeout_s must be > 0"),
        (bool(security.audit_file), "security.audit_file must be non-empty"),
        (security.audit_max_bytes >= 1024,
         "security.audit_max_bytes must be >= 1024"),
        (bool(str(security.devices_dir).strip()),
         "security.devices_dir must be a directory path"),
        (automation.max_queue_size >= 1,
         "automation.max_queue_size must be >= 1"),
        (automation.input_backend in ("auto", "xdotool", "pynput"),
         "automation.input_backend must be auto, xdotool or pynput"),
        (isinstance(automation.max_type_text_chars, int)
         and 1 <= automation.max_type_text_chars <= 5000,
         "automation.max_type_text_chars must be in 1..5000"),
        (automation.action_timeout_s > 0,
         "automation.action_timeout_s must be > 0"),
        (isinstance(automation.applications, dict)
         and all(isinstance(k, str) and k and isinstance(v, str) and v.strip()
                 for k, v in automation.applications.items()),
         "automation.applications must map names to command strings"),
        (isinstance(automation.intent_bindings, dict)
         and all(
             isinstance(intent_name, str) and intent_name
             and isinstance(binding, dict)
             and isinstance(binding.get("action"), str) and binding.get("action")
             and isinstance(binding.get("params", {}), dict)
             for intent_name, binding in automation.intent_bindings.items()
         ),
         "automation.intent_bindings must be {intent: {action: str, params: dict}}"),
    ]
    problems = [message for ok, message in checks if not ok]
    if not problems and isinstance(planner.plans, dict):
        from digital_twin.planner.plan import plan_from_config

        for plan_name, raw in planner.plans.items():
            try:
                plan_from_config(plan_name, raw, planner.max_steps)
            except (ValueError, TypeError) as exc:
                problems.append(f"planner.plans.{plan_name}: {exc}")
    if problems:
        raise ValueError("Invalid configuration: " + "; ".join(problems))
    return config


#: Config sections a user profile is allowed to override.
_PROFILE_SECTIONS = frozenset({"gesture", "intent"})


def _apply_profile(config: AppConfig, profile_name: str) -> AppConfig:
    """Overlay the named profile's gesture/intent overrides onto ``config``."""
    available = config.profiles.available
    if not isinstance(available, dict) or not all(
        isinstance(name, str) and isinstance(overrides, dict)
        for name, overrides in available.items()
    ):
        raise ValueError(
            "profiles.available must map profile names to override mappings"
        )
    if profile_name not in available:
        raise ValueError(
            f"Unknown profile {profile_name!r}; available: {sorted(available)}"
        )
    overrides = available[profile_name]
    illegal = set(overrides) - _PROFILE_SECTIONS
    if illegal:
        raise ValueError(
            f"Profile {profile_name!r} may only override "
            f"{sorted(_PROFILE_SECTIONS)}; found {sorted(illegal)}"
        )
    if not overrides:
        return config
    logger.info("Applying profile %r", profile_name)
    return _merge(config, overrides)


def load_config(path: str | Path | None = None, profile: str | None = None) -> AppConfig:
    """Load configuration, applying YAML overrides from ``path`` if given.

    ``profile`` selects a user profile from ``profiles.available``
    (overriding ``profiles.active``). A missing or malformed file falls
    back to defaults with a warning — the assistant must always be able to
    start — but an unknown or invalid *profile* is a hard error: silently
    running with someone else's tuning would be worse than not starting.
    """
    config = AppConfig()
    raw: dict[str, Any] | None = None

    if path is not None:
        file_path = Path(path)
        if not file_path.exists():
            logger.warning("Config file not found, using defaults: %s", file_path)
        else:
            try:
                loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                logger.error(
                    "Failed to parse config %s: %s — using defaults", file_path, exc
                )
                loaded = None
            if loaded is not None and not isinstance(loaded, dict):
                logger.error(
                    "Config root must be a mapping, got %s — using defaults",
                    type(loaded).__name__,
                )
            elif loaded is not None:
                raw = loaded

    if raw:
        config = _merge(config, raw)
    config = _apply_profile(config, profile or config.profiles.active)
    return _validate(config)
