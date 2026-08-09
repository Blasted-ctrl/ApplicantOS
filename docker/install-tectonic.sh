#!/bin/sh
# Install the `tectonic` LaTeX engine into the worker image.
#
# `Settings.pdf_engine` defaults to `latex` and `Settings.latex_binary` to `tectonic`, so a
# worker without this binary renders no resume at all — `render_resume` fails and every
# application in flight routes to review. It therefore has to be installed, and it cannot be
# installed with apt: Debian ships no `tectonic` package in bookworm (checked against
# packages.debian.org; only the TeX Live metapackages exist, and a usable `texlive` subset is
# an 800MB+ addition to the image for a document that Tectonic typesets in ~1s from a 60MB
# static binary).
#
# Upstream publishes statically linked musl builds, which run unchanged on a glibc Debian
# image and depend on nothing in the base layer.
#
# Usage:
#     docker/install-tectonic.sh <version> [sha256]
#
# The optional second argument is the SHA-256 of the tarball for this architecture. When it
# is supplied the download is verified and a mismatch aborts the build; when it is omitted
# the script prints an explicit, greppable warning rather than pretending it verified
# anything. Pass it through the image's TECTONIC_SHA256 build argument for a reproducible,
# supply-chain-checked build.

set -eu

VERSION="${1:?usage: install-tectonic.sh <version> [sha256]}"
EXPECTED_SHA256="${2:-}"

# Upstream tags releases as `tectonic@<version>`; the `@` is percent-encoded in the URL path.
BASE_URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${VERSION}"
INSTALL_DIR="/usr/local/bin"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# Debian's architecture names are not Rust's target triples, and only these two have upstream
# Linux builds. Anything else should fail loudly here rather than produce an image whose
# resume rendering is quietly broken.
DEBIAN_ARCH="$(dpkg --print-architecture)"
case "${DEBIAN_ARCH}" in
    amd64) TRIPLE="x86_64-unknown-linux-musl" ;;
    arm64) TRIPLE="aarch64-unknown-linux-musl" ;;
    *)
        echo "install-tectonic: no upstream Tectonic build for Debian arch '${DEBIAN_ARCH}'." >&2
        echo "install-tectonic: build on amd64/arm64, or set PDF_ENGINE=html to use the" >&2
        echo "install-tectonic: pure-Python reportlab renderer instead." >&2
        exit 1
        ;;
esac

TARBALL="tectonic-${VERSION}-${TRIPLE}.tar.gz"
echo "install-tectonic: fetching ${TARBALL}"
curl --fail --location --silent --show-error --retry 3 --retry-delay 2 \
    --output "${WORK_DIR}/${TARBALL}" "${BASE_URL}/${TARBALL}"

if [ -n "${EXPECTED_SHA256}" ]; then
    echo "${EXPECTED_SHA256}  ${WORK_DIR}/${TARBALL}" | sha256sum --check --strict -
    echo "install-tectonic: checksum verified"
else
    echo "install-tectonic: WARNING - no TECTONIC_SHA256 supplied, tarball NOT verified." >&2
    echo "install-tectonic: WARNING - pass --build-arg TECTONIC_SHA256=... for a" >&2
    echo "install-tectonic: WARNING - reproducible, verified build." >&2
fi

tar --extract --gzip --file "${WORK_DIR}/${TARBALL}" --directory "${WORK_DIR}"
install -m 0755 "${WORK_DIR}/tectonic" "${INSTALL_DIR}/tectonic"

# Prove the binary runs in this image before the layer is committed. A tarball for the wrong
# libc extracts fine and only fails at the first render, hours later, inside a Celery task.
"${INSTALL_DIR}/tectonic" --version
