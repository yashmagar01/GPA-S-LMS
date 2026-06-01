# GPA-S-LMS Adversarial Testing Audit Report


## A. Student Borrowing Lifecycle
✅ **What works:**
- Browsing books via `api_books`.
- Submitting a book request.
- Checking for duplicate active loans and borrow limits before requesting.
- Enforcing that 'Pass Out' students cannot borrow or request books.
- Email triggers upon request approval.
- Deduction of `available_copies` when librarian approves request.
- Adding borrow records to both local SQLite and Cloud (Supabase).

❌ **What is missing or broken:**
- **Scenario:** Not collecting within deadline.
  - **Persona:** Student/Librarian.
  - **Trace:** While the approval email states "If not collected by the deadline, the reservation will be cancelled", there is NO automated mechanism (e.g., cron job or background thread check) in `student_portal.py` or `database.py` that expires an approved request, cancels the reservation, and increments `available_copies` back if the book isn't physically collected within 2 days. The book stays permanently locked in `borrowed` state because approval immediately creates a `borrow_record`.
  - **Impact:** Books are permanently "lost" from the catalogue if a student never picks them up.
  - **Fix:** Do not create a `borrow_record` or decrement `available_copies` immediately upon *approving* a reservation request. Instead, approval should just change the request status to `approved`. A separate "Issue Book" flow at the counter should fulfill the request and create the `borrow_record`. Alternatively, implement a periodic job to void uncollected `borrow_records` matching the 2-day criteria.
  - **Severity:** High

- **Scenario:** Request cancellation before approval.
  - **Persona:** Student
  - **Trace:** Student calls `/api/request/<req_id>/cancel`. It successfully changes status to `cancelled`. However, the book's availability was never decremented on request, so there's no resource leak. This works correctly.

- **Scenario:** Request cancellation AFTER approval.
  - **Persona:** Student
  - **Trace:** The `/api/request/<req_id>/cancel` endpoint specifically checks `if req['status'] != 'pending': return error`. Students cannot cancel an approved request via the portal.
  - **Impact:** If a student changes their mind after approval but before collection, they cannot notify the system, leaving the book locked as `borrowed`.
  - **Fix:** Allow cancellation of `approved` (but not yet collected) requests, and if cancelled, implement logic to reverse the `borrow_record` creation and increment `available_copies`.
  - **Severity:** Medium

- **Scenario:** Duplicate Request for same book.
  - **Persona:** Student
  - **Trace:** In `api_submit_request`, the check for pending requests uses `cur_dup.execute("SELECT details FROM requests ... AND request_type = 'book_request' AND status = 'pending'")`. It loops through and parses JSON. However, a student can submit a request, have it *approved*, and then submit *another* request for the same book before collecting the first one, because the check only looks for `status = 'pending'`. The active loan check (`SELECT COUNT(*) ... status = 'borrowed'`) will block it *only if* the first request was approved (because approval creates a borrow record).
  - **Impact:** Actually, because approval creates a borrow record immediately, the active loan check prevents this.

