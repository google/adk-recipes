#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Run terraform with the given arguments. When no working terraform is on PATH
# (Cloud Shell ships only a stub), install a pinned release into ~/.local/bin.
# The zip is checked against the SHA-256 published by HashiCorp in
# terraform_<version>_SHA256SUMS before it is unzipped.

set -euo pipefail

VERSION=1.16.6
BIN="$HOME/.local/bin/terraform"

sha256_of() {
    case "$1" in
        linux_amd64) echo 38034b38d03096399a7a1264a14561a8029b2b496fd1f169452656e53968f1b7 ;;
        linux_arm64) echo a750b5a11038f165846b2658c40c88cd7070aa67a61bab431a14ba27928ea9ea ;;
        darwin_amd64) echo 794a36c3f7ebf6c58f2089c226068a539d1705ba358a5679b51671c8dc7ebc7b ;;
        darwin_arm64) echo 85e1bc78ab45bdc5f82a7bac3929e37d072834a584d22583a2498dc67bb624cd ;;
        *) return 1 ;;
    esac
}

# A subshell, so the EXIT trap removes the download before the exec below.
install_terraform() (
    local os arch platform expected actual tmp
    os=$(uname -s | tr '[:upper:]' '[:lower:]')
    case "$(uname -m)" in
        x86_64 | amd64) arch=amd64 ;;
        aarch64 | arm64) arch=arm64 ;;
        *) arch=$(uname -m) ;;
    esac
    platform="${os}_${arch}"
    if ! expected=$(sha256_of "$platform"); then
        echo "terraform is not installed and $platform is not supported by this script." >&2
        echo "Install Terraform >= 1.7: https://developer.hashicorp.com/terraform/install" >&2
        exit 1
    fi
    command -v unzip >/dev/null || { echo "Installing terraform needs unzip." >&2; exit 1; }
    echo "terraform not found: installing Terraform $VERSION into $BIN" >&2
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/tf.zip" \
        "https://releases.hashicorp.com/terraform/$VERSION/terraform_${VERSION}_${platform}.zip"
    if command -v sha256sum >/dev/null; then
        actual=$(sha256sum "$tmp/tf.zip" | cut -d' ' -f1)
    else
        actual=$(shasum -a 256 "$tmp/tf.zip" | cut -d' ' -f1)
    fi
    if [ "$actual" != "$expected" ]; then
        echo "Checksum mismatch for the Terraform download; not installing." >&2
        exit 1
    fi
    unzip -q -o "$tmp/tf.zip" terraform -d "$tmp"
    mkdir -p "$(dirname "$BIN")"
    mv "$tmp/terraform" "$BIN"
    chmod +x "$BIN"
)

# A terraform on PATH counts only if it runs: Cloud Shell has a stub that just
# prints how to install Terraform and exits 0, so init and apply "succeeded"
# without creating anything.
real_terraform() {
    command -v terraform >/dev/null || return 1
    [[ "$(terraform version 2>/dev/null)" == "Terraform v"* ]]
}

if real_terraform; then
    exec terraform "$@"
fi
[ -x "$BIN" ] || install_terraform
exec "$BIN" "$@"
