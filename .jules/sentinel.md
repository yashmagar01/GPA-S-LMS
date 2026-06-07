## 2024-06-07 - [IP Spoofing Vulnerability]
**Vulnerability:** Manually parsing the `X-Forwarded-For` header to determine the client IP in `student_portal.py` for rate limiting. This header can easily be spoofed by attackers to bypass rate limits or DoS arbitrary IP addresses.
**Learning:** For accurate client IP identification in Flask applications behind reverse proxies, never manually parse the `X-Forwarded-For` header.
**Prevention:** Use Werkzeug's `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) to handle reverse proxies securely. This allows safe reliance on `request.remote_addr` throughout the application.
