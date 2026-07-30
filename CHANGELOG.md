# Changelog

All notable changes to the Digital Twin AI Assistant.
Format follows [Keep a Changelog](https://keepachangelog.com); versions follow SemVer.

## [Unreleased] — Gemini backend

### Added
- **Gemini backend** (`reasoning/llm.py`): `GeminiModel` via Google's
  `generateContent` API. Key resolution matches Anthropic's (M14): secret
  store first (`gemini_api_key`), then the environment variable
  (`GEMINI_API_KEY` by default), never config files, sent via the
  `x-goog-api-key` **header** — never a URL query parameter, so it can't
  leak into a logged/raised URL. Handles Gemini's `role: model` (vs
  `assistant`) convention and surfaces `promptFeedback.blockReason` when
  a prompt is safety-blocked.
- **`llm.provider: gemini` is now the default** — it has a genuine free
  tier (no credit card). `anthropic` and `ollama` remain fully available;
  switching backends is still a config change, never a code change.
- 8 new tests (434 total).

## [0.17.0] — 2026-07-16 — Milestone 17: UI completion

### Added
- **Live camera in the dashboard** (`dashboard/frames.py`): a `FrameHub`
  (latest-frame-per-source fan-out) plus a `/api/frames/<name>` MJPEG
  endpoint (`multipart/x-mixed-replace`). The gesture `DebugView` gained
  a **headless** mode and a `frame_sink`, so it annotates frames and
  JPEG-encodes them into the hub without needing an OpenCV window — the
  gesture debugger now renders in the browser, including on headless
  machines. `GesturePerceptionModule` accepts a `frame_sink`.
- **Push-driven page**: the dashboard page opens an `EventSource` on the
  M15 `/api/stream` and refreshes on each pushed event (slow timer kept
  as a fallback). Live mic / wake / camera indicators read existing
  module statuses and metrics.
- **Plugin panel** (`/api/plugins`): each loaded plugin's health, sandbox
  flag, actions, and — for a disabled one — its exact error, making the
  M11/M14 contract enforcement visible.
- **Settings panel** (`/api/settings`): the active `AppConfig` with every
  non-default field flagged. Read-only by design (config is frozen at
  startup); the honest path is edit-YAML-and-restart.
- Demo `examples/ui_completion_demo.py`: all four panels plus a live
  MJPEG stream driven over the HTTP API.
- 8 new tests (426 total): FrameHub semantics (latest-only, wait/notify,
  empty-frame rejection), MJPEG streaming, plugin + settings endpoints,
  push-driven page markup, headless debug-view frame sink.

### Changed
- `DashboardServer` accepts a `frame_hub`; `DashboardModule` accepts
  `plugin_reports`, `app_config`, and `frame_hub`. `main.py` builds a
  `FrameHub`, captures plugin reports, and wires them through.


## [0.16.0] — 2026-07-16 — Milestone 16: Write-side connectors, wake word, installers

