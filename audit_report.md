# GPA-S-LMS Adversarial Testing Audit Report

## A. Student Borrowing Lifecycle
✅ **What works:**
- First login
- Catalogue browsing
- Requesting a book
- Requesting when at borrowing limit
- Requesting when overdue
- Approval received
- Going to collect
- Requesting cancellation before approval
- Physically collecting the book

❌ **What is missing or broken:**
- **Scenario:** Duplicate request
  - **Persona:** Student
  - **Trace:** The system checks for pending duplicate requests by parsing JSON in the database query, but does not use atomic transactions to prevent concurrent duplicate submissions for the same book before the first is committed.
  - **Impact:** Students can submit multiple requests for the same book simultaneously, bypassing restrictions.
  - **Fix:** Add an atomic database transaction or a unique constraint on the combination of student ID and book ID for active or pending requests.
  - **Severity:** High
- **Scenario:** NOT collecting within deadline
  - **Persona:** Student / Librarian
  - **Trace:** Approval immediately creates a borrow record, but there is no automated expiration mechanism to cancel the reservation and increment available copies if the student never physically collects the book.
  - **Impact:** Books become permanently locked in a borrowed state and lost from the available catalogue.
  - **Fix:** Delay borrow record creation until physical collection at the counter, or implement a scheduled job to void uncollected approved requests after the deadline.
  - **Severity:** High
- **Scenario:** Requesting cancellation after approval
  - **Persona:** Student
  - **Trace:** The cancel endpoint explicitly blocks cancellation if the status is no longer pending, preventing students from cancelling an approved but uncollected request.
  - **Impact:** Uncollected books remain locked as the student cannot cancel them.
  - **Fix:** Allow cancellation of approved but uncollected requests, ensuring the action increments the available copies.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## B. Renewal Lifecycle
✅ **What works:**
- Normal renewal request

❌ **What is missing or broken:**
- **Scenario:** Renewal when already overdue
  - **Persona:** Student
  - **Trace:** No explicit check exists to block renewal requests if the item is already past its due date, allowing students to avoid fines by renewing late.
  - **Impact:** Students can bypass fine policies by renewing overdue items.
  - **Fix:** Add a validation check during renewal request submission to reject the request if the current date exceeds the due date.
  - **Severity:** High
- **Scenario:** Renewal when another student is on the waitlist for the same book
  - **Persona:** Student / Librarian
  - **Trace:** The renewal approval logic extends the due date without checking the waitlist table for pending notifications.
  - **Impact:** Students can hoard high-demand books indefinitely, bypassing the waitlist queue.
  - **Fix:** Block renewal approval if the book has active entries in the waitlist table.
  - **Severity:** Medium
- **Scenario:** Renewal when at the maximum renewal count
  - **Persona:** Student
  - **Trace:** There is no tracking mechanism for how many times a specific borrow record has been renewed.
  - **Impact:** Infinite renewals are possible, violating the stated policy.
  - **Fix:** Add a renewal count column to the borrow records table, increment it upon approval, and block requests that exceed the limit.
  - **Severity:** High
- **Scenario:** Librarian approving a renewal after the book was already returned
  - **Persona:** Librarian
  - **Trace:** The query updates the record without verifying if the return date is still null, causing confusion if the status was already changed to returned.
  - **Impact:** Inconsistent state updates and confusing email notifications.
  - **Fix:** Ensure the update query for renewal strictly filters for records where the return date is null.
  - **Severity:** Low
- **Scenario:** Librarian approving a renewal for a book that is overdue with fines accrued
  - **Persona:** Librarian
  - **Trace:** The system recalculates due dates dynamically, meaning pushing the due date forward instantly erases any dynamically calculated late fines accrued up to that point.
  - **Impact:** Loss of legitimate fine data.
  - **Fix:** Calculate and permanently persist accrued fines to the record before extending the due date.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## C. Return & Fine Lifecycle
✅ **What works:**
- On-time return
- Late return with fine
- Fine appearing on dashboard

❌ **What is missing or broken:**
- **Scenario:** Fine payment acknowledgment
  - **Persona:** Librarian / Student
  - **Trace:** There is no endpoint or UI functionality to mark an accrued fine as paid.
  - **Impact:** Fines accumulate permanently on student accounts without a way to clear them.
  - **Fix:** Implement an endpoint and desktop UI button to reset the fine amount to zero for a given record.
  - **Severity:** Critical
- **Scenario:** Fine being cleared after payment
  - **Persona:** Librarian
  - **Trace:** Same as above; missing implementation.
  - **Impact:** Permanent dashboard clutter.
  - **Fix:** Create a paid status or clear action.
  - **Severity:** Critical
