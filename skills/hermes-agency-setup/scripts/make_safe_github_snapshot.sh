#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-$(pwd)}"
snapshot_dir="${2:-"$repo_root/github-safe-snapshot"}"

mkdir -p "$snapshot_dir"

copy_if_exists() {
  local src="$1"
  local dest="$2"
  if [ -e "$repo_root/$src" ]; then
    mkdir -p "$(dirname "$snapshot_dir/$dest")"
    cp -R "$repo_root/$src" "$snapshot_dir/$dest"
  fi
}

copy_if_exists ".gitignore" ".gitignore"
copy_if_exists "docs" "docs"
copy_if_exists "skills" "skills"
copy_if_exists "hermes_agency_upgrade/scripts" "hermes_agency_upgrade/scripts"
copy_if_exists "hermes_agency_upgrade/plugins" "hermes_agency_upgrade/plugins"
copy_if_exists "hermes_agency_upgrade/profiles" "hermes_agency_upgrade/profiles"

find "$snapshot_dir" \
  \( -name '.env' -o -name '*.env' -o -name '*.zip' -o -name '*token*' -o -name '*secret*' -o -name 'auth.json' \) \
  -print -delete

printf 'GitHub-safe snapshot created at: %s\n' "$snapshot_dir"
