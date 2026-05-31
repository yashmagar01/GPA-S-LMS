## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.
