#!/usr/bin/env bash
set -euo pipefail

IDF_VERSION="${IDF_VERSION:-v5.5.3}"
EIM_ROOT="${EIM_ROOT:-$HOME/.espressif}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v eim >/dev/null 2>&1; then
    echo "error: eim not found. Install it first: yay -S eim-cli" >&2
    exit 1
fi

if [ -f "$EIM_ROOT/tools/activate_idf_${IDF_VERSION}.sh" ]; then
    echo "ESP-IDF $IDF_VERSION already installed in $EIM_ROOT"
else
    echo "Installing ESP-IDF $IDF_VERSION into $EIM_ROOT ..."
    eim install --config "$REPO_ROOT/scripts/eim-config.toml" -p "$EIM_ROOT"
fi

if [ -f "$REPO_ROOT/.gitmodules" ]; then
    echo "Syncing vendor submodules ..."
    git -C "$REPO_ROOT" submodule update --init --recursive --depth 1
fi

echo "Done. Activate with: source scripts/env.sh"