- **Scenario:** Student attempting to borrow while fine is unpaid
  - **Persona:** Student
  - **Trace:** The borrowing validation logic checks active loans but does not query for outstanding unpaid fines across past returned records.
  - **Impact:** Students can ignore fines and continue utilizing library services.
  - **Fix:** Add a validation step to block new borrowing requests if the sum of unpaid fines exceeds zero.
  - **Severity:** Medium
- **Scenario:** Lost book scenario
  - **Persona:** Librarian
  - **Trace:** The system only supports borrowed and returned statuses, with no mechanism to mark an item as lost.
  - **Impact:** Lost books accrue infinite fines and permanently inflate the total copies count.
  - **Fix:** Add a lost status that stops fine accrual, adds replacement cost to the fine, and decrements total copies.
  - **Severity:** High
- **Scenario:** Damaged book scenario
  - **Persona:** Librarian
  - **Trace:** Similar to the lost scenario, no status exists for damaged returns.
  - **Impact:** Damaged books cannot be systematically removed from circulation while penalizing the user.
  - **Fix:** Implement a damaged status flag during return processing to apply a penalty and prompt administrative review.
  - **Severity:** High
- **Scenario:** Fine waiver by librarian
  - **Persona:** Librarian
  - **Trace:** Fines are calculated deterministically or saved permanently upon return; no waiver logic exists.
  - **Impact:** Inflexibility in handling legitimate grievances.
  - **Fix:** Implement an administrative override to zero out a specific fine.
  - **Severity:** Low

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## D. Waitlist Lifecycle
✅ **What works:**
- Joining waitlist for unavailable book
- Leaving waitlist
- Being notified when book is returned
- Multiple students on waitlist for same book

❌ **What is missing or broken:**
- **Scenario:** Acting on notification within a window
  - **Persona:** Student
  - **Trace:** No tracking mechanism enforces a strict window for the student to act after being notified.
  - **Impact:** The book can be claimed at any arbitrary future time or grabbed by someone else.
  - **Fix:** Enforce a reservation hold period upon notification.
  - **Severity:** High
- **Scenario:** Notification expiring
  - **Persona:** Student
  - **Trace:** The waitlist sets a notified flag but does not track the timestamp of notification, meaning if the student never acts, the next person in line is never notified.
  - **Impact:** The waitlist queue permanently stalls.
  - **Fix:** Record a timestamp when notified and implement a periodic check to expire stale notifications and notify the next user.
  - **Severity:** High

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## E. Account & Identity Scenarios
✅ **What works:**
- Forgot password request
- Profile update request
- Self-registration approval and rejection
- Account deletion without active loans and fines

❌ **What is missing or broken:**
- **Scenario:** First login password change
  - **Persona:** Student
  - **Trace:** The application lacks a mechanism to forcibly redirect users with default credentials to a password change screen upon their first successful login.
  - **Impact:** Users may retain easily guessable default passwords indefinitely.
  - **Fix:** Implement an enforced password change redirect based on a first login flag.
  - **Severity:** High
- **Scenario:** Stale password reset request
  - **Persona:** Student
  - **Trace:** Pending password reset requests remain active in the system indefinitely until manually processed.
  - **Impact:** An old request could be maliciously approved much later.
  - **Fix:** Implement a time-to-live expiration for password reset requests.
  - **Severity:** Low
- **Scenario:** Password change with session active on another device
  - **Persona:** Student
  - **Trace:** Changing the password updates the hash but does not invalidate existing active sessions across other devices.
  - **Impact:** Compromised sessions remain valid even after the legitimate user changes their password.
  - **Fix:** Implement a session invalidation mechanism or token revocation list upon password change.
  - **Severity:** High
- **Scenario:** Year changing to Pass Out with active loans
  - **Persona:** Student / Librarian
  - **Trace:** Profile updates allow changing the student year to Pass Out without verifying if they currently hold unreturned books.
  - **Impact:** Students can graduate and bypass system locks while holding library inventory.
  - **Fix:** Block profile year updates to Pass Out if the active loans count is greater than zero.
  - **Severity:** High
- **Scenario:** Account deletion with active loans and fines
  - **Persona:** Librarian
  - **Trace:** Approving an account deletion blindly marks all active loans as returned and increments available copies, without verifying physical return.
  - **Impact:** Books are falsely listed as available, causing inventory loss and phantom availability.
  - **Fix:** Prevent account deletion approval if the student has active loans or unpaid fines.
  - **Severity:** Critical

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## F. Librarian Daily Operations
✅ **What works:**
- Approving and rejecting all request types
- Issuing a book directly at the counter (bypass portal)
- Viewing and managing active loans
- Processing a return at the counter
- Broadcasting a notice
- Managing book catalogue

