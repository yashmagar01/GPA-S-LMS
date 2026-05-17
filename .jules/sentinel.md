## 2024-06-25 - [Missing Access Control] Unauthenticated Admin Endpoints Exposed Internally
**Vulnerability:** The `/api/admin/` endpoints were accessible without authentication on the public web extension interface.
**Learning:** Internal admin APIs intended only for communication from a trusted local application (the desktop app) were bound to an interface that is accessible publicly without specific host checking, creating an unauthorized access vulnerability.
**Prevention:** Always restrict access to internally-intended admin endpoints by verifying `request.remote_addr` against loopback addresses (`127.0.0.1`, `::1`), explicitly ignoring spoofable headers like `X-Forwarded-For`.