### Added
- **Write-side connector actions** — the first that change the outside
  world, classified DANGEROUS by the write-risk rule (reading is
  SENSITIVE; changing someone else's world is DANGEROUS):
  - `email.send_email` (to/subject/body over stdlib SMTP+STARTTLS):
    clamp-confirmed always, with the full outbound content visible in
    the confirmation; a `allowed_recipient_domains` list refuses
    off-list recipients **before** any gate (mis-send containment);
    password by secret name, deleted after login.
  - `calendar.create_event` (summary/start/duration): writes a proper
    `.ics` VEVENT into the connector's folder — offline, no calendar
    API; anything syncing the folder picks it up.
- **Wake word** (`voice/wake.py`): `voice.wake_word: "hey twin"` runs an
  always-on detector with its own mic + transcriber that publishes only
  `voice.control {command: start}` — pressing the existing push-to-talk
  button. It never publishes utterances (privacy by structure), reuses
  the Transcriber/AudioSource interfaces (scripted in tests), and the
  voice module needed zero changes. Config: `voice.wake_word`,
  `voice.wake_backend`.
- **Installer** (`digital_twin/setup_cli.py`, console script
  `digital-twin-setup`): creates a `DIGITAL_TWIN_HOME` (config copy +
  data/logs/models), idempotent, `--force` to overwrite — one command
  from wheel install to runnable.
- Demo `examples/write_wake_setup_demo.py`: risk taxonomy live, off-list
  recipient refused pre-gate, a clamp-confirmed `.ics` write landing on
  disk, the wake trigger with zero utterances published, and a fresh
  home directory.
- 11 new tests (418 total): write-action risk classification, send_email
  field + recipient-allow-list validation, `.ics` creation, wake trigger
  / non-wake rejection / empty-phrase guard / never-publishes-utterances,
  setup CLI creation + idempotence + `--force`.

### Fixed
- Connector test harness captured only the last-registered action per
  plugin; now keyed by name (surfaced when the connectors grew second
  actions).


## [0.15.0] — 2026-07-15 — Milestone 15: Dashboard depth + document ingestion

### Added
- **Document extraction** (`knowledge/extraction.py`): one `extract_text`
  entry point dispatching on suffix — plain text/source files, `.docx`
  (dependency-free zip+XML paragraph scan), `.html` (stdlib parser,
  scripts/styles stripped), and `.pdf` (`pdftotext` if present, else
  `pypdf`, else a guided error). `ingest_document` now routes through it
  instead of assuming UTF-8 text.
- **Folder watching** (`knowledge/watch.py`): `KnowledgeWatchModule`
  polls `knowledge.watch_paths` and auto-ingests new/changed supported
  files. Each watch path is validated inside `files.allowed_roots` at
  construction (out-of-bounds paths dropped, never read); content-hash
  idempotence makes rescans cheap; ingests are announced on the bus.
  Config: `knowledge.watch_paths`, `knowledge.watch_interval_s`.
- **Dashboard depth**: extensible `data_sources` map on the server, a
  `/api/memory` panel (recent records) and `/api/knowledge` panel
  (ingested documents + active embedder), and **`/api/stream`
  Server-Sent Events** pushing new bus events instead of polling.
  `MemoryModule` exposes its store via a `store` property.
- Demo `examples/dashboard_depth_demo.py`: extraction of docx/html,
  folder watching with idempotence + containment, the new panels, and a
  live SSE frame.
- 22 new tests (407 total): format extraction (docx/html dependency-free,
  pdf-missing guidance, unsupported types), watcher containment/
  idempotence/filtering/bus-announce, edited-file replacement, chat-note
  accumulation, dashboard memory/knowledge endpoints, SSE push.

### Fixed
- **Stale document versions on re-ingest**: a `replace_source` flag on
  `KnowledgeStore.ingest` (set for file-backed ingestion) supersedes an
  edited file's previous chunks instead of leaving them recallable beside
  the new version. Chat-sourced notes still accumulate. (Surfaced by the
  M15 demo.)


## [0.14.0] — 2026-07-15 — Milestone 14: Hardening & distribution

### Added
- **Subprocess plugin isolation** (`plugins/subprocess_host.py`,
  `plugins/sandbox.py`): a manifest may declare `isolation: subprocess`;
  the plugin runs in a child process behind a JSON-over-stdio bridge,
  its actions proxied as normal gated `ActionSpec`s with the manifest
  risk floor re-applied parent-side. A crash, hang (per-call timeout) or
  protocol desync fails only that action — the kernel and other plugins
  are untouched — and children die at interpreter exit. The sandboxed
  API is narrower than in-process: `register_module` and `api.secret()`
  raise (no bus in the child; secrets never leave the kernel). Honest
  scope: fault + capability isolation, not an OS sandbox.
- **LLM keys via the secret store**: `create_language_model` resolves
  `llm.api_key_secret` (default `anthropic_api_key`) before the
  environment variable; keys are still never read from config files.
- **Semantic embeddings**: `knowledge.embedder: semantic` selects a
  sentence-transformers backend (optional `[semantic]` extra, lazy
  import); the store's model-name pin forces a re-ingest on switch.
- **Wheel-safe asset resolution** (`digital_twin/paths.py`): config and
  model paths resolve against the CWD, then `$DIGITAL_TWIN_HOME`, then
  the package location — checkout or installed wheel both work.
- **User guide** (`docs/USER_GUIDE.md`): install → first run → operating
  every subsystem, permissions, dashboard, plugins/trust levels,
  troubleshooting.
- Demo `examples/sandbox_demo.py`: three sandboxed plugins — floor
  applied to a lying child, a secret-grabber disabled whole, a
  self-killing plugin contained while its sibling keeps answering — plus
  the hardening trio (secret-store key, redirect-proof `browser_fill`).
- 27 new tests (392 total): manifest `isolation`, sandbox roundtrip +
  floor, crash/exit/timeout containment, secret/module refusal in the
  child, browser interaction host re-check, LLM key precedence
  (store > env > error), asset resolver search order.

### Changed
- `browser_fill`/`browser_click` re-check the live page host against
  `browser.allowed_domains` at execution time (redirect containment;
  previously only `browser_fill_secret` did).
- Manifest gained the optional `isolation` field (default `in_process`,
  fully backward compatible).


## [0.13.0] — 2026-07-14 — Milestone 13: Knowledge engine (RAG) + connectors

### Added
- **Knowledge engine** (`knowledge/{embedding,store,actions}.py`): local
  document RAG — paragraph-first chunking with tail overlap, a
  **dependency-free hashing embedder** behind a replaceable `Embedder`
  interface (honest lexical similarity; semantic models are drop-ins),
  SQLite chunk/vector store with content-hash idempotent ingestion and an
  embedder name+dimension pin that refuses to mix vector spaces, and
  brute-force cosine search (deliberate: milliseconds at personal-corpus
  scale; ANN deferred until a corpus justifies the dependency).
- **RAG in the reasoner**: per message, top-k chunks above a score floor
  are injected into the prompt (bounded) with a cite-or-say-so
  instruction; recall failures degrade to no section, never no reply.
- **Knowledge actions** (all SENSITIVE, all gated): `ingest_document`
  (confined to `files.allowed_roots` via the same containment as file
  actions), `ingest_text`, `search_knowledge`, `list_knowledge`,
  `forget_document` (derived data only — the original file is never
  touched).
- **Connector plugins** shipped in `plugins/examples/` as real M11
  plugins: `calendar` (offline `.ics` parsing incl. folded lines and
  TZID parameters), `tasks` (local JSON add/list/complete — the template
  for API-backed connectors), `email` (stdlib IMAP, **headers only**,
  mailbox read-only, password resolved **by name** from the secrets
  manager).
- **`api.secret(name)`** on the PluginAPI: explicit, documented secret
  access for connectors under the M11 values-by-name rule.
- Config section `knowledge` (validated, yaml-pinned); kernel wiring
  (shared secret store between browser and plugins).
- Demo `examples/knowledge_connectors_demo.py`: ingest → ranked recall →
  the literal prompt section, idempotence + forgetting, and all three
  connectors through gates with a leak-sweep on the IMAP password.
- 18 new tests (371 total): embedder determinism/ranking, chunk bounds,
  store idempotence/pin/forget, containment on ingest, gated end-to-end
  flows, reasoner RAG section + broken-store resilience, all three
  connectors incl. header-only/read-only/no-leak email discipline.

## [0.12.0] — 2026-07-14 — Milestone 12: Dashboard, packaging, performance

### Added
- **Web dashboard** (`dashboard/module.py`, `dashboard/server.py`):
  stdlib-only local web UI (dark theme, 1 s polling) — live module
  states + bus counters, recent-event feed (bounded ring buffer), chat
  box publishing `perception.chat` exactly like the console. **Loopback
  binding enforced by config validation** (`allow_remote` is an explicit
  opt-out) and all state-changing endpoints require a per-session random
  token embedded only in the served page (CSRF containment).
- **Web confirmations** (`dashboard/web_confirmation.py`): with
  `security.confirmation: web`, gated actions appear in the page with
  Approve/Deny buttons and are answered over HTTP — fail-closed exactly
  like the console provider (timeout → deny, no answer → deny), same
  DANGEROUS clamp, same audit. **Resolves the long-tracked stdin
  conflict** between console chat and console confirmation prompts.
- **Packaging** (`pyproject.toml`): dependency-lean core (PyYAML only)
  with extras `[gesture]`, `[encryption]`, `[browser]`, `[keyring]`,
  `[voice]`, `[dev]`, `[all]`; console scripts `digital-twin`,
  `digital-twin-memory`, `digital-twin-secrets`; package version
  `digital_twin.__version__`.
- **CI** (`.github/workflows/ci.yml`): tests + all demos + syntax sweep
  + config pin check on Python 3.10/3.12, deliberately *without* heavy
  extras — the lazy-backend claim is proven on every push.
- **Performance pass** (`examples/benchmark.py`): measured on this
  machine — bus ≈ 62.5k events/s; full gate pipeline 0.32 ms median /
  0.68 ms p95 per action; late-bound catalog 0.13 µs per message.
- Config section `dashboard` (validated, yaml-pinned; loopback rule and
  `web`-provider cross-check included); `build_action_dispatcher` accepts
  a confirmation-provider override for kernel composition.
- Demo `examples/dashboard_demo.py`: drives the dashboard over its own
  HTTP API — status, chat, token guard (403 without it), and a DANGEROUS
  action approved from the web with the clamp firing.
- 9 new tests (353 total): config validation (loopback rule, web/
  dashboard cross-check), fail-closed web confirmations, endpoint shapes,
  event feed capture, token rejection, chat publication, end-to-end
  web-approved DANGEROUS action, port release on stop.

## [0.11.0] — 2026-07-14 — Milestone 11: Plugins, secrets, browser

### Added
- **Plugin system** (`plugins/manifest.py`, `plugins/loader.py`):
  discovery from `plugins.paths` (default empty — nothing loads without
  explicit opt-in); `plugin.yaml` manifests as an **enforced contract**
  — every registered action must be declared with a risk level,
  declarations are floors (a plugin can never register safer than
  declared), all capabilities are namespaced `<plugin>.<name>` /
  `<plugin>_…` so built-ins can't be shadowed; **two-phase commit** (a
  plugin failing mid-`register()` contributes nothing); per-plugin fault
  isolation at load; plugin actions execute through the full dispatcher
  gate pipeline. Third-party modules/actions now register **without
  editing `main.py`**.
