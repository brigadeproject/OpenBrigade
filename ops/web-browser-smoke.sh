#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${BRIGADE_WEB_SMOKE_URL:-http://127.0.0.1:58080}}"
OUT_DIR="${2:-${BRIGADE_WEB_SMOKE_OUT:-/tmp/openbrigade-web-smoke}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$OUT_DIR"

BROWSER="${BRIGADE_BROWSER:-}"
if [[ -z "$BROWSER" ]]; then
  for candidate in google-chrome chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      BROWSER="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$BROWSER" ]]; then
  echo "No Chromium-compatible browser found. Set BRIGADE_BROWSER." >&2
  exit 1
fi

curl_args=(-fsS)
if [[ -n "${BRIGADE_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${BRIGADE_TOKEN}")
fi

curl "${curl_args[@]}" "$BASE_URL/healthz" > "$OUT_DIR/healthz.json"
curl "${curl_args[@]}" "$BASE_URL/api/cockpit" > "$OUT_DIR/cockpit.json"
curl "${curl_args[@]}" "$BASE_URL/api/models" > "$OUT_DIR/models.json"

chrome_flags=(
  --headless
  --no-sandbox
  --disable-gpu
  --disable-dev-shm-usage
  --hide-scrollbars
  --disable-background-networking
)

dump_dom() {
  local url="$1"
  local size="$2"
  local output="$3"
  "$BROWSER" "${chrome_flags[@]}" \
    --window-size="$size" \
    --virtual-time-budget=8000 \
    --dump-dom "$url" > "$output"
}

take_screenshot() {
  local url="$1"
  local size="$2"
  local output="$3"
  "$BROWSER" "${chrome_flags[@]}" \
    --window-size="$size" \
    --virtual-time-budget=8000 \
    --screenshot="$output" "$url" >/dev/null
}

assert_contains() {
  local needle="$1"
  local file="$2"
  if ! grep -Fq "$needle" "$file"; then
    echo "Expected '$needle' in $file" >&2
    exit 1
  fi
}

assert_image_nonblank() {
  local file="$1"
  local bytes
  bytes="$(wc -c < "$file")"
  if [[ "$bytes" -lt 12000 ]]; then
    echo "Screenshot is unexpectedly small: $file ($bytes bytes)" >&2
    exit 1
  fi
  if command -v identify >/dev/null 2>&1; then
    local colors
    colors="$(identify -format "%k" "$file")"
    if [[ "$colors" -lt 8 ]]; then
      echo "Screenshot appears blank: $file ($colors colors)" >&2
      exit 1
    fi
  fi
}

if [[ -n "${BRIGADE_TOKEN:-}" ]]; then
  node "$SCRIPT_DIR/web-browser-smoke-cdp.mjs" "$BROWSER" "$BASE_URL" "$OUT_DIR"
else
  dump_dom "$BASE_URL/?view=cockpit" "1440,1000" "$OUT_DIR/cockpit-dom.html"
  dump_dom "$BASE_URL/?view=ops" "1440,1000" "$OUT_DIR/ops-dom.html"
  dump_dom "$BASE_URL/?view=proposals" "1440,1000" "$OUT_DIR/proposals-dom.html"

  take_screenshot "$BASE_URL/?view=cockpit" "1440,1000" "$OUT_DIR/cockpit-desktop.png"
  take_screenshot "$BASE_URL/?view=ops" "1440,1000" "$OUT_DIR/ops-desktop.png"
  take_screenshot "$BASE_URL/?view=proposals" "1440,1000" "$OUT_DIR/proposals-desktop.png"
  take_screenshot "$BASE_URL/?view=cockpit" "390,844" "$OUT_DIR/cockpit-mobile.png"
fi

assert_contains "OpenBrigade" "$OUT_DIR/cockpit-dom.html"
assert_contains "Cockpit" "$OUT_DIR/cockpit-dom.html"
# "Models Available" moved to the Telemetry tab; assert cockpit-only panels.
assert_contains "Task Queue" "$OUT_DIR/cockpit-dom.html"
assert_contains "Talk to Orchestrator" "$OUT_DIR/cockpit-dom.html"
assert_contains "Ops Room" "$OUT_DIR/ops-dom.html"
assert_contains "Approval Workbench" "$OUT_DIR/proposals-dom.html"

assert_image_nonblank "$OUT_DIR/cockpit-desktop.png"
assert_image_nonblank "$OUT_DIR/ops-desktop.png"
assert_image_nonblank "$OUT_DIR/proposals-desktop.png"
assert_image_nonblank "$OUT_DIR/cockpit-mobile.png"

echo "web browser smoke passed: $OUT_DIR"
