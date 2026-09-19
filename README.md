# Digital Twin AI Assistant

A cross-platform, multimodal desktop AI assistant built as a set of
independent, replaceable modules communicating over an event bus. Perception
modules observe (hand gestures in the Airboard browser page, and — in
future milestones — voice, screen, OCR, keyboard); reasoning modules interpret; automation modules act.
No module knows about any other: the event contract is the only coupling.

**Milestone 1** delivers the multimodal kernel — event bus, module
lifecycle, registry, typed configuration, rotating logs — with the
hand-gesture engine integrated as the first first-class perception plugin
(since replaced by [Airboard](digital_twin/airboard/), which tracks hands
in the browser) and a context-aware intent engine proving the
full perception → reasoning chain.

```
                         ┌──────────────────────────────────────────────┐
   PERCEPTION            │                 EVENT BUS                    │        REASONING
                         │   bounded queue · dispatcher thread ·        │
 ┌────────────────┐      │   fault isolation · backpressure · stats     │      ┌────────────────┐
 │ airboard       │─────▶│                                              │─────▶│ intent engine  │
 │ (browser hands)│      │  perception.gesture   {gesture, conf, hand}  │      │ context-aware  │
 └────────────────┘      │  perception.hand      {hand, present}        │      └───────┬────────┘
 ┌────────────────┐      │  context.changed      {context}              │
 │ screen context │─────▶│                                              │
 │ (active window)│      │                                              │
 └────────────────┘      │                                              │              │
 │ voice / screen │─ ─ ─▶│  intent.detected      {intent, context, …}   │              ▼
 │ (future)       │      │  action.requested     (future)               │       intent.detected
 └────────────────┘      │  system.module        {name, state, …}       │      (automation: M2)
                         └──────────────────────────────────────────────┘
```

The same gesture means different things in different contexts — and that
decision never lives in perception code:

| Context        | 👍 `thumbs_up`       | Other examples                      |
| -------------- | -------------------- | ----------------------------------- |
| `presentation` | `next_slide`         | 👎 `previous_slide`, ✊ `end_presentation` |
| `media`        | `like`               | ✌️ `play_pause`, 👉 `seek_forward`  |
| `coding`       | `accept_suggestion`  | 👎 `reject_suggestion`              |
| any (`"*"`)    | —                    | ✋ `assistant_attention`            |

The whole table is configuration (`config/default_config.yaml`), not code.

**Per-user tuning (Milestone 2).** Gesture recognition is calibratable
without touching core code:

```yaml
airboard:
  gesture_thresholds: {thumbs_up: 0.75}     # per-gesture confidence floors
  disabled_gestures: [finger_gun]           # never published
  repeat_interval_s: 0.8                    # re-fire a held gesture

profiles:                                   # named per-user bundles of the above
  active: arnav
  available:
    default: {}
    arnav:
      airboard: {repeat_interval_s: 0.8, gesture_thresholds: {thumbs_up: 0.7}}
      intent:  {default_context: coding}
```

Select profiles at launch with `python main.py --profile arnav`. New
gestures are one entry in `RULES` in `digital_twin/airboard/static/gestures.js`.

**Guarded automation (Milestone 3).** Intents don't run code directly —
every effect passes a fixed security pipeline: **binding → validation →
permission policy → confirmation gate → execution → audit**.

```yaml
security:
  risk_defaults: {safe: allow, sensitive: confirm, dangerous: deny}
  permissions: {open_url: allow}      # per-action overrides
  confirmation: console               # interactive y/N, fail-closed
  audit_file: logs/audit.jsonl        # append-only, full provenance

automation:
  applications: {browser: firefox}    # open_application allow-list
  intent_bindings:
    open_dashboard: {action: open_url, params: {url: https://example.com}}
```

**Hands-free context (Milestone 4).** A screen-context perception module
samples the focused window (xdotool on Linux/X11, ctypes on Windows,
osascript on macOS) and publishes `context.changed` automatically — the
intent engine cannot tell it apart from the old `--context` flag, which is
the architecture working as designed. Classification is ordered
config rules (first substring match on title/process wins):

