# GPA-S-LMS Adversarial Testing Audit Report

## A. Student Borrowing Lifecycle
✅ **What works:**
- First login and catalogue browsing.
- Submitting a book request.
- Checking for duplicate active loans and borrow limits before requesting.
- Enforcing that 'Pass Out' students cannot borrow or request books.
- Email triggers upon request approval.
- Deduction of `available_copies` when librarian approves request.
- Cancellation before approval via `/api/request/<req_id>/cancel` correctly updating status.

❌ **What is missing or broken:**
- **Scenario:** Not collecting within deadline.
  - **Persona:** Student/Librarian
  - **Trace:** While the approval email states "If not collected by the deadline, the reservation will be cancelled", there is NO automated mechanism in `student_portal.py` or `database.py` that expires an approved request, cancels the reservation, and increments `available_copies` back if the book isn't physically collected within 2 days. The book stays permanently locked in `borrowed` state because approval immediately creates a `borrow_record`.
  - **Impact:** Why this is a problem: Books are permanently "lost" from the catalogue if a student never picks them up. Real students frequently forget to collect items. This leads to artificial scarcity and complaints.
  - **Fix:** Do not create a `borrow_record` or decrement `available_copies` immediately upon *approving* a reservation request in `api_admin_approve_request`. Instead, approval should just change the request status to `approved`. A separate "Issue Book" flow at the counter should fulfill the request and create the `borrow_record`.
  - **Severity:** High

- **Scenario:** Request cancellation AFTER approval.
  - **Persona:** Student
  - **Trace:** The `/api/request/<req_id>/cancel` endpoint specifically checks `if req['status'] != 'pending': return error`. Students cannot cancel an approved request via the portal.
  - **Impact:** Why this is a problem: If a student changes their mind after approval but before collection, they cannot notify the system, leaving the book locked as `borrowed`.
  - **Fix:** Allow cancellation of `approved` (but not yet collected) requests in `api_cancel_request`, and if cancelled, implement logic to reverse the `borrow_record` creation and increment `available_copies`.
  - **Severity:** Medium

