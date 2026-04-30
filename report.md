
# Adversarial Testing Audit Report: GPA-S-LMS

## A. Student Borrowing Lifecycle
✅ **What works:**
- First login allows password setting (via `student_auth` table update).
- Catalogue browsing fetches available books.
- Normal borrowing flows properly decrement `available_copies` and track due dates.
- Returning the book increments `available_copies`.
- Physically collecting the book is handled by the librarian desktop app (issuing bypasses portal constraints).
- Duplicate request prevention works for simple cases: `api_submit_request` checks for existing pending `book_request` or active `borrowed` loan for the same `book_id`.

❌ **What is missing or broken:**
1. **Concurrent Request Race Condition / Concurrent approvals for same single-copy book:**
   - *Scenario:* Multiple students submit a `book_request` concurrently for a single-copy book, or Librarian clicks approve rapidly on multiple requests.
   - *Persona:* Student / Librarian
   - *Current Behavior:* `api_submit_request` doesn't lock or check `available_copies` transactionally. `api_admin_approve_request` decrements `available_copies` using `MAX(0, available_copies - 1)`. Concurrent approvals result in multiple borrow records for 1 copy.
   - *Why it's a problem:* Students arrive to collect a reserved book to find it already checked out, breaking trust.
   - *Fix:* In `api_admin_approve_request`, verify `available_copies > 0` *within a database transaction* right before executing `INSERT INTO borrow_records`. If 0, rollback.
   - *Severity:* High
2. **Missing Cancellation Flow for Approved Requests / NOT collecting within deadline:**
   - *Scenario:* A student requests cancellation after approval, or fails to collect the book within the 2-day deadline.
   - *Persona:* Student / Librarian
   - *Current Behavior:* Email includes a deadline, but there is no mechanism to auto-expire uncollected approved requests or return the `available_copies` count.
   - *Why it's a problem:* Books remain perpetually locked.
   - *Fix:* Add a daily cron job that scans for 'approved' requests older than 2 days, marks them 'expired', deletes the corresponding `borrow_records`, and increments `books.available_copies`. Add `/api/request/<id>/cancel` to handle cancellation post-approval by reverting state.
   - *Severity:* High

⚠️ **What is partially implemented:**
- **Requesting when at borrowing limit:** `MAX_BOOKS_PER_STUDENT` limit is checked, but the check in `api_submit_request` happens without a transaction lock, allowing a student to rapid-fire requests to bypass the limit.
- **Requesting when overdue:** Not blocked. A student with overdue books can still request more books.

## B. Renewal Lifecycle
✅ **What works:**
- Normal renewal requests are queued.
- Librarian can approve renewals, which extends the due date by 7 days.

❌ **What is missing or broken:**
1. **Renewal when another student is on the waitlist for the same book:**
   - *Scenario:* A student requests a renewal for a highly-demanded book with active waitlist entries.
   - *Persona:* Student / Librarian
   - *Current Behavior:* `api_admin_approve_request` unconditionally extends the `due_date` by 7 days. It never checks the `book_waitlist` table.
   - *Why it's a problem:* Students bypass the queue for high-demand books.
   - *Fix:* In `api_admin_approve_request` (renewal block), run `SELECT COUNT(*) FROM book_waitlist WHERE book_id = ? AND notified = 0`. If count > 0, reject the renewal automatically.
   - *Severity:* High
2. **Renewal when at the maximum renewal count:**
   - *Scenario:* Student tries to renew the same book 5 times.
   - *Persona:* Student
   - *Current Behavior:* There is no `renewal_count` tracked in `borrow_records`. Students can renew infinitely.
   - *Why it's a problem:* Violates library policy of maximum renewal limits.
   - *Fix:* Add a `renewal_count` integer column to `borrow_records`. Increment it on approval. Check `renewal_count < MAX_RENEWALS` in `api_submit_request`.
   - *Severity:* Medium