⚠️ **Partially implemented / Hidden Edge Cases:**
- **Scenario:** Requesting a book when out of stock.
  - **Persona:** Student
  - **Trace:** The portal API `/api/request` does NOT check `available_copies > 0` when inserting the request into the `requests` table. It allows submitting a request for a book with 0 copies.
  - **Impact:** Students can reserve out-of-stock books. The librarian will later click "Approve", which *will* fail with "Cannot approve: No available copies left", but the student experience is broken (they shouldn't be able to request it in the first place).
  - **Fix:** Add a check in `/api/request` to ensure `available_copies > 0` before allowing a `book_request`.
  - **Severity:** Medium


## B. Renewal Lifecycle
✅ **What works:**
- Normal renewal request via `/api/request`.
- Preventing duplicate pending renewals for the exact same accession number (Bug 9 fix).
- Approving a renewal extends the due date by 7 days.

❌ **What is missing or broken:**
- **Scenario:** Renewal when at the maximum renewal count.
  - **Persona:** Student
  - **Trace:** The system advertises "2 Renewals per book" in `/api/user-policies`. However, there is no code anywhere in `student_portal.py` (`api_submit_request`, `api_admin_approve_request`) or `database.py` that tracks how many times a specific loan has been renewed.
  - **Impact:** Students can renew infinitely.
  - **Fix:** Add a `renewal_count` column to `borrow_records`, increment it on approval, and block requests/approvals where `renewal_count >= 2`.
  - **Severity:** High

- **Scenario:** Renewal when another student is on the waitlist.
  - **Persona:** Student/Librarian
  - **Trace:** When a librarian approves a renewal in `api_admin_approve_request`, it blindly extends the due date. It does not check if the book has an active `book_waitlist`.
  - **Impact:** A student can hold onto a highly demanded book forever, completely bypassing the waitlist queue.
  - **Fix:** In `api_submit_request` (or the approval endpoint), check `SELECT COUNT(*) FROM book_waitlist WHERE book_id = ? AND notified = 0`. If > 0, block the renewal.
  - **Severity:** Medium

- **Scenario:** Librarian approving a renewal for a book that is overdue with fines accrued.
  - **Persona:** Librarian
  - **Trace:** `api_admin_approve_request` extends the due date from `max(current_due, datetime.now())`. It does not handle existing accrued fines. The fine calculation is fully dynamic based on `due_date`. If the due date is pushed to the future, the dynamic fine drops to 0.
  - **Impact:** Renewing an overdue book instantly erases the student's accrued fine for being late.
  - **Fix:** Before extending `due_date`, calculate the accrued fine and permanently save it into the `fine` column of the `borrow_records` row. The dashboard fine logic (`max(stored_fine, computed_fine)`) will then preserve it.
  - **Severity:** High

- **Scenario:** Librarian approving a renewal after the book was already returned.
  - **Persona:** Librarian
  - **Trace:** A student requests a renewal. Before the librarian approves it, the student physically returns the book. The librarian then clicks "Approve". The code uses `WHERE ... AND return_date IS NULL` so the row update affects 0 rows. The status changes to `approved` and an email is sent, but no due date is extended.
  - **Impact:** Confusion, but no data corruption.
  - **Severity:** Low


## C. Return & Fine Lifecycle
✅ **What works:**
- Dashboard dynamically calculates fines based on `due_date` vs `today`.
- Fine rate is pulled dynamically from synced database `system_settings` or `.env`.

❌ **What is missing or broken:**
- **Scenario:** Fine payment acknowledgment / Fine clearing.
  - **Persona:** Librarian/Student
  - **Trace:** The system calculates fines, but there is NO endpoint or desktop app method to mark a fine as "Paid". `borrow_records` has a `fine` column, but it's only populated when explicitly set (which never happens dynamically, it's just `0` default). When a book is returned late, `database.py`'s `return_book` calculates the fine and saves it. However, once saved, there is no way to reset it to 0 or record a payment transaction.
  - **Impact:** Students will have permanent lifetime fines accumulating on their dashboard.
  - **Fix:** Build an endpoint (e.g., `/api/admin/clear-fine`) and a corresponding desktop UI button to `UPDATE borrow_records SET fine = 0 WHERE id = ?`.
  - **Severity:** Critical

- **Scenario:** Student attempting to borrow while fine is unpaid.
  - **Persona:** Student
  - **Trace:** There is no check in `api_submit_request` (book request) or the desktop `borrow_book` function to prevent students with outstanding unpaid fines from borrowing more books.
  - **Impact:** Students can ignore fines entirely and continue using the library.
  - **Fix:** Add a check querying `SUM(fine)` across returned but unpaid records, and block borrowing if `> 0` (or some threshold).
  - **Severity:** Medium

- **Scenario:** Lost or damaged book.
  - **Persona:** Librarian
  - **Trace:** The system only supports `status = 'borrowed'` and `status = 'returned'`. There is no mechanism to mark a book as lost, charge the replacement cost, and permanently decrement `total_copies`.
  - **Impact:** Lost books remain perpetually "borrowed" (accruing infinite fines) or require manual raw SQL intervention.
  - **Fix:** Add a "Mark Lost" function that updates status to `lost`, adds the book's `price` to the `fine`, and updates `books.total_copies`.
  - **Severity:** High


## D. Waitlist Lifecycle
✅ **What works:**
- Joining the waitlist (`/api/books/<id>/notify`).
- Leaving the waitlist.
- Notification function `_notify_waitlist` triggers when a book is returned.