- **Scenario:** Requesting a book when out of stock.
  - **Persona:** Student
  - **Trace:** The portal API `api_submit_request` does NOT check `available_copies > 0` when inserting the request into the `requests` table. It allows submitting a request for a book with 0 copies.
  - **Impact:** Why this is a problem: Students can reserve out-of-stock books, causing frustration. The librarian will later click "Approve", which *will* fail with "Cannot approve: No available copies left", but the student experience is broken (they shouldn't be able to request it in the first place).
  - **Fix:** Add a check in `api_submit_request` to ensure `available_copies > 0` before allowing a `book_request`.
  - **Severity:** Medium

- **Scenario:** Requesting when overdue.
  - **Persona:** Student
  - **Trace:** `api_submit_request` does not aggregate fines or check for overdue books before allowing a student to borrow a new book.
  - **Impact:** Why this is a problem: Students can continue borrowing books while possessing extremely overdue items, defeating the entire purpose of deadlines.
  - **Fix:** In `api_submit_request`, query for active overdue books (due_date < CURRENT_DATE) and reject the request if they have any.
  - **Severity:** High

- **Scenario:** Requesting when at borrowing limit.
  - **Persona:** Student
  - **Trace:** `api_submit_request` checks `borrow_records` where `status = 'borrowed'` and compares to the limit (e.g., 5). However, it does not count `pending` or `approved` requests in that limit.
  - **Impact:** Why this is a problem: A student can request 20 books at once, bypassing the limit until the librarian tries to approve them.
  - **Fix:** In `api_submit_request`, the borrow limit check should count `COUNT(*) FROM borrow_records WHERE status = 'borrowed' OR (SELECT COUNT(*) FROM requests WHERE status IN ('pending', 'approved'))`.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Duplicate Request for same book.
  - **Persona:** Student
  - **Trace:** In `api_submit_request`, the check for pending requests only looks for `status = 'pending'`.
  - **Impact:** Why this is a problem: A student can submit a request, have it approved, and submit another one for the exact same book.
  - **Fix:** Enhance the duplicate check to prevent multiple requests for the same book, regardless of the status being pending, approved, or borrowed.
  - **Severity:** Medium

## B. Renewal Lifecycle
✅ **What works:**
- Normal renewal request via `/api/request`.
- Renewal requests properly check for duplicate pending requests for the exact same book copy.
- Librarian approving a renewal after the book was already returned safely does nothing (checks `return_date IS NULL`).

❌ **What is missing or broken:**
- **Scenario:** Renewal when already overdue.
  - **Persona:** Student
  - **Trace:** `api_submit_request` completely lacks validation for whether the book is already overdue (fines accrued).
  - **Impact:** Why this is a problem: Students can infinitely renew books indefinitely, completely bypassing return policies, even if they owe fines.
  - **Fix:** In `api_submit_request` for `req_type == 'renewal'`, check if `CURRENT_DATE > due_date`. If so, reject the request.
  - **Severity:** Critical

- **Scenario:** Renewal when at max limit.
  - **Persona:** Student
  - **Trace:** The system currently doesn't track or limit the number of times a book can be renewed.
  - **Impact:** Why this is a problem: Indefinite borrowing, preventing others from accessing the book.
  - **Fix:** Add a `renewal_count` column to `borrow_records` and enforce a limit (e.g., MAX 2 renewals) in `api_submit_request`.
  - **Severity:** High

- **Scenario:** Renewal when another student is on the waitlist.
  - **Persona:** Student
  - **Trace:** `api_submit_request` does not check the `book_waitlist` table before allowing a renewal request.
  - **Impact:** Why this is a problem: A student can renew a book indefinitely while a waitlisted student never gets a turn.
  - **Fix:** In `api_submit_request`, check `SELECT COUNT(*) FROM book_waitlist WHERE book_id = ?`. If > 0, reject the renewal request explaining that others are waiting.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Librarian approving a renewal for a book that is overdue with fines accrued.
  - **Persona:** Librarian
  - **Trace:** In `api_admin_approve_request`, when extending the due date for a renewal, it extends the date from `max(current_due, datetime.now())`. This wipes out the overdue status without collecting the fine.
  - **Impact:** Why this is a problem: Fine evasion.
  - **Fix:** Check if `datetime.now() > current_due`. If it is, the librarian should ideally not be able to approve it, or the fine should be calculated and persisted *before* the due date is extended.
  - **Severity:** High

## C. Return & Fine Lifecycle
✅ **What works:**
- On-time return completes the cycle without fines.
- Return logic properly calculates fines based on days late * `FINE_PER_DAY`.
- Returning increments `available_copies` and triggers waitlist notifications.

❌ **What is missing or broken:**
- **Scenario:** Fine payment acknowledgment and clearing.
  - **Persona:** Librarian/Student
  - **Trace:** Fines are calculated and stored in `borrow_records` during `return_book`. However, there is no UI, API, or logic to track whether a fine has been *paid*. The `borrow_records` table has a `fine` amount, but no `fine_paid` boolean. The student dashboard shows fines, but they remain forever.
  - **Impact:** Why this is a problem: Librarians cannot clear fines. Students cannot borrow books if you later implement a "no active fines" check, but right now there is no such check anyway.
  - **Fix:** Add a `fine_status` (e.g., 'pending', 'paid', 'waived') column to `borrow_records`. Create an API endpoint (`api_admin_clear_fine`) to update this status. Update `api_dashboard` to only sum 'pending' fines.
  - **Severity:** Critical

- **Scenario:** Student attempting to borrow while fine is unpaid.
  - **Persona:** Student
  - **Trace:** `api_submit_request` (book_request) and `borrow_book` (database.py) do not check if the student has outstanding unpaid fines.
  - **Impact:** Why this is a problem: Students can accumulate massive fines and continue borrowing books without penalty.
  - **Fix:** Aggregate unpaid fines for the student. If `total_unpaid > 0`, reject the borrow/request action.
  - **Severity:** High

- **Scenario:** Lost or damaged book scenario.
  - **Persona:** Librarian
  - **Trace:** There is no workflow for marking a book as lost or permanently damaged.
  - **Impact:** Why this is a problem: The book remains "issued" forever, accruing infinite fines, and the catalogue count is permanently skewed.
  - **Fix:** Add a "Mark Lost/Damaged" button that updates the `borrow_records` status, halts fine calculation, and permanently decrements `total_copies` from the `books` table.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Fine waiver by librarian.
  - **Persona:** Librarian
  - **Trace:** Librarians cannot waive fines if a student has a legitimate excuse. Fines are hardcoded into the `return_book` logic based on dates.
  - **Impact:** Why this is a problem: Prevents fair administration of library policies.
  - **Fix:** Add a mechanism to mark a fine as 'waived' in the `borrow_records` table.
  - **Severity:** Low

## D. Waitlist Lifecycle
✅ **What works:**
- Joining waitlist for unavailable book.
- Leaving waitlist.

❌ **What is missing or broken:**
- **Scenario:** Notification expiring / Action window.
  - **Persona:** Student
  - **Trace:** When a book is returned, `_notify_waitlist` adds a notification for the *first* person, but it does NOT temporarily reserve the book for them. `available_copies` just increments. ANY other student can immediately log in and request the book before the notified student sees the email.
  - **Impact:** Why this is a problem: Waitlist provides a notification, but zero actual priority or reservation guarantee.
  - **Fix:** When notifying the waitlist, the system should either immediately create an `approved` request for the waitlisted user, holding the book for a set window (e.g., 24h), or implement a `held_for_user` column on `books`.
  - **Severity:** High

- **Scenario:** Multiple students on waitlist for same book.
  - **Persona:** Student
  - **Trace:** `_notify_waitlist` only notifies the first person. Because there is no action window, if that person never claims it, the second person is never notified.
  - **Impact:** Why this is a problem: Waitlist stalls. If the first person ignores the notification, the system never moves to the next person.
  - **Fix:** Implement a background job that checks if a waitlist notification has expired (e.g., after 24h) and then notifies the next person in line.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Waitlist priority vs direct counter borrow.
  - **Persona:** Librarian
  - **Trace:** If a book is returned at the counter and `available_copies` becomes 1, the librarian can immediately issue it to another student standing there, ignoring the waitlist notification that was just sent.
  - **Impact:** Why this is a problem: Bypasses waitlist logic in person.
  - **Fix:** `borrow_book` should warn the librarian if the book has an active waitlist.
  - **Severity:** Low

## E. Account & Identity Scenarios
✅ **What works:**
- First login password change.
- Forgot password request workflow.
- Profile update request.

❌ **What is missing or broken:**
- **Scenario:** Session invalidation on password change.
  - **Persona:** Student/Attacker
  - **Trace:** When a student changes their password via `api_change_password`, it DOES NOT invalidate existing active sessions on other devices.
  - **Impact:** Why this is a problem: An attacker who compromised a session can maintain access even after the legitimate user changes their password.
  - **Fix:** Add a `session_token` timestamp to the `students` table. Store this token in the Flask `session` upon login. In an `@app.before_request` hook, verify the session's token matches the DB.
  - **Severity:** High

- **Scenario:** Account deletion with active loans.
  - **Persona:** Librarian/Student
  - **Trace:** In `api_admin_approve_deletion(del_id)`, the system deletes the student from all tables but DOES NOT check if the student has active `borrow_records` where `status = 'borrowed'`.
  - **Impact:** Why this is a problem: `borrow_records` become orphaned and `available_copies` is never recovered.
  - **Fix:** In `api_admin_approve_deletion`, before deleting, check `SELECT COUNT(*) FROM borrow_records WHERE enrollment_no = ? AND return_date IS NULL`. If > 0, return a 400 error.
  - **Severity:** Critical

- **Scenario:** Year changing to Pass Out with active loans.
  - **Persona:** Student/Librarian
  - **Trace:** The librarian can change a student's year to 'Pass Out' (in `profile_update` approval), but the system doesn't check for active loans.
  - **Impact:** Why this is a problem: Alumni cannot borrow, but if they have books issued during the transition, they might escape return enforcements.
  - **Fix:** Prevent changing year to 'Pass Out' if `borrow_records` has active loans for that student.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Stale password reset request.
  - **Persona:** Student
  - **Trace:** `api_forgot_password` creates a reset request. There is no expiration on these requests or the resulting default password state.
  - **Impact:** Why this is a problem: Old requests can be used later.
  - **Fix:** Add an expiry timestamp to password reset approvals.
  - **Severity:** Low

## F. Librarian Daily Operations
✅ **What works:**
- Approving and rejecting requests.
- Viewing and managing active loans.
- Broadcasting a notice.

❌ **What is missing or broken:**
- **Scenario:** Issuing a book directly at the counter (bypassing portal).
  - **Persona:** Librarian
  - **Trace:** When a librarian approves a portal request (`api_admin_approve_request`), it *immediately* creates a `borrow_record`. If the student comes to the counter 2 hours later, the librarian cannot use the "Issue Book" Desktop flow. This conflates "Reservation Approved" with "Physical Book Issued".
  - **Impact:** Why this is a problem: Severe workflow confusion.
  - **Fix:** Decouple Approval from Issuance. Approval should just mean "reserved". The physical issuance creates the `borrow_record`.
  - **Severity:** Critical

- **Scenario:** Uploading study materials with identical names.
  - **Persona:** Librarian
  - **Trace:** In `api_admin_study_materials`, uploaded files are saved using `secure_filename`. If two professors upload `notes.pdf`, the second will overwrite the first.
  - **Impact:** Why this is a problem: Data loss for shared filenames.
  - **Fix:** Append a UUID to the filename before saving in `api_admin_study_materials`.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Importing Excel transaction data creating duplicate records.
  - **Persona:** Librarian
  - **Trace:** The bulk import feature (`_import_data_worker` in `main.py`) might not correctly handle cases where the same transaction is imported twice.
  - **Impact:** Why this is a problem: Skews analytics and loan counts.
  - **Fix:** Implement logic in the import worker to deduplicate based on `(enrollment_no, book_id, borrow_date)`.
  - **Severity:** Medium

## G. Sync & Data Integrity
✅ **What works:**
- Dual backend architecture (SQLite + Postgres).
- Background syncing thread using `sync_now`.
- Tombstone tracking via `sync_deletions`.

❌ **What is missing or broken:**
- **Scenario:** Cloud pulling updates to local - Unidirectional overwrite risk.
  - **Persona:** System
  - **Trace:** In `sync_manager.py`, `_sync_table_bidirectional` handles conflicts by looking at `updated_at`. However, `borrow_records` and `requests` lack an `updated_at` trigger in many SQLite table creations. If a sync conflict occurs without reliable timestamps, data loss happens.
  - **Impact:** Why this is a problem: Potential loss of transaction data if the internet drops and both local and web modify the same record.
  - **Fix:** Ensure SQLite schema has `updated_at` triggers for all synced tables, or use Event Sourcing approach for `borrow_records`.
  - **Severity:** High

- **Scenario:** `available_copies` drifting.
  - **Persona:** System
  - **Trace:** `available_copies` is updated relatively (`available_copies = available_copies - 1`). If Desktop issues a book (-1) and Portal approves a request (-1), but they sync before either pushes to the other, the resulting state depends on sync order and can result in incorrect totals.
  - **Impact:** Why this is a problem: `available_copies` will drift and become inaccurate.
  - **Fix:** `available_copies` should be a computed property: `total_copies - (SELECT COUNT(*) FROM borrow_records WHERE book_id = ? AND status = 'borrowed')`.
  - **Severity:** Critical

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Offline desktop app `push_to_cloud` silent failures.
  - **Persona:** Librarian
  - **Trace:** Functions like `api_change_password` and `api_admin_approve_request` use `_push_to_cloud()` for fire-and-forget background updates. `_push_to_cloud()` in `student_portal.py` suppresses exceptions. If the Supabase connection fails, the portal update is lost until the desktop app's SyncManager runs.
  - **Impact:** Why this is a problem: Delayed consistency.
  - **Fix:** Store failed pushes locally in a robust queue rather than fire-and-forget.
  - **Severity:** Medium

## H. Deployment & Infrastructure
✅ **What works:**
- Fallback connection pooling to direct DB hosts.
- CSRF Double Submit cookie pattern.
- SQLite vs PostgreSQL query compatibility wrappers.

❌ **What is missing or broken:**
- **Scenario:** Unauthenticated access to admin endpoints.
  - **Persona:** Attacker
  - **Trace:** The desktop app communicates with the portal via endpoints like `/api/admin/all-requests`. These endpoints have NO authentication checks (`@app.route` lacks session checks or API key validation). Memory explicitly states: "do not restrict them strictly to localhost". Leaving them completely unauthenticated means anyone on the internet can query `/api/admin/all-requests` and see PII.
  - **Impact:** Why this is a problem: Massive data breach of PII (names, emails, phone numbers, loan history).
  - **Fix:** Implement an API Key authentication mechanism. The desktop app must send a `X-API-Key` header matching a shared secret stored in `.env`. Ensure this key is validated via a decorator on all `/api/admin/*` endpoints in `student_portal.py`.
  - **Severity:** Critical

- **Scenario:** Render ephemeral file system data loss.
  - **Persona:** System
  - **Trace:** `PROFILE_PHOTO_FOLDER` and `UPLOAD_FOLDER` (Study Materials) are stored in `os.path.join(BASE_DIR, 'uploads')`. Render free/standard web services have an ephemeral filesystem. Every time Render redeploys or restarts the server, the `uploads` directory is wiped clean.
  - **Impact:** Why this is a problem: All student profile photos and uploaded study materials will disappear automatically after a few days.
  - **Fix:** Use an external storage service (like Supabase Storage or AWS S3) for uploads instead of the local filesystem.
  - **Severity:** Critical

- **Scenario:** Environment variable missing at runtime.
  - **Persona:** System
  - **Trace:** The application relies on `FLASK_SECRET_KEY` and other vars. If missing, it falls back to a generated `.secret_key` file. However, Render is ephemeral, so a new `.secret_key` is generated every cold start.
  - **Impact:** Why this is a problem: This causes complete session loss on ephemeral filesystems across cold starts.
  - **Fix:** Implement strict validation on startup to ensure `FLASK_SECRET_KEY` is present.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Unauthenticated study material downloads.
  - **Persona:** Attacker
  - **Trace:** The `/api/study-materials/<id>/download` endpoint has no `@login_required` or session check. Anyone with the URL can download college materials.
  - **Impact:** Why this is a problem: Unauthorized access to intellectual property.
  - **Fix:** Add a check to ensure a valid session exists before serving the file.
  - **Severity:** Medium

- **Scenario:** Access log writing on cloud vs local.
  - **Persona:** System
  - **Trace:** `PostgresCursorWrapper` lacks an `executemany` method, causing batch access log writing to fail in Postgres.
  - **Impact:** Why this is a problem: Observability logs are lost in cloud deployments.
  - **Fix:** Implement `executemany` in `PostgresCursorWrapper`.
  - **Severity:** Medium

## I. Notification & Email Pipeline
✅ **What works:**
- Async background thread for email delivery (`send_email_bg`).
- HTML email templates with dynamic theme colors.
- In-app notification appearing correctly on dashboard.

❌ **What is missing or broken:**
- **Scenario:** Email delivery failing silently.
  - **Persona:** System
  - **Trace:** In `send_email_bg`, the entire SMTP process is wrapped in a `try...except Exception as e: print(...)`. If the email fails, the system logs it to `stdout` but the caller `api_submit_request` still returns a `200 OK` "Request submitted successfully" to the user.
  - **Impact:** Why this is a problem: System unreliability masked by silent failures.
  - **Fix:** Critical failures should ideally be logged to an `email_failures` table or trigger an in-app system alert so administrators can see the pipeline is broken via the Observability tab.
  - **Severity:** Medium

- **Scenario:** Orphaned notifications after account deletion.
  - **Persona:** System
  - **Trace:** When a student is deleted (`api_admin_approve_deletion`), `user_notifications` table is cleared. However, `email_history` is not cleared, potentially retaining PII.
  - **Impact:** Why this is a problem: GDPR/Privacy violations.
  - **Fix:** Ensure all related communication tables (like `email_history`) are purged when a student is deleted.
  - **Severity:** Low

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Unread count accuracy with virtual alerts.
  - **Persona:** Student
  - **Trace:** In `/api/notifications`, the `unread_count` aggregates `unread_db + unread_alerts`. Virtual alerts (like Overdue) are always counted as unread because they have no DB state. If a student clicks "Mark all as read", it only updates DB items.
  - **Impact:** Why this is a problem: Notification fatigue.
  - **Fix:** Track the read status of virtual alerts in local storage on the client side or add a mechanism to dismiss them temporarily.
  - **Severity:** Low

- **Scenario:** Notification for an event that has no template defined.
  - **Persona:** System
  - **Trace:** `generate_email_template` has specific branches for known events, but falls back to generic text for others.
  - **Impact:** Why this is a problem: Poor UX.
  - **Fix:** Implement robust fallback templates.
  - **Severity:** Low

## J. Edge Cases & Stress Scenarios
✅ **What works:**
- CSRF exemption lists for specific endpoints.
- Self-healing on fresh DB.
- Book title with single quotes breaking SQL queries works (parameterized queries).
- Catalogue with 0 books showing correct empty state.
- Fine rate changed mid-loan period is handled adequately (though inherently complex).

❌ **What is missing or broken:**
- **Scenario:** Concurrent approvals for the same single-copy book.
  - **Persona:** Librarian
  - **Trace:** If two librarians attempt to approve a `book_request` for the same book simultaneously via `/api/admin/requests/<req_id>/approve`, the system queries `available_copies`. If both read `1` before either writes, both will approve the request, both will create a `borrow_record`, and `available_copies` will go to `-1` or `0` (thanks to `MAX(0, available_copies - 1)`).
  - **Impact:** Why this is a problem: Negative inventory / phantom books issued.
  - **Fix:** In `api_admin_approve_request`, implement optimistic concurrency control using a `WHERE available_copies > 0` directly in the `UPDATE` statement, and only create the borrow record if the `UPDATE` affected 1 row.
  - **Severity:** High

- **Scenario:** Student submitting the same request type 20 times rapidly.
  - **Persona:** Attacker/Student
  - **Trace:** The rate limiter (`@rate_limit`) in `student_portal.py` is applied to `/api/login`, `/api/public/forgot-password`, and `/api/change_password`. It is **NOT** applied to `/api/request` (the endpoint where students submit reservations, renewals, etc.). A student can write a script to hammer `/api/request` 1,000 times a second.
  - **Impact:** Why this is a problem: Causes database bloat and librarian dashboard spam, race condition on insertion.
  - **Fix:** Add the `@rate_limit` decorator to `/api/submit_request` in `student_portal.py`.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Student with a name containing special characters breaking email templates.
  - **Persona:** Student
  - **Trace:** `generate_email_template` embeds the user name directly into the HTML string without escaping.
  - **Impact:** Why this is a problem: Potential HTML injection in emails, leading to broken rendering or security issues.
  - **Fix:** Use an HTML escaping function when inserting user data into email templates.
  - **Severity:** Medium

- **Scenario:** Librarian approving a request for a student whose account was just deleted.
  - **Persona:** Librarian
  - **Trace:** In `api_admin_approve_request`, the code doesn't verify the student still exists before creating a borrow record.
  - **Impact:** Why this is a problem: Orphaned borrow records and data inconsistency.
  - **Fix:** In `api_admin_approve_request`, verify the student exists in `students` before processing approval.
  - **Severity:** Medium
