#!/usr/bin/env bash
# Build rfi_compare as a self-contained Linux executable using PyInstaller
# inside a Linux container (cross-compiling from macOS is not possible).
#
# Usage:
#   ./build_linux.sh            # linux/amd64 (default)
#   ./build_linux.sh arm64      # linux/arm64
#
# Output: dist/linux-<arch>/rfi_compare  (single-file ELF, no Python needed
# on the target). At runtime it still expects the usual configuration:
# .env / .istari_credentials.json in the working directory.
set -euo pipefail

ARCH="${1:-amd64}"
PLATFORM="linux/${ARCH}"
OUT="dist/linux-${ARCH}"

# bullseye base = glibc 2.31; the binary runs on any distro with glibc >= 2.31
# (Ubuntu 20.04+, Debian 11+, RHEL 9+). Building on a newer base would produce
# a binary that fails with "GLIBC_x.yy not found" on older targets.
docker run --rm --platform "$PLATFORM" \
    -v "$PWD":/src -w /src \
    python:3.12-slim-bullseye \
    bash -c "
        set -e
        # bullseye is an archived release; its Release files are past their
        # Valid-Until date, so tell apt to accept them.
        apt-get -o Acquire::Check-Valid-Until=false update -qq
        apt-get install -y -qq --no-install-recommends binutils > /dev/null
        pip install --quiet --upgrade pip
        # dependencies come from pyproject.toml (poetry-core build backend)
        pip install --quiet . pyinstaller
        pyinstaller --onefile --name rfi_compare \
            --distpath '$OUT' --workpath /tmp/pyi-build --specpath /tmp \
            rfi_compare.py
    "

echo
echo "Built: $OUT/rfi_compare"
file "$OUT/rfi_compare" 2>/dev/null || true
