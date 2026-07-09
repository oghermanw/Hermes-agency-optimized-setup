# Hermes Agency Optimized Setup

GitHub-safe setup pack for the optimized Hermes Telegram agency configuration.

This repo documents each optimized part: Telegram profile, `code-glm`, `ebook-local`, `/ebook`, `/vtt`, queue, EPUB output, secret safety, and backup rules.

It stages the reusable parts of the setup only. It must not contain live `.env`, Telegram tokens, OpenRouter keys, auth files, logs, sessions, cache files, or private backup zip files.

Start here:

- [Optimized setup map](docs/HERMES_AGENCY_OPTIMIZED_SETUP.md)
- [GitHub backup guide](docs/GITHUB_BACKUP_GUIDE.md)
- [Reusable Codex skill](skills/hermes-agency-setup/SKILL.md)

Main optimized features:

- Preserves existing Hermes Telegram gateway/profile.
- Adds `code-glm` worker profile for `/code` and `/infographic_last`.
- Adds `ebook-local` worker profile for local Ollama `qwen3:8b`.
- Adds `/ebook` and `/vtt` local heavy-task queue, one job at a time.
- Rebuilds optimized Chinese-reading EPUB output while preserving original EPUB images/assets.
- Keeps secret handling explicit and no-secret by default.
