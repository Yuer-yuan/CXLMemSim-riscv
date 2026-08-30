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
server_index="$(cmdline_value io500.index= 2>/dev/null || printf '0\n')"
server_count="$(cmdline_value io500.server_count= 2>/dev/null || printf '1\n')"
case "$serving_transport" in legacy|cxl) ;; *) exit 64 ;; esac
case "$server_index" in ''|*[!0-9]*) exit 64 ;; esac
case "$server_count" in 1|2) ;; *) exit 64 ;; esac

export BADFS_SERVING_TRANSPORT="$serving_transport"
export BADFS_SERVING_MAX_CLIENTS=64
export BADFS_SERVER_INDEX="$server_index"
export BADFS_SERVER_COUNT="$server_count"
exec /payload/bin/badfs-server.real "$@"
