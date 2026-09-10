#!/bin/zsh
cd /Users/marc/odysseus
export APP_PORT=9001
exec /Users/marc/odysseus/.venv/bin/python -m uvicorn app:app \
  --host 0.0.0.0 \
  --port 9001