❌ **What is missing or broken:**
- **Scenario:** Viewing overdue list
  - **Persona:** Librarian
  - **Trace:** Dynamic fine calculation uses the current fine rate for all overdue days, ignoring what the rate was when the item originally became due.
  - **Impact:** Overdue lists display historically inaccurate fine amounts if rates change.
  - **Fix:** Persist the applicable fine rate at the time of borrowing or calculate daily snapshots.
  - **Severity:** Low
- **Scenario:** Marking a fine as paid
  - **Persona:** Librarian
  - **Trace:** No endpoint exists.
  - **Impact:** Cannot clear balances.
  - **Fix:** Implement a clear fine feature.
  - **Severity:** Critical
- **Scenario:** Uploading study materials
  - **Persona:** Librarian
  - **Trace:** Uploads use original filenames, allowing collisions to silently overwrite existing files, and deletions do not remove the physical file.
  - **Impact:** File corruption and unmanaged disk space leakage on the host server.
  - **Fix:** Use UUIDs for storage filenames and implement physical unlinking upon deletion.
  - **Severity:** Medium
- **Scenario:** Importing Excel transaction data
  - **Persona:** Librarian
  - **Trace:** The import script does not enforce idempotency or check for existing transaction signatures.
  - **Impact:** Accidental double-imports create duplicate borrow records.
  - **Fix:** Implement a unique constraint or lookup before inserting imported rows.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## G. Sync & Data Integrity
✅ **What works:**
- Desktop app syncing to Supabase
- Sync failure recovery

❌ **What is missing or broken:**
- **Scenario:** Cloud pulling updates to local
  - **Persona:** System
  - **Trace:** The sync manager resolves conflicts using timestamps, but SQLite tables lack reliable trigger-based updated timestamps.
  - **Impact:** Simultaneous edits can cause silent data loss as older data overwrites newer data.
  - **Fix:** Add update triggers to all synced SQLite tables to ensure accurate timestamping.
  - **Severity:** High
- **Scenario:** available_copies count drifting between systems
  - **Persona:** System
  - **Trace:** Both systems update copies via relative decrements, but sync uses absolute overwrites based on row timestamps, breaking delta consistency.
  - **Impact:** The available copies count becomes fundamentally inaccurate after offline events.
  - **Fix:** Derive available copies dynamically during queries or recalculate entirely post-sync instead of syncing the integer directly.
  - **Severity:** Critical
- **Scenario:** Sync conflict when the same record is modified on both desktop and portal simultaneously
  - **Persona:** System
  - **Trace:** Timestamp-based resolution blindly overwrites, losing one side's state entirely.
  - **Impact:** Loss of critical transaction data.
  - **Fix:** Implement event sourcing or strict CRDTs for borrow records.
  - **Severity:** High
- **Scenario:** Transaction imported from Excel creating duplicate records
  - **Persona:** Librarian
  - **Trace:** Missing unique constraints allow bulk imports to duplicate history.
  - **Impact:** Corrupted statistics and user histories.
  - **Fix:** Validate existing accession numbers and dates before import.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## H. Deployment & Infrastructure
✅ **What works:**
- Rate limiting or lack thereof on sensitive endpoints (Partially, see J)
- SQLite vs PostgreSQL query compatibility

❌ **What is missing or broken:**
- **Scenario:** Render cold start and session behavior
  - **Persona:** System
  - **Trace:** Flask sessions utilize an auto-generated secret key that resets on every ephemeral filesystem restart. Profile photos are stored on the ephemeral disk.
  - **Impact:** All active student sessions are killed daily. Uploaded photos and materials are permanently lost upon restart.
  - **Fix:** Define a static FLASK_SECRET_KEY in the environment and migrate file storage to a persistent cloud bucket like S3.
  - **Severity:** Critical
- **Scenario:** access log writing on cloud vs local
  - **Persona:** System
  - **Trace:** The PostgreSQL connection wrapper lacks the executemany method used by the access log batch writer.
  - **Impact:** Background access logging silently fails and crashes the background thread on the cloud deployment.
  - **Fix:** Implement executemany in the custom Postgres wrapper to support batch insertions.
  - **Severity:** High
- **Scenario:** environment variable missing at runtime
  - **Persona:** System
  - **Trace:** The application boots without validating the presence of required environment variables.
  - **Impact:** Silent failures in email and database connections.
  - **Fix:** Implement a startup validation routine that asserts critical environment variables.
  - **Severity:** Medium