- **Secrets manager** (`security/secrets.py`, `security/secrets_cli.py`):
  values by name, never by value — names travel in params/results/audit,
  values resolve inside handlers at the last moment. Backends: OS
  keyring (optional `keyring` package, names-only index for listing) or
  Fernet-encrypted file with `0600` key, atomic writes, no silent
  downgrade. CLI: `set` (value via hidden prompt or `--stdin`, never
  argv), `get` (hidden unless `--reveal`), `list`, `delete`.
- **Browser automation** (`browser/driver.py`, `browser/actions.py`):
  replaceable driver (Playwright/Chromium reference, lazy import with
  install guidance; scripted double for tests) behind gated actions —
  `browser_open`/`browser_extract_text` (SENSITIVE, http(s)-only,
  optional `allowed_domains`), `browser_fill`/`browser_click`
  (DANGEROUS, clamped), `browser_close` (SAFE), and
  `browser_fill_secret` (DANGEROUS): fills a page element from the
  secret store **by name**, refuses to run without a configured domain
  allow-list, and refuses when the current page's host isn't on it —
  a confirmation dialog is never the only thing between a secret and a
  phishing page.
- Config sections `plugins`, `secrets`, `browser` (validated,
  yaml-pinned); kernel wiring for all three.
- Demo `examples/plugins_secrets_browser_demo.py`: contract enforcement
  live (a sneaky plugin disabled), encryption-at-rest proof, and a login
  fill whose value provably never touches results, audit or prompts.
