# GPA-S-LMS Adversarial Testing Audit Report

## A. Student Borrowing Lifecycle
✅ What works:
- First login requires a password change (default password check in `/api/login` and `/api/alerts`).
- Catalogue browsing works with pagination and availability filters (`/api/books`).
- Borrow limits are enforced at the DB level (`database.py` max limit is 20, portal hardcodes a check for 5 in `api_submit_request`).
- Physical collection (issuing at counter) creates records and decrements available copies safely with WAL and `BEGIN IMMEDIATE`.

❌ What is missing or broken:
- **Scenario:** Requesting a book when overdue.
  - **Persona:** Student.
  - **What currently happens:** `api_submit_request` in `student_portal.py` checks for the active loan of the *same* book and the maximum borrow limit, but it *fails* to check if the student has any currently overdue books.
  - **Why it's a problem:** Students with overdue books can continue to reserve and hoard new books, undermining the fine policy.
  - **Fix:** In `api_submit_request` (for `book_request`), query `borrow_records` where `enrollment_no = ? AND status = 'borrowed' AND due_date < ?` (today). If count > 0, return `403 Forbidden` with an appropriate error message.
  - **Severity:** High

- **Scenario:** Concurrency race condition on single-copy book reservation.
  - **Persona:** Student / Librarian.
  - **What currently happens:** In `api_admin_approve_request` for `book_request`, the librarian checks `available_copies > 0`, and then separately executes an `INSERT` and an `UPDATE` without a transaction block. Two librarians approving requests for the same single copy simultaneously will push `available_copies` into the negative or create phantom reservations.
  - **Why it's a problem:** Over-committing physical inventory creates disputes at the circulation desk.
  - **Fix:** In `api_admin_approve_request`, when creating the `borrow_record`, wrap the `SELECT`, `INSERT`, and `UPDATE` inside a transaction or change the `UPDATE` query to include `WHERE available_copies > 0` and check `cursor.rowcount == 1` before proceeding to create the `borrow_record`.
  - **Severity:** High

⚠️ Partially implemented / Hidden Edge Case:
- **Scenario:** Requesting cancellation before/after approval.
  - **Persona:** Student.
  - **What currently happens:** A student can cancel a 'pending' request via `/api/request/<id>/cancel`. However, if the request is already 'approved' (e.g., for a book reservation), the cancellation endpoint correctly blocks it. But there is no workflow for the student to cancel an *approved* reservation if they change their mind, leaving the book locked until the librarian notices they didn't pick it up.
  - **Why it's a problem:** `available_copies` remains decremented for 2 days (pickup deadline), preventing others from borrowing it.
  - **Fix:** Implement a cron job or scheduled task (in `sync_manager.py` or similar) that scans `borrow_records` where `status = 'borrowed'` but the book was never physically scanned out (needs a sub-status or physical collection flag), and automatically reverts the reservation after 48 hours.
  - **Severity:** Medium


## B. Renewal Lifecycle
✅ What works:
- Basic renewal requests are successfully submitted.
- Approval correctly extends the due date by 7 days.

❌ What is missing or broken:
- **Scenario:** Renewal when another student is on the waitlist.
  - **Persona:** Student / Librarian.
  - **What currently happens:** `api_submit_request` and `api_admin_approve_request` do not check the `book_waitlist` table when a student requests a renewal. The librarian approves it blindly, extending the loan.
  - **Why it's a problem:** Waitlisted students are indefinitely blocked from accessing popular books.
  - **Fix:** In `api_submit_request` (when `req_type == 'renewal'`), query `book_waitlist` where `book_id = ? AND notified = 0`. If `count > 0`, return a 403 error stating the book cannot be renewed because it has a waitlist.
  - **Severity:** High

