### Adversarial QA Audit Report for GPA-S-LMS

The following is an exhaustive audit of the actual provided codebase based on adversarial testing against real-world scenarios.

**A. Student Borrowing Lifecycle**
✅ **What Works:**
- Enforcing global borrow limits: The `api_submit_request` endpoint checks the number of pending and active borrows before allowing a new `book_request`.
- Preventing duplicated requests: `student_portal.py` blocks multiple pending `book_request` entries for the same book ID.
- Request cancellation: `api_cancel_request` successfully allows cancellation and correctly validates ownership and `pending` state.

❌ **What is Missing or Broken:**
- **Overdue block bypass**
  - *Persona:* Student
  - *Current behavior:* There is no hard block in `api_submit_request` to prevent a student from requesting *new* books when they already have *overdue* items.
  - *Real-world impact:* Students can hoard library resources and evade consequences for returning books late. This is a critical problem for a college library where resources are limited and fair access is vital.
  - *Fix:* In `api_submit_request` (student_portal.py), add a check before `book_request` processing: query `borrow_records` for `status = 'borrowed'` and `due_date < current_date` for the `session['student_id']`. If any records exist, return a 403 error.
  - *Severity:* High
- **Concurrent Request Race Condition**
  - *Persona:* Student
  - *Current behavior:* While there is a check for pending requests, it is not within a transaction block. If a student spams the `api_submit_request` endpoint, they can bypass the global limit.
  - *Real-world impact:* A student using an automated script could hoard an entire category of books before the library realizes.
  - *Fix:* Use database transactions (`BEGIN EXCLUSIVE` in SQLite/Postgres) around the pending request count check and insert in `api_submit_request`.
  - *Severity:* Medium
- **Approval Expiry (Collection Deadline)**
  - *Persona:* Librarian / Student
  - *Current behavior:* The system lacks logic to auto-cancel approved but uncollected books. If a librarian approves a borrow request, but the student never shows up, the book stays reserved forever.
  - *Real-world impact:* Book copies remain stuck in "approved" limbo and cannot be lent to other students, artificially shrinking the usable catalog.
  - *Fix:* Implement a daily CRON job or a background thread check in `student_portal.py` that queries `borrow_records` (or request table) for `status = 'approved'` and `updated_at < (NOW() - 48 hours)`. Mark them `expired` and increment `available_copies`.
  - *Severity:* High

⚠️ **Edge Cases:**
- **Pass Out constraint**
  - *Persona:* Librarian
  - *Current behavior:* The system strictly checks for "pass out" text to block borrows, but this relies on perfect data entry.
  - *Real-world impact:* Graduated students might still borrow books if their year was typed as "graduated" instead of "pass out".
  - *Fix:* Standardize the `year` column enum or rely on an `is_active` boolean rather than string matching "passed out", "graduate", etc., in `student_portal.py:api_submit_request`.
  - *Severity:* Low

**B. Renewal Lifecycle**
✅ **What Works:**
- The portal has a request type for renewals and prevents duplicate pending renewal requests for the exact same accession number.

❌ **What is Missing or Broken:**
- **Waitlist Bypass**
  - *Persona:* Student
  - *Current behavior:* A student can request a renewal even if there are students waiting for that exact book on the waitlist.
  - *Real-world impact:* A single student can hold onto a highly-demanded textbook for the entire semester, completely defeating the purpose of the waitlist.
  - *Fix:* In `api_submit_request` handling `req_type == 'renewal'`, query the `waitlist` table for `book_id`. If `count > 0`, reject the renewal with a 403 error explaining others are waiting.
  - *Severity:* High
- **Maximum Renewal Limit**
  - *Persona:* Student
  - *Current behavior:* There is no hard cap on how many times a single book can be renewed.
  - *Real-world impact:* Identical to waitlist bypass, a student could monopolize a resource indefinitely.
  - *Fix:* Add a `renewals_count` column to `borrow_records`. Increment it on renewal approval. In `api_submit_request`, reject if `renewals_count >= MAX_RENEWALS` (e.g., 2).
  - *Severity:* Medium
- **Renewing Overdue Books with Fines**
  - *Persona:* Student
  - *Current behavior:* The system doesn't natively halt renewals for books that are already overdue and accruing fines.
  - *Real-world impact:* A student could potentially wipe out or pause a fine by getting a late renewal, cheating the fine system.
  - *Fix:* In `api_submit_request` renewal flow, check if `due_date < current_date`. If yes, block renewal.
  - *Severity:* High
- **Librarian Post-Return Renewal**
  - *Persona:* Librarian
  - *Current behavior:* A librarian can potentially approve a pending renewal request from the admin dashboard *after* the book was physically returned at the counter.
  - *Real-world impact:* Data corruption. The book is physically on the shelf but logically marked as extended for the previous student.
  - *Fix:* In `api_admin_approve_request` (`student_portal.py`), verify the book is still `status = 'borrowed'` by this specific student before extending the `due_date`.
  - *Severity:* Critical

**C. Return & Fine Lifecycle**
✅ **What Works:**
- Fine configuration: Fines can be read dynamically via `get_portal_fine_per_day()`.
- Return counter processing: `return_book` in `main.py` processes returns.

