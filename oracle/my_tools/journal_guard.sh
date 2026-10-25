#!/usr/bin/env bash
# Keeps journal/paper.jsonl under MAX_MB by truncating it in place (the bot appends,
# so it keeps writing at the new end). markout.py/dashboard.py detect the reset.
cd "$(dirname "$0")/.."
MAX_MB=${MAX_MB:-1000}
while true; do
  f=journal/paper.jsonl
  if [ -f "$f" ] && [ "$(du -m "$f" | cut -f1)" -ge "$MAX_MB" ]; then
    truncate -s 0 "$f"; echo "$(date -u +%FT%TZ) truncated $f"
  fi
  sleep 30
done