```yaml
context_perception:
  rules:
    - {context: presentation, any: [powerpoint, impress, keynote]}
    - {context: media, any: [youtube, vlc, spotify]}
  publish_window_info: false   # titles are sensitive; off the bus by default
```

Try it without any hardware: `python examples/hands_free_demo.py` — the
same thumbs-up becomes `next_slide`, `like`, `accept_suggestion` as the
(simulated) focus moves between Impress, YouTube and VS Code.

**Desktop input actions (Milestone 5).** The assistant can press keys,
type, set the clipboard and focus windows — through xdotool (Linux/X11, no
extra deps) or pynput (Windows/macOS, `pip install pynput`). The risk
split *is* the UX: `nav_key` (arrows only) and `media_key` are SAFE, so a
thumbs-up advances a slide with **no prompt**, while `press_keys` (chords),
`type_text`, `set_clipboard` and `focus_window` are SENSITIVE and
confirmation-gated by default. Hard limits sit before the gate: canonical
key names only, chords are modifiers+one key, `type_text` rejects newlines
outright (typed text can never carry its own Enter into a terminal), and
repeats cap at 10. Default bindings now include `next_slide`,
`previous_slide`, `play_pause`, `seek_forward/backward` and
`end_presentation` — try `python examples/presentation_demo.py`.

**Memory foundation (Milestone 6).** The assistant remembers. A
SQLite-backed store (WAL, stdlib-only) holds **episodic** memories —
every action outcome, persisted automatically with status-weighted
importance and tags — and **semantic** facts you teach it, which are
never auto-pruned. A bounded in-RAM **working memory** tracks recent
activity (intents, context switches, results) and is wiped on pause:
privacy over convenience. Content can be **encrypted at rest** (Fernet;
`memory.encryption: true`, key auto-generated 0600) — searchable metadata
stays plaintext by documented design, and enabling encryption without the
`cryptography` package fails startup rather than silently downgrading.
Ranked lexical search decays by recency half-life and bumps access stats.

You stay in charge from the terminal:

```bash
python -m digital_twin.memory stats
python -m digital_twin.memory search "presentation"
python -m digital_twin.memory remember "prefers dark theme" --tags preference
python -m digital_twin.memory edit <id> --importance 0.9
python -m digital_twin.memory delete <id>       # or: clear --kind episodic
python -m digital_twin.memory export            # decoded JSON dump
```

Try `python examples/memory_demo.py` for the full loop: act → remember →
recall → forget.

