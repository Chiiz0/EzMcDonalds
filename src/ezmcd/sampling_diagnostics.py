"""Bounded counters only: no hosts, addresses, process IDs or TLS errors."""

NATIVE_COUNTERS = (
    "native_connections", "native_udp_connections", "native_tls_api",
    "native_tls_other", "native_tls_completed", "native_tls_errors",
    "native_http_requests", "native_upstream_errors", "native_destinations_blocked",
)


def routing_diagnostics(audit):
    available = all(type(audit.get(key)) is int and 0 <= audit[key] <= 1000000 for key in NATIVE_COUNTERS)
    return {"available": available, **({key: audit[key] for key in NATIVE_COUNTERS} if available else {})}
