#!/usr/bin/env bash
set -euo pipefail

deepswe_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
deepswe_runtime="$deepswe_root/.runtime/deepswe"
deepswe_uv=${DEEPSWE_UV:-uv}
deepswe_python=${DEEPSWE_PYTHON:-python3.12}
deepswe_index=${DEEPSWE_PACKAGE_INDEX:-https://pypi.org/simple}
deepswe_pin=b26211ed2bbee6d145f6b8c5b80e39f18b296f08
deepswe_archive_sha=38996919bf68f9b4d71f67c76a8b5dae9a300df1055a3a98a6429b93b957a6d5
deepswe_binary_sha=0bc6c7766773c915b9c5dfe026c5e971700304ebba688f6e528c8ab794e989e0

test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
command -v "$deepswe_uv" >/dev/null
"$deepswe_python" --version
umask 077
mkdir -p "$deepswe_runtime/bin" "$deepswe_runtime/downloads"

if ! test -d "$deepswe_runtime/pier"; then
    git clone https://github.com/datacurve-ai/pier.git "$deepswe_runtime/pier"
    git -C "$deepswe_runtime/pier" checkout --detach "$deepswe_pin"
fi
test "$(git -C "$deepswe_runtime/pier" rev-parse HEAD)" = "$deepswe_pin"
test -z "$(git -C "$deepswe_runtime/pier" status --porcelain)"

if ! test -x "$deepswe_runtime/venv/bin/python"; then
    "$deepswe_uv" venv --python "$deepswe_python" "$deepswe_runtime/venv"
fi
"$deepswe_uv" export --project "$deepswe_runtime/pier" --frozen --quiet \
    --format requirements-txt --no-emit-project --no-header \
    --output-file "$deepswe_runtime/requirements.lock.txt" >/dev/null
"$deepswe_uv" pip sync --python "$deepswe_runtime/venv/bin/python" \
    --require-hashes --index-url "$deepswe_index" \
    "$deepswe_runtime/requirements.lock.txt"
"$deepswe_uv" pip install --python "$deepswe_runtime/venv/bin/python" \
    --no-deps --index-url "$deepswe_index" "$deepswe_runtime/pier"

deepswe_archive="$deepswe_runtime/downloads/bitfun-0.2.15.tar.gz"
if ! test -f "$deepswe_archive"; then
    curl --fail --location --retry 2 --connect-timeout 15 \
        https://github.com/GCWing/OpenBitFun/releases/download/v0.2.15/bitfun-cli-0.2.15-x86_64-unknown-linux-gnu.tar.gz \
        --output "$deepswe_archive"
fi
printf '%s  %s\n' "$deepswe_archive_sha" "$deepswe_archive" | sha256sum --check -
if ! test -e "$deepswe_runtime/bin/bitfun"; then
    tar -xzf "$deepswe_archive" --no-same-owner -C "$deepswe_runtime/downloads"
    cp "$deepswe_runtime/downloads/bitfun-cli-0.2.15-x86_64-unknown-linux-gnu/bitfun" \
        "$deepswe_runtime/bin/bitfun"
fi
printf '%s  %s\n' "$deepswe_binary_sha" "$deepswe_runtime/bin/bitfun" | sha256sum --check -
if ! test -e "$deepswe_runtime/bin/uv"; then
    cp "$(command -v "$deepswe_uv")" "$deepswe_runtime/bin/uv"
fi
"$deepswe_runtime/bin/bitfun" --version
"$deepswe_runtime/bin/uv" --version
"$deepswe_runtime/venv/bin/python" -m pip --version 2>/dev/null || true
"$deepswe_uv" pip freeze --python "$deepswe_runtime/venv/bin/python" \
    > "$deepswe_runtime/installed-requirements.txt"
sha256sum "$deepswe_runtime/bin/uv" "$deepswe_runtime/bin/bitfun" \
    "$deepswe_runtime/pier/uv.lock" > "$deepswe_runtime/installed.sha256"
printf 'Runtime ready: %s\n' "$deepswe_runtime"
