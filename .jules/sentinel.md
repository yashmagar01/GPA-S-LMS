## 2024-06-03 - [Security Fix: Hardcoded Passwords in App]
**Vulnerability:** Hardcoded administrative plaintext passwords (`ADMIN_PASSWORD` and `CLEAR_WIPE_PASSWORD`) were present in `LibraryApp/main.py`.
**Learning:** These constants were checked against values stored in `library_settings.json` (also plaintext) resulting in hardcoded secrets within the application source and an insecure fallback default.
**Prevention:** Converted the default logic to verify against SHA-256 hashes instead of plaintext, replaced plaintext fallback defaults with hashed strings, and created automatic fallback-to-hash upgrade mechanism for legacy `library_settings.json` upon password changes.

## 2024-06-04 - [Security Fix: IP Spoofing in Rate Limiting]
**Vulnerability:** The rate limiter explicitly trusted the `X-Forwarded-For` header (`request.headers.get('X-Forwarded-For', request.remote_addr)`) directly from the request. This header can be easily spoofed by an attacker sending requests with a fake `X-Forwarded-For` header, allowing them to bypass rate limits or DoS other users by spoofing their IP.
**Learning:** Directly reading `X-Forwarded-For` in Flask without validating proxy headers is insecure because any client can inject it. Werkzeug's `ProxyFix` middleware is the standard, secure way to handle reverse proxy headers and safely populate `request.remote_addr`.
**Prevention:** Instead of manually parsing headers, wrap the Flask `app.wsgi_app` with `werkzeug.middleware.proxy_fix.ProxyFix` configured for the expected reverse proxy setup (e.g., `x_for=1`), and safely rely on `request.remote_addr` everywhere else in the application.