❌ **What is Missing or Broken:**
- **Unpaid Fine Block**
  - *Persona:* Student
  - *Current behavior:* Students can continue to borrow new books even if they have unpaid fines from previously returned books.
  - *Real-world impact:* Students have no incentive to pay fines, rendering the monetary penalty system toothless.
  - *Fix:* In `api_submit_request`, query `borrow_records` for `fine > 0` and `status = 'returned'`. If unpaid fines exist, block new `book_request` and `renewal` requests.
  - *Severity:* High
- **Fine Payment Flow**
  - *Persona:* Librarian / Student
  - *Current behavior:* There is no mechanism in the portal for a student to acknowledge or clear a fine, and no explicit endpoint for librarians to mark a specific fine as "paid" without manually editing the DB.
  - *Real-world impact:* Fines are tracked but cannot be resolved in the system, leading to infinite unpaid fines for all students.
  - *Fix:* Add a `fine_status` column (unpaid/paid). Add an endpoint `api_admin_clear_fine(record_id)` in `student_portal.py` and a UI button in `main.py`'s student profile view to clear it.
  - *Severity:* Critical
- **Lost/Damaged Scenarios**
  - *Persona:* Librarian
  - *Current behavior:* The system assumes a binary "borrowed" or "returned" state. If a book is lost, the student remains perpetually overdue.
  - *Real-world impact:* The database fills with "ghost" overdue books that will never be returned, skewing reports and inventory.
  - *Fix:* Add "lost" and "damaged" to the `status` enum in `borrow_records`. Add librarian endpoints to transition a record to these states, which should automatically add a replacement cost to the fine total and decrement total inventory.
  - *Severity:* High

**D. Waitlist Lifecycle**
✅ **What Works:**
- `add_to_waitlist` and `remove_from_waitlist` endpoints exist in `student_portal.py`.

❌ **What is Missing or Broken:**
- **Waitlist Notification Expiration**
  - *Persona:* Student
  - *Current behavior:* When a book is returned, there's no automated mechanism to notify the waitlisted student AND enforce a window (e.g., 24 hours) before passing the book to the next person.
  - *Real-world impact:* The first person on the waitlist could delay everyone else indefinitely if they ignore the notification.
  - *Fix:* In `main.py`'s `_return_book_worker`, when `available_copies` increases, query the `waitlist`. Add an `active_until` timestamp for the first student. If they don't borrow by then, a background worker must pop them and notify the next.
  - *Severity:* High
- **Joining while available**
  - *Persona:* Student
  - *Current behavior:* The code doesn't strictly prevent a student from joining a waitlist for a book that has `available_copies > 0`.
  - *Real-world impact:* Confuses students who join a waitlist when they could have just requested the book immediately.
  - *Fix:* In `add_to_waitlist`, check `available_copies`. If > 0, return an error telling them to just request the book.
  - *Severity:* Low

**E. Account & Identity Scenarios**
✅ **What Works:**
- Forgot password, change password, profile photo endpoints are functional.

❌ **What is Missing or Broken:**
- **Session Revocation on Password Change**
  - *Persona:* Student
  - *Current behavior:* If a user changes their password on device A, their session on device B remains active.
  - *Real-world impact:* If a student's account is compromised and they reset their password, the attacker maintains access via the existing session.
  - *Fix:* Introduce a `session_version` integer in the `students` table. Store it in the Flask `session`. Increment it on password change. In `api_me` or `before_request`, validate the session's version matches the DB.
  - *Severity:* Critical
- **Orphaned Records on Deletion**
  - *Persona:* Librarian
  - *Current behavior:* When a student account is deleted (`api_admin_approve_deletion`), there is no check for active loans or unpaid fines.
  - *Real-world impact:* Books remain "borrowed" by a deleted user, destroying the integrity of the inventory tracking.
  - *Fix:* In `api_admin_approve_deletion`, before executing the DELETE statement, query `borrow_records` for `status = 'borrowed'` or `fine > 0`. If found, reject the deletion and alert the librarian.
  - *Severity:* Critical

**F. Librarian Daily Operations**
✅ **What Works:**
- Importing from Excel is deeply implemented in `main.py`.
- Approving/rejecting requests via `api_admin_approve_request`.

❌ **What is Missing or Broken:**
- **Counter Issue Bypass**
  - *Persona:* Librarian
  - *Current behavior:* If a librarian issues a book at the physical counter (in `main.py`), it does not check if that exact book copy was already reserved via an approved portal request for *another* student.
  - *Real-world impact:* A librarian could inadvertently give away a book that was promised to someone else who is currently walking to the library to collect it.
  - *Fix:* In `main.py`'s issue logic, query the `requests` table (or portal DB) for `status = 'approved'` and `book_id`. Warn or block the librarian.
  - *Severity:* High
- **Excel Import Duplicates**
  - *Persona:* Librarian
  - *Current behavior:* Importing transactions from Excel can create duplicate `borrow_records` if the same file is uploaded twice.
  - *Real-world impact:* Student records get duplicated, artificially inflating their borrow limits and fines.
  - *Fix:* In `main.py`'s excel import worker, implement a composite unique constraint or check `(enrollment_no, book_id, borrow_date)` before inserting.
  - *Severity:* Medium