- **Scenario:** Fine erasure on overdue renewal.
  - **Persona:** Librarian / Student.
  - **What currently happens:** If a librarian approves a renewal for a book that is *already overdue*, `api_admin_approve_request` simply updates the `due_date` to `today + 7 days`. The overdue condition vanishes dynamically, and the fine that accrued up to that point is never logged or enforced since fines are calculated dynamically based on `due_date`.
  - **Why it's a problem:** Students evade fines by simply requesting a renewal after the due date.
  - **Fix:** Before updating the `due_date` in `api_admin_approve_request`, compute the overdue fine (if `current_due < today`). Execute an `UPDATE` on `borrow_records` to increment the `fine` column by the computed amount, locking it in *before* extending the due date.
  - **Severity:** High

⚠️ Partially implemented / Hidden Edge Case:
- **Scenario:** Renewal limits per book.
  - **Persona:** Student.
  - **What currently happens:** The UI displays `privileges: { 'renewal_limit': '2 Renewals per book' }`, but there is absolutely no backend code enforcing this limit in either `api_submit_request` or the librarian's approval endpoint.
  - **Why it's a problem:** Students can hold a book indefinitely by repeatedly renewing it.
  - **Fix:** Add a `renewal_count` column to `borrow_records`. In `api_admin_approve_request` for renewals, check `if renewal_count >= 2: reject`. Otherwise, `renewal_count += 1`.
  - **Severity:** Medium


## C. Return & Fine Lifecycle
✅ What works:
- Dynamic fine calculation based on `FINE_PER_DAY` setting.
- `database.return_book` correctly computes the fine and updates `return_date` and `status`.

❌ What is missing or broken:
- **Scenario:** Borrowing while a fine is unpaid.
  - **Persona:** Student.
  - **What currently happens:** Fines are recorded in the `fine` column of `borrow_records` when a book is returned late. However, there is no check in `api_submit_request` or `database.borrow_book` to block a student from borrowing *new* books if they have unpaid fines from past returned books.
  - **Why it's a problem:** Students have no incentive to pay fines if it doesn't restrict their library privileges.
  - **Fix:** In `database.borrow_book` and `api_submit_request`, query `SUM(fine) FROM borrow_records WHERE enrollment_no = ? AND fine > 0 AND status = 'returned'`. (Assuming there is no fine payment table, fine clearance requires librarian action). If sum > 0, block new borrowings.
  - **Severity:** Medium

- **Scenario:** Lost/Damaged book scenario.
  - **Persona:** Librarian.
  - **What currently happens:** The system only supports `borrowed` and `returned` statuses. There is no mechanism to mark a book as 'lost' or 'damaged' and charge the replacement cost.
  - **Why it's a problem:** Inventory (`available_copies`) gets permanently distorted, and replacement costs cannot be tracked.
  - **Fix:** Add a 'lost' status to `borrow_records`. In `main.py`, add a "Mark Lost" context menu option that updates the status to 'lost', adds the book's `price` to the `fine` column, and does *not* increment `available_copies`.
  - **Severity:** High

⚠️ Partially implemented / Hidden Edge Case:
- **Scenario:** Fine payment acknowledgment / waiver.
  - **Persona:** Librarian.
  - **What currently happens:** There is no dedicated endpoint or UI button to mark a fine as "paid" or "waived". The only way is for the librarian to directly edit the SQLite database to set the `fine` column to 0.
  - **Why it's a problem:** Unpaid fines accumulate forever on the dashboard, frustrating students who have already paid at the desk.
  - **Fix:** Add a `clear_fine(enrollment_no)` method in `database.py` and an accompanying endpoint/UI button in the Desktop app to reset the `fine` column to 0 for returned books, and insert a log into `admin_activity`.
  - **Severity:** High


## D. Waitlist Lifecycle
✅ What works:
- Joining the waitlist prevents duplicate entries via `UNIQUE(enrollment_no, book_id)`.
- `return_book` triggers `_notify_waitlist` for the first person in line.

