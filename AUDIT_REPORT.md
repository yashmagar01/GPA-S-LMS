
# Adversarial Testing Audit Report: GPA-S-LMS

## A. Student Borrowing Lifecycle
✅ **Catalogue browsing & First login:** Handled via frontend PWA and Flask auth APIs correctly.
✅ **Duplicate request:** The system blocks duplicate requests checking pending `request_type = 'book_request'` and `details` JSON payload.
✅ **Requesting when at borrowing limit:** Handled correctly. Checks active `borrowed` records and limits up to `MAX_BOOKS_PER_STUDENT` env var (default 5).
❌ **Requesting when overdue:**
- **Scenario:** Student has an overdue book (return date passed) and tries to request a new book.
- **Current Behavior:** `api_submit_request` in `student_portal.py` checks for the total count of borrowed books, but it **does not verify if any existing books are overdue** before allowing a new request.
- **Why it’s a problem:** In real libraries, students with overdue items are blocked from borrowing new ones until they return the late items and clear fines.
- **Fix:** In `api_submit_request` (`student_portal.py`), query `borrow_records` to check if `due_date < current_date` for the student where `status = 'borrowed'`. If count > 0, return `403 Forbidden` with a message blocking the request until the overdue book is returned.
- **Severity:** High

⚠️ **Requesting cancellation before/after approval:**
- **Scenario:** Student submits cancellation for an approved request or physically collecting the book logic.
- **Current Behavior:** `api_cancel_request` sets `status = 'cancelled'`. If a request is already approved, canceling it doesn't revert the underlying record changes (like available copies).
- **Fix:** In `api_cancel_request` (`student_portal.py`), check `if request['status'] != 'pending'`. Prevent cancellation of approved requests or implement rollback logic.
- **Severity:** Medium

✅ **Approval received:** Email is dispatched by `api_admin_approve_request`. Status changes correctly.
⚠️ **Going to collect / Physically collecting the book:**
- **Scenario:** Student gets an approval, and goes to the library to collect it.
- **Current Behavior:** The `api_admin_approve_request` marks the request as `approved` and immediately inserts a `borrowed` record into `borrow_records`, deducting an available copy. This implies approval == physical collection.
- **Fix:** This design means an approved request is an active loan from that moment. The logic handles it, though it tightly couples approval with physical handover.
- **Severity:** Low

⚠️ **NOT collecting within deadline:**
- **Scenario:** A book request is approved, but the student never comes to collect it.
- **Current Behavior:** The book is permanently checked out to them because approval creates the `borrow_records` entry immediately.
- **Why it's a problem:** A book is locked indefinitely, and the student might start accruing late fees for a book they never physically took.
- **Fix:** Add a periodic cleanup script or a button for the librarian to cancel "approved but not collected" items.
- **Severity:** High

## B. Renewal Lifecycle
✅ **Normal renewal request & Duplicate renewal:** Handled by checking `details` for `accession_no` in pending requests.
✅ **Renewal when at max renewal count:** Desktop app limits it, though portal should ideally prevent the request entirely.
❌ **Renewal when another student is on the waitlist:**
- **Scenario:** Student requests renewal for a book that is currently out of stock and has students waiting.
- **Current Behavior:** The portal allows the renewal request. The librarian might blindly approve it from the admin panel without seeing waitlist status.
- **Why it’s a problem:** Waitlisted students will be unfairly delayed.
- **Fix:** In `api_submit_request` for `req_type == 'renewal'`, query the `book_waitlist` table for `book_id`. If count > 0, reject the renewal request with `409 Conflict` and prompt them to return the book.
- **Severity:** High

❌ **Renewal for overdue book with accrued fines:**
- **Scenario:** Student requests renewal on a book that is already overdue.
- **Current Behavior:** `api_submit_request` does not check if the book being renewed is overdue.
- **Why it’s a problem:** Students bypass paying fines by simply renewing late items.
- **Fix:** In `api_submit_request` (renewal branch), check if `due_date < current_date` for the specific `accession_no`. Reject the request if overdue.
- **Severity:** High

❌ **Librarian approving a renewal after the book was already returned:**
- **Scenario:** A renewal request was made, the student returns the book in person, then the librarian clicks 'Approve' on the stale renewal request.
- **Current Behavior:** `api_admin_approve_request` for `renewal` extends the due date of a record that is now marked as `returned`.
- **Fix:** Add a check `if record['status'] != 'borrowed'` before updating `due_date` in `api_admin_approve_request`.
- **Severity:** Medium

