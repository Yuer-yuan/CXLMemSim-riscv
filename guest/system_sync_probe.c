#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

/*
 * Diagnose the dynamic parent's system(3) fork/exec/wait path without
 * changing IO500's POSIX_Sync implementation.  The child is the same static
 * BusyBox used by the guest's /bin/sync, so LD_PRELOAD only affects the
 * dynamic parent and its path to exec.
 */
int
main(int argc, char **argv)
{
	const char *label = argc == 2 ? argv[1] : "unlabelled";
	const char *marker = "/tmp/legofs-system-sync-child-reached";
	const char *command =
		"/bin/busybox sh -c 'echo reached > "
		"/tmp/legofs-system-sync-child-reached; exec /bin/busybox sync'";
	int status;
	int saved_errno;
	int child_reached;
	int exited;
	int exit_status;
	int signaled;
	int term_signal;

	(void)unlink(marker);
	errno = 0;
	status = system(command);
	saved_errno = errno;
	child_reached = access(marker, F_OK) == 0;
	exited = status != -1 && WIFEXITED(status);
	exit_status = exited ? WEXITSTATUS(status) : -1;
	signaled = status != -1 && WIFSIGNALED(status);
	term_signal = signaled ? WTERMSIG(status) : -1;

	printf("LEGOFS_SYSTEM_SYNC_PROBE_RESULT "
	       "label=%s raw_status=%d errno=%d child_reached=%d "
	       "exited=%d exit_status=%d signaled=%d term_signal=%d\n",
	       label, status, saved_errno, child_reached, exited, exit_status,
	       signaled, term_signal);
	(void)fflush(stdout);
	(void)unlink(marker);

	return status == 0 && child_reached ? 0 : 1;
}