❌ What is missing or broken:
- **Scenario:** Notification expiring.
  - **Persona:** Student / Librarian.
  - **What currently happens:** When a book is returned, `_notify_waitlist` marks `notified = 1` for the first student. However, there is no expiration mechanism (e.g., 24 or 48 hours). If the student never claims the book, the second student on the waitlist is *never* notified, and the book sits on the shelf.
  - **Why it's a problem:** The waitlist stalls permanently for all subsequent students.
  - **Fix:** Create a background job or a check during `sync_manager` runs that finds waitlist entries where `notified = 1` and `updated_at` (needs to be added) is > 48 hours ago. Delete those entries and re-trigger `_notify_waitlist` for the next person in line.
  - **Severity:** High

⚠️ Partially implemented / Hidden Edge Case:
- **Scenario:** Multiple students on waitlist / Leaving waitlist.
  - **Persona:** Student.
  - **What currently happens:** The API `/api/books/<book_id>/notify` allows removing oneself from the waitlist, but only if `notified = 0`. If a student is notified but changes their mind, they cannot remove themselves, leaving the waitlist stalled.
  - **Why it's a problem:** Degrades the experience for other waitlisted students.
  - **Fix:** Remove the `AND notified = 0` condition in `remove_from_waitlist` so students can yield their spot even after being notified, and trigger a notification to the next person.
  - **Severity:** Medium


## E. Account & Identity Scenarios
✅ What works:
- Forgot password requests are logged and viewable by the librarian.
- First login correctly enforces a password change.

❌ What is missing or broken:
- **Scenario:** Account deletion with active loans or fines.
  - **Persona:** Student / Librarian.
  - **What currently happens:** A student can submit a deletion request. When the librarian approves it via `api_admin_approve_deletion`, the code immediately deletes the student from `student_auth`, `user_settings`, and `students` table. Crucially, it blindly updates *all* their active `borrow_records` to `status = 'returned'` and increments `available_copies`, effectively forgiving all active loans and stolen books without verification!
  - **Why it's a problem:** A student with 5 unreturned textbooks can request account deletion. Upon approval, the system forgives the theft and tells the catalogue the books are back on the shelf.
  - **Fix:** In `api_admin_approve_deletion`, before any DB modifications, query `borrow_records` for `status = 'borrowed'` or `fine > 0`. If true, return `400 Bad Request: Cannot delete account with active loans or unpaid fines`. The librarian must physically clear these first.
  - **Severity:** Critical

- **Scenario:** Password reset creates session hijacking risk.
  - **Persona:** Student.
  - **What currently happens:** The session relies on `session['student_id']` (a Flask cookie). If a user resets their password on device A, their session on device B remains active indefinitely because Flask's default sessions are client-side and not invalidated upon password change.
  - **Why it's a problem:** A compromised account cannot be fully secured by changing the password.
  - **Fix:** Store a `session_token` (e.g., UUID) in `student_auth` and inside the Flask session. Update the token on password change, and validate it in a `@before_request` hook.
  - **Severity:** Medium

⚠️ Partially implemented / Hidden Edge Case:
- **Scenario:** Settings update overwriting preferences.
  - **Persona:** Student.
  - **What currently happens:** Addressed partially in Bug A5 fix, but concurrent updates to `/api/settings` could still cause race conditions with the `ON CONFLICT DO UPDATE` block if read and writes interleave.
  - **Why it's a problem:** Minor data loss for preferences.
  - **Fix:** Rely purely on `COALESCE` in the SQL `UPDATE` statement rather than reading the existing values into Python first.
  - **Severity:** Low


## F. Librarian Daily Operations
✅ What works:
- Approving/rejecting requests via the desktop app endpoints.
- Returning books and viewing the borrowed list.