❌ **What is missing or broken:**
- **Scenario:** Notification expiring.
  - **Persona:** Student
  - **Trace:** `_notify_waitlist` sends an email and creates a portal notification. It sets `notified = 1`. However, there is no timeout or expiration mechanism. If the notified student doesn't act, the book just sits there as "available". The second person on the waitlist is never notified.
  - **Impact:** The waitlist queue stalls permanently after the first person is notified.
  - **Fix:** Implement a timestamp for when the notification was sent. If the book isn't claimed within X hours, a scheduled job (or a check on the next sync/request) should remove the first person and notify the next.
  - **Severity:** High

- **Scenario:** Waitlist vs Direct Counter Issue.
  - **Persona:** Librarian
  - **Trace:** A book is returned, notifying Student A. Ten minutes later, Student B walks to the counter and the librarian issues the book directly via `main.py`. The system allows this.
  - **Impact:** Student A was told the book is available, but when they log in to request it, it's gone.
  - **Fix:** When a waitlist exists, block issuing the book to anyone other than the notified student for a grace period, or clear the waitlist notification.
  - **Severity:** Medium


## E. Account & Identity Scenarios
✅ **What works:**
- Default password logic (enrollment number).
- First login forced password change (`is_first_login` tracking).
- Registration request and approval flow.

❌ **What is missing or broken:**
- **Scenario:** Year changing to Pass Out with active loans.
  - **Persona:** Librarian/Student
  - **Trace:** When a librarian updates a student's year to "Pass Out" (either via bulk promotion or individual edit), the system checks if they have active loans during the bulk promotion logic. But if done via individual profile update, or if the student requests a profile update to change their year to "Pass Out", it blindly applies it. The portal blocks requests from "Pass Out", but doesn't handle existing loans.
  - **Impact:** Students marked as Pass Out can walk away with library books.
  - **Fix:** In `database.py` `update_student` and `student_portal.py` profile update approval, block changing year to "Pass Out" if `SELECT COUNT(*) FROM borrow_records WHERE enrollment_no = ? AND status = 'borrowed'` > 0.
  - **Severity:** High

- **Scenario:** Account deletion with active loans.
  - **Persona:** Student/Librarian
  - **Trace:** In `api_admin_approve_deletion`, when a deletion request is approved, it runs `UPDATE borrow_records SET status = 'returned' ... WHERE enrollment_no = ? AND status = 'borrowed'` and increments `available_copies`. Then it deletes the student.
  - **Impact:** If a student requests account deletion and it's approved, their active loans are automatically marked as "returned" and the books become "available" in the catalogue again, even though the physical books were never returned!
  - **Fix:** Prevent approval of account deletion if the student has active loans. The librarian must ensure books are physically returned first.
  - **Severity:** Critical

- **Scenario:** Password reset request staleness.
  - **Persona:** Student
  - **Trace:** A password reset request stays `pending` indefinitely until the librarian acts. If the student remembers their password and logs in, the request remains. A malicious actor who later gains physical access to the librarian desk could approve it.
  - **Severity:** Low


## F. Librarian Daily Operations
✅ **What works:**
- Issuing and returning books at the counter via desktop UI (`main.py`).
- Approving/Rejecting requests via API.
- Dashboard analytics.

❌ **What is missing or broken:**
- **Scenario:** Uploading study materials with duplicate filenames.
  - **Persona:** Librarian
  - **Trace:** In `api_admin_study_materials` (POST), `unique_filename` is generated as `{timestamp}_{original_filename}`. If multiple files with the same name are uploaded in the same second, they overwrite each other. More importantly, there's no cleanup of old files when a material is deleted (`DELETE` method has file removal commented out).
  - **Impact:** Disk space leak on the Render server over time.
  - **Fix:** Uncomment the physical file deletion in `api_admin_manage_material`, and use `uuid` for filename generation to guarantee uniqueness.
  - **Severity:** Medium

- **Scenario:** Marking a fine as paid.
  - **Persona:** Librarian
  - **Trace:** As noted in Domain C, there is no UI or API endpoint to clear a fine.
  - **Severity:** Critical

- **Scenario:** Viewing overdue list when `fine_per_day` changes.
  - **Persona:** Librarian
  - **Trace:** The desktop app calculates total fine dynamically, but the `returned` logic saves the fine permanently into the row. If `fine_per_day` is changed midway through a semester, currently overdue books will calculate using the *new* rate for all days, not the rate at the time they were due.
  - **Impact:** Inconsistent fine application.
  - **Severity:** Low