- 40 new tests (344 total): manifest contract violations, risk floors,
  two-phase commit, fault isolation, plugin action through gates,
  both secret backends + CLI, URL policy, clamp on click/fill,
  secret-leak sweeps over audit/results/confirmations.

### Changed
- The chat reasoner's action catalog is **late-bound** (callable
  evaluated per message) — closes the M9/M10 debt where `speak` and
  late-registered actions were invisible to the LLM prompt.

## [0.10.0] — 2026-07-14 — Milestone 10: Screen OCR + file-system intelligence

### Added
- **Screen capture backends** (`perception/screen/capture.py`): subprocess
  screenshots via scrot / ImageMagick / gnome-screenshot (Linux),
  screencapture (macOS), PowerShell CopyFromScreen (Windows) — zero pip
  deps; first-available or explicit selection with install guidance.
- **Local OCR** (`perception/screen/ocr.py`): replaceable recognizer
  interface; `tesseract` subprocess reference backend (screen content
  never leaves the machine); scripted recognizer for tests/demos.
- **Screen-reading module** (`perception/screen/module.py`): **on-demand
  only — no polling**. The screen is captured exclusively inside
  `read_now()`; the screenshot file is deleted the moment OCR returns;
  OCR text (bounded by `screen_reading.max_chars`) publishes once on
  `perception.screen`. Backends resolve lazily on first use, so the
  kernel starts on machines without scrot/tesseract.
