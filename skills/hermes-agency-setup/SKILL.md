---
name: hermes-agency-setup
description: Rebuild, verify, document, or back up the user's Hermes Telegram agency setup with local eBook/VTT workers, GLM worker profiles, queue controls, and strict no-secret handling. Use when asked to restore Hermes agency, compare it to default Hermes, update Telegram commands, optimize EPUB/MOBI translation, make GitHub-safe backups, or troubleshoot this setup.
---

# Hermes Agency Setup

## Core Rules

- Preserve the existing Telegram gateway/profile unless the user explicitly requests a rebuild.
- Do not run `hermes gateway setup` during ordinary upgrades.
- Never print `.env`, auth files, Telegram tokens, OpenRouter keys, or raw environment dumps.
- When secrets are needed, stop and ask the user to enter them in a masked terminal.
- Keep `fallback_providers` unset or empty for the worker profiles.
- Keep OpenRouter credentials out of the Telegram profile and out of `ebook-local`.

## Current Architecture

Use [references/current-upgrade-map.md](references/current-upgrade-map.md) for the full component map and live file paths.

Important live paths:

- Main config: `~/.hermes/config.yaml`
- Agency plugin: `~/.hermes/plugins/hermes-agency-workers/__init__.py`
- Scripts: `~/.hermes/bin/`
- Worker profiles: `~/.hermes/profiles/code-glm/`, `~/.hermes/profiles/ebook-local/`

## Upgrade Workflow

1. Inspect existing state with no secret output.
2. Back up config files before editing.
3. Patch staged files in the workspace first.
4. Validate syntax and dry behavior.
5. Install into `~/.hermes` with explicit approval when required.
6. Restart the existing gateway only.
7. Verify gateway status and plugin command discovery.
8. Create a private setup zip if requested.

## Command Design

`/ebook` and `/vtt` must use a shared local heavy-task queue:

- One job runs at a time.
- Multiple Telegram submissions are accepted and queued.
- `/agency_queue` shows running/waiting work.
- `/agency_cancel <job-id>` cancels waiting jobs or requests stop for running jobs.
- `/agency_pause` and `/agency_resume` control the queue.

`/ebook` should rebuild optimized EPUB output:

- Preserve original EPUB images/assets/fonts.
- Translate XHTML text with local Ollama `qwen3:8b`.
- Inject Chinese reading CSS.
- Repack valid EPUB.
- Attempt MOBI/AZW3 only when Calibre `ebook-convert` exists.

`/vtt` should report:

- Backend/model/runtime.
- Duration and ETA when available.
- Final `.vtt` output path.

`/code` and `/infographic_last` use `code-glm` with OpenRouter GLM 5.2.

## Verification

Use no-secret checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import pathlib; p=pathlib.Path.home()/".hermes/plugins/hermes-agency-workers/__init__.py"; compile(p.read_text(encoding="utf-8"),str(p),"exec"); print("plugin syntax ok")'
bash -n ~/.hermes/bin/ebook_translate_local.sh
hermes gateway status
```

Check command discovery:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import pathlib, sys; sys.path.insert(0,str(pathlib.Path.home()/".hermes/hermes-agent")); from hermes_cli.plugins import discover_plugins, get_plugin_command_handler; discover_plugins(); names=["ebook","vtt","agency-queue","agency-job","agency-cancel","agency-pause","agency-resume"]; print({name: bool(get_plugin_command_handler(name)) for name in names})'
```

## GitHub Safety

Commit docs, skill files, staged scripts, plugin source, and non-secret profile templates.

Never commit:

- `.env`
- auth files
- tokens/keys
- backup zip files
- logs/sessions/cache

Use `docs/GITHUB_BACKUP_GUIDE.md` for a repo-safe checklist.