**Conversational reasoning (Milestone 7).** Type to the assistant —
get a free key at https://aistudio.google.com/apikey, `export
GEMINI_API_KEY=...`, and run `python main.py`, then just type into the
terminal (or point `llm.provider: anthropic` / `ollama` at Claude or a
fully offline local model). **Gemini is the default backend — it has a
genuine free tier**, no credit card required. Chat is *just another
perception module* publishing `perception.chat`; the reasoner answers,
explains itself (every reply carries a one-line `reasoning`), consults
memory on the way in (ranked M6 recall lands in the prompt) and teaches
memory on the way out (facts you mention persist as semantic memories,
visible and deletable via the CLI).

The security model is the headline: **the LLM has no direct access to
actions.** It may only nominate an intent from the configured allow-list;
a hallucinated name is dropped in the reasoner, and a legitimate one is
published as a normal `intent.detected` event that passes the *same*
binding, validation, permission, confirmation and audit pipeline as a
gesture. API keys come from the environment only — never from config
files. Try it without a key: `python examples/chat_demo.py`.

**The planner (Milestone 8).** Complex requests decompose into
multi-step plans — and a plan is *choreography, not privilege*: the
planner publishes each step as a gated `action.execute` event, so **every
step individually** passes registry lookup, parameter validation, the
permission policy, confirmation gates and audit (with `plan_id`/`step`
correlation in every audit row). Plans come from three sources: named
routines in config (with `{parallel: [...]}` stages — steps in a stage
have no ordering dependency, stages run strictly in order), plans
proposed by chat (the model sees the action catalog and may propose
sequential steps; gate-able per `planner.accept_llm_plans`), and **skills**:
a successful LLM plan is saved to skill memory and replayable by a text
query — the assistant learns workflows. Replays are re-confirmed step by
step; approvals never persist. Progress streams on `plan.progress`,
`plan.cancel` stops between steps, stages time out, and failure policy is
per-plan (`abort`/`continue`). A gesture can start a routine via
`planner.intent_triggers`. Try `python examples/planner_demo.py`.

**Voice (Milestone 9).** Speak to the assistant — an open palm is the
push-to-talk button by default (`voice.listen_intents`), and in
push-to-talk mode **the microphone opens only for the session**: trigger,
one utterance, released. Privacy is structural, not a promise.
Recognition is **offline** (Vosk — download a model from
https://alphacephei.com/vosk/models into `models/`; audio never leaves
the machine) and the engine is replaceable exactly like the LLM. Final
utterances publish as `perception.voice` and flow through the reasoner
like typed chat — a third modality with zero core changes. Replies are
spoken via the gated `speak` action (audited; **mute the assistant with
one rule**: `security.permissions: {speak: deny}`), speech is
non-blocking, and **barge-in** works: talk over the assistant and it
stops mid-sentence. Live partials stream on `perception.voice.partial`
for future captioning. Try `python examples/voice_demo.py` — no
microphone needed.

**Screen OCR + file-system intelligence (Milestone 10).** Ask "what's on
my screen?" — the SENSITIVE `read_screen` action captures **one**
screenshot (there is no polling and no ungated path to a capture), OCRs
it locally with `tesseract`, deletes the image immediately, and publishes
the text on `perception.screen`, where the reasoner picks it up for its
next reply. The audit log sees character counts, never the text. Capture
backends are subprocess-only (scrot / ImageMagick / gnome-screenshot /
screencapture / PowerShell) and resolve lazily, so nothing new is
required at startup. File actions are confined to `files.allowed_roots`
(empty by default — name the directories the assistant may touch):
`list_files`, `search_files`, `read_text_file`, `find_duplicates`,
`create_directory`, `write_text_file`, `copy_file` (all overwrite-refusing,
SENSITIVE) plus the assistant's **first DANGEROUS actions** — `move_file`
and `delete_file` — which the permission system *always* confirms, even
if configured `allow` (the M3 clamp, finally exercised). `delete_file`
moves to a per-root trash directory instead of unlinking: a confirmed
mistake stays recoverable. Try `python examples/screen_files_demo.py`.

**Plugins, secrets & the browser (Milestone 11).** Third parties can now
extend the assistant **without touching its code**: drop a directory with
a `plugin.yaml` into `plugins.paths` and its declared actions register
namespaced (`weather.current_weather`) through the same gates as
built-ins — undeclared actions are refused, declared risk is a floor, and
a plugin that fails during load contributes nothing. Secrets live in the
OS keyring or a Fernet-encrypted file and are referenced **by name only**
(`python -m digital_twin.security.secrets_cli set github_token`) — values
never appear in params, results, the audit log or the LLM prompt. Browser
automation (Playwright behind a replaceable driver) makes the web
actionable: navigation and text extraction are SENSITIVE, clicking and
typing are DANGEROUS (always confirmed), and `browser_fill_secret` types
a stored credential into a page only if its host is explicitly
allow-listed — login flows with no password ever stored insecurely or
shown to the model. Try
`python examples/plugins_secrets_browser_demo.py`.

**Dashboard, packaging & performance (Milestone 12).** Set
`dashboard.enabled: true` and open `http://127.0.0.1:8787`: live module
states and bus counters, a real-time event feed, chat from the browser —
and with `security.confirmation: web`, gated actions wait for your
Approve/Deny **in the page** instead of the terminal (fail-closed: no
click, no action), which finally ends the console chat/confirmation
stdin conflict. The server is stdlib-only, binds loopback (config
validation refuses anything else without an explicit `allow_remote`),
and every state-changing endpoint needs a per-session token a foreign
website can't read. The project now installs as a package
(`pip install .` — lean core, heavy capabilities as extras like
`[gesture]` and `[browser]`), ships GitHub Actions CI, and carries a
benchmark: ~62k events/s bus throughput and ~0.3 ms of gate overhead
per action (`python examples/benchmark.py`). Try
`python examples/dashboard_demo.py` — no browser needed.

