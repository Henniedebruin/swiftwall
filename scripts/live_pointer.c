/* Acceptance-test input only: temporary wlr virtual pointer, no configuration.
 * Compile with: cc live_pointer.c -o <temporary-output> -lwayland-client
 * Arguments: logical-x logical-y logical-width logical-height linux-button.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>

static const struct wl_interface pointer_interface;
static const struct wl_interface *create_types[] = {
    &wl_seat_interface, &pointer_interface
};
static const struct wl_message manager_requests[] = {
    {"create_virtual_pointer", "?on", create_types},
    {"destroy", "", NULL},
};
static const struct wl_interface manager_interface = {
    "zwlr_virtual_pointer_manager_v1", 1, 2, manager_requests, 0, NULL
};
static const struct wl_message pointer_requests[] = {
    {"motion", "uff", NULL},
    {"motion_absolute", "uuuuu", NULL},
    {"button", "uuu", NULL},
    {"axis", "uuf", NULL},
    {"frame", "", NULL},
    {"axis_source", "u", NULL},
    {"axis_stop", "uu", NULL},
    {"axis_discrete", "uufi", NULL},
    {"destroy", "", NULL},
};
static const struct wl_interface pointer_interface = {
    "zwlr_virtual_pointer_v1", 1, 9, pointer_requests, 0, NULL
};
static struct wl_proxy *manager;
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)data; (void)version;
    if (strcmp(interface, manager_interface.name) == 0)
        manager = wl_registry_bind(registry, name, &manager_interface, 1);
}
static void removed(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data; (void)registry; (void)name;
}
static const struct wl_registry_listener listener = {global, removed};
int main(int argc, char **argv) {
    if (argc != 6) return 2;
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) return 3;
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &listener, NULL);
    if (wl_display_roundtrip(display) < 0 || !manager) {
        fputs("wlr virtual pointer unavailable\n", stderr); return 4;
    }
    struct wl_proxy *pointer = wl_proxy_marshal_flags(
        manager, 0, &pointer_interface, 1, 0, NULL, NULL);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint32_t ms = (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000);
    wl_proxy_marshal_flags(pointer, 1, NULL, 1, 0, ms,
        (uint32_t)strtoul(argv[1], NULL, 10),
        (uint32_t)strtoul(argv[2], NULL, 10),
        (uint32_t)strtoul(argv[3], NULL, 10),
        (uint32_t)strtoul(argv[4], NULL, 10));
    wl_proxy_marshal_flags(pointer, 4, NULL, 1, 0);
    uint32_t button = (uint32_t)strtoul(argv[5], NULL, 10);
    if (button) {
        wl_proxy_marshal_flags(pointer, 2, NULL, 1, 0, ms, button, 1u);
        wl_proxy_marshal_flags(pointer, 4, NULL, 1, 0);
        wl_display_roundtrip(display);
        wl_proxy_marshal_flags(pointer, 2, NULL, 1, 0, ms + 1, button, 0u);
        wl_proxy_marshal_flags(pointer, 4, NULL, 1, 0);
    }
    int result = wl_display_roundtrip(display);
    wl_proxy_marshal_flags(pointer, 8, NULL, 1, WL_MARSHAL_FLAG_DESTROY);
    wl_proxy_marshal_flags(manager, 1, NULL, 1, WL_MARSHAL_FLAG_DESTROY);
    wl_registry_destroy(registry);
    wl_display_flush(display);
    wl_display_disconnect(display);
    return result < 0 ? 5 : 0;
}
