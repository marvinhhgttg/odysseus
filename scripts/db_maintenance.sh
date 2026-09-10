#!/bin/zsh
set -e
DB="/Users/marc/odysseus/data/app.db"
cp "$DB" "$DB.pre-vacuum-$(date +%Y%m%d)"
sqlite3 "$DB" "VACUUM;"
find /Users/marc/odysseus/data -name 'app.db.pre-vacuum-*' -mtime +14 -delete
