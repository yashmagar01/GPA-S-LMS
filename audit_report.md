**A. Student Borrowing Lifecycle**

✅ **What Works:**
- **First login:** Works fine, checks local library schema. Forces password reset if enrollment num matches.
- **Catalogue browsing:** Works correctly and handles query pagination.
- **Requesting a book:** Creates a request and triggers email successfully.
- **Duplicate request:** Blocked successfully for the same book using `student_portal.py` checks.
- **Requesting when at borrowing limit:** Handled via max checks based on environment variables or defaults.
- **Approval received:** Notification triggers, book borrow limit updated, borrow record made.
- **Going to collect / Physically collecting:** Covered by admin functionality.
- **Requesting cancellation before approval:** Standard cancel endpoint supports cancelling `pending` requests.

❌ **What is Missing or Broken:**
- **Requesting cancellation after approval:**
  - *Scenario:* Student cancels a request right after it is approved by the librarian but before they physically pick it up.
  - *Current Behavior:* `api_cancel_request` prevents cancellation if status is not `pending`. If they just don't pick it up, nothing happens.
  - *Impact:* The book remains reserved, `available_copies` remains decremented indefinitely because there's no timeout mechanism implemented in the portal to revert uncollected approved requests.
  - *Fix:*
    1. In `api_cancel_request` (student_portal.py), allow cancellation of 'approved' requests.
    2. If `status` is 'approved' and `request_type` is 'book_request', retrieve the `book_id` from the request details.
    3. Update `books` table to increment `available_copies`.
    4. Delete the corresponding `borrow_records` entry (which is inserted during approval).
    5. Sync these changes via `_push_to_cloud`.
  - *Severity:* High (Causes available book copies to be permanently lost)

- **NOT collecting within deadline:**
  - *Scenario:* Student is approved for a book but doesn't collect it within the specified deadline (2 days).
  - *Current Behavior:* The email says "If not collected by the deadline, the reservation will be cancelled", but there is no cron job, script, or check in `main.py` or `student_portal.py` that actually enforces this 2-day limit. The borrow record is created immediately upon approval.
  - *Impact:* Book is permanently marked as borrowed and available copies are reduced.
  - *Fix:* Create a background task or startup check in `main.py` (e.g. within `refresh_records`) that scans `borrow_records` where `status = 'borrowed'` and `due_date` is exactly 14 days away but perhaps a new field `collected_at` is null. Alternatively, when issuing a book at the counter, it should mark it as collected.
  - *Severity:* High

⚠️ **Partially Implemented/Edge Cases:**
- **Requesting when overdue:**
  - *Scenario:* Student with overdue books tries to request a new book.
  - *Current Behavior:* `api_submit_request` only checks the total number of borrowed books, not if any are overdue.
  - *Fix:* In `api_submit_request` (student_portal.py), add a check querying `borrow_records` for the student where `return_date IS NULL` and `due_date < CURRENT_DATE`. If > 0, return 403.
  - *Severity:* Medium

**B. Renewal Lifecycle**

✅ **What Works:**
- **Normal renewal request:** Can be submitted via `api_submit_request`.
- **Librarian approving renewal:** Correctly finds the latest borrow record and extends `due_date`.

❌ **What is Missing or Broken:**
- **Renewal when another student is on the waitlist:**
  - *Scenario:* Student requests renewal, but the book has 0 available copies and there are students on the waitlist.
  - *Current Behavior:* No check is made against `book_waitlist` when submitting or approving a renewal. Librarian might blindly approve it.
  - *Impact:* Waitlisted students are indefinitely blocked.
  - *Fix:* In `api_admin_approve_request` (student_portal.py) for renewals, check `SELECT COUNT(*) FROM book_waitlist WHERE book_id = ?`. If > 0, reject the renewal and notify the student.
  - *Severity:* High

- **Renewal when at the maximum renewal count:**
  - *Scenario:* Student renews the same book 5 times.
  - *Current Behavior:* There is no `renewal_count` tracking in `borrow_records` or `requests`.
  - *Impact:* Students can monopolize books.
  - *Fix:* Add a `renewal_count` column to `borrow_records`. Increment it upon approval. Block approval if `renewal_count >= MAX_RE ব্যাঙ্ক` (MAX_RENEWALS).
  - *Severity:* Medium

- **Librarian approving a renewal after the book was already returned:**
  - *Scenario:* Request is pending, student returns the book at the counter, librarian then clicks "Approve Renewal".
  - *Current Behavior:* In `api_admin_approve_request`, it checks for `return_date IS NULL`. If none found, it might still mark the request as approved and send an email, even though no date was extended.
  - *Fix:* In `api_admin_approve_request` (student_portal.py), check if `borrow_record` is `None`. If it is, `return jsonify({'error': 'No active loan found to renew'})` and do not approve.
  - *Severity:* Low

