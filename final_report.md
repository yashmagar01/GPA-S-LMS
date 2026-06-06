# GPA-S-LMS Adversarial Audit Report

## A. Student Borrowing Lifecycle
✅ **What works**
- Requesting a book checks limits and existing active loans.
- Pass out students are restricted from submitting borrow requests.
- Overdue detection for books works.

❌ **What is missing or broken**
**Scenario**: Student goes to collect an approved book request, but does not collect it within the deadline.
**Persona**: Student / Librarian
**Trace**: `api_admin_approve_request` sets an email deadline of 2 days, but there is no mechanism to expire uncollected requests or automatically return the book to the available pool. The `borrow_records` entry is created immediately upon approval with status 'borrowed', but there is no 'pending collection' state.
**Impact**: In a real college, if a student requests a book and doesn't collect it, the book is considered 'borrowed' indefinitely and unavailable to others, causing artificial scarcity.
**Fix**: Introduce a 'pending_collection' state in `borrow_records`. Create a scheduled cron job or check in the daemon that expires 'pending_collection' records after 48 hours, changes their status to 'cancelled', increments `books.available_copies`, and notifies the student.
**Severity**: High

**Scenario**: Student requests cancellation of a book request before approval.
**Persona**: Student
**Trace**: `api_cancel_request` checks `if req['status'] != 'pending': return error`. If pending, it sets the status to `cancelled` in the `requests` table. However, it fails to cancel the request gracefully for 'book_request' because no inventory rollback happens, but `api_submit_request` didn't decrement it either. So this actually works for *before* approval.
**Impact**: Works as intended before approval.
**Fix**: None needed for before approval.
**Severity**: Low

