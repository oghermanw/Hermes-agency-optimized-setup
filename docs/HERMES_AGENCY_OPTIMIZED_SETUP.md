# Hermes Agency Optimized Setup

This document records what is different from a default Hermes Telegram gateway setup and what to keep for future rebuilds or GitHub backup.

Secret rule: never commit `.env`, Telegram tokens, OpenRouter keys, auth files, runtime sessions, logs, or backup zip files.

## Current Design

Default Hermes is mainly a gateway plus configured model profile. This setup keeps the existing Telegram gateway/profile and adds a local agency layer:

| Part | Default Hermes | Current Optimized Hermes Agency |
| --- | --- | --- |
| Telegram gateway | Existing bot/gateway profile | Kept as-is; no Telegram key reset and no gateway setup rerun |
| Worker profiles | Usually one active/default model profile | Added `code-glm` for OpenRouter GLM work and `ebook-local` for local Ollama translation |
| Slash commands | Built-in Hermes/skill commands | Added `/ebook`, `/vtt`, `/code`, `/infographic_last`, queue/status commands |
| Long jobs | Usually run synchronously or as ordinary background work | `/ebook` and `/vtt` use one shared local heavy-task queue |
| Book translation | Basic text/Markdown style output | Rebuilds optimized Chinese-reading EPUB and preserves original images/assets |
| VTT transcription | Local script output | Progress-aware local STT with backend/model/runtime/ETA reporting |
| Secret handling | Depends on process/env usage | Worker child env strips API keys/tokens from subprocesses |
| Backup | Manual | Private setup zip plus GitHub-safe docs/skill pack |

## Live Paths

| Purpose | Path |
| --- | --- |
| Main Hermes config | `~/.hermes/config.yaml` |
| Secret env file | `~/.hermes/.env` |
| Code GLM profile | `~/.hermes/profiles/code-glm/config.yaml` |
| Local eBook profile | `~/.hermes/profiles/ebook-local/config.yaml` |
| Agency plugin | `~/.hermes/plugins/hermes-agency-workers/__init__.py` |
| VTT script | `~/.hermes/bin/vtt_local_optimized.py` |
| eBook script | `~/.hermes/bin/ebook_translate_local.sh` |
| Infographic GLM script | `~/.hermes/bin/infographic_glm.sh` |
| Queue/job state | `~/.hermes/cache/hermes-agency-workers/jobs/` |
| Job logs | `~/.hermes/logs/hermes-agency-workers/` |

## Profiles

### Existing Telegram Profile

The existing Telegram Hermes profile is the default profile. It was preserved.

Keep:

- Existing Telegram token/key.
- Existing gateway launchd service.
- Existing `config.yaml` gateway settings.

Do not:

- Do not run `hermes gateway setup` unless intentionally rebuilding Telegram.
- Do not paste or print `.env` contents.

### `code-glm`

Purpose:

- `/code`
- `/infographic_last`

Configuration:

- Provider: `openrouter`
- Model: `z-ai/glm-5.2`
- No fallback providers.

Secret rule:

- The OpenRouter key stays in auth/env handling, not inside Telegram profile and not inside `ebook-local`.

### `ebook-local`

Purpose:

- Local `/ebook` translation.

Configuration:

- Provider: `custom`
- Base URL: `http://127.0.0.1:11434/v1`
- Model: `qwen3:8b`
- No fallback providers.

Secret rule:

- No OpenRouter key belongs here.

## Telegram Commands

### Heavy Local Queue

`/ebook` and `/vtt` share a one-at-a-time local queue. You can send multiple books/audio files at once; Hermes accepts all tasks and runs them sequentially.

```text
/ebook /path/book1.epub
/ebook /path/book2.epub
/vtt /path/audio.mp3 --model small --language zh
```

Expected behavior:

```text
book1 running
book2 queued position 2
audio queued position 3
```

Queue controls:

```text
/agency_queue
/agency_job <job-id>
/agency_cancel <job-id>
/agency_pause
/agency_resume
```

### Optimized eBook Output

Recommended EPUB command:

```text
/ebook /path/book.epub /path/book.zh.epub "Traditional Chinese" --style comfortable
```

MOBI/AZW3 command:

```text
/ebook /path/book.epub /path/book.zh.mobi "Traditional Chinese" --format mobi
```

Current status:

- EPUB rebuild is supported locally.
- MOBI/AZW3 requires Calibre `ebook-convert`.
- This machine did not expose `ebook-convert` when checked, so MOBI/AZW3 will be skipped until Calibre is installed.

The optimized EPUB path:

- Opens original EPUB as a zip.
- Preserves images/assets/fonts.
- Translates text inside XHTML files.
- Injects Chinese reading CSS.
- Updates language metadata.
- Repackages a valid EPUB with correct `mimetype` handling.

Reading CSS improves:

- Line height.
- Paragraph spacing.
- 2em paragraph indent.
- Heading spacing.
- Image sizing and centering.
- Blockquote, list, table, and code layout.

### VTT

```text
/vtt /path/audio-or-video.mp3 --model small --language zh
```

Progress reports include:

- Model.
- Backend.
- Device.
- Duration when `ffprobe` is available.
- ETA estimate.
- Output `.vtt` path.

### GLM Work

```text
/code <task>
/infographic_last [transcript-path]
```

These use `code-glm` with OpenRouter GLM 5.2.

## Backup Status

Latest private setup backup:

```text
<repo>/backups/hermes-setup-YYYYMMDD-HHMMSS.zip
```

Properties:

- Size: `993M`
- Permissions: `600`
- Integrity check: `zip test OK`
- Includes `.env` when present.
- Do not commit this zip to GitHub.

## GitHub-Safe Files

Commit these:

- `docs/HERMES_AGENCY_OPTIMIZED_SETUP.md`
- `skills/hermes-agency-setup/`
- `hermes_agency_upgrade/scripts/`
- `hermes_agency_upgrade/plugins/hermes-agency-workers/`
- `hermes_agency_upgrade/profiles/` if they contain no secrets.
- `.gitignore`

Do not commit these:

- `backups/`
- `.env`
- `auth.json`
- API keys/tokens.
- `~/.hermes/sessions`
- `~/.hermes/logs`
- `~/.hermes/cache`

## Next-Time Rebuild Checklist

1. Restore or install Hermes normally.
2. Put secrets into masked terminal or private `.env`; never commit them.
3. Install profiles:
   - `code-glm`
   - `ebook-local`
4. Install plugin:
   - `hermes-agency-workers`
5. Install scripts:
   - `vtt_local_optimized.py`
   - `ebook_translate_local.sh`
   - `infographic_glm.sh`
6. Enable plugin in the existing default Telegram profile.
7. Restart gateway, do not run gateway setup unless rebuilding Telegram.
8. Verify:
   - `hermes gateway status`
   - plugin command discovery
   - `/agency_queue` from Telegram
   - dummy EPUB rebuild if changing eBook code

## Validation Commands

No-secret syntax checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import pathlib; p=pathlib.Path.home()/".hermes/plugins/hermes-agency-workers/__init__.py"; compile(p.read_text(encoding="utf-8"),str(p),"exec"); print("plugin syntax ok")'
bash -n ~/.hermes/bin/ebook_translate_local.sh
```

Gateway status:

```bash
hermes gateway status
```

Plugin discovery:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import pathlib, sys; sys.path.insert(0,str(pathlib.Path.home()/".hermes/hermes-agent")); from hermes_cli.plugins import discover_plugins, get_plugin_command_handler; discover_plugins(); names=["ebook","vtt","agency-queue","agency-job","agency-cancel","agency-pause","agency-resume"]; print({name: bool(get_plugin_command_handler(name)) for name in names})'
```
