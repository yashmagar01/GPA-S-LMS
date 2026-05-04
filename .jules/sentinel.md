## 2026-05-04 - [CRITICAL] Fix missing authentication on admin endpoints
**Vulnerability:** The Flask portal allowed public network access to administrative endpoints under `/api/admin/` by virtue of Waitress listening on `0.0.0.0` for regular student access.
**Learning:** These admin endpoints are intended *only* for the local Tkinter desktop app to communicate with the Waitress portal.
**Prevention:** Ensured the web server intercepts `/api/admin/` requests via an `@app.before_request` hook, strictly enforcing that `request.remote_addr` resolves to localhost and ignoring any spoofable headers.
