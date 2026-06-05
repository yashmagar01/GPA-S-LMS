## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.
## 2024-05-18 - Fix IP Spoofing via X-Forwarded-For
**Vulnerability:** The rate limiter manually parsed `request.headers.get('X-Forwarded-For')` to identify the client IP. This header can be easily spoofed by an attacker, allowing them to bypass rate limiting completely by sending arbitrary IPs in the header.
**Learning:** The application is deployed behind reverse proxies (like Render) but didn't correctly configure Flask to trust proxy headers securely. Manually reading X-Forwarded-For is a common pattern but highly insecure as it trusts client-provided headers without verifying proxy boundaries.
**Prevention:** Use Werkzeug's `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) to securely handle reverse proxies, which safely parses headers and overrides `request.remote_addr`, allowing the application to rely on `request.remote_addr` without manual header parsing.
