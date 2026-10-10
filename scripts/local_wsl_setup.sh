#!/usr/bin/env bash
# User-scoped Linux tools and locked dependencies; preserves Windows environments.
set -euo pipefail
test "$(id -u)" != 0
umask 077
em_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
em_state="${XDG_DATA_HOME:-$HOME/.local/share}/enterprise-mind"
em_tools="$em_state/tools"
em_downloads="$em_tools/downloads"
mkdir -p "$em_downloads" "$em_tools/bin"
if [[ ! -x "$em_tools/bin/uv" ]]; then
  curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 90 \
    https://astral.sh/uv/0.12.21/install.sh -o "$em_downloads/uv-install.sh"
  UV_UNMANAGED_INSTALL="$em_tools/bin" sh "$em_downloads/uv-install.sh"
fi
em_node=node-v24.16.0-linux-x64
if [[ ! -x "$em_tools/$em_node/bin/node" ]]; then
  curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 180 \
    "https://nodejs.org/dist/v24.16.0/$em_node.tar.xz" -o "$em_downloads/$em_node.tar.xz"
  curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 60 \
    https://nodejs.org/dist/v24.16.0/SHASUMS256.txt -o "$em_downloads/node-shasums.txt"
  grep "  $em_node.tar.xz$" "$em_downloads/node-shasums.txt" > "$em_downloads/node-selected.sha256"
  (cd "$em_downloads" && sha256sum --check --strict node-selected.sha256)
  tar -xJf "$em_downloads/$em_node.tar.xz" -C "$em_tools"
fi
test "$("$em_tools/$em_node/bin/node" --version)" = v24.16.0
export PATH="$em_tools/$em_node/bin:$em_tools/bin:/usr/local/bin:/usr/bin:/bin"
export UV_PROJECT_ENVIRONMENT="$em_state/backend-venv"
uv sync --locked --python "$(cat "$em_root/.python-version")" --project "$em_root/backend"
python3 "$em_root/scripts/local_wsl_prepare.py"
em_lock_hash="$(sha256sum "$em_root/frontend/package.json" "$em_root/frontend/package-lock.json" | sha256sum | cut -d' ' -f1)"
em_previous="$(cat "$em_state/frontend/.installed-lock" 2>/dev/null || true)"
if [[ "$em_previous" != "$em_lock_hash" || ! -d "$em_state/frontend/node_modules" ]]; then
  (cd "$em_state/frontend" && npm ci --include=dev --no-audit --no-fund)
  printf '%s\n' "$em_lock_hash" > "$em_state/frontend/.installed-lock"
fi
echo 'Ubuntu EM tools and locked dependencies ready.'
