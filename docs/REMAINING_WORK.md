# Remaining Work — Gap Analysis Against the Project Specification

**As of v0.17.0 (Milestone 17 complete) — 2026-07-16**

This document maps the Digital Twin AI Assistant master specification
against what is actually built, and proposes the milestone order for the
rest. It is the honest ledger: what exists is tested and shipped; what
doesn't is listed here, not hand-waved.

---

## 1. Delivered so far

| Milestone | Delivered | Version |
| --------- | --------- | ------- |
| M1 | Multimodal kernel: event bus (fault-isolated, backpressured), module lifecycle + registry, typed validated config, rotating logs, GestureSense as first perception plugin, context-aware intent engine | 0.1.0 |
| M2 | Gesture module feature-complete: calibration (per-gesture thresholds, disable lists), custom gesture registration from config, user profiles, debug visualization (display-safe) | 0.2.0 |
| M3 | Guarded action pipeline: dispatcher, permission policy (deny-by-default, dangerous-clamp), fail-closed confirmation gates, append-only audit log with perception→intent→action provenance, first safe actions | 0.3.0 |
| M4 | Screen-context perception: cross-platform active-window probes, ordered rule classification, automatic hands-free `context.changed`, privacy-default payloads | 0.4.0 |
| M5 | Desktop input actions: xdotool/pynput backends, risk-stratified keyboard/media/typing/clipboard/window-focus actions, prompt-free slide & media control, newline-free typing guarantee | 0.5.0 |
| M6 | Memory foundation: SQLite store with Fernet encryption-at-rest, episodic memory from action results, user-taught semantic facts, in-RAM working memory, ranked recall, retention pruning, full review/edit/delete CLI | 0.6.0 |
| M7 | Conversational reasoning: replaceable LLM backends (Gemini default free tier / Anthropic / Ollama offline), chat perception module, allow-listed intents through all dispatcher gates, memory-consuming prompts, persisted taught facts, explained replies | 0.7.0 |
| M8 | Planner: stage-based multi-step plans (config routines, LLM proposals, skill replays), every step individually gated with plan-correlated audit, progress/cancel/timeout, failure policies, skill memory producer with re-gated replays | 0.8.0 |
| M9 | Voice: offline Vosk STT behind a replaceable interface, push-to-talk with session-scoped microphone (privacy structural), gesture/event/API listening triggers, live partials, barge-in interruption, spoken replies via the gated audited `speak` action | 0.9.0 |
| M10 | Screen OCR + file-system intelligence: on-demand-only screen capture behind the gated SENSITIVE `read_screen` action (subprocess capture backends + local tesseract, screenshot deleted post-OCR, counts-only audit), reasoner consumes `perception.screen`; contained file actions under `files.allowed_roots` (symlink-resolved, empty default) — 7 overwrite-refusing SENSITIVE actions + the first two DANGEROUS actions (`move_file`, trash-based `delete_file`) finally exercising the M3 confirm-clamp | 0.10.0 |
| M11 | Plugins/secrets/browser: manifest-contract plugin loader (declared actions only, risk floors, namespacing, two-phase commit, per-plugin fault isolation), secrets manager (keyring or Fernet file, values by name only, CLI), browser automation behind a replaceable Playwright driver (SENSITIVE navigation/extraction, DANGEROUS clamped fill/click, allow-list-mandatory `browser_fill_secret` login primitive); reasoner action catalog made late-bound | 0.11.0 |
| M12 | Dashboard/packaging/performance: stdlib loopback web dashboard (live status, event feed, browser chat) with token-guarded endpoints and the `web` confirmation provider (Approve/Deny in the page, fail-closed — resolves the console stdin conflict); `pyproject.toml` packaging (lean core + extras, three console scripts); GitHub Actions CI (tests + demos + sweeps on 3.10/3.12 without heavy extras); benchmark: ~62.5k events/s bus, 0.32 ms median gate pipeline | 0.12.0 |
| M13 | Knowledge engine + connectors: local RAG (paragraph-first chunking, replaceable dependency-free hashing embedder, SQLite vector store with idempotent ingestion and an embedder pin, cosine recall injected into the reasoner prompt with cite-or-say-so), five gated knowledge actions confined to `files.allowed_roots`; three connector plugins on the M11 system (offline `.ics` calendar, local tasks, headers-only read-only IMAP email with password by secret name via the new `api.secret()`) | 0.13.0 |
| M14 | Hardening & distribution: subprocess plugin isolation (`isolation: subprocess` — child process + JSON/stdio bridge, proxy actions with manifest floor re-applied, crash/hang/desync contained, no secret or module access; fault+capability isolation, not an OS sandbox), LLM keys resolved from the secret store ahead of the environment, execution-time host re-check on `browser_fill`/`browser_click`, semantic embedder extra (sentence-transformers), wheel-safe asset resolution (`DIGITAL_TWIN_HOME`), user guide | 0.14.0 |
| M15 | Dashboard depth + document ingestion: multi-format extraction (txt/md, dependency-free docx + html, pdf via pdftotext/pypdf), folder watching (`knowledge.watch_paths`, containment-checked, idempotent, edited files replace stale versions), dashboard memory + knowledge panels and an `/api/stream` SSE endpoint | 0.15.0 |
| M16 | Write-side connectors + wake word + installers: `email.send_email` and `calendar.create_event` (DANGEROUS — clamp-confirmed with full content shown, recipient-domain allow-list refusing mis-sends pre-gate, credentials by secret name); always-on wake-word detector (`voice.wake_word`, trigger-only, never publishes utterances); `digital-twin-setup` installer creating a `DIGITAL_TWIN_HOME` | 0.16.0 |
| M17 | UI completion: gesture debug view streamed to the browser via a `FrameHub` + `/api/frames` MJPEG endpoint (headless `DebugView`, no OpenCV window needed); push-driven page (`EventSource` on `/api/stream`) with live mic/wake/camera indicators; plugin panel (`/api/plugins`) and read-only settings panel (`/api/settings`) | 0.17.0 |
**426 tests, all hardware-free. Working end to end today:** three input
modalities (camera gestures, typed chat, spoken utterances) plus
on-demand screen reading → context-aware reasoning with memory recall
*and* document RAG in the prompt → single actions and multi-step plans
through per-step gates — file operations, browser interaction,
secret-backed login fills, calendar/tasks/email connectors — → audit →
memory, approvable from a token-guarded web dashboard, with spoken
replies that are gated and interruptible, third-party plugins extending
the catalog under an enforced capability contract, and the whole thing
packaged (`pip install .`), CI-tested and benchmarked.

