# Adversarial Testing Audit: GPA-S-LMS

## Overview
This document contains the findings of an adversarial testing audit performed on the GPA-S-LMS system. The audit evaluates the system across various domains to identify functional gaps, security vulnerabilities, edge cases, and architectural flaws, without providing specific code suggestions.

## A. Student Borrowing Lifecycle
✅ **What works:**
*   First login (handles first-time password setup).
*   Catalogue browsing (pagination, category filtering, search).
*   Duplicate request prevention.
*   Requesting when overdue (no hard blocker exists for this in the code, though it shows overdue items on dashboard).
*   Going to collect (Librarian issues book directly or approves request and it creates borrow record).
*   Physically collecting the book.

❌ **What is missing or broken:**
**Scenario: Requesting a book when at the borrowing limit**
*   **Persona:** Student
*   **What currently happens:** The `api_submit_request` in `student_portal.py` checks `MAX_BOOKS_PER_STUDENT` (default 5) from `os.getenv` against active borrows. However, this environment variable is NOT synchronized with the dynamic `library_settings.json` that the Librarian modifies via `main.py` (`self.library_settings.get('max_books_per_student', 5)`).
*   **Why this is a problem:** If a librarian changes the maximum allowed books to 3 via the desktop app, the web portal still allows 5. This breaks policy enforcement.
*   **Precise Fix:** Modify `api_submit_request` (`student_portal.py:2812`) to fetch the `max_books_per_student` limit directly from the shared DB setting (similar to how `get_portal_fine_per_day` works, querying the `system_settings` table) instead of relying solely on `os.getenv`.
*   **Severity:** High

**Scenario: Requesting an out-of-stock book (Waitlist Bypass)**
*   **Persona:** Student
*   **What currently happens:** A student can submit a `book_request` for a book even if `available_copies` is 0. The check in `api_submit_request` only prevents duplicate requests and checks borrow limits, but it DOES NOT check `available_copies`. The librarian will later see an error when trying to approve it ("No available copies left").
*   **Why this is a problem:** Students think they successfully requested a book, but the librarian can never approve it. The UI should force them to use the waitlist feature instead.
*   **Precise Fix:** In `api_submit_request` (`student_portal.py`), when `req_type == 'book_request'`, query the `books` table for `available_copies`. If it is <= 0, return a 400 error indicating the book is out of stock and the user should join the waitlist instead.
*   **Severity:** High

**Scenario: NOT collecting within deadline**
*   **Persona:** Student/Librarian
*   **What currently happens:** When a book request is approved, `api_admin_approve_request` inserts a `borrow_record` (creating an active loan) and decrements `available_copies`. The email sent to the student says "If not collected by the deadline [2 days], the reservation will be cancelled." However, there is NO logic anywhere in the codebase to actually check for uncollected books and cancel them automatically.
*   **Why this is a problem:** If a student never collects the book, it remains "borrowed" indefinitely in the system, preventing others from accessing it.
*   **Precise Fix:** Implement a background thread/scheduler in the backend that periodically queries `borrow_records` where the physical collection flag (which would need to be added to the schema, or distinguishing reservations from actual issuance) is false, and automatically deletes the borrow record and restores `available_copies` if the timestamp exceeds 48 hours. Currently, approval equals issuance in the system.
*   **Severity:** Critical

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Request cancellation before/after approval**
*   **Persona:** Student
*   **What currently happens:** `api_cancel_request` updates the status to 'cancelled'. This works fine before approval. However, if a request is already 'approved' (meaning the borrow record is created and copies decremented), the endpoint correctly prevents cancellation (`if req['status'] != 'pending'`). But the student receives no guidance on how to return the approved, uncollected book.
*   **Precise Fix:** No backend logic change needed for the cancellation logic, but the frontend should clarify that approved requests must be collected or manually cancelled by the librarian.
*   **Severity:** Low

## B. Renewal Lifecycle
✅ **What works:**
*   Normal renewal request.
*   Checking for duplicate pending renewals.

❌ **What is missing or broken:**
**Scenario: Renewal when another student is on the waitlist**
*   **Persona:** Student/Librarian
*   **What currently happens:** `api_submit_request` allows a student to submit a renewal request. `api_admin_approve_request` allows the librarian to approve it. Neither endpoint checks if the book has an active `book_waitlist` queue.
*   **Why this is a problem:** Students can perpetually renew a book while others wait indefinitely, defeating the purpose of loan limits and waitlists.
*   **Precise Fix:** In `api_admin_approve_request`, before extending the due date for a 'renewal', check if there are any unnotified waitlist entries (`notified=0`) for that `book_id`. If there are, reject the renewal and notify the student that the book is requested by others.
*   **Severity:** High

