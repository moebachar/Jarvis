#!/usr/bin/env sh
# Install a heavy set of wheels (torch + CUDA libs + coqui-tts + onnxruntime-gpu) over a network
# that THROTTLES long transfers — the thing that keeps killing `pip install` here.
#
# Why pip alone fails: pip has no resume. When the link shapes a big wheel down to ~80 kB/s and the
# read times out, pip retries FROM BYTE ZERO, so a 600 MB wheel never completes. curl resumes with
# an HTTP Range request, and each fresh connection gets another fast burst before the shaper kicks
# in — so even a 1.8 GB file lands in a handful of cycles.
#
# Strategy (no hardcoded versions — pip decides, curl fetches):
#   1. Resolve the exact wheel set with `pip --dry-run --report` (metadata only: small requests
#      that survive throttling — no wheel bytes are downloaded here).
#   2. curl every resolved URL into /wheels with resume (-C -) + a speed floor that abandons a
#      throttled connection and retries on a fresh one. /wheels is a BuildKit cache mount, so a
#      build that dies mid-download resumes on the next run instead of restarting.
#   3. Install strictly from the local wheels (--no-index): no network, no version drift.
#      A networked --find-links fallback covers anything the resolver couldn't express as a wheel.
set -e

INDEX_URL="$1"; shift            # torch wheel index (e.g. .../whl/cu121)
# remaining args = top-level specs to resolve + install (torch, torchaudio, coqui-tts, ...)

: "${CURL_OPTS:=-fL --retry 100 --retry-delay 5 --retry-all-errors --speed-limit 100000 --speed-time 20 -C -}"
EXTRA_INDEX="https://pypi.org/simple"
mkdir -p /wheels

echo "[wheels] resolving the wheel set (metadata only) for: $*"
pip install --dry-run --report /tmp/wheel-plan.json --progress-bar off \
    --index-url "$INDEX_URL" --extra-index-url "$EXTRA_INDEX" "$@"

# Pull every download URL the resolver chose (wheels AND any sdists).
python - <<'PY' > /tmp/wheel-urls.txt
import json
plan = json.load(open("/tmp/wheel-plan.json"))
for item in plan.get("install", []):
    url = (item.get("download_info") or {}).get("url", "")
    if url.startswith("http"):
        print(url)
PY

total=$(grep -c . /tmp/wheel-urls.txt || true)
echo "[wheels] $total files to fetch into /wheels"

cd /wheels
while IFS= read -r url; do
    [ -z "$url" ] && continue
    fname=${url##*/}                       # basename
    fname=${fname%%\?*}                     # drop any ?query
    case "$fname" in *%2B*) fname=$(printf '%s' "$fname" | sed 's/%2B/+/g') ;; esac  # +cu121
    if [ -f "$fname" ]; then
        echo "[wheels] have    $fname"
        continue
    fi
    echo "[wheels] fetching $fname"
    # Download to .part and only rename on success, so a half file is never mistaken for complete
    # (curl -C - resumes the .part across retries and across whole failed builds via the cache mount).
    curl $CURL_OPTS -o "$fname.part" "$url"
    mv "$fname.part" "$fname"
done < /tmp/wheel-urls.txt

echo "[wheels] installing from local wheels (offline)"
if ! pip install --no-index --find-links /wheels "$@"; then
    echo "[wheels] offline install incomplete — filling gaps from the network (big wheels reused from /wheels)"
    pip install --find-links /wheels --index-url "$INDEX_URL" --extra-index-url "$EXTRA_INDEX" "$@"
fi
echo "[wheels] done"
