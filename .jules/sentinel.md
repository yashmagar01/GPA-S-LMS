## 2024-05-24 - [Missing Authorization]
**Vulnerability:** The `/api/admin/` endpoints in the Flask Student Portal were accessible externally, lacking authorization and IP-based restrictions.
**Learning:** External network traffic could access sensitive administrative endpoints. Relying on obscurity or front-end hiding is not sufficient.
**Prevention:** Strictly enforce local access via the `enforce_admin_local_access` `@app.before_request` hook, checking `request.remote_addr` against localhost IPs (`127.0.0.1`, `::1`) and ignoring `X-Forwarded-For` to prevent spoofing.