⚠️ **Partially Implemented/Edge Cases:**
- **Renewal when already overdue:** Allowed by default; could be a college policy, but typically late fines should block renewals until paid.

**C. Return & Fine Lifecycle**

✅ **What Works:**
- **On-time return:** Desktop app `_return_book` works fine and calculates fine correctly if late.
- **Late return with fine:** `_return_book` calculates days late * fine_per_day.
- **Fine appearing on dashboard:** API returns fine amount to student dashboard.

❌ **What is Missing or Broken:**
- **Student attempting to borrow while fine is unpaid:**
  - *Scenario:* Student owes ₹500 in fines but tries to borrow more books.
  - *Current Behavior:* No check in `api_submit_request` or `api_admin_approve_request` for outstanding unpaid fines.
  - *Impact:* Students accumulate massive fines without restriction.
  - *Fix:* In `api_submit_request` (student_portal.py), calculate total unpaid fines `SELECT SUM(fine) FROM borrow_records WHERE enrollment_no = ? AND fine_paid = 0`. If > 0, block request.
  - *Severity:* High

- **Fine payment acknowledgment / clearing:**
  - *Scenario:* Student pays the fine at the desk.
  - *Current Behavior:* `borrow_records` has `fine`, but no `fine_paid` boolean or tracking of payments. Once a book is returned late, the fine is hardcoded into `fine` column. There is no way for the librarian to "mark fine as paid" in the Desktop app.
  - *Impact:* Fines are permanent and cannot be cleared.
  - *Fix:*
    1. Add `fine_status` (default 'unpaid') or `fine_paid_date` to `borrow_records` via migration.
    2. Add a desktop UI button and `mark_fine_paid(record_id)` in `database.py`.
    3. Update `api_dashboard` to only show unpaid fines.
  - *Severity:* Critical

- **Damaged / Lost Book Scenario:**
  - *Scenario:* Student reports a book lost.
  - *Current Behavior:* No functionality exists to handle lost/damaged books.
  - *Impact:* Inventory becomes inaccurate, fines cannot be levied for book cost.
  - *Fix:* Add a "Mark as Lost" button in the desktop app's Active Loans tab, which sets status to 'lost', applies a replacement fine, and permanently decrements `total_copies`.
  - *Severity:* Medium

⚠️ **Partially Implemented/Edge Cases:**
- **Fine waiver by librarian:** There's no UI for this.

**D. Waitlist Lifecycle**

✅ **What Works:**
- **Joining waitlist for unavailable book:** Works via `add_to_waitlist`.
- **Leaving waitlist:** Works via `remove_from_waitlist`.
- **Being notified when book is returned:** `_notify_waitlist` in `database.py` inserts a notification.
- **Multiple students on waitlist for same book:** Supported. Only first unnotified gets notified.

❌ **What is Missing or Broken:**
- **Acting on notification within a window / notification expiring:**
  - *Scenario:* Book is returned, user 1 is notified. They ignore it.
  - *Current Behavior:* `_notify_waitlist` marks `notified = 1` for the first person. There is no expiration. User 2 will never get notified because User 1 holds the "next in line" spot indefinitely.
  - *Impact:* Waitlists stall completely after one notification.
  - *Fix:*
    1. Add `notified_at` timestamp to `book_waitlist`.
    2. Create a background task that deletes waitlist entries where `notified_at < CURRENT_TIMESTAMP - 24 hours` and triggers `_notify_waitlist` for the next person.
  - *Severity:* High

**E. Account & Identity Scenarios**

✅ **What Works:**
- **First login password change:** Enforced via `is_first_login` flag.
- **Forgot password request:** Submitted to librarian successfully.
- **Account deletion:** Successfully cleans up `student_auth`, `requests`, and `borrow_records` and returns borrowed books.
- **Profile update request:** Handled via requests.
- **Self-registration approval and rejection:** Handled well.

❌ **What is Missing or Broken:**
- **Year changing to Pass Out with active loans:**
  - *Scenario:* Student's year is updated to "Pass Out" (alumni) but they still have 3 books borrowed.
  - *Current Behavior:* Profile update or Excel import can change year to "Pass Out". `api_submit_request` blocks new requests from 'Pass Out', but existing loans are untouched. No alert is generated.
  - *Impact:* Books are lost as students leave the college.
  - *Fix:* During profile update approval (`api_admin_approve_request`), if year changes to "Pass Out", check for active loans. If `COUNT > 0`, return an error `Cannot mark as Pass Out with active loans` and block the update.
  - *Severity:* High

