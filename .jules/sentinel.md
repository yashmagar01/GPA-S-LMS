## 2025-02-24 - [Enforce Local Access for Admin Endpoints]
**Vulnerability:** Admin APIs were exposed via Waitress on `0.0.0.0` to the network, potentially allowing remote unauthorized access to desktop admin capabilities like `/api/admin/*`.
**Learning:** The desktop UI endpoints must be locked down to `localhost`. Waitress exposes all routes, so Flask routing needs explicitly restricted authorization hooks.
**Prevention:** Always ensure internal service endpoints restrict IP access natively when sharing the same Flask app routing. Explicitly rely on `request.remote_addr` rather than `X-Forwarded-For` to combat spoofing.