**Scenario: Librarian approving a renewal after the book was already returned**
*   **Persona:** Librarian
*   **What currently happens:** If a student returns a book physically, and the librarian marks it returned via the desktop app, but a stale renewal request is still pending in the portal, the librarian can approve the renewal. `api_admin_approve_request` tries to find the latest borrow record (`return_date IS NULL`). If none exists, the `UPDATE borrow_records SET due_date = ...` query finds 0 rows and fails silently, but the request status is still marked 'approved' and a success notification/email is sent to the student.
*   **Why this is a problem:** Confusing state and false notifications sent to the student.
*   **Precise Fix:** In `api_admin_approve_request` for `renewal`, check `cursor_lib.rowcount` after the UPDATE. If it's 0, it means no active borrow record exists. Rollback the transaction and return an error ("Cannot renew: Book is not currently borrowed by this student").
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Renewal at maximum renewal count**
*   **Persona:** Student
*   **What currently happens:** The system specifies policies (e.g., "2 Renewals per book" in `api_me`), but there is NO tracking of how many times a specific borrow record has been renewed.
*   **Why this is a problem:** Students can renew a book an unlimited number of times, ignoring the stated library policy.
*   **Precise Fix:** Add a `renewal_count` integer column to the `borrow_records` table (default 0). In `api_admin_approve_request`, increment this count when approving a renewal. Add a check to reject the renewal if the count is >= 2.
*   **Severity:** Medium

**Scenario: Renewal when already overdue**
*   **Persona:** Student/Librarian
*   **What currently happens:** The system allows submitting and approving renewals for overdue books. The due date is extended from today (using `max(current_due, datetime.now())`). However, accrued fines up to this point are not explicitly locked or dealt with; they might be wiped or frozen depending on how fines are handled on final return.
*   **Precise Fix:** Decide if overdue renewals are allowed by policy. If not, block them in `api_submit_request`.
*   **Severity:** Low

## C. Return & Fine Lifecycle
✅ **What works:**
*   On-time return (managed via desktop app).
*   Late return with fine (managed via desktop app).
*   Fine appearing on dashboard (dynamically calculated).

❌ **What is missing or broken:**
**Scenario: Student attempting to borrow while fine is unpaid**
*   **Persona:** Student
*   **What currently happens:** There is no check in `api_submit_request` to prevent a student from requesting new books if they have outstanding unpaid fines.
*   **Why this is a problem:** Students can accumulate large fines and continue borrowing books, leading to unrecoverable debts and policy violations.
*   **Precise Fix:** In `api_submit_request`, before allowing a `book_request`, sum the unpaid fines for the student from `borrow_records` (where `fine > 0`). If the sum exceeds a threshold (or is > 0), reject the request with a message to clear fines first.
*   **Severity:** High

**Scenario: Lost/Damaged book scenario**
*   **Persona:** Librarian
*   **What currently happens:** There is no explicit "Lost" or "Damaged" status for books or borrow records. A librarian can only mark a book as "returned".
*   **Why this is a problem:** A lost book cannot be accurately tracked, and inventory will mistakenly increment available copies if simply marked returned.
*   **Precise Fix:** Add a "Lost/Damaged" status option to the desktop app (`main.py`) for borrow records, which updates the `borrow_records` status, applies a replacement fine, and DOES NOT increment `available_copies` on the `books` table.
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Fine payment acknowledgment, clearing, and fine waiver**
*   **Persona:** Librarian/Student
*   **What currently happens:** Fines are stored in `borrow_records.fine` upon return. The system tracks the fine amount, but there is no ledger for *payments*. A fine is either there or not. The desktop app does not seem to have a clear "Pay Fine" or "Waive Fine" ledger transaction mechanism, just an edit to the record.
*   **Precise Fix:** Add a dedicated payments table or payment status column to handle fine clearing and waivers properly.
*   **Severity:** Low