## C. Return & Fine Lifecycle
✅ **On-time return & late return with fine:** Fines are calculated correctly on the dashboard and in desktop app.
✅ **Fine payment acknowledgment & clearance:** Managed in desktop app transactions.
❌ **Student attempting to borrow while fine is unpaid:**
- **Scenario:** Student has an active fine (unpaid) and requests a new book.
- **Current Behavior:** `api_submit_request` checks for maximum allowed books, but completely ignores unpaid fines.
- **Why it’s a problem:** Students avoid paying fines while still using library services.
- **Fix:** In `api_submit_request` (`student_portal.py`), calculate the student's total unpaid fines (or check for any). If `total_fine > 0`, reject new book requests and renewals.
- **Severity:** Critical

❌ **Lost book scenario / Damaged book scenario:**
- **Scenario:** A student loses or damages a book.
- **Current Behavior:** There is no workflow or endpoint to mark an active loan as `lost` or `damaged` and assign a penalty fine for the cost of the book.
- **Fix:** Add a dedicated function in `main.py` (Desktop app) or a portal request type for lost items, which calculates a lost penalty fine and changes the `borrow_records` status to `lost`.
- **Severity:** High

❌ **Fine waiver by librarian:**
- **Scenario:** The librarian wants to waive a late fine (e.g., due to medical emergency).
- **Current Behavior:** The `fine` amount is automatically computed based on the difference between `due_date` and `return_date` in the desktop app's return processing. There is no easy way to override this dynamically at the point of return.
- **Fix:** Add a checkbox or input field in the `Return` form in `main.py` allowing a custom fine override (or 0 fine).
- **Severity:** Medium

## D. Waitlist Lifecycle
✅ **Joining/Leaving waitlist:** Handled via `add_to_waitlist` and `remove_from_waitlist`.
✅ **Being notified when book is returned:** Implemented in `email_batch_service` or via trigger logic.
⚠️ **Waitlist notification race condition / expiration:**
- **Scenario:** A book is returned. Waitlisted students get notified.
- **Current Behavior:** There is no strict expiration or reservation window for the first person on the waitlist. First come, first served to whoever requests it first.
- **Why it’s a problem:** The person who waited longest might miss it again.
- **Fix:** Add a `reserved_until` column in `books` or a new reservation table, and only allow the top waitlisted user to request it within 24 hours.
- **Severity:** Medium

❌ **Multiple students on waitlist for same book:**
- **Scenario:** 5 students are on the waitlist. 1 copy becomes available.
- **Current Behavior:** The waitlist does not process notifications sequentially. If all get an email, 5 students rush to request it, and 4 get rejected.
- **Fix:** Only email the oldest waitlist entry (`ORDER BY created_at ASC LIMIT 1`).
- **Severity:** Medium

## E. Account & Identity Scenarios
✅ **First login password change:** Enforced by PWA alerts.
✅ **Forgot password & profile update:** Handled correctly.
❌ **Password change with session active on another device:**
- **Scenario:** Student changes password on mobile, but laptop is still logged in.
- **Current Behavior:** Flask's default cookie-based session doesn't automatically invalidate other sessions on password change because the session cookie signature doesn't include a password hash version.
- **Fix:** Append a `password_version` or hash fragment to the Flask session, and verify it in a `@app.before_request` hook. Update the version upon password change.
- **Severity:** Medium

❌ **Stale password reset request:**
- **Scenario:** A user clicks a forgot password link sent a week ago.
- **Current Behavior:** The password reset flow does not seem to employ expiring tokens correctly.
- **Fix:** Store an expiry timestamp in the token (e.g. JWT) or in a database `reset_tokens` table. Check if `current_time > expiry_time` before resetting.
- **Severity:** High

❌ **Year changing to Pass Out with active loans:**
- **Scenario:** The academic year advances, and final-year students are updated to "Pass Out", but they still have unreturned books.
- **Current Behavior:** Pass Out students are blocked from making *new* requests (`api_submit_request`), but their existing active loans stay active. There is no automated prompt or strict lock for the librarian to recover these.
- **Fix:** When updating a student's year to "Pass Out" in `main.py`, run a check against `borrow_records`. If active loans exist, prompt a stern warning or generate an automatic "No Dues Pending" report requirement.
- **Severity:** High

❌ **Self-registration approval and rejection:**
- **Scenario:** Users self-register via `api_public_register_student`.
- **Current Behavior:** The `api_public_register_student` directly inserts the student into the `students` table and `student_auth` table as fully verified and active. There is no approval step.
- **Fix:** Change self-registration to insert into a `pending_registrations` table or flag `status='pending'`. Require the librarian to approve it via `main.py` or `/api/admin/` endpoints.
- **Severity:** Critical

