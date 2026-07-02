# MicroPython source patches (version-specific)

Small, version-specific fixes applied to the MicroPython tree at build time. Each is tied to a
MicroPython/ESP-IDF version and should be **removed** once a version bump no longer needs it —
the workflow gates them behind a per-target flag.

## `network_common.c` — WIFI_AUTH_MAX (ESP-IDF v5.1.2)

That MicroPython's `_Static_assert(WIFI_AUTH_MAX == 10, ...)` in `ports/esp32/network_common.c`
lagged ESP-IDF v5.1.2, which defines **11** auth modes — so the build fails without the fix. The
workflow applies it as a one-line change (`== 10` → `== 11`) when a target sets
`apply_wifi_auth_patch: "true"`. Newer MicroPython + ESP-IDF fix this upstream, so leave it
`"false"` unless a build hits the assert.

`network_common.c` here is the exact patched file from the original ESP-IDF v5.1.2 hand-build,
kept for reference (do not blindly copy it over a different MicroPython version — the rest of the
file changes across releases; the workflow's targeted edit is the portable form).