- **Password change with session active on another device:**
  - *Scenario:* Student logs in on phone, changes password on laptop.
  - *Current Behavior:* Changing password (`api_change_password`) does not invalidate existing sessions (because Flask session cookies are purely client-side signed).
  - *Fix:* Add a `session_token` or `password_version` column to `student_auth`. Increment it on password change. Validate it on every request.
  - *Severity:* Medium

⚠️ **Partially Implemented/Edge Cases:**
- **Stale password reset request:** If an admin takes weeks to approve a password reset, it will still reset the password. Could use an expiry.

**F. Librarian Daily Operations**

✅ **What Works:**
- **Approving/rejecting requests:** Handled correctly in `api_admin_approve_request` and `api_admin_reject_request`.
- **Excel import:** Implemented via `import_students_from_excel`.
- **Viewing and managing active loans:** Available in desktop app.
- **Processing a return at the counter:** Available in desktop app.
- **Viewing overdue list:** Available.
- **Broadcasting a notice:** Handled in student portal.
- **Uploading study materials:** Implemented.
- **Managing book catalogue:** Available in desktop app.

❌ **What is Missing or Broken:**
- **Issuing a book directly at the counter (bypass portal):**
  - *Scenario:* Librarian issues a book directly via the desktop app.
  - *Current Behavior:* When issuing via desktop app (which creates a borrow record), if there are pending requests in the portal for that same book, they are ignored. The portal might later approve the request, causing `available_copies` to go negative.
  - *Impact:* Double booking of the same copy.
  - *Fix:* When a book is issued via desktop, check `requests` for pending `book_request` for that `book_id`. If found, auto-reject or cancel them if `available_copies` hits 0.
  - *Severity:* High

- **Marking a fine as paid:**
  - *Current Behavior:* Missing entirely (as noted in Section C).
  - *Severity:* Critical

**G. Sync & Data Integrity**

✅ **What Works:**
- **Push to cloud:** `_push_to_cloud` attempts to replicate SQL statements to Supabase.
- **Cloud pulling updates to local:** `sync_manager.py` manages bidirectional sync.

❌ **What is Missing or Broken:**
- **Available_copies count drifting between systems:**
  - *Scenario:* Race conditions between local and cloud operations or failed pushes can cause `available_copies` to drift.
  - *Current Behavior:* Simple updates like `available_copies = available_copies - 1` are pushed, but there's no periodic reconciliation job for `available_copies` based on `total_copies - COUNT(borrow_records)`.
  - *Impact:* Discrepancy between actual borrowed books and available count.
  - *Fix:* Add a daily reconciliation job in `sync_manager.py` that recalculates `available_copies` for each book based on active `borrow_records` and updates both local and cloud.
  - *Severity:* High

- **Sync conflict when the same record is modified on both desktop and portal simultaneously:**
  - *Scenario:* Desktop app updates a student record offline. Supabase updates it online.
  - *Current Behavior:* `_push_to_cloud` blindly executes SQL strings on Supabase. There is no timestamp-based conflict resolution (CRDT or Last-Write-Wins logic on individual fields).
  - *Impact:* Data overwrites and split-brain scenarios.
  - *Fix:* Implement an `updated_at` check in `sync_manager.py` before applying updates.
  - *Severity:* High

⚠️ **Partially Implemented/Edge Cases:**
- **Transactions imported from Excel creating duplicate records:**
  - *Scenario:* Librarian imports Excel twice.
  - *Current Behavior:* `import_students_from_excel` handles some duplication, but name casing issues can create dupes.
  - *Severity:* Low

**H. Deployment & Infrastructure**

✅ **What Works:**
- **Waitress server:** Serves portal properly.
- **SQLite vs PostgreSQL query compatibility:** Addressed via custom database wrappers.
- **Access log writing on cloud vs local:** Logging is done via `_log_writer_loop`.
- **Environment variable missing at runtime:** Addressed via fallback `.env.example`.

❌ **What is Missing or Broken:**
- **Unauthenticated access to study material downloads:**
  - *Scenario:* A random person scrapes the Render URL.
  - *Current Behavior:* `/api/study-materials/<int:material_id>/download` has no `@rate_limit` and no `if 'student_id' not in session:` check.
  - *Impact:* College IP and copyrighted materials leaked to the public internet, massive bandwidth bill on Render.
  - *Fix:* Add session authentication check to `download_study_material` in `student_portal.py`.
  - *Severity:* Critical

