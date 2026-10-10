#!/usr/bin/env bash
# Install the locked PG18 pgvector artifacts already present on this machine.
# No PG package upgrade, database alteration, container restart or network pull.
set -euo pipefail
test "$(id -u)" = 0
test "$(pg_config --version)" = 'PostgreSQL 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)'
source_container=em-p10-20220922-p1008-pgvolume-postgres-1
expected_image=sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a
test "$(docker inspect --format '{{.Image}}' "$source_container")" = "$expected_image"
em_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
stage="$em_root/.local/runtime/pgvector-artifacts"
mkdir -p "$stage"
docker cp "$source_container:/usr/lib/postgresql/18/lib/vector.so" "$stage/vector.so"
docker cp "$source_container:/usr/share/postgresql/18/extension/vector.control" "$stage/vector.control"
docker cp "$source_container:/usr/share/postgresql/18/extension/vector--0.8.6.sql" "$stage/vector--0.8.6.sql"
grep -q "default_version = '0.8.6'" "$stage/vector.control"
if ldd "$stage/vector.so" | grep -q 'not found'; then
  echo 'Extension library dependency missing.' >&2
  exit 1
fi
for file in vector.so vector.control vector--0.8.6.sql; do
  target="$(pg_config --sharedir)/extension/$file"
  if [[ "$file" = vector.so ]]; then target="$(pg_config --pkglibdir)/$file"; fi
  if [[ -e "$target" ]]; then
    cmp "$stage/$file" "$target"
  else
    install -m 0644 "$stage/$file" "$target"
  fi
done
sha256sum "$stage/vector.so" "$stage/vector.control" "$stage/vector--0.8.6.sql" > "$stage/sha256.txt"
echo 'pgvector 0.8.6 artifacts installed for native PostgreSQL 18; databases unchanged.'
