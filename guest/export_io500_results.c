#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int copy_file(const char *source, const char *destination)
{
    char buffer[64 * 1024];
    int input = open(source, O_RDONLY);
    int output;

    if (input < 0) {
        perror(source);
        return -1;
    }
    output = open(destination, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (output < 0) {
        perror(destination);
        close(input);
        return -1;
    }
    for (;;) {
        ssize_t received = read(input, buffer, sizeof(buffer));
        size_t offset = 0;

        if (received < 0 && errno == EINTR) {
            continue;
        }
        if (received < 0) {
            perror(source);
            close(output);
            close(input);
            return -1;
        }
        if (received == 0) {
            break;
        }
        while (offset < (size_t)received) {
            ssize_t written = write(output, buffer + offset,
                                    (size_t)received - offset);

            if (written < 0 && errno == EINTR) {
                continue;
            }
            if (written <= 0) {
                perror(destination);
                close(output);
                close(input);
                return -1;
            }
            offset += (size_t)written;
        }
    }
    if (fsync(output) != 0 || close(output) != 0 || close(input) != 0) {
        perror(destination);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    char source_directory[128];
    char destination_directory[64];
    DIR *directory;
    struct dirent *entry;
    unsigned int copied = 0;
    int copied_config = 0;
    int copied_result = 0;
    int partial = 0;

    if (argc == 3 && strcmp(argv[2], "--allow-partial") == 0) {
        partial = 1;
    }
    if (!((argc == 2) || partial) ||
        (strcmp(argv[1], "scc") != 0 && strcmp(argv[1], "standard") != 0 &&
         strcmp(argv[1], "stress-tiny") != 0)) {
        fprintf(stderr, "usage: %s scc|standard|stress-tiny [--allow-partial]\n", argv[0]);
        return 64;
    }
    if (snprintf(source_directory, sizeof(source_directory),
                 "/badfs/io500-%s-results", argv[1]) >=
            (int)sizeof(source_directory) ||
        snprintf(destination_directory, sizeof(destination_directory),
                 "/results/%s", argv[1]) >=
            (int)sizeof(destination_directory)) {
        return 64;
    }
    if (mkdir(destination_directory, 0755) != 0 && errno != EEXIST) {
        perror(destination_directory);
        return 1;
    }
    directory = opendir(source_directory);
    if (directory == NULL) {
        perror(source_directory);
        return 1;
    }
    while ((entry = readdir(directory)) != NULL) {
        char source[192];
        char destination[128];
        struct stat metadata;

        if (strcmp(entry->d_name, ".") == 0 ||
            strcmp(entry->d_name, "..") == 0) {
            continue;
        }

        if (snprintf(source, sizeof(source), "%s/%s", source_directory,
                     entry->d_name) >= (int)sizeof(source) ||
            snprintf(destination, sizeof(destination), "%s/%s",
                     destination_directory, entry->d_name) >=
                (int)sizeof(destination)) {
            closedir(directory);
            return 1;
        }
        if (lstat(source, &metadata) != 0) {
            perror(source);
            closedir(directory);
            return 1;
        }
        if (!S_ISREG(metadata.st_mode)) {
            continue;
        }
        if (copy_file(source, destination) != 0) {
            closedir(directory);
            return 1;
        }
        copied_config |= strcmp(entry->d_name, "config.ini") == 0;
        copied_result |= strcmp(entry->d_name, "result.txt") == 0;
        copied++;
    }
    if (closedir(directory) != 0) {
        perror(source_directory);
        return 1;
    }
    if (partial && copied > 0 && copied_config) {
        printf("LEGOFS_IO500_PARTIAL_RESULTS_EXPORTED stage=%s source=%s destination=%s artifacts=%u config=%d result=%d\n",
               argv[1], source_directory, destination_directory, copied,
               copied_config, copied_result);
        return 0;
    }
    if (!copied_config || !copied_result) {
        fprintf(stderr,
                "incomplete IO500 result export: copied=%u config=%d result=%d\n",
                copied, copied_config, copied_result);
        return 1;
    }
    printf("LEGOFS_IO500_RESULTS_EXPORTED stage=%s source=%s destination=%s artifacts=%u\n",
           argv[1], source_directory, destination_directory, copied);
    return 0;
}
