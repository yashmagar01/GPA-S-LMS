## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.

## 2025-01-22 - [Security Fix: Unauthenticated Remote Admin Endpoints]
**Vulnerability:** The Flask Student Portal allowed unauthenticated access to `/api/admin/*` endpoints. Waitress bound these endpoints to `0.0.0.0`, exposing sensitive administrative actions remotely in cloud and network deployments.
**Learning:** Using `@app.before_request` to selectively skip CSRF checks on admin endpoints without enforcing another authentication mechanism exposes those administrative actions directly, especially because network listeners are not strictly bound to localhost.
**Prevention:** Implemented an API Key mechanism (`X-Admin-Api-Key`) using a locally persisted secret (`.admin_api_key`) and an `AdminApiAuthHandler` injected globally via `urllib.request` in the desktop app, coupled with a mandatory auth check before fulfilling any `/api/admin/*` requests in the portal.
