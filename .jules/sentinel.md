## 2026-05-02 - [Restrict Admin API to Localhost]
**Vulnerability:** The Flask student portal exposed `/api/admin/` endpoints publicly, which are intended to only be used by the desktop app locally on the server.
**Learning:** Waitress binds the Flask app to 0.0.0.0, exposing all endpoints to the network unless explicitly restricted in the application layer.
**Prevention:** Implement IP-based restrictions in `@app.before_request` for sensitive API routes to ensure they are only accessible from `127.0.0.1` or `::1`.