---

## 2. Remaining work, by specification area

### 2.1 Conversational interaction & chat interface — MOSTLY DONE (M7)
Have: replaceable LLM abstraction (Gemini default/Anthropic/Ollama), chat perception
module, multi-turn history, memory-grounded prompts, explained replies,
gated intent triggering. Missing: a chat *UI* beyond the terminal
(dashboard milestone), streaming responses, and knowledge retrieval over
documents (RAG — knowledge-engine milestone).

### 2.2 Voice system — MOSTLY DONE (M9+M16)
Have: push-to-talk (session-scoped mic) and continuous modes, offline
Vosk STT behind a replaceable interface, live partials, barge-in
interruption, gesture/event/API listening triggers, spoken replies via
the gated `speak` action, and an always-on **wake-word detector** (M16:
`voice.wake_word`, trigger-only, never publishes utterances).
Missing: noise filtering, a dedicated keyword-spotting model (matching
is lexical substring today), and a Whisper backend as a drop-in.

### 2.3 Screen understanding beyond the active window — MOSTLY DONE (M4+M10)
Have: focused window title/process → context (M4); on-demand screenshot
capture + local tesseract OCR behind the gated `read_screen` action, with
structural privacy (no polling, screenshot deleted post-OCR, counts-only
audit) and reasoner prompt injection — "summarize what's on screen"
works one capture behind the question (M10).
Missing: UI element/button detection and layout understanding (needs a
vision model or accessibility APIs, not plain OCR), synchronous
ask-capture-answer in one chat turn, and region/window-scoped capture.

### 2.4 Reasoning engine — MOSTLY DONE (M7)
Have: deterministic gesture→intent tables (fast path) plus LLM-backed
interpretation of free-form text with context retrieval from memory and
screen state, response generation, per-reply reasoning strings, and the
replaceable-model interface. Missing: multi-step task planning (the
planner milestone) and automatic semantic extraction beyond explicit
`remember` decisions.

### 2.5 Planner — MOSTLY DONE (M8)
Have: stage-based decomposition (sequential + parallel-stage semantics),
per-step gating through the full dispatcher pipeline with plan-correlated
audit, progress events, cancellation, stage timeouts, failure policies,
LLM-proposed plans from the action catalog, and skill replay.
Missing: true parallel *execution* (dispatcher has one worker — semantics
are ready), conditional/branching steps and step-output chaining
(outputs feeding later params), and LLM-side re-planning on failure.

### 2.6 Memory system — MOSTLY DONE (M6)
Have: SQLite storage with encryption-at-rest, episodic (auto, from action
results), semantic (user-taught, never auto-pruned) and working (in-RAM,
privacy-wiped) memory; ranked lexical search with recency decay and
access stats; retention pruning; full review/edit/delete/export CLI.
Missing: **summarization/consolidation** of old episodic memories,
embedding-based relevance (knowledge-engine milestone), and richer
automatic semantic extraction. Skill memory shipped with the planner
(M8); reasoning consumes memory since M7.

