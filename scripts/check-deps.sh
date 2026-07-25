#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CXL_REQUIRED_COMMANDS:-}" ]]; then
	read -r -a required_commands <<<"${CXL_REQUIRED_COMMANDS}"
else
	required_commands=(
		bash
		git
		make
		gcc
		g++
		cmake
		ninja
		meson
		pkg-config
		python3
		riscv64-linux-gnu-gcc
		riscv64-linux-gnu-ld
		riscv64-linux-gnu-readelf
		dtc
		mke2fs
		debugfs
	)
fi

missing=()
for command_name in "${required_commands[@]}"; do
	if ! command -v "${command_name}" >/dev/null 2>&1; then
		missing+=("${command_name}")
	fi
done

if ((${#missing[@]})); then
	printf 'missing required host command(s):' >&2
	printf ' %s' "${missing[@]}" >&2
	printf '\n' >&2
	printf '%s\n' \
		'Ubuntu/Debian hint: install build-essential cmake ninja-build meson pkg-config python3 gcc-riscv64-linux-gnu binutils-riscv64-linux-gnu device-tree-compiler e2fsprogs' \
		>&2
	exit 1
fi