**Knowledge engine & connectors (Milestone 13).** Teach the assistant
your documents: `ingest_document` (confined to your allow-listed
folders) chunks and embeds files locally into `data/knowledge.db`, and
every chat message retrieves the best-matching excerpts into the model's
prompt with instructions to cite them — local RAG with a
zero-dependency embedder (semantic models drop in behind one
interface). Alongside it, three **connector plugins** ship in
`plugins/examples/` and load through the M11 manifest contract: calendar
(`.ics` files, offline), tasks (local JSON), and email (IMAP, unread
*headers only*, mailbox read-only, password pulled by name from the
secrets manager — never stored in a config file). Try
`python examples/knowledge_connectors_demo.py`.

**Hardening & distribution (Milestone 14).** Plugins you didn't write can
now run behind real isolation: set `isolation: subprocess` in a plugin's
manifest and it runs in its own process — a crash or hang costs one
action, never the kernel, and it can't reach kernel memory or your
secrets (fault + capability isolation, not an OS sandbox). LLM API keys
resolve from the secret store (`digital-twin-secrets set
gemini_api_key`, or `anthropic_api_key` if you switch `llm.provider`)
ahead of the environment, never from config. Browser
typing and clicking re-check the live page host, so a redirect can't move
your keystrokes onto a look-alike site. The knowledge engine gains an
optional `semantic` embedder (sentence-transformers) for paraphrase-aware
recall, asset paths survive a wheel install via `DIGITAL_TWIN_HOME`, and
there's now a full [user guide](docs/USER_GUIDE.md). Try
`python examples/sandbox_demo.py`.

**Dashboard depth & document ingestion (Milestone 15).** The knowledge
engine now ingests real formats — PDF (`pdftotext`/`pypdf`), Word
(`.docx`, dependency-free), HTML, and text — and can **watch folders**:
point `knowledge.watch_paths` at a directory inside your allow-list and
new or edited files are ingested automatically (an edited file replaces
its old version rather than duplicating it). The dashboard grows a memory
panel, a knowledge panel (documents + active embedder), and a
`/api/stream` Server-Sent Events endpoint so it can be pushed updates
instead of polling. Try `python examples/dashboard_depth_demo.py`.

**Write-side connectors, wake word & installers (Milestone 16).** The
assistant can now *send*: `email.send_email` and `calendar.create_event`
are DANGEROUS-class — always confirmed, with the full outbound content in
the prompt you approve — and a recipient-domain allow-list refuses
mis-sends before the confirmation even appears. Say `voice.wake_word:
"hey twin"` and an always-on detector presses push-to-talk when it hears
you — it triggers and nothing else; utterances are never published by it.
And `digital-twin-setup` turns a wheel install into a runnable home
directory in one command. Try
`python examples/write_wake_setup_demo.py`.

**UI completion (Milestone 17).** The dashboard is now the whole cockpit.
The gesture debugger renders **in the browser** — a headless view
JPEG-encodes annotated frames into a hub streamed at `/api/frames` — so
you can watch hand tracking without an OpenCV window, even on a headless
box. The page is push-driven (an `EventSource` on `/api/stream`, no more
polling) with live mic/wake/camera indicators, a plugin panel (health,
sandbox status, actions, load errors), and a read-only settings panel
that flags every non-default field. Try
`python examples/ui_completion_demo.py`.

Security invariants enforced in code, not convention: unknown actions are
denied; unanswered confirmations are denied; DANGEROUS actions can never be
configured to run unattended (`allow` clamps to `confirm`); and
`open_application` only launches allow-listed commands — there is no
"run arbitrary command" action. Every attempt (allowed, denied, failed,
even unbound intents) lands in the audit log with the intent event id
*and* the original perception event id, so any executed action traces back
to the exact gesture that caused it. Built-in actions: `log_message`,
`notify`, `open_url`, `open_application`. Custom
gestures get auto-slugified semantic ids (`Three Count` → `three_count`)
and immediately work in thresholds, profiles and intent mappings — see
`examples/custom_gestures/three_count.py` for the template. The debug
window is a dev/calibration tool: it refuses to start without a usable
display (headless-safe) and never affects event flow.