### 2.7 Desktop automation — MOSTLY DONE (M5)
Have: notifications, URL/app launch (allow-listed), keyboard shortcuts,
media keys, typing (newline-free by design), clipboard write, window
focus — all risk-stratified behind permissions/confirmation/audit.
Missing: mouse control and drag-and-drop (risk analysis pending), window
management beyond focus (move/resize/close), clipboard *read* (a
perception concern), file operations and downloads management (fold into
2.9 as DANGEROUS/SENSITIVE actions).

### 2.8 Browser automation — MOSTLY DONE (M11)
Have: replaceable driver (Playwright/Chromium reference, lazy with
guidance), gated `browser_open`/`browser_extract_text` (SENSITIVE,
http(s)-only, optional domain allow-list), `browser_fill`/`browser_click`
(DANGEROUS, clamped), and `browser_fill_secret` — login flows without
insecure password storage (secret by name, allow-listed hosts only).
Missing: tab management (one shared page today), downloads management,
structured extraction/search helpers beyond visible text, and page
content ingestion richer than the 300-char result detail (knowledge
engine).

### 2.9 File system intelligence — MOSTLY DONE (M10)
Have: allowed-roots containment (symlink-resolved, validated pre-gate and
re-checked in handlers, empty-by-default allow-list), `list_files`,
`search_files`, `read_text_file` (bounded), `find_duplicates`
(size + SHA-256), `create_directory`, `write_text_file` /`copy_file`
(overwrite-impossible), DANGEROUS `move_file` and trash-based
`delete_file` — the confirm-clamp's first real workload. Batch = planner
steps, each individually gated.
Missing: PDF/document summarization (belongs with the knowledge engine's
ingestion), content-based categorization/auto-organize proposals,
downloads-folder watching, and richer results than the 300-char
`action.result` detail field (a `files.listing`-style event or the
dashboard).

### 2.10 Calendar, tasks, email, knowledge engine (RAG) — MOSTLY DONE (M13)
Have: local RAG — gated ingestion (file ingestion contained to
`files.allowed_roots`), paragraph chunking with overlap, replaceable
embedder (dependency-free hashing reference, vector-space pin), ranked
recall injected into every reasoner prompt with cite-or-say-so;
connector plugins for calendar (.ics), tasks (local JSON) and email
(IMAP headers-only, read-only, secrets by name).
Have also (M15): multi-format extraction (dependency-free docx + html,
pdf via pdftotext/pypdf, text/source) and folder watching that
auto-ingests new/edited files under `files.allowed_roots` (idempotent;
an edited file replaces its stale chunks).
Have also (M16): write-side connector actions — `email.send_email` and
`calendar.create_event` — DANGEROUS-classified, clamp-confirmed with
full content shown, recipient-domain allow-list, credentials by secret
name.
Missing: OCR of scanned PDFs (would reuse the M10 screen-OCR path),
recurrence (RRULE) expansion for calendars, richer outbound mail (HTML,
attachments, OAuth2 — each its own risk pass), calendar invites with
attendees, and unifying memory recall onto the knowledge embedder.
(Semantic embedder shipped as the `[semantic]` extra in M14.)

### 2.11 Security — MOSTLY DONE (M3+M6+M11+M14)
Have: permission system with deny-by-default and the code-enforced
dangerous-confirm floor; fail-closed confirmation gates; append-only audit
with full provenance; least-privilege allow-lists; no arbitrary-command
surface; encrypted memory at rest (M6); secrets manager (M11) with LLM
keys now resolved from it (M14); per-plugin capability scoping via the
manifest contract (M11) plus **subprocess isolation** for untrusted
plugins (M14: child process, no secret/bus/memory access, crash/hang
contained).
Missing: OS-level sandboxing of the child (seccomp/containers) — today's
isolation is fault + capability, and a sandboxed child is still a user
process; network egress policy per plugin; audit-log signing/rotation.

### 2.12 Plugin system — MOSTLY DONE (M11)
Have: `plugin.yaml` manifest format with declared-capability enforcement
(undeclared actions refused, risk floors, namespacing), discovery from
`plugins.paths`, versioned manifests, third-party modules/actions
registering without editing `main.py`, two-phase commit and per-plugin
fault isolation.
Missing: OS-level sandboxing (see 2.11 — subprocess isolation shipped in
M14; seccomp/containers remain), dependency declarations / packaging
beyond a directory, plugin-scoped configuration reload, and a
signed-plugin / review story.

