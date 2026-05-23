# GPA-S-LMS Adversarial Testing Audit Report

As requested, here is the comprehensive adversarial testing audit of the GPA-S-LMS system across all requested domains.

## A. Student Borrowing Lifecycle

✅ **What Works:**
- Restricting Pass Out students from submitting requests is properly implemented in `api_submit_request`.
- Basic duplicate request checks exist for pending renewals and book requests.
- Max borrow limit checking exists for active loans.

❌ **Missing or Broken:**
- **Scenario:** Student requesting when at borrowing limit (Student)
  - **Trace:** In `api_submit_request` (req_type = 'book_request'), the code checks if `COUNT(*)` in `borrow_records` where `status = 'borrowed'` is `>= limit`. It does *not* include the count of currently pending `book_request` entries in the `requests` table.
  - **Problem:** A student with a limit of 5 and 4 active loans can submit 5 separate pending book requests. If the librarian blindly approves them, the student ends up with 9 borrowed books, bypassing the institutional limit.
  - **Fix:** In `student_portal.py` `api_submit_request`, update the max limit check to calculate `total_active_loans + total_pending_book_requests` against the `MAX_BOOKS_PER_STUDENT` environment variable.
  - **Severity:** High

- **Scenario:** Requesting cancellation after approval (Student / Librarian)
  - **Trace:** `api_cancel_request` allows a student to cancel a request. However, there is no state-machine validation to prevent cancellation if the request status has already transitioned from 'pending' to 'approved'.
  - **Problem:** A student could cancel a request *after* the librarian has approved it and pulled the physical book, leading to orphaned books at the counter and out-of-sync cloud/local states.
  - **Fix:** In `api_cancel_request`, add a query to verify `status == 'pending'` before executing the `DELETE` or `UPDATE` cancellation statement. Return a 400/409 error if already approved.
  - **Severity:** High

⚠️ **Partially Implemented / Edge Cases:**
- **Scenario:** Going to collect but NOT collecting within deadline (Both)
  - **Trace:** There is no automated cleanup cron or scheduled task to auto-cancel or mark 'expired' for approved requests where the physical book hasn't been issued via `main.py` counter operations within X days.
  - **Problem:** Approved requests hold a reservation indefinitely, locking the available stock and preventing waitlisted students from getting the book.
  - **Fix:** Implement a daily cleanup job in the infrastructure (or a startup check in `main.py`) that queries approved requests older than a configurable window (e.g., 48 hours) and sets them to 'expired', while updating book availability counts.
  - **Severity:** Medium


## B. Renewal Lifecycle

✅ **What Works:**
- JSON details parsing for `accession_no` extraction to verify existing pending renewals.
- Email templates are correctly routed for renewal request submissions.

❌ **Missing or Broken:**
- **Scenario:** Renewal when another student is on the waitlist (Student / Librarian)
  - **Trace:** `api_submit_request` allows renewal submissions, and `api_admin_approve_request` / `main.py` approves them without checking the `waitlist` table for the specific `book_id`.
  - **Problem:** A student can continually renew a highly demanded book, indefinitely starving waitlisted students.
  - **Fix:** In the desktop app's renewal approval logic (`main.py`) and portal submission (`student_portal.py`), add a validation check against the `waitlist` table. If `COUNT(*) > 0` for that `book_id`, block the renewal or prompt the librarian with a hard warning.
  - **Severity:** High

- **Scenario:** Renewal when at the maximum renewal count (Student)
  - **Trace:** The `borrow_records` table does not track `renewal_count` per loan, and there is no check in `api_submit_request` limiting how many consecutive times a specific loan can be renewed.
  - **Problem:** Students can renew the same book infinitely, bypassing library circulation policies.
  - **Fix:** Add a `renewal_count` integer column to `borrow_records`. In `api_submit_request`, query this column and reject the request if it exceeds the max allowed renewals. Increment this count upon renewal approval.
  - **Severity:** High


## C. Return & Fine Lifecycle

✅ **What Works:**
- Fine calculation dynamic setting retrieval (`get_portal_fine_per_day`) utilizes a 15-second in-memory cache to reduce DB load.

❌ **Missing or Broken:**
- **Scenario:** Student attempting to borrow while fine is unpaid (Student)
  - **Trace:** In `api_submit_request`, there is no validation against the student's current outstanding fines before allowing a new `book_request`.
  - **Problem:** Students with massive unpaid fines can continue depleting library inventory without penalty, breaking the incentive structure of the fine system.
  - **Fix:** Add a check in `api_submit_request` to query total unpaid fines for `session['student_id']`. If `total_fines > 0`, return a 403 error preventing new book reservations until cleared.
  - **Severity:** Critical

- **Scenario:** Damaged or lost book scenario (Librarian)
  - **Trace:** The system only supports standard 'returned' workflows. There is no status for 'lost' or 'damaged' in `borrow_records` or the portal API.
  - **Problem:** If a book is lost, the librarian cannot cleanly remove it from the student's active loans while simultaneously applying a replacement fee. It either stays overdue forever or is marked returned (falsifying inventory).
  - **Fix:** Introduce 'lost' and 'damaged' statuses in `borrow_records`. Add corresponding administrative endpoints and desktop UI actions that mark the item lost, deduct it from total inventory counts, and optionally generate a fixed replacement fine record.
  - **Severity:** High