- **The `read_screen` action** (`perception/screen/actions.py`):
  SENSITIVE — every capture passes permissions, confirmation and audit;
  results and audit rows carry **character counts, never the text**; one
  permission rule (`{read_screen: deny}`) removes the capability. Default
  intent binding `read_screen` lets chat ("read my screen") nominate it.
- Reasoner consumes `perception.screen`: the latest OCR text is injected
  into the prompt while fresh (10-minute TTL, bounded), so "what's on my
  screen?" is answerable — one additive subscription, `core/` untouched.
- **File-system intelligence** (`automation/file_actions.py`) — the
  **first DANGEROUS-class actions**, finally exercising the M3
  confirm-clamp. Containment: every path must resolve (post-symlink)
  inside `files.allowed_roots` (default **empty** — the
  `open_application` precedent), checked at validation *and* re-checked
  in handlers. SENSITIVE: `list_files`, `search_files`, `read_text_file`
  (bounded), `find_duplicates` (size + SHA-256), `create_directory`,
  `write_text_file` (exclusive-create: overwrite impossible), `copy_file`
  (never overwrites). DANGEROUS: `move_file` (never overwrites) and
  `delete_file` — which moves to a per-root, listing-hidden **trash
  directory** instead of unlinking; a confirmed mistake stays
  recoverable, and deleting from the trash is refused.
- Config sections `screen_reading` and `files` (validated, yaml-pinned);
  kernel wiring registers screen/file actions **before** the reasoner
  snapshots the action catalog, so the LLM can propose them in plans.
- Demo `examples/screen_files_demo.py`: gated OCR → reasoner answering
  from the screen → file ops → DANGEROUS `delete_file` configured `allow`
  yet still confirmed (the clamp, live), recovered from trash.
- 33 new tests (304 total, all hardware-free): backend selection/guidance,
  screenshot-deleted-even-on-OCR-failure, counts-only audit, symlink-escape
  rejection, overwrite refusals, trash recovery, and the clamp end to end.

## [0.9.0] — 2026-07-13 — Milestone 9: Voice

### Added
- **Audio sources** (`voice/audio.py`): blocking-read interface;
  sounddevice/PortAudio implementation (lazy import); scripted source
  with open/close counters for privacy assertions.
- **Streaming transcription** (`voice/transcriber.py`): replaceable
  engine interface; offline **Vosk** reference backend (audio never
  leaves the machine; missing model fails start with download guidance);
  scripted transcriber for tests/demos.
- **Speech synthesis** (`voice/synthesis.py`): subprocess backends
  (espeak-ng/espeak/say/PowerShell SAPI), zero pip deps, non-blocking
  `speak`, pollable `speaking`, interruptible `stop` — the barge-in
  primitive.
- **The `speak` action** (`voice/actions.py`): spoken output as a normal
  SAFE action — audited, plan-usable, and **one permission rule mutes the
  assistant** (`security.permissions: {speak: deny}`).
- **Voice module** (`voice/module.py`): push-to-talk (microphone opens
  *only* per session — privacy is structural) and continuous modes;
  listening toggles via `voice.control`, configured intents (open palm by
  default) or the API; live partials on `perception.voice.partial`;
  finals on `perception.voice`; utterance deadline; **barge-in** (a
  partial while speaking stops TTS); spoken replies via gated
  `action.execute {speak}`.
- Reasoner consumes `perception.voice` through the same handler as chat —
  third input modality, zero core changes.
- Config: `voice` section; topics `perception.voice`,
  `perception.voice.partial`, `voice.control`; dispatcher exposes a
  public `registry` property for kernel-composed actions.
- **Demo** (`examples/voice_demo.py`): gesture-triggered session →
  spoken command through the full chain → audited spoken reply →
  barge-in. No microphone needed.
- 17 new tests (271 total).

## [0.8.0] — 2026-07-08 — Milestone 8: The Planner

### Added
- **Plan model** (`planner/plan.py`): stages of steps — one stage's steps
  have no ordering dependency, stages are strict barriers; `{parallel:
  [...]}` config syntax; strict structural validation with step caps;
  sources: config routines, LLM proposals (sequential only), skill
  replays.
- **Planner module** (`planner/module.py`): single-worker choreography —
  publishes each step as `action.execute`, correlates `action.result` by
  `plan_id`/`step`, per-plan failure policy (`abort`/`continue`), stage
  timeouts, `plan.cancel`, full lifecycle on `plan.progress`, and
  gesture-triggered routines via `planner.intent_triggers`. Config plans
  parse at startup (broken routines fail config load).
