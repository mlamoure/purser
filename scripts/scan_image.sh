#!/usr/bin/env bash
# Scan the exact native image (or registry digest) with a fresh advisory DB.
set -euo pipefail
image=${1:?usage: scan_image.sh IMAGE [REPORT_DIRECTORY]}
reports=${2:-security-reports}
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$reports"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
case "$(uname -m)" in
  x86_64) arch=64bit; checksum=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf ;;
  aarch64|arm64) arch=ARM64; checksum=ab9d51f86e6a5fb936f605faff6b593ac51bc378fff4d3e8b9d58358b9c367c3 ;;
  *) echo 'Unsupported scanner architecture' >&2; exit 1 ;;
esac
curl --fail --silent --show-error --location --retry 3 \
  "https://github.com/aquasecurity/trivy/releases/download/v0.76.2/trivy_0.76.2_Linux-${arch}.tar.gz" \
  --output "$scratch/trivy.tar.gz"
echo "$checksum  $scratch/trivy.tar.gz" | sha256sum --check --status
tar -xzf "$scratch/trivy.tar.gz" -C "$scratch" trivy
"$scratch/trivy" image --download-db-only
# Keep the full report, including exceptions and lower severities, for review.
"$scratch/trivy" image --skip-db-update --scanners vuln --ignorefile /dev/null \
  --format json --output "$reports/image.json" "$image"
# An exception must never hide a newly available fix for that same version.
"$scratch/trivy" image --skip-db-update --scanners vuln --severity HIGH,CRITICAL \
  --ignorefile /dev/null --ignore-unfixed --exit-code 1 "$image"
"$scratch/trivy" convert --format table --severity HIGH,CRITICAL \
  --ignorefile "$root/.trivyignore.yaml" --exit-code 1 "$reports/image.json"