3. **Librarian approving a renewal after the book was already returned:**
   - *Scenario:* Student requests renewal, returns the book physically, then librarian approves the old request.
   - *Persona:* Librarian
   - *Current Behavior:* `api_admin_approve_request` extends `due_date` where `return_date IS NULL`. If returned, it might silently fail to update or update the wrong record if not scoped properly.
   - *Fix:* Check if `return_date IS NULL` *before* sending approval email. If already returned, mark request invalid.
   - *Severity:* Low
4. **Renewal when overdue with fines accrued:**
   - *Scenario:* Student requests renewal for a book 10 days overdue.
   - *Persona:* Student
   - *Current Behavior:* Renewal is approved and extends the due date from `max(current_due, datetime.now())`. Fines are computed dynamically based on `due_date`. Extending it effectively erases the fine accrued so far.
   - *Why it's a problem:* Students can clear their fines by just getting a renewal approved.
   - *Fix:* Prevent renewal requests if `due_date < CURRENT_DATE` in `api_submit_request`, forcing physical return to pay fine.
   - *Severity:* High

## C. Return & Fine Lifecycle
✅ **What works:**
- On-time returns correctly update `return_date` and status.
- Late returns with fine are computed correctly using `FINE_PER_DAY`.
- Fine appearing on dashboard (dynamically computed or static).

❌ **What is missing or broken:**
1. **Student attempting to borrow while fine is unpaid:**
   - *Scenario:* Student attempts to borrow/request while having unpaid fines.
   - *Persona:* Student
   - *Current Behavior:* The system calculates active fines, but neither `api_submit_request` nor `database.borrow_book` checks if the student has pending fines before allowing a new borrow.
   - *Why it's a problem:* No consequence for not paying fines.
   - *Fix:* In `database.borrow_book` and `api_submit_request`, query `SUM(fine) FROM borrow_records WHERE enrollment_no = ? AND status = 'returned'`. If sum > 0, block the borrow/request.
   - *Severity:* High
2. **Fine payment acknowledgment / Fine being cleared after payment:**
   - *Scenario:* Student pays fine to librarian.
   - *Persona:* Librarian
   - *Current Behavior:* The codebase completely lacks an explicit `mark_fine_paid` endpoint or database mechanism to clear/zero-out fines on a returned book.
   - *Why it's a problem:* Fines remain on the student's dashboard permanently even after physical payment.
   - *Fix:* Create a desktop UI button and `database.py` function `mark_fine_paid(record_id)` that updates the `fine_paid` status or zeroes the `fine` column.
   - *Severity:* Critical
3. **Lost book / Damaged book scenario:**
   - *Scenario:* Student loses a book.
   - *Persona:* Librarian / Student
   - *Current Behavior:* No mechanism to mark a book as 'lost'. It remains permanently 'borrowed' accruing infinite fines.
   - *Fix:* Add a 'Mark Lost' action in `main.py` that sets status to 'lost', applies a fixed replacement fee to `fine`, and removes the copy from total circulation.
   - *Severity:* Medium

## D. Waitlist Lifecycle
✅ **What works:**
- Joining waitlist for unavailable book.
- Being notified when book is returned (`database.return_book` triggers `_notify_waitlist`).

❌ **What is missing or broken:**
1. **Notification expiring / Acting on notification within a window / Queue Advancement:**
   - *Scenario:* First student on waitlist is notified but never borrows the book.
   - *Persona:* Student
   - *Current Behavior:* `_notify_waitlist` sets `notified = 1`. No logic expires the notification to advance to the next person.
   - *Why it's a problem:* Second person is permanently blocked.
   - *Fix:* Implement a cron job checking if a book is available and the last notified person hasn't borrowed it within 24 hours. If so, delete their waitlist entry and call `_notify_waitlist` again.
   - *Severity:* High
2. **Leaving waitlist:**
   - *Scenario:* Student finds the book elsewhere and wants to leave the waitlist.
   - *Persona:* Student
   - *Current Behavior:* `/api/books/<book_id>/notify` handles adding, but there's no UI/backend endpoint to explicitly *remove* oneself from the waitlist once joined.
   - *Fix:* Implement `DELETE` method on `/api/books/<book_id>/notify` to remove the waitlist entry.
   - *Severity:* Low