- **Gated direct execution**: the dispatcher consumes `action.execute`
  through the *identical* pipeline as intents — registry, validation,
  permissions, confirmation, audit — with `plan_id`/`step` in every audit
  row. A plan is choreography, not privilege.
- **Skill memory's first producer**: successful LLM plans persist as
  `skill` memories and replay by text query (`plan.request {skill: ...}`).
  **Replays are re-confirmed step by step; approvals never persist.**
- **Reasoner plan proposals**: the model sees the action catalog (name,
  risk, description) and may return a sanitised `plan` field; proposals
  publish `plan.request` and are accepted only when
  `planner.accept_llm_plans` is on.
- Config: `planner` section; new topics `action.execute`, `plan.request`,
  `plan.progress`, `plan.cancel`.
- **Demo** (`examples/planner_demo.py`): routine with a parallel stage →
  chat-proposed plan with mid-plan confirmations → learned skill replayed
  (and re-gated).
- 22 new tests (254 total).

## [0.7.0] — 2026-07-08 — Milestone 7: Conversational Reasoning

### Added
- **Replaceable LLM interface** (`reasoning/llm.py`): one abstract
  completion API with Anthropic (Claude; key from a configurable
  environment variable only — never config files) and Ollama (local,
  offline) backends, stdlib HTTP, actionable `LLMError`s, and a scripted
  model for tests/demos.
- **Chat perception module** (`perception/chat/`): typed text as
  `perception.chat` events — programmatic `submit()` plus an optional
  console reader when the kernel terminal is a TTY. First non-camera
  input source; required zero core changes.
- **Chat reasoner** (`reasoning/chat_reasoner.py`): bounded-queue worker
  (LLM latency never touches the bus) producing structured decisions
  `{reply, intent, remember, reasoning}` with forgiving JSON parsing.
  **The LLM has no direct action access**: it may only nominate intents
  from the configured allow-list; nominations are ordinary
  `intent.detected` events passing every dispatcher gate; hallucinated
  names are dropped and counted. Memory recall (M6 ranked search) is
  injected into every prompt; `remember` facts persist as semantic
  memories through the memory module. Every reply carries a one-line
  `reasoning`; LLM failures degrade to apologetic responses; a missing
  API key fails the module at start with guidance while the rest of the
  assistant runs.
- Working memory now buffers `perception.chat` / `chat.response`
  activity; the kernel pretty-prints assistant replies.
- Config: `chat` and `llm` sections (provider, model, key env, Ollama
  URL, sampling, history window, memory injection count).
- **Demo** (`examples/chat_demo.py`): converse, teach a fact, recall it,
  execute a gated action, watch a hostile intent get dropped — no API
  key needed.
- 30 new tests (232 total).

## [0.6.0] — 2026-07-08 — Milestone 6: Memory Foundation

### Added
- **Memory store** (`memory/store.py`): SQLite (WAL, stdlib-only), one
  table for all kinds, thread-safe, with per-record access stats; ranked
  lexical search (importance × recency half-life), retention pruning
  (age + episodic record cap; semantic never auto-pruned), JSON export.
- **Encryption at rest** (`memory/codec.py`): Fernet content codec with an
  auto-generated 0600 key file; metadata stays queryable by documented
  design; enabling encryption without `cryptography` fails startup —
  security settings never silently downgrade.
- **Memory module** (`memory/module.py`): episodic memories derived from
  `action.result` with status-weighted importance, tags and provenance;
  in-RAM working memory buffering recent intent/context/result activity
  (wiped on pause — privacy over convenience); `remember_fact()` for
  user-taught semantic memories; periodic pruning; `memory.stored`
  announcements.
- **User control CLI** (`python -m digital_twin.memory`): list, ranked
  search, show, remember, edit (content/importance/tags), delete, bulk
  clear, JSON export, stats — review/edit/delete without any UI.
- Config: `memory` section (db path, encryption, key path, working
  capacity/window, episodic cap, retention, prune interval, search
  half-life); kernel registers the module when enabled.
- **Demo** (`examples/memory_demo.py`): act → remember → recall → teach a
  fact → forget it.
- 31 new tests (202 total).

## [0.5.0] — 2026-07-07 — Milestone 5: Desktop Input Actions