---

## Quick start

```bash
pip install -r requirements.txt

# Install the package itself (editable) so the console scripts land on PATH:
#   digital-twin, digital-twin-setup, digital-twin-memory,
#   digital-twin-secrets, digital-twin-audit, digital-twin-device
# Without this the CLIs are reachable only as `python -m digital_twin.…`.
pip install -e .

# Full kernel (set airboard.enabled: true, then open http://127.0.0.1:8794/
# in Chrome — the page tracks your hands and sends gestures to JARVIS):
python main.py --context presentation

# No-hardware demo of the gesture → bus → intent chain:
python examples/gesture_to_intent_demo.py

# Tests (no camera required; the JS gesture engine is checked under Node):
python -m pytest tests/ -q
```

The Airboard page loads MediaPipe's hand-landmark model from Google's CDN
(Apache 2.0); Python needs no camera or vision packages.

---

## The event contract

Everything on the bus is an immutable `Event` (see
`digital_twin/core/events.py`). Serialised form:

```json
{
  "event_id": "9f2c4d…",
  "timestamp": "2026-07-06T12:00:00.412000+00:00",
  "module": "gesture",
  "type": "perception.gesture",
  "gesture": "thumbs_up",
  "confidence": 0.97,
  "hand": "right",
  "repeat": false
}
```

| Topic                | Publisher       | Payload                                    |
| -------------------- | --------------- | ------------------------------------------ |
| `perception.gesture` | gesture module  | `gesture`, `confidence`, `hand`, `repeat`  |
| `perception.hand`    | gesture module  | `hand`, `present`                          |
| `context.changed`    | screen context / anyone | `context` (+ window info if opted in) |
| `perception.chat`    | chat module     | `text`, `user`                             |
| `perception.voice`   | voice module    | `text` (final utterances)                  |
| `perception.voice.partial` | voice module | `text` (live transcription)            |
| `perception.screen`  | screen reader   | `text` (OCR, bounded), `chars`, `truncated` |
| `voice.control`      | anyone          | `command`: start \| stop \| toggle         |
| `chat.response`      | reasoner        | `text`, `reasoning`, `intent_triggered?`, `remembered?` |
| `intent.detected`    | intent engine   | `intent`, `context`, `gesture`, `hand`, `confidence`, `repeat`, `source_event` |
| `action.requested`   | dispatcher      | `action`, `intent`, `params`, `risk`       |
| `action.result`      | dispatcher      | `action`, `status`, `detail`, provenance (+`plan_id`, `step`) |
| `action.execute`     | planner         | `action`, `params`, `plan_id`, `step`, `label` |
| `plan.request`       | reasoner/anyone | `plan` \| `goal`+`steps` \| `skill`        |
| `plan.progress`      | planner         | `plan_id`, `plan`, `status`, `step`, `total_steps` |
| `plan.cancel`        | anyone          | `plan_id`                                  |
| `action.confirmation`| dispatcher      | `action`, `state`                          |
| `system.module`      | registry        | `name`, `action`, `state`, `detail`        |

Subscriptions match an exact topic, a subtree (`perception.*`), or
everything (`*`).

Gesture identifiers are **semantic**, stable `snake_case` names
(`thumbs_up`, `peace`, `i_love_you`, `pointing_left`, …) emitted directly by
the browser engine, with aliases (`high_five` → `open_palm`). See
`digital_twin/airboard/semantics.py`.

---

## Module model

Every subsystem implements one small lifecycle
(`digital_twin/core/module.py`):

```
CREATED ──start──▶ RUNNING ⇄ PAUSED        any hook raising → FAILED
                      │        (pause/resume = runtime disable/enable)
                    stop
                      ▼
                  STOPPED (re-startable)
```