## E. Account & Identity Scenarios
✅ **What works:**
- First login password change, forgot password request.
- Profile update requests.

❌ **What is missing or broken:**
1. **Year changing to Pass Out with active loans:**
   - *Scenario:* Student changes year to 'Pass Out' but has 5 books borrowed.
   - *Persona:* Student
   - *Current Behavior:* `api_admin_approve_request` blindly applies profile updates (`year`). It doesn't check for active loans.
   - *Why it's a problem:* Graduating students abscond with books.
   - *Fix:* In `api_admin_approve_request` (profile update block), if `details_updated.get('year')` is 'pass out', query `SELECT COUNT(*) FROM borrow_records WHERE enrollment_no = ? AND status = 'borrowed'`. If > 0, reject the update.
   - *Severity:* High
2. **Account deletion with active loans and fines:**
   - *Scenario:* Student requests account deletion while having books borrowed or fines.
   - *Persona:* Student
   - *Current Behavior:* `api_admin_approve_deletion` executes `DELETE FROM students` but lacks pre-flight checks for `status = 'borrowed'` or unpaid fines.
   - *Why it's a problem:* Books are lost, fines are erased.
   - *Fix:* In `api_admin_approve_deletion`, enforce a check: reject if active loans > 0 or unpaid fines > 0.
   - *Severity:* High
3. **Password change with session active on another device:**
   - *Scenario:* Student changes password.
   - *Persona:* Student
   - *Current Behavior:* Active sessions are not invalidated because session tokens aren't tracked with a `session_id` in the DB or secret rotation.
   - *Fix:* Add a `session_version` to the `student_auth` table that increments on password change. Validate it in `@app.before_request`.
   - *Severity:* Medium

## F. Librarian Daily Operations
✅ **What works:**
- Approving/rejecting requests, issuing/returning at counter.
- Uploading study materials, importing Excel.

❌ **What is missing or broken:**
1. **Issuing a book directly at the counter bypassing pending portal requests:**
   - *Scenario:* Librarian issues the last copy of a book at the counter. A student portal request is already pending for it.
   - *Persona:* Librarian
   - *Current Behavior:* `borrow_book` in `database.py` allows borrowing. It doesn't clear the pending `book_request`. If librarian later clicks 'approve' on the portal request, a ghost duplicate borrow record is created.
   - *Why it's a problem:* Database corruption (2 active borrow records for 1 copy).
   - *Fix:* In `database.borrow_book`, execute `DELETE FROM requests WHERE request_type = 'book_request' AND JSON_EXTRACT(details, '$.book_id') = ?` upon successful counter issue.
   - *Severity:* High

## G. Sync & Data Integrity
✅ **What works:**
- Desktop syncing to Supabase via background threads using `updated_at`.

❌ **What is missing or broken:**
1. **Sync conflict when the same record is modified on both desktop and portal simultaneously:**
   - *Scenario:* Student updates profile on portal; librarian updates student on desktop simultaneously.
   - *Persona:* System
   - *Current Behavior:* Sync manager blindly overwrites based on local state (`_sync_table_local_to_remote`) using `ON CONFLICT DO UPDATE`. It lacks true bidirectional conflict resolution (e.g., comparing `updated_at` timestamps on a per-row level before overwriting).
   - *Why it's a problem:* Silent data loss.
   - *Fix:* In `sync_manager.py` `_sync_table_local_to_remote`, add `WHERE EXCLUDED.updated_at > table.updated_at` to the `DO UPDATE SET` clause.
   - *Severity:* High
