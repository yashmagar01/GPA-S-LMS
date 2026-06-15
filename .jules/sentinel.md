## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.

## 2024-06-15 - [Security Fix: Add Authentication to Admin Endpoints]
**Vulnerability:** The `/api/admin/*` endpoints in the Flask Student Portal were entirely unauthenticated, allowing remote users to trigger administrative tasks such as approving requests or resetting student passwords.
**Learning:** Security controls shouldn't rely solely on routing mechanisms without enforcing checks. An API key system using `X-Admin-Api-Key` must be verified.
**Prevention:** An `@app.before_request` hook was added to explicitly enforce API key checking for all `/api/admin/` routes. Additionally, `AdminApiAuthHandler` was injected into the desktop application's `urllib.request` globals to automatically handle authorized internal traffic seamlessly.
