## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.

## 2024-06-03 - [Security Fix: IP Spoofing in Rate Limiter]
**Vulnerability:** The rate limiter manually parsed the `X-Forwarded-For` header to determine the client IP address (`request.headers.get('X-Forwarded-For', request.remote_addr)`). This allowed an attacker to easily spoof their IP address by supplying a fake `X-Forwarded-For` header, completely bypassing the rate limiting protections.
**Learning:** For accurate and secure client IP identification in Flask applications served behind reverse proxies, never manually parse the `X-Forwarded-For` header. The first IP in the list is easily spoofable by an attacker.
**Prevention:** Used Werkzeug's `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) to handle reverse proxies securely. This configures the WSGI environment correctly so that `request.remote_addr` safely reflects the true client IP, preventing spoofing attacks.
