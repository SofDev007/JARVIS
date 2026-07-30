# Architecture — Digital Twin AI Assistant

Milestone 1: the multimodal kernel. This document records the design and
its rationale so future modules are built against the same contracts.

## 1. Design principles

1. **Event-bus-only coupling.** Modules never import each other; they share
   only `digital_twin/core/events.py`. A gesture module, a voice module and
   a screen module are indistinguishable to the reasoning layer.
2. **Perception publishes facts, reasoning assigns meaning.** A perception
   payload may say `thumbs_up @ 0.97, right hand` — never `next_slide`.
   Application behaviour is therefore impossible to hardcode in perception
   by construction.
3. **Graceful degradation.** A failing module is marked `FAILED`, announced
   on the bus, and the rest of the assistant keeps running. A missing
   camera puts capture into a reconnect loop rather than crashing.
4. **Everything observable is an event.** Module lifecycle, context
   changes, intents — one uniform stream that logs, dashboards and future
   watchdogs consume identically.
5. **No hardcoded values.** Every tunable lives in typed, validated
   configuration.

## 2. Event bus (`core/bus.py`)

| Property        | Choice                                | Why |
| --------------- | ------------------------------------- | --- |
| Delivery thread | single dedicated dispatcher           | callbacks of one bus never run concurrently → subscribers need no locks; strict event ordering |
| Publishing      | non-blocking, from any thread         | perception publishes from capture/inference threads |
| Backpressure    | bounded queue, **drop-oldest**        | perception data ages badly; recency wins; drops are counted, never silent |
| Failures        | per-callback try/except + error count | one bad subscriber cannot poison the bus |
| Diagnostics     | slow-handler warnings (rate-limited), `BusStats` | makes the single-dispatcher trade-off visible instead of mysterious |
| Shutdown        | `flush()` waits for cascades, sentinel wakes dispatcher | handlers may publish follow-up events; shutdown drains them |

`flush(timeout)` tracks *outstanding* events (published − dispatched), so a
gesture event that triggers an intent event is fully settled before flush
returns — this is what makes the threaded tests deterministic.

## 3. Module lifecycle (`core/module.py`, `core/registry.py`)

```
CREATED ──start(bus)──▶ STARTING ──▶ RUNNING ──pause──▶ PAUSED
   ▲                                  │  ▲                │
   │                                  │  └────resume──────┘
   └───────── STOPPED ◀──stop─────────┘        any hook raising → FAILED
```

* `stop` is idempotent; `resume` on RUNNING is a no-op (enable-twice is not
  an error); everything else invalid raises `InvalidStateError`.
* Hook failures capture detail, set `FAILED`, and re-raise; the registry
  catches, logs, announces on `system.module`, and continues with the next
  module (fault isolation).
* `status()` returns state + subclass metrics and is exception-proof —
  health reporting must never crash.
* Registry starts in registration order, stops in reverse (dependencies
  released last-in-first-out).

## 4. Topic catalog

| Topic                | Source          | Payload schema |
| -------------------- | --------------- | -------------- |
| `perception.gesture` | `gesture`       | `gesture: str` (semantic id), `confidence: float`, `hand: "left"\|"right"`, `repeat: bool` |
| `perception.hand`    | `gesture`       | `hand: str`, `present: bool` |
| `context.changed`    | any             | `context: str` |
| `intent.detected`    | `intent`        | `intent: str`, `context: str`, `gesture: str`, `hand`, `confidence`, `repeat`, `source_event: str` (provenance) |
| `action.requested`   | — reserved (M2) | `action`, `params`, permission metadata |
| `system.module`      | `registry`      | `name: str`, `action: str`, `state: str`, `detail: str` |

Envelope keys (`event_id`, `timestamp`, `module`, `type`) are reserved and
cannot be shadowed by payloads (enforced at `Event` construction).

## 5. Gesture perception module

```
ThreadedCamera ─frames─▶ InferenceWorker ─┐ (GestureSense: tracker →
                                          │  smoother → gesture engine)
                    poll thread ◀─latest──┘
                        │  _process_output(): diff vs previous state
                        ▼
        perception.hand (presence edges)   perception.gesture (gesture edges)
```

* **Edge-triggered**: a held gesture publishes once; a gap in stability
  re-arms the edge; `repeat_interval_s > 0` re-publishes held gestures with
  `repeat: true` for hold-to-repeat use cases.
* **Deterministic core**: `_process_output(output, now)` is pure event
  derivation, unit-tested directly without threads; the poll thread is a
  thin loop around it.
* **Injectable hardware**: camera and tracker are constructor factories —
  the integration test runs the real GestureSense `InferenceWorker` against
  a fake camera and scripted tracker, no hardware or MediaPipe needed.
* **Pause = privacy**: `_on_pause` tears down camera + MediaPipe graph
  entirely; `resume` rebuilds from factories.
* **Semantic layer**: display names ("Peace / Victory") never leave the
  module; stable ids (`peace`) do. A test pins the mapping to the library's
  registered gesture list so drift fails CI.

