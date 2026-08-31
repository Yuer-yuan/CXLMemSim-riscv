#!/usr/bin/env bash

# Resolve every declared host tool before a build starts, then expose only the
# directories containing those resolved executables.  Callers may provide an
# absolute, colon-separated LEGOFS_TOOLCHAIN_PATH when migrating to a host with
# a non-standard toolchain layout.

legofs_toolchain_activate()
{
	if (($# < 2)); then
		printf '%s\n' \
			'error: legofs_toolchain_activate requires a profile and at least one tool' \
			>&2
		return 2
	fi

	local profile=$1
	shift
	local search_path=
	if [[ -n "${LEGOFS_TOOLCHAIN_PATH:-}" ]]; then
		search_path=$LEGOFS_TOOLCHAIN_PATH
	elif [[ -n "${LEGOFS_TOOLCHAIN_SEARCH_PATH:-}" ]]; then
		search_path=$LEGOFS_TOOLCHAIN_SEARCH_PATH
	else
		local account_home=${LEGOFS_TOOLCHAIN_HOME:-${HOME:-}}
		if [[ -n "$account_home" ]]; then
			[[ "$account_home" == /* ]] || {
				printf 'error: toolchain home must be absolute: %s\n' \
					"$account_home" >&2
				return 2
			}
			search_path="$account_home/.cargo/bin:$account_home/.local/bin"
		fi
		if [[ -n "${PATH:-}" ]]; then
			search_path="${search_path:+$search_path:}$PATH"
		else
			search_path="${search_path:+$search_path:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
		fi
	fi

	[[ -n "$search_path" ]] || {
		printf 'error: toolchain search path is empty for profile %s\n' \
			"$profile" >&2
		return 2
	}
	local entry
	local -a search_entries=()
	IFS=: read -r -a search_entries <<<"$search_path"
	for entry in "${search_entries[@]}"; do
		[[ -n "$entry" ]] || {
			printf 'error: empty PATH entry is forbidden for profile %s\n' \
				"$profile" >&2
			return 2
		}
		[[ "$entry" == /* ]] || {
			printf 'error: PATH entry must be absolute: %s\n' "$entry" >&2
			return 2
		}
	done

	local used_path=
	local tool resolved directory
	for tool in "$@"; do
		if [[ "$tool" == /* ]]; then
			resolved=$tool
		else
			resolved=$(PATH="$search_path" command -v -- "$tool" 2>/dev/null) || {
				printf 'error: required tool is missing for %s: %s\n' \
					"$profile" "$tool" >&2
				return 2
			}
		fi
		[[ "$resolved" == /* && -x "$resolved" ]] || {
			printf 'error: required tool did not resolve to an executable: %s -> %s\n' \
				"$tool" "$resolved" >&2
			return 2
		}
		directory=${resolved%/*}
		case ":$used_path:" in
		*":$directory:"*) ;;
		*) used_path="${used_path:+$used_path:}$directory" ;;
		esac
		printf 'legofs_tool[%s].%s=%s\n' "$profile" "$tool" "$resolved"
	done

	# Keep the original search precedence. Directory order based on declaration
	# order could let a later, lower-priority directory shadow a tool that was
	# already resolved and logged from an earlier search entry.
	local frozen_path=
	for entry in "${search_entries[@]}"; do
		case ":$used_path:" in
		*":$entry:"*)
			case ":$frozen_path:" in
			*":$entry:"*) ;;
			*) frozen_path="${frozen_path:+$frozen_path:}$entry" ;;
			esac
			;;
		esac
	done
	local -a used_entries=()
	IFS=: read -r -a used_entries <<<"$used_path"
	for entry in "${used_entries[@]}"; do
		case ":$frozen_path:" in
		*":$entry:"*) ;;
		*) frozen_path="${frozen_path:+$frozen_path:}$entry" ;;
		esac
	done

	LEGOFS_TOOLCHAIN_SEARCH_PATH=$search_path
	LEGOFS_TOOLCHAIN_RESOLVED_PATH=$frozen_path
	LEGOFS_TOOLCHAIN_PROFILE=$profile
	PATH=$frozen_path
	export LEGOFS_TOOLCHAIN_SEARCH_PATH LEGOFS_TOOLCHAIN_RESOLVED_PATH
	export LEGOFS_TOOLCHAIN_PROFILE PATH
	printf 'legofs_toolchain[%s].path=%s\n' "$profile" "$PATH"
}
