# Digital Twin — User Guide

A practical guide for running and operating the assistant. The
[README](../README.md) explains what exists and why; this document is
*how to drive it*. The architecture deep-dive lives in
[docs/architecture.md](architecture.md).

## 1. Install

From a checkout (recommended today):

```bash
pip install .                 # lean core: kernel, chat, planner, files, plugins
pip install .[all]            # everything (voice, browser, semantic…)
```

Pick capabilities individually: `[encryption]` encrypted memory + file-backend secrets, `[browser]`
Playwright (then run `playwright install chromium`), `[keyring]`
OS-keyring secrets, `[voice]` microphone capture (download a Vosk model
separately), `[semantic]` sentence-transformers embeddings.

Installed console commands: `digital-twin` (the kernel),
`digital-twin-setup`, `digital-twin-memory`, `digital-twin-secrets`.

Running from a wheel rather than a checkout? Run `digital-twin-setup`
once — it creates `~/.digital_twin` with a config copy and the
data/logs/models folders — then `export DIGITAL_TWIN_HOME=~/.digital_twin`.
Asset paths resolve against the working directory, then
`DIGITAL_TWIN_HOME`, then the package location.

## 2. First run

```bash
python main.py                     # chat, voice, actions — no camera needed
```

Hand gestures run in the browser, not in Python. Set `airboard.enabled:
true` in your config, start JARVIS, then open http://127.0.0.1:8794/ in
Chrome and allow the camera. Gestures you make there (thumbs up, peace,
pointing…) reach JARVIS like any other input; the translucent blue blob
shows when JARVIS is listening, thinking or speaking.

```bash
python main.py --config my_config.yaml   # with airboard.enabled: true
```

Type into the console to chat. Without an LLM configured the assistant
still runs — intents, actions, plans and memory all work; only free-form
conversation needs a model. To enable one:

* **Local (private, offline):** run [Ollama](https://ollama.com), then in
  your config: `llm: {provider: ollama, model: llama3}`.
* **Anthropic:** store a key — `digital-twin-secrets set
  anthropic_api_key` — or export `ANTHROPIC_API_KEY`. The secret store is
  checked first; keys are **never** read from config files.

## 3. Configuration

Everything lives in one YAML (see `config/default_config.yaml`, which
documents every field). Pass your own with `--config my.yaml`; anything
you omit keeps its default. Per-person tuning goes in `profiles`.
A malformed config falls back to defaults with a warning; an *invalid
value* is a hard error with the exact field named.

## 4. Permissions and confirmations

Every action carries a risk class: **safe** (allowed by default),
**sensitive** (confirmed by default), **dangerous** (denied by default —
and if you configure `allow`, the kernel still **clamps it to confirm**;
that rule is code, not config). Per-action overrides:

```yaml
security:
  permissions:
    next_slide: allow
    delete_file: confirm
```

Confirmation styles: `console` (y/N in the terminal), `web` (approve in
the dashboard — see below), `auto_deny` (headless: everything gated is
refused). Every decision lands in the append-only audit log
(`logs/audit.jsonl`) with full perception→intent→action provenance.

## 5. The dashboard

```yaml
dashboard: {enabled: true}          # http://127.0.0.1:8787
security: {confirmation: web}       # approve actions in the page
```

Live module states, bus counters, event feed, and chat. With `web`
confirmations, gated actions wait in the page for Approve/Deny —
fail-closed (no click = deny). The server binds loopback only unless you
explicitly set `allow_remote: true` (then TLS/reverse-proxying is on
you), and state-changing endpoints require a per-session token embedded
in the page, so foreign websites cannot forge approvals.

## 6. Memory, secrets, knowledge

* **Memory** — episodic (what happened) and semantic (what you taught
  it: "remember I prefer dark mode"). Review and prune:
  `digital-twin-memory list|show|delete|prune`. Optional encryption at
  rest: `memory: {encryption: true}`.
* **Secrets** — `digital-twin-secrets set|get|list|delete`. Values are
  prompted (never on argv), stored in the OS keyring or an encrypted
  file, and referenced everywhere else **by name only**.
* **Knowledge (RAG)** — ingest documents the assistant may cite:
  configure `files.allowed_roots`, then ask it to ingest (or use the
  `ingest_document` action). Relevant excerpts are injected into the
  LLM prompt with an instruction to cite or admit absence. `hashing`
  embedder is offline/dependency-free; `semantic` (extra) understands
  paraphrase but requires re-ingesting when switched.

## 7. Plugins and connectors

Point `plugins.paths` at directories of plugins. Each plugin declares
its actions and their minimum risk in `plugin.yaml`; undeclared actions
are refused and everything is namespaced (`weather.current_weather`).
Shipped examples in `plugins/examples/`: `.ics` calendar, local tasks,
IMAP email (headers only, read-only, password by secret name).

**Trust levels** (the `isolation` manifest field):

* `in_process` — full speed, full trust: the plugin runs inside the
  kernel and may request `api.secret(name)`.
* `subprocess` — the plugin runs in its own child process; a crash or
  hang costs one action, never the kernel, and it **cannot** touch
  kernel memory or secrets. Prefer this for anything you didn't write.
  (It is fault + capability isolation, not an OS sandbox.)

**Write-side actions** (sending email, creating calendar events) are
DANGEROUS: they are *always* confirmed, and the confirmation shows the
full outbound content — the exact recipient, subject and body — so you
approve precisely what leaves the machine. Configure a connector's
`allowed_recipient_domains` to have off-list recipients refused before
the confirmation even appears.

## 8. Voice, screen, files, browser

* **Voice**: push-to-talk by default — the microphone opens per session,
  structurally. `voice_demo.py` shows the flow without hardware. Set
  `voice.wake_word: "hey twin"` for an always-on detector that presses
  push-to-talk when it hears the phrase — it triggers only, and never
  transcribes your speech for anyone; disable it with `voice.wake_word:
  ""`.
* **Screen**: "what's on my screen?" triggers the gated `read_screen`
  action — one capture, OCR'd locally, image deleted immediately, only
  character counts in the audit.
* **Files**: nothing is reachable until you name directories in
  `files.allowed_roots`. Deletes go to a per-root trash, never `unlink`.
* **Browser**: set `browser.allowed_domains` before anything sensitive;
  `browser_fill_secret` refuses to type credentials anywhere not on that
  list, and interaction re-checks the live host so redirects can't move
  your keystrokes.

## 9. Troubleshooting

* *Module `failed` at startup* — normal for missing hardware/backends
  (no camera, no tesseract, no API key). The kernel keeps running; the
  status line and audit say exactly what to install.
* *"denied" results* — the permission policy: check §4 and your
  `security.permissions` rules.
* *Assistant can't find config/models when installed* — set
  `DIGITAL_TWIN_HOME` (§1).
* *Everything else* — `logs/` has rotating logs; `examples/` has a
  hardware-free demo for every subsystem; the test suite
  (`python -m pytest tests/ -q`) verifies your environment in seconds.