- **Render cold start and session behavior:**
  - *Scenario:* Render spins down the free tier instance.
  - *Current Behavior:* Flask uses default client-side sessions with a secret key. In `student_portal.py`, `get_or_create_secret_key()` creates a new `secret.key` file locally. On Render, the local disk is ephemeral, meaning on every cold start, a new secret key is generated, invalidating all student sessions instantly.
  - *Impact:* Students are logged out constantly.
  - *Fix:* Read `SECRET_KEY` from `os.getenv('SECRET_KEY')` directly and strictly enforce it in deployment, rather than generating local files.
  - *Severity:* High

- **CSRF protection gaps:**
  - *Scenario:* Attacker attempts a CSRF attack on API endpoints.
  - *Current Behavior:* Double-submit cookie pattern is implemented via `csrf_protect()`, but some state-changing endpoints might bypass it if not explicitly configured or if the method check fails (e.g. relying on GET for actions).
  - *Severity:* Medium

- **Unauthenticated access to admin endpoints:**
  - *Scenario:* External user accesses `/api/admin/` endpoints.
  - *Current Behavior:* Endpoints check `request.remote_addr` against localhost. However, Waitress configuration might not pass the correct remote address if behind a reverse proxy on Render (though Waitress by default handles this if properly configured, but direct proxying without `ProxyFix` can lead to `remote_addr` being 127.0.0.1 on Render).
  - *Impact:* Complete system compromise.
  - *Fix:* Implement strong token-based authentication or explicitly configure `werkzeug.middleware.proxy_fix.ProxyFix` so that `remote_addr` correctly reflects the real client IP.
  - *Severity:* Critical

**I. Notification & Email Pipeline**

✅ **What Works:**
- **Email generation:** `generate_email_template` creates good HTML.
- **Background send:** `send_email_bg` works asynchronously.
- **In-app notification appearing correctly:** Added to `user_notifications`.
- **Unread count accuracy:** Maintained.

❌ **What is Missing or Broken:**
- **Email failing silently:**
  - *Scenario:* SMTP credentials are wrong or rate limited by Google.
  - *Current Behavior:* `send_email_bg` has a `try/except` that prints to stdout, but doesn't log to a database or retry.
  - *Impact:* Students miss critical approvals/rejections and deadlines.
  - *Fix:* Implement an `email_outbox` table. Store emails there. Have a background thread poll and send, marking as `failed` with error logs if SMTP fails, allowing the librarian to see failed emails in the UI.
  - *Severity:* Medium

⚠️ **Partially Implemented/Edge Cases:**
- **Notification for an event that has no template defined:** Falls back to generic message.
- **Orphaned notifications after account deletion:** `api_admin_approve_deletion` cleans up `user_notifications`.

**J. Edge Cases & Stress Scenarios**

✅ **What Works:**
- **Book title with single quotes breaking SQL queries:** Addressed by parameterized queries in `student_portal.py`.
- **Catalogue with 0 books showing correct empty state:** Handled by frontend.

❌ **What is Missing or Broken:**
- **Student submitting the same request type 20 times rapidly:**
  - *Scenario:* Double clicking the "Submit" button before the UI disables it.
  - *Current Behavior:* `api_submit_request` checks for existing requests, but race conditions exist because there's no database transaction lock around the SELECT and INSERT.
  - *Impact:* Multiple pending requests for the same book.
  - *Fix:* Add a `UNIQUE` constraint to the `requests` table for `(enrollment_no, request_type, JSON_EXTRACT(details, '$.book_id')) WHERE status = 'pending'`, or enforce an application-level mutex per user.
  - *Severity:* Medium

- **Concurrent approvals for the same single-copy book:**
  - *Scenario:* Two librarians on different machines approve two different students' requests for the same book simultaneously.
  - *Current Behavior:* `api_admin_approve_request` checks `available_copies` and decrements it, but `get_library_db()` doesn't wrap the read and update in a transaction with `SELECT ... FOR UPDATE` (SQLite doesn't support it well, but `BEGIN EXCLUSIVE` works).
  - *Impact:* `available_copies` can go negative.
  - *Fix:* Use `BEGIN EXCLUSIVE` transaction when checking and approving book requests in `api_admin_approve_request`.
  - *Severity:* High

⚠️ **Partially Implemented/Edge Cases:**
- **Librarian approving a request for a student whose account was just deleted:** Will fail to find student during approval.
- **Fine rate changed mid-loan period:** Fine calculation happens dynamically based on the current rate, so previous days might be charged at the new rate. Standard behavior, but worth noting.
- **Student with a name containing special characters breaking email templates:** Name formatting in `generate_email_template` doesn't sanitize HTML properly, potentially leading to rendering issues if names contain `<` or `>`.
