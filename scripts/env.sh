# shellcheck shell=sh
# Usage: . scripts/env.sh
EIM_ROOT="${EIM_ROOT:-$HOME/.espressif}"
IDF_VERSION="${IDF_VERSION:-v5.5.3}"
ACTIVATE="$EIM_ROOT/tools/activate_idf_${IDF_VERSION}.sh"

if [ ! -f "$ACTIVATE" ]; then
    echo "error: $ACTIVATE not found. Run scripts/setup.sh first." >&2
    return 1 2>/dev/null || exit 1
fi

. "$ACTIVATE"
echo "ESP-IDF $IDF_VERSION activated (target: esp32s3)"