❌ **Account deletion with active loans/fines:**
- **Scenario:** Student requests account deletion via `/api/request-deletion`.
- **Current Behavior:** The API accepts the deletion request and sets `status='pending'` without checking active loans or fines. The admin might approve it, leaving orphaned loans.
- **Fix:** In `request_deletion` endpoint, check `borrow_records` for active loans or unpaid fines. If any exist, return `409` and prevent the deletion request submission entirely.
- **Severity:** High

## F. Librarian Daily Operations
✅ **Approving/rejecting requests & managing notices:** Admin API endpoints are functional.
❌ **Unauthenticated access to admin endpoints:**
- **Scenario:** Any external user or student accesses `/api/admin/*` endpoints.
- **Current Behavior:** The code memory states `enforce_admin_local_access` hook ensures local access. However, this hook was removed or is missing from `student_portal.py` (verified by grep). The admin routes like `/api/admin/all-requests` have **no authentication or IP restriction** at all.
- **Why it’s a problem:** Anyone on the internet can approve requests, clear fines, or delete users.
- **Fix:** Add an `@app.before_request` hook that checks `if request.path.startswith('/api/admin/')` and validates `request.remote_addr` against `['127.0.0.1', '::1']`. Reject with 403 if it doesn't match. Do not trust `X-Forwarded-For`.
- **Severity:** Critical

❌ **Issuing a book directly at the counter (bypass portal):**
- **Scenario:** Student walks up to the counter, librarian issues book via `main.py`.
- **Current Behavior:** Handled correctly in Desktop App. However, the sync might be delayed, causing the portal to still show it as available.
- **Fix:** Trigger a synchronous `_push_to_cloud` immediately in `main.py` when a direct issue happens.
- **Severity:** Low

❌ **Viewing overdue list & marking fine as paid:**
- **Scenario:** Desktop app shows overdue lists.
- **Current Behavior:** Managed correctly.

❌ **Importing Excel transaction data:**
- **Scenario:** Librarian bulk imports from Excel.
- **Current Behavior:** Code exists in `main.py` for Excel import but lacks deduplication checks or transaction atomicity. Duplicate rows can be inserted if imported twice.
- **Fix:** In the Excel import function in `main.py`, use an UPSERT query (`ON CONFLICT`) based on `enrollment_no` and `transaction_date` or `receipt_no`.
- **Severity:** High

❌ **Managing book catalogue:**
- **Scenario:** Adding or editing books.
- **Current Behavior:** Handled correctly via Tkinter, but changing the `total_copies` does not validate if `available_copies` becomes negative.
- **Fix:** In the book edit function, ensure `new_total_copies >= (total_copies - available_copies)`.
- **Severity:** Medium

## G. Sync & Data Integrity
✅ **Desktop app syncing to Supabase:** Handled by `sync_manager.py`.
⚠️ **Sync conflict when same record modified:**
- **Scenario:** Desktop and cloud update the same book's `available_copies` simultaneously.
- **Current Behavior:** It's a last-write-wins model.
- **Fix:** Use optimistic locking (e.g., `updated_at` timestamp check or version column) during updates in `sync_manager.py`.
- **Severity:** Medium

❌ **Cloud pulling updates to local & transaction duplicates:**
- **Scenario:** Sync manager pulls cloud data to local SQLite.
- **Current Behavior:** The bidirectional sync in `sync_manager.py` uses delta sync but might replay transactions if IDs desync.
- **Fix:** Ensure UUIDs or unique transaction hashes are used for sync comparisons instead of auto-incrementing integer IDs.
- **Severity:** High

## H. Deployment & Infrastructure
✅ **SQLite vs PostgreSQL compatibility:** Handled by wrappers.
❌ **Render cold start and session behavior:**
- **Scenario:** Render spins down the Flask app. It spins back up.
- **Current Behavior:** If Flask uses the default in-memory session signing key, all active user sessions will be invalidated on a cold start.
- **Fix:** Provide a hardcoded `FLASK_SECRET_KEY` environment variable in the Render dashboard and use it instead of generating a random one at startup in `get_or_create_secret_key()`.
- **Severity:** Medium

❌ **SQLite vs PostgreSQL query compatibility (ILIKE vs LIKE):**
- **Scenario:** Search functionality is case-insensitive.
- **Current Behavior:** In PostgreSQL, `LIKE` is case-sensitive and `ILIKE` is required. SQLite does not support `ILIKE` but its `LIKE` is case-insensitive.
- **Fix:** In `database.py` or search queries, use an abstraction or dynamic query builder that uses `ILIKE` for Postgres and `LIKE` for SQLite.
- **Severity:** Medium

