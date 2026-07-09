#!/usr/bin/env bash
set -euo pipefail

profile="${HERMES_CODE_GLM_PROFILE:-code-glm}"
hermes_bin="${HERMES_BIN:-hermes}"
cache_dir="${HERMES_HOME:-$HOME/.hermes}/cache/infographic-last"
mkdir -p "$cache_dir"
unset OPENROUTER_API_KEY

input="${1:-}"
if [[ -z "$input" ]]; then
  input="$(find "${HERMES_HOME:-$HOME/.hermes}/sessions" -maxdepth 2 -type f \( -name '*.md' -o -name '*.txt' -o -name '*.json' \) -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1 || true)"
fi

if [[ -z "$input" || ! -f "$input" ]]; then
  echo "No transcript/session file found. Pass a transcript path as the first argument." >&2
  exit 2
fi

prepared="$cache_dir/latest-transcript-for-glm.md"
python3 - "$input" "$prepared" <<'PY'
from __future__ import annotations

import json
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2])
secret_key = re.compile(r"(api[_-]?key|token|secret|authorization|password)", re.I)


def scrub(value):
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if secret_key.search(str(k)) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
        value = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", value)
    return value


def flatten(obj, lines):
    obj = scrub(obj)
    if isinstance(obj, dict):
        role = obj.get("role") or obj.get("type") or obj.get("source") or "entry"
        content = obj.get("content") or obj.get("text") or obj.get("message")
        if isinstance(content, str) and content.strip():
            lines.append(f"## {role}\n\n{content.strip()}\n")
            return
        for value in obj.values():
            flatten(value, lines)
    elif isinstance(obj, list):
        for value in obj:
            flatten(value, lines)
    elif isinstance(obj, str) and obj.strip():
        lines.append(obj.strip() + "\n")


text = source.read_text(encoding="utf-8", errors="replace")
if source.suffix.lower() == ".json":
    try:
        payload = json.loads(text)
        lines = [f"# Extracted transcript\n\nSource: {source}\n"]
        flatten(payload, lines)
        text = "\n".join(lines)
    except json.JSONDecodeError:
        pass

dest.write_text(text, encoding="utf-8")
print(str(dest))
PY

prompt="Read the transcript file at '$prepared'. Create an infographic-ready Markdown brief: title, audience, key message, 5-9 content blocks, suggested layout, visual hierarchy, icons/visual motifs, and final production prompt. Preserve facts, redact secrets, and do not invent details."
exec "$hermes_bin" -p "$profile" -z "$prompt"