### 2.13 User interface — DONE (M12+M15+M17)
Have: stdlib loopback web dashboard — live module states (with detail and
metrics), bus counters, real-time event feed (bounded), browser chat on
the same topic as console chat, and **web confirmations**
(`security.confirmation: web`): Approve/Deny in the page, fail-closed,
token-guarded against cross-site request forgery.
Have also (M15): memory and knowledge panels, and an `/api/stream` SSE
endpoint.
Have also (M17): a push-driven page (`EventSource`), live mic/wake/camera
indicators, a plugin panel (health, sandbox, actions, errors), a
read-only settings panel (non-defaults flagged), and the gesture debug
view streamed to the browser (`/api/frames` MJPEG, headless — no OpenCV
window needed).
Missing (enhancements only): a live settings *editor* (config is frozen
at startup by design — edit YAML + restart), themes/shortcuts, and a
notifications surface.

### 2.14 Performance — MEASURED (M12)
Have: benchmark harness (`examples/benchmark.py`) with recorded numbers —
bus ≈ 62,500 events/s (perception needs tens/s), full gate pipeline
0.32 ms median / 0.68 ms p95 per action, late-bound catalog 0.13 µs per
message. Architecture headroom is proven, not assumed.
Missing: gesture→action end-to-end latency with a live camera (needs
hardware), startup/memory budgets, and profiling under a real LLM +
browser workload.

### 2.15 Packaging, CI, API docs — MOSTLY DONE (M12+M14+M16)
Have: `pyproject.toml` (lean core + capability extras, four console
scripts, package version), GitHub Actions CI on 3.10/3.12 without heavy
extras, wheel-safe asset resolution + `DIGITAL_TWIN_HOME` (M14), a
`digital-twin-setup` installer that makes a wheel install runnable (M16),
and a user guide (M14).
Missing: pinned lockfile, a published PyPI release, Sphinx/pdoc API docs,
and native platform installers/bundles (`.app`/`.msi`).

### 2.16 Known smaller debts (tracked in docs/architecture.md)
- Runtime profile switching (needs config-reload + module rebuild).
- Wayland active-window support (compositor-restricted by design).
- Debug window: macOS declined; Linux renders off-main-thread.
- Two same-handedness hands collapse to one presence key.
- Single action worker: one pending confirmation queues later actions.
- Timed-out action threads cannot be killed (reported, abandoned on stop).
- Custom gestures have no margin-sweep tooling against built-ins yet.
- `read_screen` is asynchronous relative to a chat turn: the capture it
  triggers informs the *next* reply, not the current one.
- File-action results ride the 300-char `action.result` detail field —
  previews, not ingestion (knowledge engine will own document content).
- ~~Voice's `speak` registers after the reasoner snapshots the action
  catalog~~ — fixed in M11 (late-bound catalog).
- Plugins are in-process: the manifest contract cannot stop malicious
  code; subprocess isolation is the real sandbox.
- ~~LLM API keys come from environment variables, not the secret store~~
  — fixed in M14 (`llm.api_key_secret`, store ahead of env).
- One browser page per session (no tabs). (Interaction host re-check
  closed in M14 for all `browser_fill`/`browser_click`.)
- Dashboard polls at 1 s (no SSE/WebSocket) and speaks plain HTTP on
  loopback; `allow_remote` without a TLS proxy is on the operator.
- The hashing embedder is lexical (no paraphrase matching); the store's
  embedder pin makes swapping in a semantic backend a clean re-ingest.
- Email connector: headers-only and read-only by design; no pagination.
- Calendar connector does not expand RRULE recurrences.
- The gesture debug view is still an OpenCV window, not a dashboard
  panel.

---

## 3. Proposed milestone order (with rationale)

| # | Milestone | Why this order |
| - | --------- | -------------- |
| M17 | **UI completion**: plugin manager, settings editor, live camera/voice indicators, gesture-view frame streaming, fully streaming page | Finishes the 2.13 gaps once the dashboard's data endpoints (M12/M15) are all in place |

(M8–M16 rows moved to §1 as shipped.) The original product spec is now
fully covered; M17 is UI polish, and beyond it the ledger holds only
enhancements on finished subsystems (OCR'd PDFs, RRULE calendars, richer
outbound mail, a keyword-spotting model, PyPI/native installers, OS-level
plugin sandboxing).

(M8–M14 rows moved to §1 as shipped.) The platform is now
feature-complete and hardened; everything above is extension on finished,
tested subsystems rather than new core surface.

---

*This document is maintained per milestone: items move from §2 to §1 as
they ship, and new debts discovered during builds are added to §2.16
rather than forgotten.*
