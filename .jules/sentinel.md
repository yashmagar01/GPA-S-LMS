## 2025-05-18 - [Fix Rate Limiter IP Spoofing]
**Vulnerability:** Rate limiting bypass via IP spoofing (X-Forwarded-For header misuse). The rate limiter consumed the entire `X-Forwarded-For` header instead of the first IP, allowing attackers to append IPs and evade tracking.
**Learning:** Raw headers like `X-Forwarded-For` shouldn't be trusted or used directly as keys; they can contain multiple IPs separated by commas if passing through multiple proxies, or arbitrarily forged content.
**Prevention:** Always parse `X-Forwarded-For` securely. Split the header value on `,` and extract the first valid IP address (`.split(',')[0].strip()`) to use as the true client IP for security features like rate limiting.