### 5.1 Calibration, custom gestures, profiles, debug view (M2)

* **Calibration** is a perception-side post-filter: per-gesture confidence
  floors (`gesture_thresholds`) and a block-list (`disabled_gestures`),
  applied in `_process_output` *before* edge detection, so a sub-threshold
  detection behaves exactly like an unstable frame (and re-arms the edge).
  Library internals stay untouched.
* **Custom gestures** load from config (`custom_gesture_modules`: dotted
  paths or `.py` files) before the engine builds its rule set. Loading is
  idempotent across pause/resume and fails soft: broken specs are logged
  and surfaced in module metrics, never fatal. Unknown display names get
  deterministic slug ids, so custom gestures work in thresholds, profiles
  and intent mappings with zero core changes.
* **Profiles** are config-time overlays restricted to the `gesture` and
  `intent` sections, selected via `profiles.active` or `--profile`.
  Unknown profiles are a hard startup error (running with someone else's
  tuning silently would be worse than not starting); overlays pass full
  validation.
* **Debug view** is an optional render thread reusing the GestureSense
  renderer. Pre-flight display check before any GUI call — the Qt backend
  *aborts the process* on headless `imshow`, so try/except is not a
  defence; the view refuses to start instead (and macOS is declined
  outright: OpenCV GUI must own the main thread there).

## 6. Intent engine

Configuration-driven `context → gesture → intent` lookup with a `"*"`
wildcard fallback, alias resolution (`high_five` → `open_palm`), thread-safe
context switched by `context.changed` events or `set_context()`. Emits
`intent.detected` carrying full provenance (`source_event` id) for the
future audit log. Malformed events are logged and dropped, never raised.

## 7. Action pipeline and security model (M3)

```
intent.detected ─▶ [queue] ─▶ binding lookup ─▶ param validation
   (bus thread:      (worker thread)                │
    enqueue only)                                   ▼
                                            permission policy
                                    DENY ◀──────────┼─────────▶ ALLOW
                                      │      CONFIRM▼                │
                                      │     confirmation gate        │
                                      │      (fail-closed)           │
                                      ▼             ▼                ▼
                                   audit ◀── execute (pool, hard timeout)
                                                    │
                                        action.result + audit entry
```

Invariants (enforced in code, covered by tests):

1. Unknown action / missing rule / invalid rule string → **deny**.
2. Unanswered, timed-out or TTY-less confirmations → **deny**.
3. `DANGEROUS` + configured `allow` → clamped to **confirm** with a warning.
4. `open_application` executes only allow-listed commands; no arbitrary
   command action exists.
5. Params are validated *before* the gate — users never confirm garbage.
6. The bus subscription only enqueues; confirmation prompts and execution
   run on the dispatcher's worker so perception latency is untouched.
7. Every terminal state (and every drop, and every unbound intent) is an
   audit entry carrying `intent_event` and `perception_event` ids.

The audit log is append-only JSONL with size-based rollover to timestamped
files; audit failures are loud in the normal logs but never stop the
pipeline.

## 8. Screen-context perception (M4)

One replaceable probe per platform (xdotool / win32 ctypes / osascript)
behind a two-method interface; the module polls, classifies via ordered
substring rules (pure function, heavily unit-tested) and publishes
``context.changed`` edge-triggered. Privacy default: window titles and
process names never reach the bus unless ``publish_window_info: true``.
A missing probe fails start *loudly* with install guidance; the registry
isolates the failure and ``--context`` remains as the manual fallback.
Wayland is unsupported (compositors hide the active window by design) —
tracked in REMAINING_WORK.

## 9. Input synthesis (M5)

Backends (xdotool / pynput) sit behind a five-method interface speaking a
canonical key vocabulary; validation rejects anything outside it before
the permission gate, so there is no raw-keysym injection path. Risk is
stratified per action, not per parameter: provably-benign subsets get
their own SAFE actions (``nav_key`` = arrows only, ``media_key``) so
frequent flows run prompt-free, while the general-purpose actions stay
SENSITIVE. ``type_text`` rejects newlines — synthesised text can never
execute itself in a focused terminal; Enter is a separate gated action.
Backends resolve lazily on first use: kernels start on machines with no
input stack, and the actions fail there as audited results carrying
install guidance.

## 10. Memory system (M6)

One SQLite table for all kinds; content passes through a codec
(``plaintext`` or ``fernet``) while metadata — kind, source, importance,
tags, timestamps, access stats — stays queryable. Trade-off is explicit:
search never decrypts, so nothing sensitive should be smuggled into
summaries/tags. Ranking = term hits weighted by importance and a
recency half-life, with access counts bumped on retrieval (groundwork for
"frequently recalled = important"). The module is a normal bus citizen:
episodic records are derived from ``action.result`` (status-weighted
importance, full payload as structured data, provenance ids included),
working memory buffers recent ``intent``/``context``/``result`` activity
in RAM only, and pause wipes it. Semantic facts enter only through the
user (``remember_fact`` / CLI) and are never auto-pruned; episodic memory
prunes by retention age and a record cap. Every stored memory is
announced on ``memory.stored`` for the future dashboard timeline.