### Added
- **Input backends** (`automation/input_backend.py`): xdotool (Linux/X11,
  reference) and pynput (Windows/macOS, optional dep) behind one interface
  speaking a canonical key vocabulary; clipboard via xclip/xsel/pbcopy/clip;
  window focus via xdotool / osascript / Win32 EnumWindows. Lazy resolution:
  kernels start without an input stack; actions fail there as audited
  results with install guidance.
- **Six input actions** (`automation/input_actions.py`), risk-stratified:
  `nav_key` (arrows only) and `media_key` are SAFE → slide/media control
  runs prompt-free; `press_keys` (modifier chords), `type_text`,
  `set_clipboard`, `focus_window` are SENSITIVE → confirmation-gated.
  Pre-gate hard limits: canonical keys only, chords = modifiers + one key,
  media keys only via `media_key`, repeat ≤ 10, `type_text` rejects
  newlines/control chars (synthesised text cannot execute itself).
- **Default bindings**: `next_slide`, `previous_slide`, `seek_forward`,
  `seek_backward` → `nav_key`; `play_pause` → `media_key`;
  `end_presentation` → `press_keys [escape]` (gated).
- Config: `automation.input_backend` (auto/xdotool/pynput),
  `automation.max_type_text_chars`.
- **Demo** (`examples/presentation_demo.py`): gesture-driven slide control
  with zero prompts; ending the show hits the confirmation gate.
- 30 new tests (171 total).

### Fixed
- Unit-test gesture fixtures now use inert pipelines; the M4 startup-race
  fix had exposed that direct `_process_output` tests raced a live worker.

## [0.4.0] — 2026-07-07 — Milestone 4: Screen-Context Perception

### Added
- **Context perception module** (`perception/context/module.py`): samples
  the focused window and publishes `context.changed` on change — the
  gesture → intent chain is now hands-free (no `--context` needed).
- **Cross-platform window probes** (`perception/context/probe.py`):
  xdotool (Linux/X11), ctypes/win32 (Windows), osascript (macOS) behind a
  two-method interface; missing probes fail start loudly with install
  guidance and are isolated by the registry.
- **Rule-based classification**: ordered `context_perception.rules`
  (first case-insensitive substring match on title/process wins), pure
  function, injectable probes for tests.
- **Privacy default**: window titles/process names stay off the bus
  unless `publish_window_info: true`.
- **Demo** (`examples/hands_free_demo.py`): identical thumbs-up becomes
  `next_slide` / `like` / `accept_suggestion` as focus changes.
- `docs/REMAINING_WORK.md`: full gap analysis against the master
  specification with the proposed M5–M12 order.
- 14 new tests (141 total).

### Fixed
- Startup race in gesture and context poll loops: the poll thread could
  sample before the state machine reached RUNNING, silently dropping the
  first observation(s).

## [0.3.0] — 2026-07-07 — Milestone 3: Guarded Action Pipeline

### Added
- **Action dispatcher** (`automation/dispatcher.py`): consumes
  `intent.detected` and runs each bound action through binding lookup →
  param validation → permission policy → confirmation gate → executor with
  hard timeout. Bus handler only enqueues (perception latency untouched);
  bounded work queue with audited drops; per-status metrics; `action.requested`,
  `action.result` and `action.confirmation` events.
- **Permission system** (`security/permissions.py`): risk levels
  (safe/sensitive/dangerous), config-driven risk defaults + per-action
  overrides, deny-by-default for anything unknown, and a code-enforced
  floor: DANGEROUS actions can never be configured to run unattended.
- **Confirmation gates** (`security/confirmation.py`): replaceable
  providers — interactive console y/N with hard timeout, auto-deny for
  headless, scripted for tests/demos. All fail-closed (no TTY, no answer,
  no time → no action).
- **Audit log** (`security/audit.py`): thread-safe append-only JSONL with
  size-based rollover; every attempt (completed, denied, rejected, failed,
  timeout, dropped, unbound) recorded with intent **and** perception event
  ids — full provenance from effect back to gesture.
- **Built-in actions** (`automation/builtin.py`): `log_message`, `notify`
  (desktop notification with log fallback), `open_url` (http/https only),
  `open_application` (configured allow-list only; no arbitrary commands).
- **Configuration**: `security` and `automation` sections incl.
  `intent_bindings` (intent → action decoupling as config, not code);
  full fail-fast validation.