2. **available_copies count drifting between systems:**
   - *Scenario:* Network disconnects.
   - *Persona:* System
   - *Current Behavior:* `available_copies` is incremented/decremented via fire-and-forget `_push_to_cloud('UPDATE books SET available_copies = ...')`. If it fails, `available_copies` drifts permanently because `sync_manager` doesn't fully mirror `books` table state frequently enough, relying on deltas.
   - *Fix:* Run a daily reconciliation query in `sync_manager` that forces a full overwrite of `available_copies` based on the authoritative local SQLite state.
   - *Severity:* Medium

## H. Deployment & Infrastructure
✅ **What works:**
- Flask on Waitress, Supabase cloud sync, CSRF protection on most endpoints.

❌ **What is missing or broken:**
1. **Unauthenticated access to admin endpoints:**
   - *Scenario:* Student accesses `/api/admin/all-requests`.
   - *Persona:* Attacker / Student
   - *Current Behavior:* The `api_admin_*` endpoints have *no authentication check* or decorator.
   - *Why it's a problem:* Complete system takeover. Any student can approve requests or reset passwords.
   - *Fix:* Add an `@admin_required` decorator checking `if request.remote_addr != '127.0.0.1'` or requiring an admin session token on all `/api/admin/` endpoints.
   - *Severity:* Critical
2. **Unauthenticated study material downloads:**
   - *Scenario:* Script mass-downloads materials.
   - *Persona:* Attacker
   - *Current Behavior:* `download_study_material` has no session check.
   - *Fix:* Add `if 'student_id' not in session: return jsonify({'error': 'Unauthorized'}), 401`.
   - *Severity:* Medium
3. **SQLite vs PostgreSQL query compatibility (placeholders):**
   - *Scenario:* Complex JSON queries.
   - *Persona:* System
   - *Current Behavior:* `PostgresCursorWrapper` simply `.replace('?', '%s')`. If a literal string contains `?` (e.g., in a book title), it breaks the query.
   - *Fix:* Use a proper regex or an AST parser to replace only unbound `?` parameters in SQL.
   - *Severity:* Low
4. **Rate limiting or lack thereof on sensitive endpoints:**
   - *Scenario:* Student brute-forces `/api/login` or submits request 100 times.
   - *Persona:* Attacker
   - *Current Behavior:* No rate limiting (Flask-Limiter is missing).
   - *Fix:* Install and configure `Flask-Limiter` on `/api/login` and `/api/request`.
   - *Severity:* Medium

## I. Notification & Email Pipeline
✅ **What works:**
- Email delivery via background batch service.

❌ **What is missing or broken:**
1. **Silent failure / Bad formats:**
   - *Scenario:* Student has invalid email "student@.com".
   - *Persona:* System
   - *Current Behavior:* Fails silently in `email_batch_service.py` print statements.
   - *Fix:* Validate email format using regex during profile update.
   - *Severity:* Low
2. **Orphaned notifications after account deletion:**
   - *Scenario:* Student account deleted.
   - *Persona:* System
   - *Current Behavior:* `user_notifications` lacks `ON DELETE CASCADE`.
   - *Fix:* Add cascading deletes or explicitly delete them in `api_admin_approve_deletion`.
   - *Severity:* Low

## J. Edge Cases & Stress Scenarios
✅ **What works:**
- Parameterized queries prevent most SQL injection.

❌ **What is missing or broken:**
1. **Student submitting the same request type 20 times rapidly:**
   - *Scenario:* Student double-clicks submit.
   - *Persona:* Student
   - *Current Behavior:* No frontend debounce and no backend transactional lock on `api_submit_request`.
   - *Fix:* Add frontend button disable on submit, and backend unique index on `(enrollment_no, request_type, JSON_EXTRACT(details, '$.book_id')) WHERE status='pending'`.
   - *Severity:* Medium
2. **Student with a name containing special characters breaking email templates:**
   - *Scenario:* Name is `O'Connor`.
   - *Persona:* System
   - *Current Behavior:* HTML templates might not escape the name properly if it's injected directly via f-strings rather than Jinja templates.
   - *Fix:* Use `jinja2.escape` or proper templating engines for all email bodies instead of python f-strings.
   - *Severity:* Low
