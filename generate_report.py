report_content = """# adversarial Testing Audit Report: GPA-S-LMS

## A. Student Borrowing Lifecycle

✅ **What Works:**
- Browsing books, searching, and viewing availability works.
- Requesting an available book properly sets it to "Requested".
- Enforced borrowing limits for students are verified before creating a new borrow request.
- Request cancellation is implemented for pending requests.

❌ **What is Missing or Broken:**
1. **Scenario: Requesting when at borrowing limit.**
   - **Persona:** Student
   - **Trace:** The limit check on the frontend (`/api/request`) counts the number of *currently active loans*. It checks `total_active_loans = len([h for h in history if not h.get('returned')])`. However, it does not count other pending *Requests*. So if a student is allowed 3 books and currently has 2 loans, they can submit 5 simultaneous borrow requests for 5 different books. The desktop app (Librarian) might blindly approve them if not carefully checked, leading to exceeding limits.
   - **Why this is a problem:** Students can game the system and hoard books beyond their limit.
   - **Fix:** In `student_portal.py` -> `api_submit_request`, query the database for BOTH active loans AND pending (Status='pending') 'book_request' requests. Deny the request if `active_loans + pending_borrow_requests >= max_books`.
   - **Severity:** High

2. **Scenario: Not collecting within deadline.**
   - **Persona:** Student/Librarian
   - **Trace:** When a librarian approves a "book_request" request, the book is practically reserved (`api_admin_approve_request` creates a `borrow_record` and decrements `available_copies`). There is no automated job or librarian action trace to expire an approved request if the student never shows up physically to collect the book. The book remains "held" indefinitely, preventing others from borrowing it.
   - **Why this is a problem:** Inventory gets locked up. Students who actually need the book can't get it.
   - **Fix:** Implement a daily cron/background job or an endpoint for the Librarian in `main.py` / `student_portal.py` to list and cancel "approved" borrow requests older than a configurable `collect_deadline_days` (default e.g., 2 days). Cancel them, release the book inventory, and notify the student.
   - **Severity:** High

⚠️ **What is Partially Implemented or Edge Case:**
1. **Scenario: Concurrent requests for single-copy book.**
   - **Persona:** Both
   - **Trace:** If Book X has 1 available copy. Student A and Student B both see it available and submit a "book_request" request at the same time. Both requests go to "pending". The Librarian sees both. The Librarian approves Student A's request. Student B's request stays "pending" but the book is now unavailable.
   - **Why this is a problem:** Librarian might try to approve Student B's request, causing inventory to go negative or failing.
   - **Fix:** In `api_admin_approve_request`, after confirming book availability (`actual_available <= 0`), immediately inside the transaction lock, gracefully reject or hold the approval with an error message instead of failing or causing negative inventory (This seems correctly handled by the `actual_available <= 0` check, but transaction isolation level matters for concurrent approvals).

## B. Renewal Lifecycle

✅ **What Works:**
- Submitting renewal requests.
- Librarian can approve/reject renewals.

❌ **What is Missing or Broken:**
1. **Scenario: Renewal when at the maximum renewal count.**
   - **Persona:** Student
   - **Trace:** Students can submit infinite "renewal" requests. `api_submit_request` does not track or check how many times a specific accession number has been renewed.
   - **Why this is a problem:** Defeats the purpose of loan periods. Students can hold a book indefinitely.
   - **Fix:** Add a `renewal_count` column to `borrow_records` (or calculate it dynamically by checking past 'renewal' requests for this transaction). In `api_submit_request`, block 'renewal' requests if the count exceeds a system-defined maximum.
   - **Severity:** High

2. **Scenario: Renewal when another student is on the waitlist.**
   - **Persona:** Student / Librarian
   - **Trace:** A student requests a renewal. The system doesn't check if the book has a waitlist. The librarian can approve it, keeping the book from the waitlist queue.
   - **Why this is a problem:** Unfair to students waiting for a high-demand book.
   - **Fix:** In `student_portal.py` -> `api_submit_request` (when `type == 'renewal'`), check if `SELECT count(*) FROM book_wishlist WHERE book_id = ?` > 0. If yes, automatically reject or block the renewal request and notify the student they must return it.
   - **Severity:** Medium

3. **Scenario: Librarian approving a renewal for a book already returned.**
   - **Persona:** Librarian
   - **Trace:** Student requests renewal. Student then physically returns the book before librarian approves. Librarian clicks approve. `api_admin_approve_request` will attempt to update the due date of a closed borrow record.
   - **Why this is a problem:** Modifies historical records improperly or fails depending on how the update query is structured.
   - **Fix:** In `api_admin_approve_request` for renewals, check if the corresponding `borrow_records` entry (linked by accession_no/student) is already marked as `returned=1` or `status='returned'`. If so, return an error and auto-reject the stale request.
   - **Severity:** Medium

⚠️ **What is Partially Implemented or Edge Case:**
1. **Scenario: Renewal when fine accrued.**
   - **Persona:** Student
   - **Trace:** If a book is overdue and has an unpaid fine, a student can still request a renewal, and a librarian might approve it.
   - **Fix:** Decide on policy (block renewal if fine > 0) and implement check in `api_submit_request` or warn the librarian in the admin UI.

## C. Return & Fine Lifecycle

✅ **What Works:**
- Fines are calculated based on overdue days.
- Desktop app processes returns.

❌ **What is Missing or Broken:**
1. **Scenario: Student attempting to borrow while fine is unpaid.**
   - **Persona:** Student
   - **Trace:** A student with unpaid fines can continue to submit "book_request" requests. `api_submit_request` checks for limits but doesn't check if the student has pending fines.
   - **Why this is a problem:** Removes the incentive to pay fines; students can ignore fines indefinitely.
   - **Fix:** In `student_portal.py` -> `api_submit_request`, calculate the total unpaid fine for the student (sum of fines where `fine_paid` is false/0). If > 0, return a 403 error preventing any new "book_request" or "renewal" requests until fines are cleared.
   - **Severity:** High

2. **Scenario: Fine clearing sync mechanism.**
   - **Persona:** Both
   - **Trace:** When a fine is marked as paid in the desktop app, does it reliably sync to the portal? If the desktop app updates a local record, the sync must push this state. Checking `sync_manager.py` shows it syncs `borrow_records`, but if `fine_paid` status isn't explicitly pushed, it might drift.
   - **Fix:** Ensure `sync_manager.py` syncs the `fine_paid` status properly when syncing `borrow_records`.

⚠️ **What is Partially Implemented or Edge Case:**
1. **Scenario: Rate change mid-loan.**
   - **Persona:** Student
   - **Trace:** Fines are calculated dynamically using `get_portal_fine_per_day()`. If the fine rate changes, past overdue days are suddenly recalculated at the new rate, changing the fine retroactively.
   - **Fix:** Instead of fully dynamic calculation, fines should be snapshotted or calculated with historical rates, or the rate change only applies to future overdue days. For now, document this behavior.

## D. Waitlist Lifecycle

✅ **What Works:**
- Joining/leaving waitlist endpoints exist (`/api/books/<book_id>/notify`).

❌ **What is Missing or Broken:**
1. **Scenario: Notification expiring / Acting on notification window.**
   - **Persona:** Student
   - **Trace:** There is no mechanism to expire a waitlist notification. If Book X is returned, waitlisted students are notified. But there is no queue management (e.g., reserving it for User A for 24 hours, then User B). It's a free-for-all race condition.
   - **Why this is a problem:** High-demand books will be snatched by whoever clicks fastest, not necessarily the one who waited longest.
   - **Fix:** Implement a "reserved_until" column for books or a specific "next in line" logic where only the top waitlist user is notified and given a 24-48 hour exclusive window to request the book. If they don't, move to the next.
   - **Severity:** Medium

## E. Account & Identity Scenarios

✅ **What Works:**
- Registration, password changes, forgot password flow.
- Profile updates.

❌ **What is Missing or Broken:**
1. **Scenario: Account deletion with active loans/fines.**
   - **Persona:** Student/Librarian
   - **Trace:** In `api_admin_approve_deletion(del_id)`, when an account deletion request is approved, the user record is deleted. However, there is no check if the student currently has unreturned books or unpaid fines.
   - **Why this is a problem:** College loses books and fine revenue if a student deletes their account before settling debts.
   - **Fix:** In `api_admin_approve_deletion`, before proceeding, query `borrow_records` for the user for `returned = 0` (or `status = 'borrowed'`) or unpaid fines. If any exist, reject the deletion approval and return a 400 error indicating debts must be settled first.
   - **Severity:** Critical

2. **Scenario: Stale password reset request.**
   - **Persona:** Student
   - **Trace:** In `api_forgot_password`, a generic flow might not expire tokens. Need to verify how reset links/tokens are managed.
   - **Fix:** Ensure pending "Reset Password" requests in the `requests` table expire or are automatically cleaned up after 24 hours to prevent old requests from being approved maliciously later.
   - **Severity:** Medium

⚠️ **What is Partially Implemented or Edge Case:**
1. **Scenario: Year changing to Pass Out with active loans.**
   - **Persona:** Student
   - **Trace:** No automated offboarding for graduating students. They must be manually cleared.

## F. Librarian Daily Operations

✅ **What Works:**
- Approving/rejecting requests via `api_admin_approve_request` and `api_admin_reject_request`.
- Managing notices and study materials.

❌ **What is Missing or Broken:**
1. **Scenario: Approving a request for a student whose account was just deleted.**
   - **Persona:** Librarian
   - **Trace:** If a student requests a book, then requests account deletion, and the librarian approves the deletion first, the original borrow request remains "pending". If the librarian then tries to approve the borrow request, the system might crash or create an orphaned transaction because the `enrollment_no` no longer exists in `users`/`students`.
   - **Why this is a problem:** Causes database integrity errors or orphaned records.
   - **Fix:** In `api_admin_approve_request`, verify the user still exists in the `students` table before processing the approval. Also, cascade delete pending requests when a user is deleted.
   - **Severity:** High

## G. Sync & Data Integrity

✅ **What Works:**
- `sync_manager.py` handles bi-directional pushes.

❌ **What is Missing or Broken:**
1. **Scenario: Missing `accession_no` drops records silently.**
   - **Persona:** System
   - **Trace:** In `sync_manager.py`, records inserted without `accession_no` are silently ignored during synchronization because `accession_no` is part of the natural key for `borrow_records`. `api_admin_approve_request` for `book_request` currently creates a `borrow_record` *without* an `accession_no`!
   - **Why this is a problem:** Web portal approved loans will NEVER sync back to the local desktop app, causing massive data fragmentation and invisible loans on the librarian's desktop app.
   - **Fix:** Ensure that when a "book_request" request is approved via `api_admin_approve_request`, the librarian *must* supply the `accession_no` of the physical book being assigned (e.g. modify the API to accept `accession_no`), and it must be saved to the database. Add validation in the approval endpoint to require `accession_no`.
   - **Severity:** Critical

## H. Deployment & Infrastructure

✅ **What Works:**
- Dual DB architecture.
- Waitress server.

❌ **What is Missing or Broken:**
1. **Scenario: Unauthenticated access to admin endpoints.**
   - **Persona:** Attacker/Student
   - **Trace:** Routes like `/api/admin/all-requests`, `/api/admin/requests/<int:req_id>/approve` lack `@login_required` or API key checks. They rely on network isolation, but Waitress binds to `0.0.0.0` and Render is public.
   - **Why this is a problem:** ANYONE on the internet can call `/api/admin/requests/1/approve` and approve loans, delete accounts, etc.
   - **Fix:** Implement a hardcoded API Key or Admin Token check. Create an `@admin_required` decorator in `student_portal.py` that checks for a specific `X-Admin-Token` header. Apply this decorator to ALL routes starting with `/api/admin/`. Configure this token in `.env`. Update `main.py` to send this token.
   - **Severity:** Critical

2. **Scenario: Flask Secret Key Ephemerality.**
   - **Persona:** All Users
   - **Trace:** `get_or_create_secret_key()` writes to `.secret_key` file. On Render, the filesystem is ephemeral across deploys or cold starts. This causes all users to be logged out unexpectedly.
   - **Why this is a problem:** Terrible UX, constant logouts.
   - **Fix:** In `student_portal.py`, ensure `app.secret_key` prioritizes the `FLASK_SECRET_KEY` environment variable. If not set, raise a warning but use a fallback. It must be configured via environment variables in production.
   - **Severity:** High

3. **Scenario: Executemany lacking in PostgresCursorWrapper.**
   - **Persona:** System
   - **Trace:** `PostgresCursorWrapper` lacks `executemany`. If the access log writer uses `executemany` (or any other batch insert), it will crash on the cloud DB.
   - **Why this is a problem:** Observability data is lost, or the app crashes.
   - **Fix:** Implement `executemany(self, sql, seq_of_parameters)` in `PostgresCursorWrapper` in `LibraryApp/database.py` that loops over `seq_of_parameters` and calls `self.execute(sql, params)`.
   - **Severity:** Medium

4. **Scenario: Unauthenticated study material downloads.**
   - **Persona:** External Users
   - **Trace:** `/api/study-materials/<int:material_id>/download` likely doesn't check session.
   - **Fix:** Add session/authentication check to the download endpoint.

## I. Notification & Email Pipeline

✅ **What Works:**
- Email generation.

❌ **What is Missing or Broken:**
1. **Scenario: Email template errors crash the request.**
   - **Persona:** Student
   - **Trace:** `send_email_bg` or `trigger_notification_email`. If the student name has special characters or email is invalid, the SMTP send might fail. If called synchronously anywhere, it blocks the user action.
   - **Fix:** Ensure all email sending is fully detached in a background thread and wrapped in broad `try/except` blocks so SMTP timeouts/failures never return a 500 error to the client.
   - **Severity:** Medium

## J. Edge Cases & Stress Scenarios

✅ **What Works:**
- Rate limiting exists for some endpoints.

❌ **What is Missing or Broken:**
1. **Scenario: Client IP Spoofing via X-Forwarded-For.**
   - **Persona:** Attacker
   - **Trace:** If rate limiting relies on `request.remote_addr` without `ProxyFix`, an attacker can send fake `X-Forwarded-For` headers to bypass rate limits or spoof IPs in the access logs.
   - **Why this is a problem:** Renders rate limiting useless.
   - **Fix:** In `student_portal.py` setup, wrap the Flask app with `werkzeug.middleware.proxy_fix.ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_prefix=1)`.
   - **Severity:** High

2. **Scenario: Title with single quotes breaks SQL.**
   - **Persona:** System
   - **Trace:** Ensure all search queries (e.g., catalogue search) use parameterized queries (`?` or `%s`) and NOT string concatenation or f-strings.
   - **Fix:** Audit `search_books` or catalogue query generation to guarantee parameterization.
"""

with open("audit_report.md", "w") as f:
    f.write(report_content)