**G. Sync & Data Integrity**
✅ **What Works:**
- Two-way sync is established via `sync_manager.py` handling SQLite and Supabase.

❌ **What is Missing or Broken:**
- **Available Copies Drift**
  - *Persona:* Both
  - *Current behavior:* `available_copies` is often updated by incrementing/decrementing. If a sync fails midway, the count drifts from the actual number of active `borrow_records`.
  - *Real-world impact:* The portal shows a book as available, but the physical shelf is empty, or vice versa. Highly frustrating for students.
  - *Fix:* Create a database view or a nightly cleanup function in `sync_manager.py` that recalculates `available_copies` exactly as `total_copies - COUNT(active borrow_records)`.
  - *Severity:* Critical
- **Concurrent Modification Conflict**
  - *Persona:* Both
  - *Current behavior:* If a librarian updates a student's profile locally in `main.py` while the student updates it on the portal, sync might arbitrarily overwrite one.
  - *Real-world impact:* Data loss of one party's updates.
  - *Fix:* Implement an `updated_at` timestamp check in `sync_manager.py`. Only overwrite if the incoming record's timestamp is strictly newer.
  - *Severity:* Medium

**H. Deployment & Infrastructure**
✅ **What Works:**
- Rate limiting is implemented (`rate_limit` decorator). CSRF protection is wired.

❌ **What is Missing or Broken:**
- **Waitress Admin Exposure**
  - *Persona:* External Attacker
  - *Current behavior:* Waitress binds to `0.0.0.0` by default. While there's an `enforce_admin_local_access` hook mentioned, Waitress running on Render will see requests routed through Render's proxies, meaning `request.remote_addr` might not be `127.0.0.1` even if it's internal, or worse, it could be spoofed.
  - *Real-world impact:* Anyone on the internet could potentially reach the `/api/admin` endpoints meant only for the desktop app, allowing complete takeover of the system.
  - *Fix:* Parse the `X-Forwarded-For` header securely in `student_portal.py`'s admin authentication. Rely on API Keys or explicit Render private network headers rather than raw IPs for admin endpoints.
  - *Severity:* Critical
- **Query Compatibility**
  - *Persona:* Infrastructure
  - *Current behavior:* Some endpoints might use SQLite `?` while the cloud uses Postgres `%s`. Though a wrapper exists, direct execution in `student_portal.py` sometimes uses `?` which will crash against the Supabase URL.
  - *Real-world impact:* Random 500 errors for students trying to use basic features in production.
  - *Fix:* Ensure all direct `execute` calls in `student_portal.py` use the `get_portal_db()` wrapper that automatically handles placeholder translation, avoiding raw `sqlite3` driver calls.
  - *Severity:* High

**I. Notification & Email Pipeline**
✅ **What Works:**
- Background email sending (`send_email_bg`).

❌ **What is Missing or Broken:**
- **Silent Email Failures**
  - *Persona:* Librarian / Student
  - *Current behavior:* If `send_email_bg` fails (e.g., SMTP timeout), it swallows the error. The librarian/student has no idea the email wasn't sent.
  - *Real-world impact:* Important communications (like overdue notices or waitlist triggers) fail silently, leading to disputes over fines and missed opportunities.
  - *Fix:* Add an `email_status` column to the `notifications` table. If `send_email_bg` throws an exception, update the row to `failed` and log it.
  - *Severity:* Medium
- **Orphaned Notifications**
  - *Persona:* System
  - *Current behavior:* When a student is deleted, their notifications remain in the DB.
  - *Real-world impact:* Unnecessary database bloat.
  - *Fix:* Add an `ON DELETE CASCADE` constraint to the `enrollment_no` foreign key in the `notifications` table in `database.py`.
  - *Severity:* Low

**J. Edge Cases & Stress Scenarios**
✅ **What Works:**
- Basic exception logging via `_log_portal_exception`.

❌ **What is Missing or Broken:**
- **SQL Injection via Single Quotes**
  - *Persona:* Malicious user
  - *Current behavior:* If a book title has a single quote (e.g., "O'Reilly"), and it is unsafely interpolated anywhere in `main.py` or `student_portal.py`, it breaks the query.
  - *Real-world impact:* Allows attackers to drop tables or exfiltrate student data (SQL injection).
  - *Fix:* Audit all queries to ensure 100% parameterization. E.g., never do `f"SELECT * FROM books WHERE title = '{title}'"`. Use `(?, (title,))`.
  - *Severity:* Critical
- **Fine Rate Change Mid-Loan**
  - *Persona:* Student / Librarian
  - *Current behavior:* If `FINE_PER_DAY` is changed from $5 to $10, it retroactively affects all currently overdue books.
  - *Real-world impact:* Students are suddenly charged more than they agreed to when borrowing, leading to massive disputes and angry parents/students.
  - *Fix:* Store the `applied_fine_rate` in the `borrow_records` table at the time the book becomes overdue, rather than calculating it dynamically using the global setting.
  - *Severity:* High