## G. Sync & Data Integrity
✅ **What works:**
- Dual backend architecture (SQLite + Postgres).
- Background syncing thread using `sync_now`.
- Tombstone tracking via `sync_deletions`.

❌ **What is missing or broken:**
- **Scenario:** Cloud pulling updates to local - Unidirectional overwrite risk.
  - **Persona:** System
  - **Trace:** In `sync_manager.py`, `_sync_table_bidirectional` handles conflicts by looking at `updated_at`. If `remote_row['updated_at'] > local_row['updated_at']`, remote wins. However, `borrow_records` and `requests` lack an `updated_at` trigger in many SQLite table creations (or rely on app-level updates that might be missed). If a sync conflict occurs without reliable timestamps, data loss happens.
  - **Impact:** Potential loss of transaction data if the internet drops and both local and web modify the same record.
  - **Fix:** Ensure SQLite schema has `updated_at` triggers for all synced tables, or use a strict CRDT/Event Sourcing approach for `borrow_records`.
  - **Severity:** High

- **Scenario:** `available_copies` drifting.
  - **Persona:** System
  - **Trace:** Both local desktop and cloud portal modify `available_copies` using relative statements (`UPDATE books SET available_copies = MAX(0, available_copies - 1)`). During bidirectional sync, if `books` is synced, it overwrites the count with the absolute value from whichever side had the latest `updated_at`. If an issue happens offline, and a return happens online, the sync will overwrite the count rather than replaying the delta.
  - **Impact:** `available_copies` will drift and become inaccurate.
  - **Fix:** The sync manager should ideally not sync `available_copies` directly, but rather derive it dynamically from `total_copies - COUNT(active borrow_records)`. Alternatively, trigger a recalculation after every sync cycle.
  - **Severity:** Critical

- **Scenario:** Offline desktop app `push_to_cloud` silent failures.
  - **Persona:** Librarian
  - **Trace:** Functions like `api_change_password` and `api_admin_approve_request` use `_push_to_cloud()` for fire-and-forget background updates. If the Render server processes a request, it modifies Postgres directly. If the desktop app processes it locally, it relies on SyncManager. However, `_push_to_cloud()` in `student_portal.py` suppresses exceptions. If the Supabase connection fails, the portal update is lost until the desktop app's SyncManager runs (which it might not, if the librarian's PC is off).
  - **Impact:** Delayed consistency. A student changes their password on the web, but if the web instance uses SQLite (not Render), it doesn't push reliably. (Note: Render uses Postgres directly, so this specific edge case is mitigated, but the architecture is fragile).
  - **Severity:** Medium


## H. Deployment & Infrastructure
✅ **What works:**
- Fallback connection pooling to direct DB hosts.
- CSRF Double Submit cookie pattern.
- Rate limiting middleware.

❌ **What is missing or broken:**
- **Scenario:** Unauthenticated access to admin endpoints.
  - **Persona:** Attacker
  - **Trace:** The desktop app communicates with the portal via endpoints like `/api/admin/all-requests`. These endpoints have NO authentication checks (`@app.route` lacks session checks or API key validation). Memory explicitly states: "do not restrict them strictly to localhost... as this causes major regressions by blocking legitimate remote administrators." However, leaving them completely unauthenticated means anyone on the internet can query `/api/admin/all-requests` and see PII.
  - **Impact:** Massive data breach of PII (names, emails, phone numbers, loan history).
  - **Fix:** Implement an API Key authentication mechanism. The desktop app must send a `X-API-Key` header matching a shared secret stored in `.env` (e.g., `ADMIN_API_KEY`).
  - **Severity:** Critical

- **Scenario:** Unauthenticated study material downloads.
  - **Persona:** Attacker
  - **Trace:** The `/api/study-materials/<id>/download` endpoint has no `@login_required` or session check. Anyone with the URL can download college materials.
  - **Severity:** Medium

- **Scenario:** Render ephemeral file system data loss.
  - **Persona:** System
  - **Trace:** `PROFILE_PHOTO_FOLDER` and `UPLOAD_FOLDER` (Study Materials) are stored in `os.path.join(BASE_DIR, 'uploads')`. Render free/standard web services have an ephemeral filesystem. Every time Render redeploys or restarts the server (which happens daily on free tiers), the `uploads` directory is wiped clean.
  - **Impact:** All student profile photos and uploaded study materials will disappear automatically after a few days.
  - **Fix:** Integrate cloud storage (e.g., AWS S3, Supabase Storage) for profile photos and study materials.
  - **Severity:** Critical