- **Scenario:** CSRF protection gaps
  - **Persona:** Attacker
  - **Trace:** The API request endpoint is excluded from CSRF checks.
  - **Impact:** Susceptible to cross-site request forgery attacks on critical functions.
  - **Fix:** Remove the exclusion and enforce the double-submit token.
  - **Severity:** High
- **Scenario:** unauthenticated access to admin endpoints
  - **Persona:** Attacker
  - **Trace:** Administrative endpoints used by the desktop app are exposed over the internet without authentication.
  - **Impact:** Complete exposure of all user PII and system controls to the public internet.
  - **Fix:** Secure all admin endpoints using an API key verification header.
  - **Severity:** Critical
- **Scenario:** unauthenticated study material downloads
  - **Persona:** Attacker
  - **Trace:** The download endpoint does not enforce session validation.
  - **Impact:** Unauthorized public access to proprietary academic materials.
  - **Fix:** Enforce session validation on the download route.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## I. Notification & Email Pipeline
✅ **What works:**
- Email delivery for all request types
- In-app notification appearing correctly on dashboard

❌ **What is missing or broken:**
- **Scenario:** unread count accuracy
  - **Persona:** Student
  - **Trace:** Virtual alerts like overdue notices increment the unread count but cannot be marked as read because they lack a database record.
  - **Impact:** The unread badge permanently displays a false positive count until the item is returned, causing notification fatigue.
  - **Fix:** Separate virtual alerts from the unread badge tally or allow dismissing virtual alerts via session state.
  - **Severity:** Low
- **Scenario:** notification for an event that has no template defined
  - **Persona:** System
  - **Trace:** Sending an email for an unknown request type falls back to a generic block but can cause key errors if expected formatting isn't met.
  - **Impact:** Failed notifications.
  - **Fix:** Implement a strict fallback template and validation for allowed request types.
  - **Severity:** Low
- **Scenario:** email failing silently
  - **Persona:** System
  - **Trace:** The background email thread catches and suppresses all SMTP exceptions, logging to stdout while returning success to the user.
  - **Impact:** Users believe their request succeeded and an email was sent, but it failed completely.
  - **Fix:** Log failures to a persistent observability table and optionally alert the user in the UI.
  - **Severity:** Medium
- **Scenario:** orphaned notifications after account deletion
  - **Persona:** System
  - **Trace:** Deleting a user does not cascade to the email history tables.
  - **Impact:** PII remains in the system post-deletion.
  - **Fix:** Clear associated email history records during the deletion routine.
  - **Severity:** Low

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.

## J. Edge Cases & Stress Scenarios
✅ **What works:**
- Book title with single quotes breaking SQL queries (safely parameterized)
- Catalogue with 0 books showing correct empty state

❌ **What is missing or broken:**
- **Scenario:** Student with a name containing special characters breaking email templates
  - **Persona:** Student
  - **Trace:** Email templates inject unescaped student names into HTML.
  - **Impact:** Potential HTML injection or broken rendering in email clients.
  - **Fix:** Apply proper HTML escaping to user input before injecting into email templates.
  - **Severity:** Low
- **Scenario:** concurrent approvals for the same single-copy book
  - **Persona:** Librarian
  - **Trace:** The system reads available copies and then updates it in separate operations without row-level locking.
  - **Impact:** Two concurrent approvals will both succeed, driving inventory negative and phantom-issuing a non-existent book.
  - **Fix:** Use an atomic update with a RETURNING clause and verify availability directly within the update statement.
  - **Severity:** High
- **Scenario:** librarian approving a request for a student whose account was just deleted
  - **Persona:** Librarian
  - **Trace:** Deleting a student does not automatically cascade-delete their pending requests.
  - **Impact:** The librarian can approve a request for a nonexistent user, causing foreign key violations or orphaned records.
  - **Fix:** Cascade deletions to the requests table or validate user existence at approval time.
  - **Severity:** Medium
- **Scenario:** student submitting the same request type 20 times rapidly
  - **Persona:** Attacker
  - **Trace:** The general request endpoint lacks rate limiting and uses non-atomic duplication checks.
  - **Impact:** Database bloat, spam, and bypassing duplicate checks through race conditions.
  - **Fix:** Apply the rate limiter middleware to the request submission endpoint.
  - **Severity:** High
- **Scenario:** fine rate changed mid-loan period
  - **Persona:** Student / Librarian
  - **Trace:** The system applies the current fine rate to the entirety of an overdue period.
  - **Impact:** Changing the system fine rate retroactively changes the fines for days already past.
  - **Fix:** Store historical fine rates or log daily fine snapshots instead of relying on a single deterministic calculation.
  - **Severity:** Low

⚠️ **Partially implemented / Hidden Edge Cases:**
- None identified.
