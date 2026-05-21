## 2024-05-24 - [Information Leakage] Overly Verbose API Exceptions
**Vulnerability:** The Flask Student Portal API returned unhandled exception objects `str(e)` directly in user-facing JSON responses (`jsonify({'error': str(e)})`).
**Learning:** This exposes sensitive backend architecture, database logic, stack traces, and queries to malicious users, violating the "fail securely" principle.
**Prevention:** All API exception handling blocks must return generic error messages to the client and utilize the `_log_portal_exception` helper to preserve actual debugging details securely in backend logs.

## 2024-05-24 - [Rate Limiter] X-Forwarded-For IP Spoofing Risk
**Vulnerability:** The `RateLimiter` consumed the raw `X-Forwarded-For` header string directly (`request.headers.get('X-Forwarded-For')`) instead of parsing the IP list.
**Learning:** An attacker can inject comma-separated spoofed IPs or random strings to bypass rate limiting or orchestrate DoS attacks by exhausting rate limits of legitimate users.
**Prevention:** Always parse `X-Forwarded-For` defensively (`forwarded_for.split(',')[0].strip()`) to extract the genuine client IP when trusting proxy headers.
