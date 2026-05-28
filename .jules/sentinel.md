
## 2026-05-28 - [IP Spoofing] Fix IP Spoofing in Rate Limiter
**Vulnerability:** The application was extracting client IPs using `request.headers.get('X-Forwarded-For', request.remote_addr)` directly in the rate limiter, making it vulnerable to IP spoofing.
**Learning:** Never trust the `X-Forwarded-For` header directly from the client without proper proxy configuration. Attackers can forge this header to bypass IP-based restrictions like rate limiters.
**Prevention:** Use Werkzeug's `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) to securely parse proxy headers and populate `request.remote_addr`. Use `request.remote_addr` for IP-based checks.
