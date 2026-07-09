# Current Hermes Agency Upgrade Map

## Optimized Parts

| Area | Current behavior |
| --- | --- |
| Telegram profile | Existing default Telegram profile preserved |
| Worker profile: code-glm | OpenRouter provider, model `z-ai/glm-5.2`, used by `/code` and `/infographic_last` |
| Worker profile: ebook-local | Custom provider, base URL `http://127.0.0.1:11434/v1`, model `qwen3:8b`, used by `/ebook` |
| Queue | `/ebook` and `/vtt` share one local heavy queue and run one by one |
| eBook output | Optimized EPUB rebuild, original EPUB images/assets preserved, Chinese reading CSS injected |
| MOBI/AZW3 | Attempted only when Calibre `ebook-convert` exists |
| VTT | Local STT with progress, ETA, model/backend/runtime reporting |
| Secrets | Child worker env strips API keys/tokens before subprocess runs |
| Backup | Private setup zip exists locally; GitHub backup should use docs/source only |

## Live File Paths

```text
~/.hermes/config.yaml
~/.hermes/.env
~/.hermes/profiles/code-glm/config.yaml
~/.hermes/profiles/ebook-local/config.yaml
~/.hermes/plugins/hermes-agency-workers/__init__.py
~/.hermes/bin/ebook_translate_local.sh
~/.hermes/bin/vtt_local_optimized.py
~/.hermes/bin/infographic_glm.sh
```

## Telegram Commands

```text
/ebook <input.epub|txt|md> [output.epub|mobi|azw3] [target language] [--format epub|mobi|azw3|both] [--style comfortable|compact]
/vtt <audio-or-video-path> [--model small] [--language zh]
/code <prompt>
/infographic_last [transcript-path]
/agency_queue
/agency_job <job-id>
/agency_cancel <job-id>
/agency_pause
/agency_resume
```

## Private Backup

Latest known private backup:

```text
<repo>/backups/hermes-setup-YYYYMMDD-HHMMSS.zip
```

Do not commit it to GitHub.
