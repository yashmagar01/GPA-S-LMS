## 2026-04-14 - [Admin Endpoint Authorization Bypass]
**Vulnerability:** Admin endpoints under `/api/admin/` were accessible remotely as they lacked proper access verification in the Flask application, relying solely on Waitress serving them without restriction. The `enforce_admin_local_access` logic described in documentation was missing.
**Learning:** Security constraints must be explicitly enforced in code and not just documented. Further, `request.remote_addr` correctly reflects the immediate TCP connection peer, ignoring spoofable `X-Forwarded-For` headers, which is critical for local-only validation.
**Prevention:** Implement and verify explicit role or local access middleware for all administrative routes.
