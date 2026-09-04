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
durability_token="$(cmdline_value d= 2>/dev/null || printf 'd\n')"
payload_owner_token="$(cmdline_value u= 2>/dev/null || printf 'v\n')"
case "$serving_transport" in legacy|cxl) ;; *) exit 64 ;; esac
case "$server_index" in ''|*[!0-9]*) exit 64 ;; esac
case "$server_count" in 1|2) ;; *) exit 64 ;; esac
case "$durability_token" in
d) durability_profile=d_before_v ;;
n) durability_profile=coherent_seal_no_writeback ;;
w) durability_profile=coherent_seal_needs_writeback ;;
*) exit 64 ;;
esac
case "$payload_owner_token" in
a) payload_persistence_owner=authority_bi_acquire; writer_persist_provider=msync ;;
r) payload_persistence_owner=writer_receipt; writer_persist_provider=msync ;;
R) payload_persistence_owner=writer_receipt; writer_persist_provider=riscv-zicbom-dax ;;
h) payload_persistence_owner=placement_routed; writer_persist_provider=msync ;;
H) payload_persistence_owner=placement_routed; writer_persist_provider=riscv-zicbom-dax ;;
v) payload_persistence_owner=writer_before_visibility; writer_persist_provider=msync ;;
*) exit 64 ;;
esac

export BADFS_SERVING_TRANSPORT="$serving_transport"
export BADFS_SERVING_MAX_CLIENTS=64
export BADFS_SERVER_INDEX="$server_index"
export BADFS_SERVER_COUNT="$server_count"
export BADFS_LIFECYCLE_DURABILITY_PROFILE="$durability_profile"
export BADFS_PAYLOAD_PERSISTENCE_OWNER="$payload_persistence_owner"
export BADFS_WRITER_PERSIST_PROVIDER="$writer_persist_provider"
# The QEMU functional adapter numbers one endpoint's CXL requests globally.
# Multiple guest vCPUs may allocate request IDs out of send order, pinning its
# contiguous response-ack watermark behind an unsent ID. Keep this model's CXL
# issuer on one Tokio worker, but retain multiple asynchronous dispatch slots
# on that worker: a durability boundary must not prevent an unrelated arena
# refill SQE from being admitted. Production launchers omit these overrides
# and retain the server's configurable multi-worker defaults on real CXL.
export BADFS_SERVER_RUNTIME_WORKERS=1
export BADFS_SERVING_DISPATCH_WORKERS=4
exec /payload/bin/badfs-server.real "$@"