The `ModuleRegistry` starts modules in order, stops them in reverse,
isolates failures (one module failing never blocks the others) and
announces every transition on `system.module`. A paused gesture module
fully releases the camera and the MediaPipe graph — runtime disable is a
privacy switch, not a mute button.

### Adding a perception module

1. Subclass `BaseModule`; set `name` and `topics`.
2. Acquire resources in `_on_start`, release them in `_on_stop`.
3. Publish `Event`s via `self._publish(...)` — describe **what you saw**,
   never what it means (interpretation belongs to reasoning).
4. Register it in `main.py`'s `build_registry`.

Nothing else in the system changes — that is the point.

---

## Repository layout

```
digital-twin/
├── main.py                     # kernel entry point
├── config/default_config.yaml  # every tunable, documented
├── digital_twin/
│   ├── core/                   # events, bus, module contract, registry
│   ├── configuration/          # typed config with YAML overrides
│   ├── airboard/               # browser board: hand tracking, named gestures, the blob
│   ├── perception/context/     # active-window probes + context classification
│   ├── perception/screen/      # on-demand screenshot capture + local OCR (gated)
│   ├── reasoning/              # intent engine + LLM interface + chat reasoner
│   ├── memory/                 # store, codec, working memory, module, CLI
│   ├── automation/             # action registry, built-ins, input & file actions, guarded dispatcher
│   ├── planner/                # plan model + stage choreography module
│   ├── voice/                  # audio sources, STT, TTS, speak action, module
│   ├── browser/                # replaceable browser driver + gated web actions
│   ├── dashboard/              # local web UI + web confirmation provider
│   ├── knowledge/              # local RAG: chunking, embeddings, vector store
│   ├── plugins/                # manifest contract + scoped third-party loader
│   ├── security/               # permission policy, confirmation gates, audit log, secrets
│   └── utils/                  # logging setup (rotating files)
├── plugins/examples/           # connector plugins: calendar, tasks, email
├── examples/                   # no-hardware demos
├── tests/                      # 426 tests, hardware-free
└── docs/architecture.md        # event catalog, lifecycle, sequence diagrams
```

---

## Configuration

All tunables live in `config/default_config.yaml` — bus sizing, camera,
tracking, gesture stability, event behaviour (`repeat_interval_s`
re-publishes held gestures for hold-to-repeat actions), logging, and the
full context → gesture → intent table. Unknown keys are reported at
startup; invalid values fail fast. Run with `--config my.yaml`.

## Roadmap

- **M1** — multimodal kernel + gesture perception + intent engine ✔
- **M2** — gesture module feature-complete: calibration, custom gestures,
  user profiles, debug visualization ✔
- **M3** — action pipeline: dispatcher, permissions, confirmation gates,
  audit log, first safe automations ✔
- **M4** — screen-context perception: automatic hands-free
  `context.changed` ✔
- **M5** — desktop input actions: keyboard/media keys, typing, clipboard,
  window focus behind the gates ✔
- **M6** — memory foundation: encrypted-at-rest store, episodic +
  semantic + working memory, ranked recall, full user control CLI ✔
- **M7** — conversational reasoning: replaceable LLM backends
  (Gemini default/Anthropic/Ollama), chat perception, allow-listed gated
  intents, memory-consuming prompts, explained replies ✔
- **M8** — planner: multi-step plans (config routines, LLM proposals,
  replayable skills), every step individually gated, progress/cancel/
  timeout, skill memory's first producer ✔
- **M9** — voice: offline push-to-talk/continuous STT, gesture-triggered
  listening, session-scoped microphone, gated spoken replies, barge-in
  interruption ✔ (this release)
- **M5+** — see `docs/REMAINING_WORK.md` for the full gap analysis against
  the project specification and the proposed milestone order

See `CHANGELOG.md` for history and `docs/architecture.md` for detail.

## Model files

Model weights are not tracked in git. A fresh clone needs:

- `models/hand_landmarker.task` — MediaPipe hand landmarker
- `models/vosk-model-small-en-us-0.15/` — Vosk small English ASR

Download both before first run.