- **Demo** (`examples/intent_to_action_demo.py`): gesture → intent →
  guarded action across allow/confirm/deny/unbound, printing the audit trail.
- 41 new tests (127 total).

## [0.2.0] — 2026-07-07 — Milestone 2: Gesture Module Feature-Complete

### Added
- **Calibration**: per-gesture confidence floors
  (`gesture.gesture_thresholds`) and a gesture block-list
  (`gesture.disabled_gestures`) applied before edge detection — per-user
  recognition tuning with no library changes.
- **Custom gesture registration** (`perception/gesture/custom.py`):
  `gesture.custom_gesture_modules` imports dotted modules or `.py` files
  registering `GestureRule` classes at startup. Idempotent across
  pause/resume, fails soft (logged + surfaced in module metrics), and
  unknown display names auto-slugify into semantic ids. Shipped template:
  `examples/custom_gestures/three_count.py`.
- **User profiles** (`ProfilesConfig`): named overlays of the `gesture`
  and `intent` sections selected via `profiles.active` or `--profile`.
  Unknown profiles and out-of-section overrides are hard startup errors;
  overlays pass full validation.
- **Debug visualization** (`perception/gesture/debug_view.py`): optional
  live window with skeletons, boxes and gesture labels (reusing the
  GestureSense renderer) plus FPS/hand-count status line; `q`/Esc closes.
  Pre-flight display check refuses to start headless — the Qt GUI backend
  aborts the process on display-less `imshow`, so failing early is the only
  safe behaviour — and declines macOS (GUI must own the main thread).
- Module metrics now report custom-gesture load results and debug-window
  state; 19 new tests (86 total).

### Changed
- Semantic-id drift guard relaxed from set equality to subset so runtime
  custom registrations don't invalidate it.

## [0.1.0] — 2026-07-06 — Milestone 1: Multimodal Kernel

### Added
- **Event system** (`digital_twin/core/events.py`): immutable `Event`
  envelope (topic, source, frozen payload, timestamp, id) with reserved-key
  protection, JSON serialisation matching the platform contract, and
  hierarchical topic matching (exact / `prefix.*` / `*`).
- **Event bus** (`digital_twin/core/bus.py`): thread-safe pub/sub with a
  single dispatcher thread (ordered, lock-free callbacks), non-blocking
  publish, bounded queue with drop-oldest backpressure, per-subscriber
  fault isolation, slow-handler warnings, cascade-aware `flush()`, and
  delivery statistics.
- **Module contract** (`digital_twin/core/module.py`): `BaseModule`
  lifecycle state machine (CREATED/RUNNING/PAUSED/STOPPED/FAILED) with
  thread-safe transitions, idempotent stop/resume semantics, and
  exception-proof health reporting.
- **Module registry** (`digital_twin/core/registry.py`): ordered start,
  reverse-order stop, runtime enable/disable, failure isolation, and
  lifecycle announcements on `system.module`.
- **Typed configuration** (`digital_twin/configuration/settings.py`):
  frozen dataclasses, YAML overrides, unknown-key warnings, fail-fast
  validation; shipped `config/default_config.yaml` mirrors defaults
  (pinned by a test).
- **Gesture perception module**
  (`digital_twin/perception/gesture/`): GestureSense wrapped as a
  first-class plugin publishing semantic `perception.gesture` /
  `perception.hand` events — edge-triggered, optional held-gesture repeat,
  full camera/MediaPipe release on pause, injectable camera/tracker
  factories, stable snake_case gesture ids with aliases.
- **Intent engine** (`digital_twin/reasoning/intent.py`): context-aware
  gesture → intent mapping from configuration with `"*"` fallback, alias
  resolution, context switching via `context.changed` events, and
  provenance (`source_event`) on every intent.
- **Kernel entry point** (`main.py`) with `--config`, `--context`,
  `--no-gesture`; console event trace; graceful shutdown.
- **Demo** (`examples/gesture_to_intent_demo.py`): hardware-free proof of
  the perception → reasoning chain across contexts.
- **Vendored GestureSense** library (dual MediaPipe backend) and bundled
  hand-landmark model.
- **Tests**: 67 hardware-free tests covering the event contract, bus
  concurrency/backpressure/isolation, module lifecycle, registry fault
  isolation, configuration validation, gesture event derivation, the full
  fake-camera pipeline, and intent mapping.
- Documentation: `README.md`, `docs/architecture.md`, this changelog.