## 11. Conversational reasoning (M7)

``perception.chat`` (console or programmatic) → bounded queue → worker →
LLM → structured decision ``{reply, intent, remember, reasoning}`` parsed
forgivingly (fenced/embedded JSON accepted; garbage degrades to a plain
reply, never an exception).

Security invariants:

1. **No direct action access.** The model nominates intents from the
   configured allow-list only; unknown nominations are dropped and
   counted. Accepted ones are ordinary ``intent.detected`` events — the
   dispatcher cannot tell chat from a gesture, so every gate applies.
2. **Keys from the environment only** (configurable variable name),
   never from config files, never logged.
3. **Slow work off the bus**: the handler enqueues; model latency lives
   on the reasoner's worker thread.
4. **Failures degrade**: LLM errors become apologetic responses; a
   missing key fails the module at start with guidance while the rest of
   the assistant runs.

Memory integration is pull-in/push-out: ranked recall (M6 search over the
user's words) is injected into the system prompt; ``remember`` facts
persist through the memory module's own API and remain fully
user-controllable. The kernel wires this by injecting the memory module
into the reasoner — composition in ``main.py``, not cross-module imports;
the bus stays the only push channel.

## 12. Planner (M8)

```
plan.request ──▶ planner worker ──▶ stage N steps as action.execute ──▶ dispatcher
   (config /        (owns all           {action, params,                (FULL gate
    llm / skill)     run state)          plan_id, step}                  pipeline per
                        ▲                                                step)
                        └────────── action.result {plan_id, step} ◀──────────┘
```

Semantics: a stage's steps have no mutual ordering (they parallelise when
the executor has capacity — today's dispatcher serialises with one
worker, and plan definitions won't change when that grows); stages are
strict barriers. Failure policy is per-plan (``abort`` default,
``continue`` collects errors); stages carry a timeout; ``plan.cancel``
stops between steps (an in-flight action cannot be killed — dispatcher
limitation, tracked). Config plans parse at *startup*, so a broken
routine fails config load, not the moment it's needed.

Trust model: ``action.execute`` names an action directly, but names buy
nothing — the dispatcher applies the identical registry/validation/
permission/confirmation/audit pipeline as the intent path, and every
audit row carries ``plan_id``/``step``. LLM-proposed plans add no
privilege either: the model sees the action catalog (names, risk,
descriptions) and proposes sequential steps; acceptance is a config
switch, and a SENSITIVE step inside a plan prompts exactly like one
outside it. **Skill replays are re-gated — approvals never persist.**

Skill memory gains its first producer: successful LLM plans are stored
(kind ``skill``) through the memory module and replayed by text query.

## 13. Voice (M9)

```
mic (session-scoped) ─▶ AudioSource.read ─▶ Transcriber.feed ─▶ partials ─▶ perception.voice.partial
        ▲                                        │                              │ (barge-in: stop TTS)
   listen triggers                               └─ finals ──▶ perception.voice ─▶ reasoner (same path as chat)
   (voice.control /                                                                   │
    listen_intents /                                                                  ▼
    API)                                chat.response ─▶ action.execute {speak} ─▶ dispatcher gates ─▶ TTS
```

Privacy is structural: in push-to-talk mode the microphone is opened when
a session starts and closed when it ends (one utterance, an explicit
stop, or the ``max_utterance_s`` deadline) — the device is untouched
between sessions, and pause tears everything down. Recognition is
offline (Vosk) so raw audio never leaves the machine; the transcriber is
an interface, so a Whisper backend is a drop-in.

Spoken output is a normal SAFE action (``speak``): audited, deniable
(one permission rule mutes the assistant), usable in plans. The
synthesizer is shared between the action and the voice module by kernel
composition, which is what makes **barge-in** work — a partial transcript
while speech is playing terminates the utterance immediately.

The zero-core-change claim, kept: this milestone added topic constants,
one additive subscription in the reasoner (voice events reuse the chat
handler), and kernel wiring. ``core/`` is untouched.

## 14. Screen reading and file-system intelligence (M10)

```
action.execute {read_screen} ─▶ dispatcher gates (SENSITIVE: confirm) ─▶ ScreenReadingModule.read_now()
                                                                             │ capture → OCR → delete shot
                                     reasoner prompt ◀── perception.screen ◀─┘ {text ≤ max_chars, counts}
                                     (fresh only; 600s TTL)      audit/result: character counts, never text
```

**Screen reading is deliberately not a sensor.** Window titles (§8) are
one line; the full screen is passwords and private messages — the most
sensitive perception surface the assistant has. So there is no poll loop
and no ungated code path to a screenshot: the *only* caller of
`read_now()` is the `read_screen` action (SENSITIVE — a human approves
each capture unless explicitly allowed; one rule removes the capability).
The screenshot lives in a private temp dir and is deleted in a `finally`
the moment OCR returns; audit rows and action results carry character
counts only. Backends follow the §8/§9 patterns exactly: subprocess
capture (`scrot`/`import`/`gnome-screenshot`/`screencapture`/PowerShell)
and subprocess `tesseract` behind tiny ABCs, resolved lazily on first use
so the kernel starts on machines without them. The reasoner gained one
additive subscription (`perception.screen`) and injects the latest text
into its prompt while fresh (10-minute TTL) — the same
near-zero-core-change discipline as M9 (`core/` untouched again).

**File actions are the first DANGEROUS-class actions** — the M3 clamp
(DANGEROUS + configured `allow` → CONFIRM in code) finally has real work.
Three independent safety layers:

1. *Containment*: every path parameter must resolve — after symlink
   resolution — inside a configured `files.allowed_roots` directory.
   The default allow-list is empty (the `open_application` precedent).
   Enforced at validation (before the gate: nobody confirms garbage) and
   re-enforced inside every handler.
2. *Risk stratification*: `list_files`, `search_files`, `read_text_file`,
   `find_duplicates`, `create_directory`, `write_text_file`, `copy_file`
   are SENSITIVE; the write-side ones **refuse to overwrite anything**
   (`write_text_file` uses `open('x')` so overwrite is impossible even if
   validation went stale). `move_file` and `delete_file` relocate existing
   user data — DANGEROUS.
3. *Recoverability*: `delete_file` never unlinks — it moves the file into
   a per-root trash directory (`files.trash_dir_name`, hidden from
   listings/search/duplicate scans) with a timestamped name. A confirmed
   mistake is an inconvenience, not a loss; deleting *from* the trash is
   refused outright.

Batch operations come free: the M8 planner runs sequences of these as
plan steps, each individually gated — the spec's "batch operations with
confirmation" without new machinery.

## 15. Plugins, secrets and the browser (M11)

```
plugins.paths ─▶ discover ─▶ plugin.yaml (the CONTRACT) ─▶ import entry ─▶ register(api)
                                 │ declared actions+risks        │ two-phase: staged, committed
                                 ▼                               ▼ only after a clean return
                        undeclared → refused          <plugin>.<action> into the SAME
                        declared risk = floor         dispatcher gate pipeline as built-ins
```

**Plugins.** Loading is explicit trust (only `plugins.paths` is scanned;
default empty) and the manifest is enforced, not advisory: every action a
plugin registers must be declared with a risk level, the declaration is a
floor (registering *safer* than declared is refused upward), everything
is namespaced `<plugin>.<name>` so built-ins can never be shadowed, and
registration is two-phase — a plugin that fails halfway through
`register()` contributes nothing at all. Failures disable one plugin,
never the kernel (M1 fault isolation applied to load time). Stated
honestly: in-process Python cannot be truly sandboxed — a malicious
import can do anything the process can. This is least-privilege scoping
for honest plugins plus a reviewable capability contract; OS-level
isolation (subprocess plugins over an IPC bridge) remains open.

**Secrets.** One rule: values by name, never by value. Actions, events,
plans, prompts, results and audit rows carry the secret's *name*; the
value is resolved inside a handler at the last moment and immediately
forgotten. Backends behind one interface: the OS keyring (optional
`keyring` package; a names-only index file makes `names()` work since
keyrings can't enumerate) or a Fernet-encrypted file with a `0600` key —
no silent downgrade, same policy as memory encryption. The CLI
(`python -m digital_twin.security.secrets_cli`) never accepts values on
argv (process lists are world-readable) and hides them on `get` unless
`--reveal`.

**Browser.** A replaceable driver (Playwright/Chromium reference, lazy
import with install guidance; scripted double for tests) behind gated
actions: `browser_open`/`browser_extract_text` SENSITIVE with
http(s)-only and optional `allowed_domains` restriction;
`browser_fill`/`browser_click` DANGEROUS (clicking is how purchases
happen — the M3 clamp guarantees a human per click);
`browser_fill_secret` DANGEROUS *plus* two hard rules — it refuses to
exist without a non-empty `allowed_domains`, and refuses unless the
current page's host is on that list, so a confirmation dialog is never
the only thing between a secret and a phishing page. Login flows without
insecure password storage: the parameter is a secret *name*.

The reasoner's action catalog became **late-bound** this milestone
(a callable evaluated per message), closing the M9 debt where `speak` —
and now plugin/browser actions — registered after the prompt snapshot.

## 16. Dashboard, packaging and performance (M12)

```
browser page ── GET /api/status ──▶ module states + bus counters
   │  1s poll ── GET /api/events ──▶ ring buffer (dashboard subscribes)
   │           ── GET /api/confirmations ─▶ WebConfirmation.pending()
   └─ POST /api/chat, /api/confirmations/<id>   [X-Dashboard-Token]
                     │                                │
                     ▼                                ▼
             perception.chat            dispatcher's blocked request()
             (same topic as console)    unblocks → gates proceed
```

**The dashboard displays; it does not decide.** It is a normal module
(start/stop/pause via the registry) owning a stdlib `ThreadingHTTPServer`
— zero new dependencies. Chat typed into the page publishes
`perception.chat`, indistinguishable from console chat to the reasoner.
The **web confirmation provider** finally resolves the tracked stdin
conflict: with `security.confirmation: web`, the dispatcher's blocking
`request()` becomes a pending entry the page polls and answers over HTTP
— same fail-closed rules (timeout → deny, no dashboard → deny, anything
but explicit approval → deny), same clamp, same audit. Security posture:
loopback binding enforced by config validation (`allow_remote` is an
explicit opt-out), and every state-changing endpoint requires a
per-session random token embedded only in the served page — a malicious
website can *fire* a cross-origin POST at localhost but cannot read the
token, so it cannot approve actions (CSRF containment).

**Packaging** (`pyproject.toml`): the core installs with PyYAML alone —
every heavy capability is an extra (`[gesture]`, `[encryption]`,
`[browser]`, `[keyring]`, `[voice]`), matching the runtime design where
backends resolve lazily. Console scripts: `digital-twin`,
`digital-twin-memory`, `digital-twin-secrets`. **CI**
(`.github/workflows/ci.yml`) runs the suite, every demo and a syntax
sweep on Python 3.10 and 3.12 *without* the heavy extras — proving the
lazy-backend claim on every push.

**Performance** (`examples/benchmark.py`, this machine, Python 3.12):
bus throughput ≈ **62,500 events/s** (perception produces tens/s — two
orders of magnitude of headroom); full gate pipeline (validate →
permission → execute → audit → result) ≈ **0.32 ms median / 0.68 ms
p95** per SAFE action — the safety machinery costs milliseconds to
protect decisions that take humans seconds; M11's late-bound catalog ≈
**0.13 µs** per message — free.

## 17. Knowledge engine and connectors (M13)

```
ingest_document/ingest_text ─▶ gates ─▶ chunk (¶-first, overlap) ─▶ embed ─▶ SQLite
                                                                              │
user message ─▶ reasoner ─▶ store.search (cosine, top-k, floor) ◀─────────────┘
                    └─▶ {knowledge} prompt section (bounded, cite-or-say-so)
```

**RAG, local and honest.** Documents enter the index only through gated
actions (`ingest_document` is confined to `files.allowed_roots` via the
*same* `resolve_within` as file actions — reused, not reimplemented);
chunks are paragraph-packed with tail overlap so boundary-straddling
facts are findable; ingestion is idempotent by content hash. The
reference embedder is a **dependency-free hashing embedder** (words +
character trigrams, unit-normalised) — deliberately described as lexical
similarity, not semantics; a sentence-transformer or API embedder is a
drop-in `Embedder` subclass, and the store pins embedder name+dimension,
refusing to mix vector spaces. Search is brute-force cosine — at
personal-corpus scale that is milliseconds, and an ANN index is a heavy
dependency that earns its place past ~100k chunks, not before. Per
message, the reasoner injects the top chunks (score floor, char budget)
with an instruction to cite them or say they don't answer — recall
failures degrade to no section, never to no reply. Knowledge content
never travels on the bus.

**Connectors ride the plugin system.** Three example connectors ship in
`plugins/examples/` as real M11 plugins — loaded through the manifest
contract, namespaced, gated: `calendar` (local `.ics` files, offline,
folded-line/RFC 5545-subset parser), `tasks` (local JSON — the template
for API-backed task connectors), and `email` (stdlib IMAP, **headers
only** — reading bodies is an explicit ingestion decision, not a
connector side effect — with the mailbox opened read-only and the
password resolved *by name* via the new `api.secret()`). `api.secret()`
is an honest addition to the trust surface: in-process plugin code could
reach secrets anyway; the API makes the capability explicit and holds it
to the M11 rule — values at the last moment, never in results or audit.

## 18. Hardening and distribution (M14)

```
manifest: isolation ─┬─ in_process ─▶ imported into the kernel (full trust,
                     │                 may call api.secret())
                     └─ subprocess ─▶ child process ⇄ JSON-over-stdio bridge
                                       proxy ActionSpec per action; manifest
                                       floor re-applied parent-side; crash/
                                       hang/desync → that action fails, kernel
                                       and other plugins untouched
```

The tracked debts closed here, each the honest fix rather than a patch:

**Subprocess plugin isolation.** A manifest may now declare
`isolation: subprocess`; the loader spawns a child running
`digital_twin.plugins.subprocess_host`, which loads the plugin under the
*same* contract (undeclared actions refused, risk floors applied) and
speaks a line-oriented JSON protocol. Each declared action becomes a
proxy `ActionSpec` whose handler does one request/response; the parent
re-applies the manifest risk floor, so a lying child can make its actions
*more* guarded, never less. Failure is contained three ways — a crashed
child fails the in-flight action then fails fast (kernel and siblings
live), a hung child is killed on a per-call timeout so it cannot pin a
dispatcher worker, and children are terminated at interpreter exit. The
child's API is deliberately *narrower* than in-process: `register_module`
and `api.secret()` raise, because a sandboxed plugin has no bus and the
secret store never leaves the kernel process. Stated precisely: this is
fault + capability isolation, not an OS sandbox — the child is a normal
process running as the user; seccomp/containers remain future work.

**LLM keys via the secret store.** `create_language_model` takes an
optional secret store and resolves `llm.api_key_secret` (default
`gemini_api_key`, since Gemini is now the default provider — `Anthropic`
mode uses `anthropic_api_key`) *before* the environment variable; keys
are still never read from config files. Store one with
`digital-twin-secrets set gemini_api_key`.

**Browser interaction host re-check.** `browser_fill` and `browser_click`
now re-verify the live page host against `allowed_domains` at execution
time (previously only `browser_fill_secret` did), so a redirect between
validation and execution cannot relocate keystrokes onto an
off-allow-list page.

**Semantic embeddings.** `knowledge.embedder: semantic` selects a
sentence-transformers backend (optional `[semantic]` extra, lazy import
with guidance); the store's embedder pin includes the model name, so
switching correctly forces a re-ingest rather than mixing vector spaces.

**Wheel-safe assets.** `digital_twin.paths.resolve_asset` searches the
working directory, then `$DIGITAL_TWIN_HOME`, then the package location —
so config and model files are found whether the assistant runs from a
checkout or an installed wheel invoked from anywhere.

## 19. Dashboard depth and document ingestion (M15)

```
watch_paths ─▶ KnowledgeWatchModule (poll) ─▶ extract_text (format dispatch)
   │ contained in files.allowed_roots          │ txt/md · pdf(pdftotext|pypdf)
   │ idempotent (content hash)                  │ docx(zip+xml) · html(stdlib)
   └─ replace_source: an edited file            ▼
      supersedes its old chunks           store.ingest → chunks + embeddings

browser page ── GET /api/memory  ─▶ recent memories (kind, content)
   │         ── GET /api/knowledge ─▶ ingested docs (title, chunks, embedder)
   └─ GET /api/stream (SSE) ─▶ new bus events pushed as they happen
```

Two finished subsystems gained depth rather than the platform gaining
new surface.

**Document ingestion.** `ingest_document` no longer assumes UTF-8 text;
`knowledge/extraction.py` dispatches on suffix behind one entry point,
with the now-familiar lazy-backend discipline — plain text and source
files need nothing, `.docx` is read dependency-free (a docx *is* a zip of
XML, so a tiny tag scan pulls paragraph text), `.html` uses the stdlib
parser with scripts/styles dropped, and `.pdf` uses `pdftotext` if on
PATH else `pypdf` if importable else a guided error. The
`KnowledgeWatchModule` polls `knowledge.watch_paths` (each validated
inside `files.allowed_roots` at construction — an out-of-bounds path is
dropped, never read); watching a directory is standing consent declared
once in config, the same model as `open_application`'s allow-list, so
per-file gating would be theatre. Idempotence is the store's content
hash, and a new `replace_source` flag closes a bug this milestone's demo
surfaced: an edited file now *supersedes* its previous chunks instead of
leaving stale text recallable beside the new version — while chat-sourced
notes still accumulate, because distinct notes legitimately should.

**Dashboard depth.** The server grew an extensible `data_sources` map
(name → callable, served at `/api/<name>`) so panels are additive: the
memory panel reads recent records, the knowledge panel lists ingested
documents and the active embedder. The headline is `/api/stream` —
Server-Sent Events — so the page can stop polling and instead have new
bus events pushed the instant they are recorded; a `_closing` flag lets
in-flight streams exit promptly on shutdown. Everything stays loopback,
token-guarded, stdlib-only.

## 20. Write-side connectors, wake word and installers (M16)

```
send_email {to, subject, body} ─▶ validate: recipient domain allow-list
                                   (off-list → refused BEFORE any gate)
                               ─▶ DANGEROUS → clamp → confirmation shows
                                   the FULL outbound content
                               ─▶ SMTP (stdlib), password by secret name

"hey twin" ─▶ WakeWordModule (own mic+STT, always on, trigger-ONLY)
                └─▶ voice.control {start} ─▶ the existing push-to-talk path
                    (utterances are never published by the detector)
```

**The write-risk analysis, applied.** Reading the world is SENSITIVE;
*changing someone else's world* is DANGEROUS — `email.send_email` and
`calendar.create_event` are the first connector actions to cross that
line, and three properties make them safe to exist: (1) the M3 clamp
guarantees a human confirmation regardless of configuration, and the
confirmation prompt shows the full params — recipient, subject, body —
so what's approved is exactly what's sent; (2) validation refuses
foreseeable mistakes *before* the gate: a recipient outside the
connector's `allowed_recipient_domains` never even reaches the
confirmation (mis-send containment, the browser allow-list idea applied
to email); (3) credentials stay names — the SMTP password resolves via
`api.secret()` at the last moment and is deleted after login. The
calendar write stays offline by design: it emits a proper `.ics` VEVENT
into the synced folder rather than talking to a calendar API.

**Wake word as a separate module, on purpose.** `voice.wake_word: "hey
twin"` starts a small always-on detector with its *own* microphone and
transcriber that does exactly one thing when it hears the phrase:
publish `voice.control {command: start}` — pressing the same
push-to-talk button a gesture would. It never publishes utterances, so
"always listening" is structurally trigger-only, lives in exactly one
nameable, disableable module, and the voice module needed zero changes.
Matching is normalised-substring over a rolling transcript — forgiving
by choice, since a false trigger only opens a gated session while a
missed one annoys.

**Installers.** `digital-twin-setup` creates a `DIGITAL_TWIN_HOME`
(config copy + data/logs/models dirs, idempotent, `--force` to
overwrite) so a wheel install becomes runnable in one command; paired
with M14's asset resolver this closes the checkout-only gap.

## 21. UI completion (M17)

```
gesture module ─ frame_sink ─▶ DebugView(headless) ─ JPEG ─▶ FrameHub
                                                              │ latest-only
browser <── multipart/x-mixed-replace ◀── /api/frames/<name> ┘
browser <── EventSource ◀── /api/stream        (push, not poll)
        <── /api/plugins  (health, sandbox, actions, errors)
        <── /api/settings (active config, non-defaults flagged, read-only)
```

The dashboard becomes the whole cockpit, closing the §2.13 gaps without
new core surface — every panel reads a seam that already existed.

**Live camera in the browser.** A `FrameHub` (latest-frame-per-source
fan-out) lets the gesture `DebugView` run **headless**: it annotates
frames exactly as it would for its OpenCV window but, given a
`frame_sink`, JPEG-encodes them into the hub instead of (or alongside)
`imshow`. The dashboard streams them at `/api/frames/<name>` as
`multipart/x-mixed-replace` — so the gesture debugger finally leaves its
desktop window and renders on any machine, including the headless boxes
where the OpenCV window was disabled all along. Latest-only means a slow
tab drops frames rather than growing memory.

**Push, not poll.** The page now opens an `EventSource` against the M15
`/api/stream` and refreshes on each pushed event (a slow timer remains as
a fallback and to refresh the polled panels). Live indicators for mic /
wake / camera read the module statuses and metrics already published.

**Plugin and settings panels.** `/api/plugins` surfaces each
`LoadedPlugin` report — health, sandbox flag, actions, and the exact
error for a disabled one — so the contract enforcement from M11/M14 is
finally visible. `/api/settings` renders the active `AppConfig` with
every non-default field flagged, and is deliberately **read-only**:
configuration is frozen at startup so modules never see it shift under
them, so the honest UI is "edit the YAML and restart," not a live editor
that would violate that invariant.

## 22. Sequence: thumbs-up during a presentation

```
camera thread   inference thread   poll thread      bus dispatcher    intent engine
     │  frame ─────▶ track+classify     │                 │                │
     │               (stabilised)       │                 │                │
     │                   └── latest ──▶ diff              │                │
     │                                  └─ publish ─────▶ deliver ──────▶ lookup(presentation,
     │                                     gesture         │              thumbs_up)
     │                                                     │◀─ publish ─── intent.detected
     │                                                     └─▶ subscribers (console, M2 dispatcher…)
```

## 23. Known trade-offs (tracked)

* Single dispatcher thread: a slow subscriber delays all deliveries.
  Mitigated by slow-handler warnings; per-subscriber queues are the planned
  evolution if profiling justifies it.
* Two hands with the same handedness label collapse to one presence key
  (rare MediaPipe misclassification); harmless but imprecise.
* Context is manual (`--context`, events) until a screen-context perception
  module exists (M3).
* Action execution uses a single worker: one confirmation prompt or slow
  action queues everything behind it (bounded queue, drops audited).
  Parallel lanes per risk class are the planned evolution if needed.
* A timed-out action's thread cannot be killed in Python — it is reported
  (`still running!`) and the executor is abandoned on stop; handlers are
  expected to be short-lived.
* Mouse control and drag-and-drop are not implemented (risk analysis
  pending); window management covers focus only.
* pynput media keys vary by platform build; xdotool is the reference
  backend.
* Memory search is lexical (tokens over content/tags); the knowledge
  engine (M13) provides vector recall for documents — unifying memory
  recall onto the knowledge store's embedder is a candidate follow-up.
  Skill memory has no producer yet.
* The Fernet key file is plain on disk (0600); an OS keyring integration
  belongs to the secrets-manager milestone.
* ~~Console chat and console confirmations share stdin~~ — resolved in
  M12: `security.confirmation: web` moves approvals to the dashboard.
  (The console pairing still has the conflict if chosen.)
* One reasoner worker: a slow completion queues later messages (bounded,
  polite busy replies). Streaming responses arrive with the UI.
* Plan cancellation takes effect between steps; in-flight actions run to
  completion (thread-kill limitation).
* Stage parallelism is semantic today (single dispatcher worker
  serialises execution); adding workers requires no plan changes.
* Vosk models are a manual download (offline trade-off); no wake word —
  triggers are gestures, events or the API. Push-to-talk sessions carry
  one utterance; subprocess TTS voices vary by platform.
* Runtime profile *switching* needs config-reload machinery (module
  rebuild); profiles are startup-selected for now.
* Debug window on Linux renders from a worker thread; solid in practice
  with X11, but GUI-in-main-thread purism argues for moving visualization
  into the future dashboard UI, which is the plan.
* `read_screen` is asynchronous relative to a chat turn: "what's on my
  screen?" triggers the capture, and the *next* question is answered from
  it. Synchronous ask-capture-answer needs reasoner-side action awaiting
  (a dashboard/streaming-era change).
* Document extraction is text-level: `.docx` paragraph text (no tables/
  styles), `.pdf` via external tools, no OCR of scanned PDFs — that would
  reuse the M10 screen-OCR path, tracked as a follow-up.
* Screen OCR is plain text: no layout, no UI-element detection, no
  "click the OK button" grounding — that needs a vision model or
  accessibility APIs, tracked in REMAINING_WORK §2.3.
* File action results ride the 300-char `action.result` detail field, so
  `read_text_file` is a bounded preview, not document ingestion —
  document Q&A belongs to the knowledge engine (RAG) milestone.
* File containment re-checks paths in handlers, but validation→execution
  is still not atomic against a hostile local process racing renames
  (single-user desktop threat model; `open('x')` closes the overwrite
  case specifically).
* ~~Voice's `speak` registers after the reasoner snapshots the action
  catalog~~ — fixed in M11: the catalog is late-bound (a callable
  evaluated per message).
* Plugins run in-process: the manifest contract scopes honest plugins;
  it cannot stop a malicious one. Subprocess isolation with an IPC
  bridge is the real sandbox (tracked in REMAINING_WORK §2.11/§2.12).
* The keyring backend's names index is a plaintext list of secret
  *names* (values stay in the OS keyring) — metadata-not-secret, the
  same documented policy as memory encryption.
* One shared browser page per session: no tab management yet.
* `send_email` is plain-text body over STARTTLS SMTP: no HTML mail, no
  attachments, no OAuth2 (app passwords / basic auth only) — deliberate
  first cut; attachments especially need their own risk pass.
* `create_event` writes local `.ics` and never edits or cancels existing
  events; invites (attendees, ORGANIZER) are out of scope.
* Wake-word matching is lexical substring over a small-model transcript:
  expect occasional false accepts (harmless: opens a gated session) and
  misses in noise; a dedicated keyword-spotting model is the upgrade
  path. (The
  redirect-between-validation-and-fill gap is closed in M14 — all
  interaction actions re-check the live host against `allowed_domains`.)
* `browser_extract_text` returns page text through the 300-char
  `action.result` detail — previews, not ingestion (knowledge engine).
* Playwright and Chromium are a manual install (`pip install playwright
  && playwright install chromium`); the kernel starts without them and
  browser actions fail audited with guidance.
* The page is push-driven via `EventSource` on `/api/stream` (M17), with
  a slow timer only as a fallback and to refresh the read-only panels.
* Settings are shown read-only: the config is frozen at startup by
  design, so applying changes means editing the YAML and restarting — a
  live editor is intentionally not offered.
* MJPEG framing is simple `multipart/x-mixed-replace`: fine on loopback
  for a debug view; a production remote feed would want WebRTC/H.264.
* Dashboard traffic is plain HTTP on loopback; `allow_remote` exposes it
  unencrypted and is deliberately gated behind config validation. TLS or
  a reverse proxy is the operator's job if they flip it.
* ~~The gesture debug view renders via OpenCV, not the dashboard~~ —
  fixed in M17: a headless `DebugView` JPEG-encodes annotated frames into
  a `FrameHub` streamed at `/api/frames/<name>`. The OpenCV window is
  still available when a display exists and `debug_window` is set.
* The hashing embedder is lexical: paraphrases with no shared tokens or
  trigrams won't match ("PTO" vs "vacation"). A semantic embedder is a
  drop-in; the store's embedder pin forces a clean re-ingest.
* Knowledge search is a full scan (fine to ~100k chunks); ANN indexing
  is deliberately deferred until a corpus justifies the dependency.
* The email connector fetches headers of the *latest N* unread messages;
  no pagination, threading, or body ingestion (the latter by design).
* Calendar parsing covers the common .ics subset; recurrence rules
  (RRULE) are not expanded.
