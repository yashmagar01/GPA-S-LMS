## 2024-06-14 - [Flask IP Spoofing] Fix X-Forwarded-For parsing vulnerability
**Vulnerability:** IP spoofing via manual parsing of the `X-Forwarded-For` header in Flask `request.headers.get('X-Forwarded-For', request.remote_addr)`. This allows attackers to bypass IP-based rate limiting or authentication.
**Learning:** Never manually parse the `X-Forwarded-For` header by taking the first IP, as it is easily spoofable by an attacker.
**Prevention:** Use Werkzeug's `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) to handle reverse proxies securely, allowing safe reliance on `request.remote_addr`.
