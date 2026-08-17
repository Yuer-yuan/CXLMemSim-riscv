#include <mpi.h>

#include <stdio.h>
#include <time.h>
#include <unistd.h>

static long long monotonic_ns(void)
{
	struct timespec now;

	clock_gettime(CLOCK_MONOTONIC, &now);
	return (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
}

int main(int argc, char **argv)
{
	char hostname[128] = {0};
	int rank;
	int size;
	long long begin;
	long long end;

	(void)argv;
	MPI_Init(&argc, &argv);
	MPI_Comm_rank(MPI_COMM_WORLD, &rank);
	MPI_Comm_size(MPI_COMM_WORLD, &size);
	gethostname(hostname, sizeof(hostname) - 1);
	MPI_Barrier(MPI_COMM_WORLD);
	begin = monotonic_ns();
	usleep(250000);
	MPI_Barrier(MPI_COMM_WORLD);
	end = monotonic_ns();
	printf("LEGOFS_MPI_HELLO rank=%d size=%d host=%s begin_ns=%lld end_ns=%lld\n",
	       rank, size, hostname, begin, end);
	fflush(stdout);
	MPI_Finalize();
	return size == 10 ? 0 : 2;
}