**Scenario**: Student requests cancellation of a book request AFTER approval.
**Persona**: Student
**Trace**: `api_cancel_request` prevents cancellation if `status` is not `pending`. So students cannot cancel an approved request.
**Impact**: If a student changes their mind after approval, they have to physically go to the library to return the book, or it sits in 'borrowed' state forever.
**Fix**: Allow cancellation of 'approved' requests if they are in 'pending_collection' state (see previous fix). If cancelled, increment `available_copies` and mark the `borrow_records` entry as 'cancelled'.
**Severity**: Medium

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Duplicate request submission edge case.
**Persona**: Student
**Trace**: `api_submit_request` parses `details` using `json.loads` in a try-except block, then checks for existing requests by iterating over `cur_dup.fetchall()` and parsing `details` *again*. If a student rapidly submits the same request twice, the `MAX_BOOKS_PER_STUDENT` limit or duplicate check might have a race condition since there's no database-level unique constraint on `(enrollment_no, book_id, status='pending')`.
**Impact**: A student could use a script to spam the endpoint and reserve multiple copies of the same book, exhausting the library's stock.
**Fix**: Add a database-level unique index on `requests` for `(enrollment_no, json_extract(details, '$.book_id'))` where `status = 'pending'`, or enforce the check inside a transaction with `BEGIN IMMEDIATE` (which `get_portal_db` doesn't enforce tightly).
**Severity**: Medium

## B. Renewal Lifecycle
✅ **What works**
- Renewal requests can be submitted via `api_submit_request`.
- `api_admin_approve_request` extends the `due_date` by 7 days.

❌ **What is missing or broken**
**Scenario**: Renewal request submitted when another student is on the waitlist for the same book.
**Persona**: Student / Librarian
**Trace**: `api_admin_approve_request` handles `req_type == 'renewal'` without checking the `book_waitlist` table. It simply adds 7 days to `due_date`.
**Impact**: In a real college environment, high-demand books should not be renewable if others are waiting. Students can monopolize a book indefinitely.
**Fix**: In `api_admin_approve_request` (or `api_submit_request`), query the `book_waitlist` table for `book_id`. If `COUNT(*) > 0`, block the renewal and return an error message to the student/librarian.
**Severity**: High

**Scenario**: Renewal request submitted when the book is already overdue with fines accrued.
**Persona**: Student / Librarian
**Trace**: `api_admin_approve_request` blindly extends the `due_date` from `max(current_due, datetime.now())`. Fines are calculated dynamically in the dashboard based on the *current* `due_date`. By extending the due date past `datetime.now()`, the overdue days become negative or zero, effectively erasing the accrued fine.
**Impact**: Students can avoid paying fines by simply requesting a renewal and having the librarian approve it, which wipes out the existing fine.
**Fix**: In `api_admin_approve_request`, before extending `due_date`, check if `datetime.now() > current_due`. If so, calculate the fine and lock it into the `fine` column of the `borrow_records` table, or block renewals for overdue books entirely until the fine is paid.
**Severity**: High

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Renewal at the maximum renewal count.
**Persona**: Student
**Trace**: There is no column tracking how many times a specific `borrow_records` entry has been renewed.
**Impact**: A student can infinitely renew a book.
**Fix**: Add a `renewal_count` integer column to `borrow_records`. Increment it in `api_admin_approve_request` and reject renewals if `renewal_count >= MAX_RENEWALS`.
**Severity**: Medium

## C. Return & Fine Lifecycle
✅ **What works**
- The `return_book` function correctly calculates fines and updates the `return_date` and `status` to 'returned'.
- Waitlist notifications are triggered upon return.

❌ **What is missing or broken**
**Scenario**: Fine payment acknowledgment and clearing.
**Persona**: Librarian / Student
**Trace**: `LibraryApp/database.py` does not contain a `mark_fine_paid` function. Fines are stored as an integer on `borrow_records`. There is no mechanism to mark a specific fine as paid without modifying the `fine` column directly, which destroys the history of the fine ever existing, or no mechanism exists at all in the API.
**Impact**: Librarians cannot clear fines from the desktop app or portal. Students will perpetually see fines on their dashboard even if they paid them physically.
**Fix**: Add a `fine_paid` boolean column (or a `fine_status` enum) to `borrow_records`. Implement a `mark_fine_paid(enrollment_no, borrow_id)` function in `database.py` and a corresponding admin endpoint in `student_portal.py` to toggle this status.
**Severity**: Critical

**Scenario**: Student attempting to borrow while fine is unpaid.
**Persona**: Student
**Trace**: `api_submit_request` and `borrow_book` check the `MAX_BOOKS_PER_STUDENT` limit, but they do not aggregate unpaid fines to block borrowing.
**Impact**: Students can accumulate massive fines and continue borrowing books, defeating the purpose of the fine system.
**Fix**: In `api_submit_request` and `database.py:borrow_book`, sum the unpaid fines for the student. If `total_unpaid_fines > MAX_ALLOWED_FINE` (or > 0), reject the borrow request.
**Severity**: High

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Lost or damaged book scenario.
**Persona**: Librarian / Student
**Trace**: There is no distinct status for 'lost' or 'damaged' in `borrow_records`. The only statuses are 'borrowed' and 'returned'.
**Impact**: If a book is lost, the librarian must either leave it as 'borrowed' (accruing infinite fines) or mark it 'returned' (making it appear available in the catalogue, which is false).
**Fix**: Add 'lost' and 'damaged' as valid statuses in `borrow_records`. When marked lost, do NOT increment `available_copies` on the `books` table, and assess a fixed replacement fine.
**Severity**: Medium

## D. Waitlist Lifecycle
✅ **What works**
- Joining waitlist works via `add_to_waitlist`.
- Leaving waitlist works via `remove_from_waitlist`.
- Notification is triggered via `_notify_waitlist` when a book is returned.

❌ **What is missing or broken**
**Scenario**: Acting on notification within a window / notification expiring.
**Persona**: Student
**Trace**: `_notify_waitlist` sets `notified = 1` and sends a notification. However, it does not reserve the book for that specific student. Any other student can immediately borrow or request the newly returned book.
**Impact**: The waitlisted student receives a notification, but by the time they log in, someone else might have requested it, rendering the waitlist useless.
**Fix**: When notifying a waitlisted student, place a 24-hour hold on one copy of the book (e.g., decrement `available_copies` but don't assign it to a student yet, or add a `reserved_until` column in `books`). If the student doesn't request it in 24 hours, release the hold and notify the next person.
**Severity**: High

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Multiple students on waitlist for same book.
**Persona**: System
**Trace**: `_notify_waitlist` queries with `LIMIT 1` and marks only the first person as notified. This is correct, but if the first person doesn't borrow it, there is no recurring job to notify the next person.
**Impact**: The second person on the waitlist will never be notified if the first person ignores their notification.
**Fix**: Implement a daemon task that checks for expired waitlist notifications (e.g., `notified = 1` and `updated_at < NOW() - 1 day`), deletes that waitlist entry, and calls `_notify_waitlist` again to alert the next student.
**Severity**: Medium

## E. Account & Identity Scenarios
✅ **What works**
- First login password change forces the user to change from the default.
- Profile update requests can be submitted and approved.

❌ **What is missing or broken**
**Scenario**: Year changing to Pass Out with active loans.
**Persona**: Librarian / Student
**Trace**: The application prevents 'Pass Out' students from borrowing *new* books in `api_submit_request` and `borrow_book`. However, if a student's year is updated to 'Pass Out' (e.g., via bulk promotion or profile update), the system does not check for or enforce the return of active loans.
**Impact**: Students who graduate can leave with college property, and the system won't flag it during the promotion process.
**Fix**: When updating a student's year to 'Pass Out', check `borrow_records` for active loans. If loans exist, either block the promotion or automatically flag the account to the librarian.
**Severity**: High

**Scenario**: Account deletion with active loans.
**Persona**: Librarian
**Trace**: `delete_student` in `database.py` blocks deletion if `active_borrows > 0`. However, the student portal's `api_admin_approve_deletion` (which approves student-requested deletions) does not check active loans before deleting the account.
**Impact**: A student with borrowed books can request account deletion, the librarian approves it on the portal, and the student's records are wiped, losing track of the unreturned books.
**Fix**: Add a check in `api_admin_approve_deletion` to query `borrow_records` for `status = 'borrowed'`. Reject the deletion if active loans exist.
**Severity**: Critical

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Self-registration approval.
**Persona**: Librarian
**Trace**: `api_admin_approve_request` handles `student_registration`. It inserts into `students` and `user_settings`. However, it does not insert a record into `student_auth`. The student cannot log in until the librarian manually resets their password or the system generates one.
**Impact**: Approved self-registered students cannot log in.
**Fix**: In the `student_registration` branch of `api_admin_approve_request`, insert a default hashed password (e.g., their enrollment number) into `student_auth` with `is_first_login = 1`.
**Severity**: High

## F. Librarian Daily Operations
✅ **What works**
- Approving and rejecting requests.
- Viewing and managing active loans.
- Viewing overdue list.

❌ **What is missing or broken**
**Scenario**: Issuing a book directly at the counter (bypass portal).
**Persona**: Librarian
**Trace**: `borrow_book` in `database.py` is used by the desktop app. It decrements `available_copies` and creates a `borrow_records` entry. However, it does NOT insert a record into the `requests` table or mark any pending portal request as 'fulfilled'.
**Impact**: If a student submits a portal request, then goes to the counter and the librarian issues it directly, the portal request remains 'pending'. The librarian might later approve the request, decrementing `available_copies` *again* and creating a duplicate `borrow_records` entry.
**Fix**: When issuing a book at the counter via `borrow_book`, check the `requests` table for a pending request from that `enrollment_no` for that `book_id` and mark it as 'fulfilled' or 'approved'.
**Severity**: High

⚠️ **What is partially implemented or has a hidden edge case**
**Scenario**: Importing Excel transaction data creating duplicate records.
**Persona**: Librarian
**Trace**: Legacy Excel imports often use arbitrary dates or lack unique accessions. `sync_manager.py` uses `enrollment_no, accession_no, borrow_date` as the natural key for `borrow_records`.
**Impact**: If imported Excel data lacks an `accession_no`, the sync manager silently ignores the record or fails to sync it, causing divergence between local and cloud.
**Fix**: Ensure the Excel import script assigns a synthesized `accession_no` (e.g., `<book_id>_legacy_<row>`) if one is missing, so sync logic functions correctly.
**Severity**: Medium

## G. Sync & Data Integrity
✅ **What works**
- Desktop app syncing to Supabase via `SyncManager`.
- Natural keys map correctly for most tables.

❌ **What is missing or broken**
**Scenario**: Sync conflict when the same record is modified on both desktop and portal simultaneously.
**Persona**: System
**Trace**: `sync_manager.py` uses `updated_at` to resolve conflicts during remote-to-local sync (`if remote_ts <= local_ts: continue`). However, local-to-remote sync blindly uses `ON CONFLICT (...) DO UPDATE SET ...` without checking the remote `updated_at`.
**Impact**: If a local change is made (e.g., desktop app) and a remote change is made (e.g., student portal), the last one to push wins, potentially overwriting newer data with older data during the local-to-remote phase.
**Fix**: Modify `_sync_table_local_to_remote` to include a `WHERE table_name.updated_at < EXCLUDED.updated_at` clause in the `ON CONFLICT DO UPDATE SET` statement.
**Severity**: High

**Scenario**: `accession_no` missing from web portal approvals.
**Persona**: System
**Trace**: `api_admin_approve_request` inserts into `borrow_records` but does not generate or include an `accession_no` (it passes the generic `book_id`). `sync_manager.py` relies on `accession_no` as part of the natural key.
**Impact**: Records inserted from the web portal without an `accession_no` will be silently ignored during synchronization, causing cloud and local databases to drift permanently.
**Fix**: In `api_admin_approve_request`, generate a unique `accession_no` (e.g., appending a UUID or timestamp to the `book_id`) when creating the `borrow_records` entry.
**Severity**: Critical

## H. Deployment & Infrastructure
✅ **What works**
- Waitress serves the Flask app.
- ProxyFix is used (memory constraint mentions this).

❌ **What is missing or broken**
**Scenario**: SQLite vs PostgreSQL query compatibility (placeholders, ILIKE vs LIKE).
**Persona**: System
**Trace**: `PostgresCursorWrapper` automatically translates SQLite `?` to PostgreSQL `%s`. However, it lacks an `executemany` method.
**Impact**: Batch operations, such as access log writing in `_log_writer_loop`, use `cursor.executemany`. In cloud deployments (PostgreSQL), this will throw an AttributeError, causing the access logs batch insert to fail and potentially crash the background thread.
**Fix**: Implement `executemany(self, sql, param_list)` in `PostgresCursorWrapper` that iterates over `param_list` and calls `self.execute(sql, params)` for each, or uses `psycopg2.extras.execute_batch`.
**Severity**: Critical

**Scenario**: Render cold start and session behavior.
**Persona**: Student
**Trace**: Flask app fallback behavior generates a `.secret_key` file. On ephemeral filesystems like Render, this file is lost across cold starts.
**Impact**: Complete session loss for all logged-in students across cold starts and horizontal scaling.
**Fix**: Do not rely on file-based secret keys. Ensure `FLASK_SECRET_KEY` is strictly required in the environment variables, and throw a fatal startup error if it is missing, rather than generating a temporary one.
**Severity**: High

## I. Notification & Email Pipeline
✅ **What works**
- Notifications are generated on request approvals.
- Emails are sent with templates.

❌ **What is missing or broken**
**Scenario**: Email failing silently.
**Persona**: Student / System
**Trace**: `trigger_notification_email` and `generate_email_template` are called inside `api_submit_request` and `api_admin_approve_request`. If the SMTP server is down or times out, the exception is caught and logged, but the user is not warned.
**Impact**: Students assume their request was submitted successfully, but they never receive the email confirmation. The UI says 'success' even if email dispatch fails.
**Fix**: Queue emails asynchronously using a robust task queue (like Celery) or log failures to a `failed_emails` table with a retry mechanism. Do not block the API response, but ensure delivery is guaranteed.
**Severity**: Medium

## J. Edge Cases & Stress Scenarios
✅ **What works**
- Empty catalogue handles 0 books.

❌ **What is missing or broken**
**Scenario**: Concurrent approvals for the same single-copy book.
**Persona**: Librarian
**Trace**: `api_admin_approve_request` decrements `available_copies` (`UPDATE books SET available_copies = GREATEST(0, available_copies - 1)`). It does not check if `available_copies` was already 0 *before* the update, nor does it check rowcount.
**Impact**: If two librarians approve two different requests for the same book simultaneously, and there is only 1 copy, the `borrow_records` table will show 2 active loans, but `available_copies` will bottom out at 0. Physical reality breaks.
**Fix**: Change the update query to `UPDATE books SET available_copies = available_copies - 1 WHERE book_id = ? AND available_copies > 0`. Then, check `cursor.rowcount`. If it is 0, the book is out of stock; abort the transaction and return a 409 Conflict error.
**Severity**: High

**Scenario**: Fine rate changed mid-loan period.
**Persona**: Student / Librarian
**Trace**: The fine is calculated dynamically as `days_late * fine_per_day` using the current `get_portal_fine_per_day()`. It is not historically snapshotted.
**Impact**: If the library changes the fine rate from ₹5 to ₹10, all existing overdue books instantly double their fines retrospectively. This is legally/ethically problematic for students.
**Fix**: When a book becomes overdue, lock the daily fine rate into the `borrow_records` table (e.g., `applied_fine_rate`). Calculate total fine based on that locked rate, not the global current rate.
**Severity**: Medium