❌ What is missing or broken:
- **Scenario:** Approving requests for deleted accounts.
  - **Persona:** Librarian.
  - **What currently happens:** If a student submits a book request, and then their account is deleted (or they graduate), the pending request remains in the `requests` table. If a librarian approves it, `api_admin_approve_request` attempts to insert a `borrow_record`. If the student is missing from the `students` table, it fails violently (Foreign Key constraint) or creates an orphaned record.
  - **Why it's a problem:** Application crashes or data corruption.
  - **Fix:** In `api_admin_approve_request`, verify the student exists in the `students` table before proceeding with approval. If not, automatically reject the request.
  - **Severity:** Medium

- **Scenario:** Bypassing portal waitlist at the counter.
  - **Persona:** Librarian.
  - **What currently happens:** If a book is out of stock and has 5 students on the waitlist, and the book is returned at the desk, the librarian can immediately issue it to a 6th student physically present at the counter using `database.borrow_book`. There is no warning or block that the book is reserved for the waitlist.
  - **Why it's a problem:** Unfair advantage to walk-ins; undermines the waitlist system completely.
  - **Fix:** In `database.borrow_book`, check `book_waitlist`. If there are active waitlist entries and the `enrollment_no` doesn't match the first person in line, raise a warning/error in the Desktop UI requiring an explicit override.
  - **Severity:** High


## G. Sync & Data Integrity
✅ What works:
- Local-first architecture allows offline functionality.
- SyncManager performs delta syncs and full mirrors effectively.

❌ What is missing or broken:
- **Scenario:** Duplicate records on Excel Import vs Sync.
  - **Persona:** Librarian.
  - **What currently happens:** If a librarian imports legacy transactions via Excel in `main.py`, new `borrow_records` are created. If `SyncManager` is running concurrently and pulling from the cloud, it might pull duplicate records if the natural keys aren't perfectly aligned, resulting in double-borrowing.
  - **Why it's a problem:** Corrupts the available copies count and student dashboards.
  - **Fix:** Ensure Excel import logic is wrapped in a transaction lock that pauses `SyncManager` or uses `INSERT OR IGNORE` with robust unique constraints on `(enrollment_no, accession_no, borrow_date)` in `database.py`.
  - **Severity:** High

- **Scenario:** Deletion propagation bugs.
  - **Persona:** Librarian.
  - **What currently happens:** `database.delete_student` writes a tombstone to `sync_deletions`. However, if the cloud DB is wiped or reset, the desktop might push deletions that no longer map to anything, or worse, fail to push new records because it thinks they were deleted.
  - **Why it's a problem:** Zombie records appearing on the portal.
  - **Fix:** Ensure `SyncManager` clears `sync_deletions` entries once successfully propagated, and uses hard deletes on the cloud side.
  - **Severity:** Medium


## H. Deployment & Infrastructure
✅ What works:
- Render deployment setup handles environment variables.
- Waitress serves the Flask app efficiently.

❌ What is missing or broken:
- **Scenario:** Unauthenticated access to admin endpoints.
  - **Persona:** Attacker.
  - **What currently happens:** Endpoints like `/api/admin/all-requests`, `/api/admin/requests/<id>/approve`, and `/api/admin/study-materials` are completely unprotected. The `@app.before_request` hook `enforce_admin_local_access` is *missing* from `student_portal.py`. Since Waitress listens on `0.0.0.0`, anyone on the internet or college network can approve requests, delete accounts, and reset passwords.
  - **Why it's a problem:** Complete system compromise. Any student can curl the approve endpoint and grant themselves books or delete other students.
  - **Fix:** Add a `@app.before_request` hook `enforce_admin_local_access` that checks if `request.path.startswith('/api/admin/')`. If true, verify that `request.remote_addr` is `127.0.0.1` or `::1`. If not, return `403 Forbidden`.
  - **Severity:** Critical

