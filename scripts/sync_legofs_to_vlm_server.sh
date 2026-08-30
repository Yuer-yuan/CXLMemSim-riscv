#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${LEGOFS_VLM_REMOTE:-vlm-server:/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv/}"

# Source is maintained locally, while cross-toolchains and build products are
# machine-local artifacts whose generated wrappers contain absolute prefixes.
# Never copy those artifacts across hosts and never delete remote build state.
exec rsync -az \
	--exclude='.git' \
	--exclude='/.agents/' \
	--exclude='/.cxl-bi-tools/' \
	--exclude='/target/' \
	--exclude='/out/' \
	--exclude='/build/' \
	--exclude='/components/legofs/target/' \
	--exclude='/components/legofs/models/vd-metadata-pipeline/target/' \
	--exclude='/components/qemu/build/' \
	"$ROOT/" "$REMOTE"