## D. Waitlist Lifecycle

✅ **What Works:**
- Waitlist addition endpoint (`add_to_waitlist`) checks if the book exists before inserting.

❌ **Missing or Broken:**
- **Scenario:** Notification expiring / Multiple students on waitlist (System)
  - **Trace:** When a book is returned, there is no queued mechanism to notify the *first* person on the waitlist, give them a 24-hour window, and then automatically notify the *second* person if the first fails to claim it.
  - **Problem:** The waitlist is entirely passive or broadcasts to everyone simultaneously, leading to a race condition at the physical library counter ("first to run to the library gets it").
  - **Fix:** Implement a sequential waitlist fulfillment engine. Add `notified_at` and `expires_at` timestamps to waitlist rows. Upon return, notify `ORDER BY joined_at ASC LIMIT 1` and set the timestamps. A cron job should expire un-claimed reservations and notify the next in line.
  - **Severity:** High


## E. Account & Identity Scenarios

✅ **What Works:**
- Pass Out students are explicitly restricted from making new requests.

❌ **Missing or Broken:**
- **Scenario:** Account deletion with active loans and fines (Student / Librarian)
  - **Trace:** `api_admin_approve_deletion` in `student_portal.py` deletes the account by taking the `student_id` from `deletion_requests` and simply executing the delete. It does *not* check `borrow_records` for active loans or unpaid fines.
  - **Problem:** A student can delete their account (or a librarian can approve the deletion) while holding 5 overdue books. The books are permanently lost from the system's tracking, and fines are erased.
  - **Fix:** In `api_admin_approve_deletion`, before approving, query `borrow_records` for active loans and unpaid fines. If any exist, abort the deletion and return an error requiring the student to clear their account first.
  - **Severity:** Critical

- **Scenario:** Profile update session hijacking / password change on active sessions (Student)
  - **Trace:** Changing the password does not invalidate existing sessions. Flask's default session cookie is used without a session generation timestamp check.
  - **Problem:** If a student leaves a terminal logged in, then changes their password from their phone, the terminal session remains fully authenticated and active indefinitely.
  - **Fix:** Add a `session_token` or `password_last_changed` timestamp to the `student_auth` table. Store this value in the Flask session upon login. In a `@app.before_request` hook, verify the session's token matches the DB; if not, force logout.
  - **Severity:** Medium


## F. Librarian Daily Operations

✅ **What Works:**
- Counter operations in `main.py` for issuing and returning bypass the portal queues, prioritizing physical presence.

❌ **Missing or Broken:**
- **Scenario:** Unauthenticated access to admin endpoints via SSRF / Local port forwarding (Librarian/Attacker)
  - **Trace:** `enforce_admin_local_access` is supposedly protecting `/api/admin/` endpoints by restricting them to localhost. However, the `@app.before_request` decorator for it is missing or improperly mapped in the provided code snippet, and Waitress listens on `0.0.0.0`.
  - **Problem:** If a student manages to access the local network or bypasses the IP check (e.g., via `X-Forwarded-For` spoofing which is used in the rate limiter `_get_client_key`), they can invoke `/api/admin/approve` to approve their own requests or wipe out fines.
  - **Fix:** Ensure `enforce_admin_local_access` strictly validates `request.remote_addr` (ignoring HTTP headers like `X-Forwarded-For` which can be spoofed) and is explicitly registered with `@app.before_request`.
  - **Severity:** Critical

- **Scenario:** Concurrent approvals for the same single-copy book (Librarian)
  - **Trace:** If two librarians use two desktop apps, or click approve rapidly, `api_admin_approve_request` does not use database transactions (`BEGIN IMMEDIATE` / `SELECT FOR UPDATE`) to lock the book's availability count.
  - **Problem:** A book with 1 copy can be assigned to 2 different students simultaneously, causing a negative available copies count and real-world counter conflict.
  - **Fix:** In the approval endpoints and `main.py` logic, wrap the availability check and the borrow record insertion in a strict ACID transaction to prevent dirty reads.
  - **Severity:** High


## G. Sync & Data Integrity

✅ **What Works:**
- Bidirectional SyncManager (`_do_push`) translates SQLite `?` to PostgreSQL `%s` placeholders.
- `_normalize_database_url` manages pooler fallbacks and `sslmode`.

❌ **Missing or Broken:**
- **Scenario:** Sync conflict when the same record is modified on desktop and portal simultaneously (System)
  - **Trace:** `sync_manager.py` relies on a push/pull mechanism but lacks a robust Last-Write-Wins (LWW) conflict resolution timestamp on individual rows (like `updated_at`).
  - **Problem:** If a student updates their profile online exactly when a librarian edits their record locally, the sync manager may overwrite one with the other non-deterministically based on network latency.
  - **Fix:** Implement an `updated_at` column across all synced tables with SQLite triggers. Modify the sync manager to only apply inbound row updates if the remote `updated_at` is strictly greater than the local `updated_at`.
  - **Severity:** High