❌ **Missing environment variable at runtime:**
- **Scenario:** Application starts without `DATABASE_URL` in production.
- **Current Behavior:** It quietly falls back to SQLite, which writes locally to an ephemeral Render disk, meaning all data is lost on the next deployment or restart.
- **Fix:** If `os.getenv("PORTAL_USE_CLOUD") == "1"`, explicitly check for `DATABASE_URL` and `raise ValueError` if missing to fail fast.
- **Severity:** Critical

❌ **Unauthenticated study material downloads:**
- **Scenario:** External user accesses `/api/study-materials/<id>/download`.
- **Current Behavior:** The endpoint `download_study_material` lacks session validation (`if 'student_id' not in session:`).
- **Why it’s a problem:** Unauthorized public access to copyrighted or internal college materials, driving up bandwidth costs on Render.
- **Fix:** Add `if 'student_id' not in session: return jsonify({'error': 'Unauthorized'}), 401` to `download_study_material`.
- **Severity:** High

❌ **CSRF protection gaps:**
- **Scenario:** Admin routes and webhooks might bypass CSRF.
- **Current Behavior:** Admin routes are excluded via `if request.path.startswith('/api/admin/'): return` in `csrf_protect`. But if the IP restriction is also missing, an attacker can perform CSRF on the admin endpoints.
- **Fix:** Enforce the IP restriction rigorously, or require CSRF tokens even for admin routes if the desktop app can send them.
- **Severity:** High

## I. Notification & Email Pipeline
✅ **Email delivery & in-app notifications:** Triggers background threads.
❌ **Email failing silently:**
- **Scenario:** SMTP credentials are wrong or rate-limited.
- **Current Behavior:** `send_email_bg` in `student_portal.py` runs in a thread. If it fails, it prints to console but doesn't alert the system or queue it for retry.
- **Fix:** Implement a retry queue or save failed emails to a database table `email_outbox` with status `failed` for the librarian to review.
- **Severity:** Medium

❌ **Notification for an event that has no template defined:**
- **Scenario:** The system tries to send an email for an unknown notification type.
- **Current Behavior:** The template generator might throw an exception, crashing the thread and silently failing the notification.
- **Fix:** Add a fallback default template in `generate_email_template` if `theme` or `req_type` is unrecognized.
- **Severity:** Low

❌ **Orphaned notifications after account deletion:**
- **Scenario:** A student account is deleted.
- **Current Behavior:** The notifications in `user_notifications` might not cascade delete if there's no FK `ON DELETE CASCADE`.
- **Fix:** Ensure `DELETE FROM user_notifications WHERE enrollment_no = ?` is run when an account is deleted.
- **Severity:** Low

## J. Edge Cases & Stress Scenarios
✅ **0 books empty state:** Frontend handles empty arrays correctly.
❌ **Student submitting the same request 20 times rapidly:**
- **Scenario:** Student spams the "Request Book" button.
- **Current Behavior:** The `rate_limit` decorator in `student_portal.py` is in-memory and only applied to auth routes (login/register). `/api/request` is not rate-limited.
- **Fix:** Apply the `@rate_limit(limit=5, window=60)` decorator to `api_submit_request`.
- **Severity:** High

❌ **Student with a name containing special characters breaking email templates:**
- **Scenario:** Student name is `O'Connor` or contains HTML tags.
- **Current Behavior:** The email template might inject the name directly, potentially leading to malformed HTML or injection.
- **Fix:** Use standard HTML escaping (`html.escape()`) for variables before injecting them into the email template string.
- **Severity:** Medium

❌ **Concurrent approvals for the same single-copy book:**
- **Scenario:** Two librarians operate two desktop app instances and approve two requests for the same book simultaneously. The book only has 1 available copy.
- **Current Behavior:** Both will deduct `available_copies`, resulting in `-1` copies and two loans.
- **Fix:** In `api_admin_approve_request`, add a constraint check `if available_copies <= 0: return error` inside a database transaction lock (e.g. `SELECT ... FOR UPDATE` in Postgres).
- **Severity:** High

❌ **Fine rate changed mid-loan period:**
- **Scenario:** A book is borrowed, fine rate is 1. The admin changes it to 5. The student returns it 1 day late.
- **Current Behavior:** The fine calculation probably uses the *current* fine rate at the time of return, punishing the student retroactively.
- **Fix:** Store the applicable fine rate at the time of borrowing in `borrow_records`, or explicitly document that the fine rate is dynamic.
- **Severity:** Medium
