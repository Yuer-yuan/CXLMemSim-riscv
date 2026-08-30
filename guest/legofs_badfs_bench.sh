#!/bin/busybox sh
set -eu

cmdline_value()
{
	prefix="$1"
	for word in $(cat /proc/cmdline); do
		case "$word" in
		"$prefix"*) printf '%s\n' "${word#"$prefix"}"; return 0 ;;
		esac
	done
	return 1
}

serving_transport="$(cmdline_value io500.serving_transport= 2>/dev/null || printf 'legacy\n')"
case "$serving_transport" in legacy) exec /payload/bin/badfs-bench.real "$@" ;; cxl) ;; *) exit 64 ;; esac

client_count="$(cmdline_value io500.client_count= 2>/dev/null || printf '0\n')"
case "$client_count" in ''|*[!0-9]*) exit 64 ;; esac
[ "$client_count" -lt 64 ] || exit 64

# IO500 ranks retain lanes [0, client_count).  Post-run inspection uses the
# next lane so diagnostics never overwrite a still-published rank generation.
export BADFS_SERVING_TRANSPORT=cxl
export BADFS_SERVING_MAX_CLIENTS=64
export BADFS_CLIENT_ENDPOINT_ID="$client_count"
export BADFS_LIFECYCLE_REGION_SIZE=68719476736
export BADFS_SERVERS="$(cat /run/server-addresses)"
export BADFS_LIFECYCLE_DEVICES="$(cat /run/lifecycle-devices)"
export BADFS_CXL_MAP_ALIGNMENT="$(cat /run/dax-align)"
export BADFS_LIFECYCLE_DIRECT_FINAL=1
export BADFS_LIFECYCLE_DIRECT_REQUIRED=1
export BADFS_LIFECYCLE_DIRECT_READ=1
export BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1
export BADFS_LIFECYCLE_COHERENT_PUBLICATION=1
export BADFS_TRACK_OPEN_SET=1
export BADFS_DISABLE_FABRIC_MMAP=0
export BADFS_FABRIC_STAGED_IO=0

exec /payload/bin/badfs-bench.real "$@"