**Scenario: Fine rate changed mid-loan period**
*   **Persona:** Both
*   **What currently happens:** `api_alerts` and `api_dashboard` calculate fines dynamically based on `days_late * current_fine_per_day`. If a book is 10 days late under a 5 Rs fine (50 Rs), and the librarian changes the fine to 10 Rs, the student's dashboard instantly shows 100 Rs fine. However, manual returns might lock in different values depending on when they happen.
*   **Why this is a problem:** Discrepancy between what the student sees on the dashboard and the actual database `fine` column which is only updated occasionally or on return.
*   **Precise Fix:** The dashboard calculation (`api_dashboard`) should be clear that it's an "Estimated Fine". Ideally, fines should be calculated based on the fine rate at the time of borrowing (storing it in `borrow_records`).
*   **Severity:** Low

## D. Waitlist Lifecycle
✅ **What works:**
*   Joining waitlist for unavailable book.
*   Leaving waitlist.

❌ **What is missing or broken:**
**Scenario: Being notified when book is returned**
*   **Persona:** Student/Librarian
*   **What currently happens:** The system allows students to join a waitlist via `/api/books/<book_id>/notify`. However, when a book is returned (processed in `main.py`'s desktop UI or during account deletion), there is NO logic to check the `book_waitlist` table and notify the waiting students.
*   **Why this is a problem:** The waitlist feature is a dead end. Students join the waitlist but are never actually notified when the book becomes available.
*   **Precise Fix:** Create a new endpoint in `student_portal.py` (e.g., `/api/admin/trigger-waitlist/<book_id>`) that the desktop app (`main.py`) calls when a book is successfully returned. This endpoint should find the first unnotified student in `book_waitlist` for that book, trigger a notification email, and update `notified=1`.
*   **Severity:** Critical

**Scenario: Acting on notification within a window / Notification expiring**
*   **Persona:** Student
*   **What currently happens:** Because notifications are never sent, there is also no logic for waitlist expirations.
*   **Why this is a problem:** Even if notifications were sent, if the first student doesn't act, the second student never gets a chance.
*   **Precise Fix:** Implement a cron job or background thread that checks for `notified=1` entries older than 24/48 hours. If found, delete/mark expired, and notify the next student in the queue.
*   **Severity:** High

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Multiple students on waitlist for same book**
*   **Persona:** Students
*   **What currently happens:** Multiple students can join, but since the queue is never processed, order doesn't matter.
*   **Precise Fix:** Once the above fixes are made, ensure the queue processes in order of `created_at`.
*   **Severity:** Low

## E. Account & Identity Scenarios
✅ **What works:**
*   First login password change.
*   Forgot password request submission.
*   Self-registration request submission.
*   Profile update request submission.

❌ **What is missing or broken:**
**Scenario: Account deletion with active loans and fines**
*   **Persona:** Librarian
*   **What currently happens:** In `api_admin_approve_deletion`, the system approves the deletion request, force-updates all active `borrow_records` to `status='returned'`, increments `available_copies`, and deletes the student record. It does NOT check if the student actually returned the books or paid their fines.
*   **Why this is a problem:** A student could borrow 5 expensive books, submit an account deletion request, and if the librarian blindly approves it, the books are recorded as returned and inventory is incremented, destroying the audit trail and leading to lost inventory.
*   **Precise Fix:** In `api_admin_approve_deletion`, before approving, query `borrow_records` for any active loans (`status='borrowed'`) or unpaid fines for that `student_id`. If any exist, return a 400 error ('Cannot delete account: Student has unreturned books or unpaid fines').
*   **Severity:** Critical

**Scenario: Stale password reset request**
*   **Persona:** Student / Librarian
*   **What currently happens:** A student submits a `forgot-password` request. It sits pending. The student remembers their password, logs in, and changes it via `api_change_password`. The pending reset request remains in the queue. The librarian might approve it weeks later, unexpectedly resetting the student's password to the default.
*   **Why this is a problem:** Creates administrative clutter and a security risk where passwords are reset inappropriately.
*   **Precise Fix:** In `api_change_password` and `api_login` (upon successful login if not first login), execute a query to update any pending `password_reset` requests for that `enrollment_no` in the `requests` table to 'cancelled'.
*   **Severity:** Medium

**Scenario: Password change with session active on another device**
*   **Persona:** Student
*   **What currently happens:** Sessions are handled by standard Flask session cookies. There is no session invalidation mechanism for other devices when a password is changed.
*   **Why this is a problem:** If an account is compromised, changing the password doesn't kick out the attacker.
*   **Precise Fix:** Add a `session_version` or `last_password_change` timestamp to the session cookie and validate it against the database on every request.
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Year changing to Pass Out with active loans**
*   **Persona:** Student/Librarian
*   **What currently happens:** The desktop app allows updating a student's year to 'Pass Out'. Pass out students are restricted from making new requests in `api_submit_request`. However, there is no automatic check or alert during the promotion process in `main.py` if the student has active loans.
*   **Precise Fix:** When promoting students to 'Pass Out', display an alert listing any students who still have unreturned books.
*   **Severity:** Low

## F. Librarian Daily Operations
✅ **What works:**
*   Approving and rejecting basic requests.
*   Viewing active loans and overdue lists.
*   Broadcasting notices.
*   Uploading study materials.
*   Importing Excel transaction data.
*   Managing book catalogue.

❌ **What is missing or broken:**
**Scenario: Unauthenticated access to admin endpoints**
*   **Persona:** External Attacker
*   **What currently happens:** The Flask app `student_portal.py` has numerous sensitive endpoints under `/api/admin/*` (e.g., `/api/admin/all-requests`, `/api/admin/deletion/<id>/approve`, `/api/admin/bulk-password-reset`). The memory states these enforce local access by checking `request.remote_addr`, but the actual source code is MISSING this check entirely. There is no authentication or IP restriction on these routes.
*   **Why this is a problem:** Since the portal is deployed on Render and exposed to the public internet, anyone can hit these endpoints to approve deletions, approve requests, or reset all student passwords, leading to complete system compromise.
*   **Precise Fix:** Implement the missing `@app.before_request` hook. If `request.path.startswith('/api/admin/')`, check if `request.remote_addr` is `127.0.0.1` or `::1`. Since it's behind Render, ensure you also validate a shared secret token passed in headers by the Desktop App, as `remote_addr` might reflect the load balancer.
*   **Severity:** Critical

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Issuing a book directly at the counter (bypass portal) / Processing a return**
*   **Persona:** Librarian
*   **What currently happens:** This is handled in `main.py` (Desktop app). While functional, it does not trigger portal notifications (like waitlist updates as mentioned above) or realtime sync to the portal's active connection without a polling interval.
*   **Severity:** Low

## G. Sync & Data Integrity
✅ **What works:**
*   Syncing `user_settings` partial updates (BUG A5 FIX implemented).
*   Desktop app syncing to Supabase.
*   Cloud pulling updates to local.

❌ **What is missing or broken:**
**Scenario: Transaction imported from Excel creating duplicate records**
*   **Persona:** Librarian
*   **What currently happens:** In `main.py` `_import_transactions_worker`, it caches existing keys (`enrollment_no, accession_no, borrow_date`) to detect duplicates. However, if an Excel file has slight variations in dates or missing times, or if the same transaction is imported twice with different statuses, it might bypass the weak duplicate check. Furthermore, it creates placeholder books if `accession_no` isn't found, but does not gracefully merge them if the real book is added later.
*   **Why this is a problem:** Can lead to inflated loan statistics and duplicate fine generation.
*   **Precise Fix:** Improve the duplicate checking logic in `_import_transactions_worker` to use a more robust combination of fields and exact date matching, or rely on a unique external ID if available.
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Sync conflict when the same record is modified on both desktop and portal simultaneously**
*   **Persona:** System
*   **What currently happens:** The `sync_manager.py` uses bidirectional sync. For portal requests, `_push_to_cloud` in `student_portal.py` replicates writes to Supabase. If the desktop app updates a record (e.g., `borrow_records`) locally and the portal pushes a change to the same record in the cloud at the same time, the sync manager relies on `updated_at` timestamps (last write wins).
*   **Why this is a problem:** `_push_to_cloud` is a fire-and-forget background thread. If it fails or lags, and a sync happens, data might be overwritten silently.
*   **Precise Fix:** Ensure `_push_to_cloud` implements retry logic for transient network failures, and log conflicts clearly for manual resolution.
*   **Severity:** Medium

## H. Deployment & Infrastructure
✅ **What works:**
*   Render cold start handling (Waitress).
*   SQLite vs PostgreSQL query compatibility (mostly handled via `PostgresConnectionWrapper`).

❌ **What is missing or broken:**
**Scenario: Unauthenticated study material downloads**
*   **Persona:** External User
*   **What currently happens:** The `/api/study-materials/<int:material_id>/download` endpoint does not check if the user is authenticated (i.e., it doesn't verify if `student_id` is in `session`).
*   **Why this is a problem:** Study materials are meant for registered students. Without authentication, anyone with the URL can scrape and download all college study materials.
*   **Precise Fix:** Add a session check at the beginning of `download_study_material`: `if 'student_id' not in session: return jsonify({'error': 'Unauthorized'}), 401`.
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Rate limiting or lack thereof on sensitive endpoints**
*   **Persona:** External Attacker
*   **What currently happens:** Rate limiting is implemented via `RateLimiter` class and applied to `/api/login` and `/api/public/forgot-password`. However, it is NOT applied to `/api/request` (which could be spammed to fill the database or send endless emails) or the admin endpoints.
*   **Precise Fix:** Apply the `@rate_limit` decorator to `/api/request` and any other state-changing or email-triggering endpoints.
*   **Severity:** Medium

## I. Notification & Email Pipeline
✅ **What works:**
*   Email delivery for standard request types.
*   In-app notifications appearing correctly.
*   Unread count accuracy.

❌ **What is missing or broken:**
**Scenario: Orphaned notifications after account deletion**
*   **Persona:** System
*   **What currently happens:** `api_admin_approve_deletion` deletes from `student_auth`, `requests`, `user_settings`, and `students`. It wraps the deletion of `user_notifications` in a `try/except: pass` block. If it fails for any reason, the notifications are orphaned. Furthermore, the `book_waitlist`, `book_wishlist`, and `book_ratings` tables are completely ignored during account deletion.
*   **Why this is a problem:** Leaves orphaned data scattered across the database, which can cause referential integrity issues or skew analytics.
*   **Precise Fix:** In `api_admin_approve_deletion`, add `DELETE FROM book_waitlist`, `DELETE FROM book_wishlist`, and `DELETE FROM book_ratings` for the given `student_id`.
*   **Severity:** Medium

⚠️ **What is partially implemented or has a hidden edge case:**
**Scenario: Student with a name containing special characters breaking email templates**
*   **Persona:** Student
*   **What currently happens:** In `generate_email_template`, user names are embedded directly into HTML. Python's `smtplib` and MIME modules handle UTF-8, but the HTML template does not explicitly escape HTML entities for the name variable.
*   **Why this is a problem:** If a student registers with a name like `<script>` or containing ampersands, it could break the email formatting or lead to minor HTML injection within the email client.
*   **Precise Fix:** Import `html` and use `html.escape(user_name)` and `html.escape(main_text)` inside `generate_email_template` before injecting them into the HTML string.
*   **Severity:** Low

## J. Edge Cases & Stress Scenarios
✅ **What works:**
*   Student submitting the same request type rapidly (prevented by pending duplicate checks).
*   Catalogue with 0 books showing correct empty state.

❌ **What is missing or broken:**
**Scenario: Concurrent approvals for the same single-copy book**
*   **Persona:** Librarian
*   **What currently happens:** In `api_admin_approve_request`, when approving a `book_request`, the code reads `available_copies`, checks if > 0, and then executes two separate queries: one to insert the `borrow_records` and another to update `available_copies` (`UPDATE books SET available_copies = MAX(0, available_copies - 1)`).
*   **Why this is a problem:** This is a classic race condition. If two librarians (or concurrent API calls) approve requests for the same book simultaneously, both might read `available_copies = 1`, pass the check, and insert borrow records. The `MAX` function prevents negative copies, but 2 copies will be issued when only 1 physical copy exists.
*   **Precise Fix:** Wrap the check and the updates in a single database transaction. In SQLite, use `BEGIN IMMEDIATE` to acquire an exclusive lock before checking `available_copies`. If using Postgres, use `SELECT available_copies FROM books WHERE book_id = ? FOR UPDATE`.
*   **Severity:** High

**Scenario: Librarian approving a request for a student whose account was just deleted**
*   **Persona:** Librarian
*   **What currently happens:** If a student requests a book, then requests deletion, and the librarian approves the deletion first, the book request might still exist if the deletion cleanup failed (or due to concurrent processing). If the librarian then approves the book request, it inserts a borrow record for a non-existent student.
*   **Why this is a problem:** Breaks referential integrity if foreign keys aren't strictly enforced.
*   **Precise Fix:** In `api_admin_approve_request`, verify that the `student_id` still exists in the `students` table before proceeding with the approval.
*   **Severity:** Medium