- **Scenario:** CSRF Protection Exclusion Loophole.
  - **Persona:** Attacker.
  - **What currently happens:** `CSRF_EXCLUDED_ENDPOINTS` in `student_portal.py` excludes `/api/request`, `/api/settings`, and `/api/request-deletion` explicitly. This completely disables CSRF protection for the most sensitive student actions.
  - **Why it's a problem:** An attacker can trick a logged-in student into visiting a malicious site that submits a hidden form to `/api/request-deletion` or changes their settings.
  - **Fix:** Remove these endpoints from the `CSRF_EXCLUDED_ENDPOINTS` array. The React frontend should be configured to pass the `X-CSRF-Token` header for all `POST` requests.
  - **Severity:** Critical

- **Scenario:** Ephemeral Secret Key causing Session Invalidation.
  - **Persona:** Student.
  - **What currently happens:** `get_or_create_secret_key()` generates a random token and saves it to a `.secret_key` file. On Render (ephemeral filesystem), every time the server restarts or scales, the file is lost, a new key is generated, and *all* active student sessions are invalidated.
  - **Why it's a problem:** Students are constantly logged out.
  - **Fix:** Modify `get_or_create_secret_key()`. If `os.getenv('RENDER')` is true, require `FLASK_SECRET_KEY` from the environment and explicitly crash/raise an error if it's missing, rather than generating an ephemeral one.
  - **Severity:** High

- **Scenario:** Unauthenticated Study Material Downloads.
  - **Persona:** Non-student.
  - **What currently happens:** `/api/study-materials/<int:material_id>/download` lacks the `if 'student_id' not in session:` check.
  - **Why it's a problem:** External users can scrape and download copyrighted college study materials.
  - **Fix:** Add session validation to the download endpoint.
  - **Severity:** Medium


## I. Notification & Email Pipeline
✅ What works:
- Email threading is handled in the background to prevent UI blocking.

❌ What is missing or broken:
- **Scenario:** Silent email configuration failure.
  - **Persona:** Librarian.
  - **What currently happens:** `send_email_bg` catches *all* exceptions and simply prints them. If the college's SMTP server changes its password, the librarian is never alerted in the UI that emails have stopped sending.
  - **Why it's a problem:** Students stop receiving overdue notices and waitlist alerts without the administration knowing.
  - **Fix:** Log email failures to a new `system_alerts` table or display a warning banner in the Desktop UI if the last N emails failed.
  - **Severity:** Medium

- **Scenario:** Orphaned notifications after account deletion.
  - **Persona:** System.
  - **What currently happens:** If a student is deleted via `database.delete_student` (from Desktop), the cascade deletes from core tables, but `user_notifications` in the portal DB are left orphaned.
  - **Why it's a problem:** Database bloat over time.
  - **Fix:** Add an explicit delete query for `user_notifications` in the deletion pipeline, or ensure SQLite foreign key constraints are properly configured with `ON DELETE CASCADE` for the portal DB.
  - **Severity:** Low


## J. Edge Cases & Stress Scenarios
✅ What works:
- SQL injection is generally mitigated via parameterization.

❌ What is missing or broken:
- **Scenario:** Missing Rate Limits on Request Submissions.
  - **Persona:** Student.
  - **What currently happens:** `/api/request` lacks the `@rate_limit` decorator.
  - **Why it's a problem:** A disgruntled student can write a script to submit 10,000 book requests per minute, overwhelming the database and freezing the librarian's desktop app.
  - **Fix:** Apply the `@rate_limit` decorator to `api_submit_request`.
  - **Severity:** High

- **Scenario:** Single quote in special characters breaking search.
  - **Persona:** Student / Librarian.
  - **What currently happens:** While parameterized queries are used for insertion, if a user searches for `O'Reilly` in `get_books`, it's safely parameterized. However, bulk data imports (Excel) sometimes fail if pandas data isn't perfectly sanitized before hitting manual query constructions.
  - **Why it's a problem:** Import failures.
  - **Fix:** Standardize all bulk inserts to use `.executemany()` with strictly parameterized templates.
  - **Severity:** Low
