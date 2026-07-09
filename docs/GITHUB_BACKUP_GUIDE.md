# GitHub Backup Guide

Use GitHub for the reusable setup pack, not for live secrets.

## Safe To Commit

```text
.gitignore
docs/
skills/
hermes_agency_upgrade/scripts/
hermes_agency_upgrade/plugins/
hermes_agency_upgrade/profiles/
```

Before committing profiles, confirm they contain provider/model/base URL settings only, not keys.

## Never Commit

```text
.env
auth.json
backups/
*.zip
*.key
*.pem
*.token
sessions/
logs/
cache/
```

## Suggested Git Flow

```bash
git status --short
git add .gitignore docs skills hermes_agency_upgrade
git status --short
git commit -m "Document Hermes agency optimized setup"
```

Push only after checking `git diff --cached` has no secrets.

## Private Backup

Keep private setup zips locally or in encrypted storage. The zip can include `.env`, so treat it as secret.

Private backup path pattern:

```text
<repo>/backups/hermes-setup-YYYYMMDD-HHMMSS.zip
```

## Public Repo Restore Pattern

1. Clone repo.
2. Review `docs/HERMES_AGENCY_OPTIMIZED_SETUP.md`.
3. Copy scripts/plugin/profile templates into `~/.hermes`.
4. Add secrets manually in a masked terminal.
5. Restart existing gateway.
6. Verify commands.