- **Scenario:** Transaction imported from Excel creating duplicate records (Librarian)
  - **Trace:** `main.py` relies on `pandas` and `openpyxl` to import transactions. There is no idempotent check to ensure the exact same transaction (same student, same book, same date) hasn't already been imported in a previous batch.
  - **Problem:** A librarian accidentally clicking 'Import' twice on the same Excel sheet will duplicate all historical loans, corrupting statistics and potentially re-applying old fines.
  - **Fix:** Calculate a deterministic hash of the Excel row (student + book + date) and store it in a `transaction_hash` column with a `UNIQUE` constraint in `borrow_records`, catching and ignoring `IntegrityError` during import.
  - **Severity:** High


## H. Deployment & Infrastructure

✅ **What Works:**
- Render cold start logic is accounted for by the architecture, using Waitress for production serving on 0.0.0.0.
- CSRF protection uses double-submit cookies on state-changing endpoints.

❌ **Missing or Broken:**
- **Scenario:** Rate limiter IP spoofing bypass (Attacker)
  - **Trace:** In `RateLimiter._get_client_key`, `request.headers.get('X-Forwarded-For', request.remote_addr)` is used, but `X-Forwarded-For` can contain a comma-separated list, and if not parsed securely by a reverse proxy middleware (like `werkzeug.middleware.proxy_fix.ProxyFix`), the attacker can send random IPs to bypass the rate limit.
  - **Problem:** An attacker can script brute-force attacks against `/api/login` by simply changing the `X-Forwarded-For` header on every request, completely neutralizing the memory-based RateLimiter.
  - **Fix:** Wrap the Flask app with `ProxyFix` to correctly and securely resolve the remote address, and change the rate limiter to rely exclusively on `request.remote_addr`.
  - **Severity:** Critical

- **Scenario:** Unauthenticated study material downloads (Student/Attacker)
  - **Trace:** `download_study_material(material_id)` in `student_portal.py` serves files. It is not clear that there is a strict session check enforcing that only enrolled active students can download.
  - **Problem:** Direct links to proprietary college study materials can be shared on the public internet, draining Render bandwidth and leaking intellectual property.
  - **Fix:** Ensure `download_study_material` explicitly checks `if 'student_id' not in session:` before serving the file via `send_file`.
  - **Severity:** Medium


## I. Notification & Email Pipeline

✅ **What Works:**
- Email templating is robust with `generate_email_template` and utilizes background threads (`send_email_bg`) to prevent blocking the HTTP response.

❌ **Missing or Broken:**
- **Scenario:** Email failing silently / Unread count accuracy (System)
  - **Trace:** `send_email_bg` wraps the SMTP call in a try/except, but it does not update a 'delivery_status' in the database if it fails. Furthermore, the dashboard's unread count does not distinguish between in-app notifications and email statuses.
  - **Problem:** If SendGrid/SMTP credentials expire, emails stop sending silently. The user is never warned, and the librarian has no visibility into delivery failures.
  - **Fix:** Add an `email_status` column to the `notifications` table (values: pending, sent, failed). Update this column inside the `send_email_bg` thread's try/except block. Expose failed delivery counts on the Admin Observability dashboard.
  - **Severity:** Medium


## J. Edge Cases & Stress Scenarios

✅ **What Works:**
- String processing optimizations and SQLite WAL configurations are in place to handle high concurrency locking safely.

❌ **Missing or Broken:**
- **Scenario:** Student submitting the same request type 20 times rapidly (Student)
  - **Trace:** In `api_submit_request`, duplicate checking uses a manual `SELECT`, loops over rows, parses JSON, and checks values. It does *not* utilize database-level constraints.
  - **Problem:** Due to the time taken to parse JSON in Python, an attacker sending 20 concurrent HTTP POST requests will pass the `SELECT` check on all threads before the first `INSERT` finishes. This results in 20 duplicate pending requests.
  - **Fix:** Use a database transaction lock (`SELECT ... FOR UPDATE` on the student's row) at the start of the request handling, or better, extract the `book_id` or `accession_no` into a dedicated indexed column in the `requests` table and apply a `UNIQUE(enrollment_no, request_type, target_id) WHERE status='pending'` partial index (supported by both Postgres and modern SQLite).
  - **Severity:** High

- **Scenario:** Fine rate changed mid-loan period (Librarian)
  - **Trace:** `get_portal_fine_per_day` dynamically fetches the fine rate. When calculating overdue fines upon return, it multiplies `days_overdue * current_fine_rate`.
  - **Problem:** If a student borrows a book when the fine is $1/day, and is 10 days overdue, they expect a $10 fine. If the librarian changes the system setting to $5/day today, the system will retroactively calculate their fine as $50 at the counter.
  - **Fix:** The fine rate must be locked in at the time of borrowing. Add a `fine_rate_applied` column to `borrow_records` populated at checkout. Use this historical rate for the fine calculation upon return, not the current global system setting.
  - **Severity:** High