## I. Notification & Email Pipeline
✅ **What works:**
- Async background thread for email delivery (`send_email_bg`).
- HTML email templates with dynamic theme colors.

❌ **What is missing or broken:**
- **Scenario:** Email delivery failing silently.
  - **Persona:** System
  - **Trace:** In `send_email_bg`, the entire SMTP process is wrapped in a `try...except Exception as e: print(...)`. If the email fails (e.g., bad credentials, network issue, rate limit from Gmail), the system logs it to `stdout` but the caller `api_submit_request` still returns a `200 OK` "Request submitted successfully" to the user. The user has no idea the email failed.
  - **Impact:** System unreliability masked by silent failures.
  - **Fix:** While backgrounding is good for latency, critical failures (like invalid SMTP config) should ideally be logged to the `access_logs` or an `email_failures` table so administrators can see the pipeline is broken via the Observability tab.
  - **Severity:** Medium

- **Scenario:** Unread count accuracy with virtual alerts.
  - **Persona:** Student
  - **Trace:** In `/api/notifications`, the `unread_count` aggregates `unread_db + unread_alerts`. However, virtual alerts (like Overdue) are always counted as unread because they have no DB state. If a student clicks "Mark all as read", it only updates DB items. The overdue alert stays, meaning the badge count never clears until the book is returned. This is technically by design, but creates notification fatigue.
  - **Severity:** Low

- **Scenario:** Orphaned notifications after account deletion.
  - **Persona:** System
  - **Trace:** When a student is deleted (`api_admin_approve_deletion`), the `user_notifications` table is cleared. This works correctly. However, `email_history` (mentioned in SyncManager wipe) is not cleared, potentially retaining PII.
  - **Severity:** Low


## J. Edge Cases & Stress Scenarios
✅ **What works:**
- CSRF exemption lists for specific endpoints.
- Self-healing on fresh DB where `students` table might not exist yet (`sqlite3.OperationalError` catch in `api_login`).

❌ **What is missing or broken:**
- **Scenario:** Concurrent approvals for the same single-copy book.
  - **Persona:** Librarian
  - **Trace:** If two librarians (or a librarian and a malicious script) attempt to approve a `book_request` for the same book simultaneously via `/api/admin/requests/<req_id>/approve`, the system queries `available_copies`. If both read `1` before either writes, both will approve the request, both will create a `borrow_record`, and `available_copies` will go to `0` (thanks to `MAX(0, available_copies - 1)`).
  - **Impact:** Negative inventory / phantom books issued.
  - **Fix:** Implement row-level locking or optimistic concurrency control using a `RETURNING` clause or `WHERE available_copies > 0` directly in the `UPDATE` statement, and only create the borrow record if the `UPDATE` affected 1 row.
  - **Severity:** High

- **Scenario:** Catalogue with 0 books showing correct empty state.
  - **Persona:** Student
  - **Trace:** The frontend handles this adequately, but if the database has absolutely 0 books, the `api_books` endpoint returns an empty array. This works correctly.

- **Scenario:** Student submitting the same request type 20 times rapidly.
  - **Persona:** Attacker/Student
  - **Trace:** The rate limiter (`@rate_limit`) in `student_portal.py` is applied to `/api/login`, `/api/public/forgot-password`, and `/api/change_password`. It is **NOT** applied to `/api/request` (the endpoint where students submit reservations, renewals, etc.).
  - **Impact:** A student can write a script to hammer `/api/request` 1,000 times a second. Since it checks for existing pending requests via a slow JSON-parsing `SELECT` query, it creates a race condition where multiple duplicate requests can be inserted before the first one is committed, causing database bloat and librarian dashboard spam.
  - **Fix:** Add the `@rate_limit` decorator to `/api/request` and use an atomic DB transaction for the duplicate check.
  - **Severity:** High

- **Scenario:** Book title with single quotes breaking SQL queries.
  - **Persona:** Student/Librarian
  - **Trace:** The codebase uses parameterized queries (e.g., `execute("SELECT ... WHERE title = ?", (title,))`) consistently throughout `database.py` and `student_portal.py`. This protects against SQL injection and handles single quotes safely. This works correctly.
