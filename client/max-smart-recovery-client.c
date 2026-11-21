#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#if defined(RECOVERY_SOCKET_PATH) && !defined(RECOVERY_DISPOSABLE_TEST_BUILD)
#error "RECOVERY_SOCKET_PATH is restricted to disposable test builds"
#endif

#ifndef RECOVERY_SOCKET_PATH
#define RECOVERY_SOCKET_PATH \
    "/Library/Application Support/MaxSmartRecovery/service/s"
#endif

#define RESPONSE_LIMIT 96
#define RECEIVE_TIMEOUT_SECONDS 2

static const char SNAPSHOT_REQUEST[] = "{\"op\":\"snapshot\"}";
static const char VERIFY_REQUEST[] = "{\"op\":\"verify\"}";

static const char *const REFUSED_CODES[] = {
    "anchor_contradictory",
    "anchor_invalid",
    "anchor_pending",
    "anchor_uncertain",
    "failed",
    "invalid_request",
    "quota_exceeded",
    "rollback",
    "unauthorized",
};

static void discard_rights(struct msghdr *message) {
    struct cmsghdr *header;

    for (header = CMSG_FIRSTHDR(message); header != NULL; header = CMSG_NXTHDR(message, header)) {
        int *descriptors;
        unsigned char *data;
        size_t count;
        size_t index;
        if (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS) {
            continue;
        }
        if (header->cmsg_len < CMSG_LEN(0)) {
            continue;
        }
        data = CMSG_DATA(header);
        descriptors = (int *)data;
        count = (header->cmsg_len - CMSG_LEN(0)) / sizeof(int);
        for (index = 0; index < count; index++) {
            if (descriptors[index] >= 0) {
                close(descriptors[index]);
            }
        }
    }
}

static int response_is_exact(const char *payload, size_t length) {
    char expected[RESPONSE_LIMIT + 1];
    size_t index;

    if (length == 0 || length > RESPONSE_LIMIT || payload[length - 1] != '\n') {
        return 0;
    }
    if (strncmp(payload, "{\"generation\":", 14) == 0) {
        unsigned long generation = 0;
        const char *cursor = payload + 14;
        if (*cursor < '1' || *cursor > '9') {
            return 0;
        }
        while (*cursor >= '0' && *cursor <= '9') {
            generation = generation * 10 + (unsigned long)(*cursor - '0');
            if (generation > 999999) {
                return 0;
            }
            cursor++;
        }
        if (strcmp(cursor, ",\"status\":\"ok\"}\n") != 0) {
            return 0;
        }
        return 1;
    }
    for (index = 0; index < sizeof(REFUSED_CODES) / sizeof(REFUSED_CODES[0]); index++) {
        int written = snprintf(
            expected,
            sizeof(expected),
            "{\"code\":\"%s\",\"status\":\"refused\"}\n",
            REFUSED_CODES[index]
        );
        if (written > 0 && (size_t)written == length &&
            memcmp(payload, expected, length) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *request = NULL;
    struct sockaddr_un address;
    char response[RESPONSE_LIMIT + 2];
    size_t received = 0;
    int descriptor;
    size_t path_length;
    struct timeval receive_timeout;

    if (argc != 2) {
        return 1;
    }
    if (strcmp(argv[1], "snapshot") == 0) {
        request = SNAPSHOT_REQUEST;
    } else if (strcmp(argv[1], "verify") == 0) {
        request = VERIFY_REQUEST;
    } else {
        return 1;
    }

    path_length = strlen(RECOVERY_SOCKET_PATH);
    if (path_length == 0 || path_length >= sizeof(address.sun_path)) {
        return 1;
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, RECOVERY_SOCKET_PATH, path_length);

    descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0) {
        return 1;
    }
    receive_timeout.tv_sec = RECEIVE_TIMEOUT_SECONDS;
    receive_timeout.tv_usec = 0;
    if (setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_RCVTIMEO,
            &receive_timeout,
            sizeof(receive_timeout)
        ) != 0) {
        close(descriptor);
        return 1;
    }
#ifdef SO_NOSIGPIPE
    {
        int enabled = 1;
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
    }
#endif
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(descriptor);
        return 1;
    }
    if (send(descriptor, request, strlen(request), 0) != (ssize_t)strlen(request)) {
        close(descriptor);
        return 1;
    }
    if (shutdown(descriptor, SHUT_WR) != 0) {
        close(descriptor);
        return 1;
    }

    while (received <= RESPONSE_LIMIT) {
        struct iovec vector;
        struct msghdr message;
        char control[256];
        ssize_t count;

        memset(&message, 0, sizeof(message));
        vector.iov_base = response + received;
        vector.iov_len = RESPONSE_LIMIT + 1 - received;
        message.msg_iov = &vector;
        message.msg_iovlen = 1;
        message.msg_control = control;
        message.msg_controllen = sizeof(control);
        count = recvmsg(descriptor, &message, 0);
        if (count < 0) {
            close(descriptor);
            return 1;
        }
        if (message.msg_controllen > 0 || (message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)) != 0) {
            discard_rights(&message);
            close(descriptor);
            return 1;
        }
        if (count == 0) {
            break;
        }
        received += (size_t)count;
    }
    close(descriptor);
    response[received] = '\0';
    if (!response_is_exact(response, received)) {
        return 1;
    }
    if (fwrite(response, 1, received, stdout) != received) {
        return 1;
    }
    return 0;
}
