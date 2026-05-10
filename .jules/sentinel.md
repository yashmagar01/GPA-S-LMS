## 2024-05-24 - [Local Access Restriction Bypass]
**Vulnerability:** Missing local access restriction on admin endpoints (`/api/admin/`).
**Learning:** External networks could potentially access sensitive admin API functions (e.g., approve/reject requests, password reset) since the app bound to `0.0.0.0` but did not verify the request's origin IP for `/api/admin/` routes.
**Prevention:** Implement a `@app.before_request` hook that strictly checks `request.remote_addr` for localhost IPs (`127.0.0.1`, `::1`) on sensitive local-only endpoints. Ensure to ignore `X-Forwarded-For` to prevent IP spoofing.
