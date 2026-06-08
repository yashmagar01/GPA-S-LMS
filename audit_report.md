# GPA-S-LMS Adversarial Testing Audit Report

## A. Student Borrowing Lifecycle

✅ **What works:**
- Browsing books via the API.
- Submitting a book request.
- Checking for duplicate active loans and borrow limits before requesting.
- Enforcing that 'Pass Out' students cannot borrow or request books.
- Email triggers upon request approval.
- Deduction of available copies when librarian approves request.
- Adding borrow records to both local and Cloud environments.
- Request cancellation before approval.

❌ **What is missing or broken:**

- **Scenario:** Not collecting within deadline.
  - **Persona:** Student / Librarian
  - **Trace:** While the approval email states a deadline, there is no automated mechanism that expires an approved request, cancels the reservation, and increments the available copies back if the book isn't physically collected. Approval immediately creates a borrow record.
  - **Impact:** Books are permanently marked as unavailable in the catalogue if a student never picks them up.
  - **Fix:** Do not create a borrow record or decrement available copies immediately upon approving a reservation request. Instead, approval should just change the request status to approved. A separate flow at the counter should fulfill the request and create the borrow record. Alternatively, implement a periodic background job to void uncollected borrow records matching the deadline criteria.
  - **Severity:** High

- **Scenario:** Request cancellation AFTER approval.
  - **Persona:** Student
  - **Trace:** The cancellation endpoint specifically checks if the request status is not pending and returns an error. Students cannot cancel an approved request via the portal.
  - **Impact:** If a student changes their mind after approval but before collection, they cannot notify the system, leaving the book locked as borrowed.
  - **Fix:** Allow cancellation of approved (but not yet collected) requests, and if cancelled, implement logic to reverse the borrow record creation and increment available copies.
  - **Severity:** Medium

- **Scenario:** Duplicate Request for same book.
  - **Persona:** Student
  - **Trace:** In the request submission logic, the check for pending requests only looks for pending status. A student can submit a request, have it approved, and then submit another request for the same book before collecting the first one.
  - **Impact:** While the active loan check might prevent some abuse, multiple requests can bypass the initial pending check.
  - **Fix:** Broaden the duplicate request check to include approved but uncollected requests.
  - **Severity:** Medium

⚠️ **Partially implemented / Hidden Edge Cases:**

- **Scenario:** Requesting a book when out of stock.
  - **Persona:** Student
  - **Trace:** The request submission API does not check if available copies are greater than zero when inserting the request. It allows submitting a request for a book with zero copies.
  - **Impact:** Students can reserve out-of-stock books. The librarian will later click "Approve", which will fail with a "No available copies left" error, but the student experience is broken as they shouldn't be able to request it in the first place.
  - **Fix:** Add a check in the request API to ensure available copies are greater than zero before allowing a book request.
  - **Severity:** Medium

## B. Renewal Lifecycle

✅ **What works:**
- Normal renewal request via the API.
- Preventing duplicate pending renewals for the exact same book copy.
- Approving a renewal extends the due date by the standard period.

❌ **What is missing or broken:**

- **Scenario:** Renewal when at the maximum renewal count.
  - **Persona:** Student
  - **Trace:** The system advertises a specific number of renewals per book. However, there is no code anywhere in the backend logic that tracks how many times a specific loan has been renewed.
  - **Impact:** Students can renew infinitely.
  - **Fix:** Add a column to track renewal counts in the database, increment it on approval, and block requests or approvals where the count exceeds the maximum limit.
  - **Severity:** High

- **Scenario:** Renewal when another student is on the waitlist.
  - **Persona:** Student / Librarian
  - **Trace:** When a librarian approves a renewal, it blindly extends the due date. It does not check if the book has an active waitlist queue.
  - **Impact:** A student can hold onto a highly demanded book forever, completely bypassing the waitlist queue.
  - **Fix:** In the renewal submission or approval logic, check the waitlist table for unnotified entries for the given book. If entries exist, block the renewal.
  - **Severity:** Medium

- **Scenario:** Librarian approving a renewal for a book that is overdue with fines accrued.
  - **Persona:** Librarian
  - **Trace:** The approval logic extends the due date from the current date or original due date. It does not handle existing accrued fines. The fine calculation is fully dynamic based on the due date. If the due date is pushed to the future, the dynamic fine drops to zero.
  - **Impact:** Renewing an overdue book instantly erases the student's accrued fine for being late.
  - **Fix:** Before extending the due date, calculate the accrued fine and permanently save it into the fine tracking column of the database. The dashboard fine logic will then preserve it using the maximum of the stored fine and computed fine.
  - **Severity:** High

- **Scenario:** Librarian approving a renewal after the book was already returned.
  - **Persona:** Librarian
  - **Trace:** A student requests a renewal. Before the librarian approves it, the student physically returns the book. The librarian then attempts to approve. The logic looks for an unreturned record, so the row update affects zero rows. The status changes to approved and an email is sent, but no due date is actually extended.
  - **Impact:** Causes confusion for both the student and librarian, though no data corruption occurs.
  - **Fix:** Check if the book has already been returned before allowing the renewal approval to proceed.
  - **Severity:** Low

## C. Return & Fine Lifecycle

✅ **What works:**
- Dashboard dynamically calculates fines based on the due date versus the current date.
- Fine rate is pulled dynamically from the synchronized database settings or environment variables.

❌ **What is missing or broken:**

- **Scenario:** Fine payment acknowledgment / Fine clearing.
  - **Persona:** Librarian / Student
  - **Trace:** The system calculates fines, but there is no endpoint or desktop app method to mark a fine as "Paid". The database has a fine column, but it is only populated when a book is returned late. Once saved, there is no way to reset it to zero or record a payment transaction.
  - **Impact:** Students will have permanent lifetime fines accumulating on their dashboard.
  - **Fix:** Build an endpoint and a corresponding desktop UI button to update the fine value to zero for a given record.
  - **Severity:** Critical

- **Scenario:** Student attempting to borrow while fine is unpaid.
  - **Persona:** Student
  - **Trace:** There is no check in the request submission or the desktop issuing logic to prevent students with outstanding unpaid fines from borrowing more books.
  - **Impact:** Students can ignore fines entirely and continue using the library.
  - **Fix:** Add a check querying the total fine across returned but unpaid records, and block borrowing if the sum is greater than zero or a defined threshold.
  - **Severity:** Medium

- **Scenario:** Lost or damaged book.
  - **Persona:** Librarian
  - **Trace:** The system only supports borrowed and returned statuses. There is no mechanism to mark a book as lost, charge the replacement cost, and permanently decrement the total copies count.
  - **Impact:** Lost books remain perpetually borrowed, accruing infinite fines, or require manual database intervention by an administrator.
  - **Fix:** Add a feature to mark a book as lost. This should update the status to lost, add the book's price to the fine, and update the total copies count in the database.
  - **Severity:** High

## D. Waitlist Lifecycle

✅ **What works:**
- Joining the waitlist for an unavailable book.
- Leaving the waitlist.
- Background logic triggers waitlist notification when a book is returned.

❌ **What is missing or broken:**

- **Scenario:** Notification expiring.
  - **Persona:** Student
  - **Trace:** The notification logic sends an email and creates a portal alert, marking the student as notified. However, there is no timeout or expiration mechanism. If the notified student does not act, the book simply sits as "available". The next person on the waitlist is never notified.
  - **Impact:** The waitlist queue stalls permanently after the first person is notified.
  - **Fix:** Implement a timestamp for when the notification was sent. If the book is not claimed within a specific time window, a scheduled background job or a check during the next system interaction should remove the first person and notify the next.
  - **Severity:** High

- **Scenario:** Waitlist vs Direct Counter Issue.
  - **Persona:** Librarian
  - **Trace:** A book is returned, triggering a notification to a waitlisted student. Shortly after, a different student walks to the counter and the librarian issues the book directly via the desktop application. The system allows this operation without any warnings.
  - **Impact:** The originally notified student was told the book is available, but when they log in or arrive to request it, it is already gone.
  - **Fix:** When a waitlist exists, block issuing the book to anyone other than the notified student for a defined grace period, or explicitly clear the waitlist notification during the counter transaction.
  - **Severity:** Medium

## E. Account & Identity Scenarios

✅ **What works:**
- Default password logic using the enrollment number.
- First login forced password change behavior.
- Registration request and approval flow.

❌ **What is missing or broken:**

- **Scenario:** Year changing to Pass Out with active loans.
  - **Persona:** Librarian / Student
  - **Trace:** When a librarian updates a student's year to "Pass Out" during a bulk promotion, the system checks for active loans. However, if this is done via an individual profile update, or if the student requests a profile update to change their year, the system blindly applies it. The portal then blocks future requests from the student, but does not handle existing loans.
  - **Impact:** Students marked as Pass Out can leave the institution with library books still in their possession.
  - **Fix:** In both the librarian update logic and the student profile update approval flow, block changing the academic year to "Pass Out" if the student currently has active borrowed records.
  - **Severity:** High

- **Scenario:** Account deletion with active loans.
  - **Persona:** Student / Librarian
  - **Trace:** During the approval of a deletion request, the system automatically marks any active loans as returned and increments the available copies, before fully deleting the student record.
  - **Impact:** If an account deletion is approved, the physical books are never verified as returned, but the system treats them as available, leading to false inventory counts and lost assets.
  - **Fix:** Prevent the approval of account deletion if the student has active loans. The interface should enforce that books are physically returned before the account can be removed.
  - **Severity:** Critical

- **Scenario:** Password reset request staleness.
  - **Persona:** Student
  - **Trace:** A password reset request stays pending indefinitely until the librarian takes action. If the student remembers their password and logs in, the request remains active. A malicious actor who later gains physical access to the librarian desk could approve it.
  - **Impact:** A potential, though unlikely, avenue for unauthorized account access if old requests are blindly approved.
  - **Fix:** Introduce an expiration time for password reset requests or automatically invalidate them upon a successful student login.
  - **Severity:** Low

## F. Librarian Daily Operations

✅ **What works:**
- Issuing and returning books at the counter via the desktop interface.
- Approving and rejecting requests via the API.
- Dashboard analytics display.

❌ **What is missing or broken:**

- **Scenario:** Uploading study materials with duplicate filenames.
  - **Persona:** Librarian
  - **Trace:** The filename logic uses a timestamp concatenated with the original filename. If multiple files with the same name are uploaded in the exact same second, they overwrite each other. Furthermore, there is no cleanup of old physical files when a material record is deleted from the system.
  - **Impact:** Leads to data loss for the overwritten files and a progressive disk space leak on the deployment server over time.
  - **Fix:** Implement a universally unique identifier generation for filenames to guarantee uniqueness, and ensure physical file deletion is executed when a material record is removed.
  - **Severity:** Medium

- **Scenario:** Marking a fine as paid.
  - **Persona:** Librarian
  - **Trace:** As detailed in the Return & Fine Lifecycle domain, there is no method provided to clear an accumulated fine.
  - **Impact:** Unresolved fines permanently impact a student's account standing.
  - **Fix:** Create a dedicated interface and backend logic to mark a specific fine value as zero or paid.
  - **Severity:** Critical

- **Scenario:** Viewing overdue list when the fine rate changes.
  - **Persona:** Librarian
  - **Trace:** The desktop app calculates total fines dynamically for overdue books, but the return logic saves the final fine permanently into the database row. If the daily fine rate is altered midway through a semester, currently overdue books will calculate using the new rate for all overdue days, rather than the historical rate.
  - **Impact:** Leads to inconsistent fine application and student complaints regarding retroactive fine increases.
  - **Fix:** Implement logic to lock in fine rates upon the due date, or support rate versioning over time.
  - **Severity:** Low

## G. Sync & Data Integrity

✅ **What works:**
- Dual backend architecture utilizing both local and cloud databases.
- Background syncing thread operations.
- Tombstone tracking for deleted records.

❌ **What is missing or broken:**

- **Scenario:** Cloud pulling updates to local - Unidirectional overwrite risk.
  - **Persona:** System
  - **Trace:** The synchronization logic resolves conflicts by comparing update timestamps. If the remote row has a newer timestamp, it overwrites the local row. However, several tables lack an automatic update timestamp trigger during schema creation, relying instead on application-level updates that might be missed.
  - **Impact:** Potential loss of transaction data if the internet drops and both local and web applications modify the same record, leading to an untracked conflict.
  - **Fix:** Ensure the local database schema includes automatic update timestamp triggers for all synchronized tables, or transition to an event sourcing approach for critical records.
  - **Severity:** High

- **Scenario:** Available copies count drifting.
  - **Persona:** System
  - **Trace:** Both the local desktop and cloud portal modify available copies using relative decrement or increment operations. During synchronization, the system overwrites the count with the absolute value from whichever side has the latest timestamp. If an action occurs offline, and another action occurs online simultaneously, the synchronization will overwrite the count rather than mathematically replaying the delta.
  - **Impact:** The available copies count will drift over time, becoming highly inaccurate compared to actual physical inventory.
  - **Fix:** The synchronization process should not directly sync the available copies value. Instead, it should derive the value dynamically by subtracting the count of active borrow records from the total copies count, or explicitly trigger a full recalculation after every sync cycle.
  - **Severity:** Critical

- **Scenario:** Offline desktop app cloud push silent failures.
  - **Persona:** Librarian
  - **Trace:** Certain update operations execute fire-and-forget background pushes to the cloud. If the application processes these updates locally and relies on the sync manager, exceptions during the cloud push are intentionally suppressed. If the cloud connection fails, the update is not pushed immediately and must wait for the next background sync cycle.
  - **Impact:** Delayed consistency across the system. For instance, a student might change their password on the web interface, but if the primary instance is acting locally without a solid connection, the sync fails silently.
  - **Fix:** Remove exception suppression for critical background pushes, and implement an offline queue that guarantees delivery once connectivity is restored.
  - **Severity:** Medium

## H. Deployment & Infrastructure

✅ **What works:**
- Fallback connection pooling logic.
- CSRF Double Submit cookie pattern implementation.
- Rate limiting middleware on standard endpoints.

❌ **What is missing or broken:**

- **Scenario:** Unauthenticated access to administrative endpoints.
  - **Persona:** Attacker
  - **Trace:** The desktop application communicates with the web portal via administrative endpoints. Currently, these endpoints completely lack authentication checks, session validation, or API key verification. Memory instructions indicate they cannot be restricted to local host to support remote administration.
  - **Impact:** Massive potential data breach, exposing personally identifiable information such as names, emails, phone numbers, and loan histories to the public internet.
  - **Fix:** Implement a robust API key authentication mechanism. The desktop application must include a specific, secure header matching a shared secret stored in the server environment variables for all administrative requests.
  - **Severity:** Critical

- **Scenario:** Unauthenticated study material downloads.
  - **Persona:** Attacker
  - **Trace:** The endpoint responsible for serving study material file downloads lacks any session requirement or authentication check. Anyone possessing the URL can freely download the materials.
  - **Impact:** Unauthorized access to proprietary or restricted college materials.
  - **Fix:** Add a mandatory session check to the study material download endpoint to ensure only authenticated students can access the files.
  - **Severity:** Medium

- **Scenario:** Ephemeral file system data loss.
  - **Persona:** System
  - **Trace:** User uploads such as profile photos and study materials are stored in a local directory relative to the application base. The deployment environment utilizes an ephemeral filesystem, meaning every restart or redeployment completely wipes this directory.
  - **Impact:** All student profile photos and uploaded study materials will disappear automatically after routine server maintenance or scaling operations.
  - **Fix:** Integrate a persistent cloud storage provider to handle all uploaded assets, ensuring data survives application restarts.
  - **Severity:** Critical

## I. Notification & Email Pipeline

✅ **What works:**
- Asynchronous background thread for email delivery.
- HTML email templates supporting dynamic visual themes.

❌ **What is missing or broken:**

- **Scenario:** Email delivery failing silently.
  - **Persona:** System
  - **Trace:** In the background email delivery logic, the entire SMTP process is wrapped in a broad exception handler that merely prints errors to the standard output. If an email fails due to bad credentials, network issues, or provider rate limits, the caller still receives a success response, leaving the user completely unaware of the failure.
  - **Impact:** System unreliability masked by silent failures, leading to missed critical communications.
  - **Fix:** While background processing is beneficial, critical failures such as invalid SMTP configurations should be explicitly logged to the database or a dedicated error tracking table, allowing administrators to monitor pipeline health via the observability dashboard.
  - **Severity:** Medium

- **Scenario:** Unread count accuracy with virtual alerts.
  - **Persona:** Student
  - **Trace:** The notification system aggregates database-backed notifications with virtual alerts, such as overdue warnings. However, virtual alerts lack a database state. When a student marks all notifications as read, only the database items are updated. The overdue alert persists, meaning the unread badge count never fully clears until the underlying issue is resolved.
  - **Impact:** Creates notification fatigue, as the student cannot clear the persistent badge indicator.
  - **Fix:** Modify the notification aggregation logic to differentiate between informational read states and persistent system warnings, updating the badge logic accordingly.
  - **Severity:** Low

- **Scenario:** Orphaned notifications after account deletion.
  - **Persona:** System
  - **Trace:** When a student account is deleted, their associated notifications in the primary notification table are cleared. However, the system's email history logs are not scrubbed.
  - **Impact:** Potential retention of personally identifiable information violating data cleanup policies.
  - **Fix:** Ensure that the email history logs associated with the specific student are also purged during the account deletion process.
  - **Severity:** Low

## J. Edge Cases & Stress Scenarios

✅ **What works:**
- CSRF exemption lists functioning correctly for explicitly defined endpoints.
- Self-healing database logic capable of graceful error handling on fresh environments.
- Safe handling of special characters like single quotes in queries through parameterized execution.

❌ **What is missing or broken:**

- **Scenario:** Concurrent approvals for the same single-copy book.
  - **Persona:** Librarian
  - **Trace:** If two administrative users, or automated scripts, attempt to approve a request for the exact same book simultaneously, the system queries the available copies. If both read a positive value before either performs a write operation, both will approve the request and both will create a borrow record, decrementing the available copies below zero.
  - **Impact:** Negative inventory counts and phantom books issued to students that do not physically exist.
  - **Fix:** Implement strict row-level locking or optimistic concurrency control using explicit returning clauses or direct mathematical constraints within the update statement, ensuring a borrow record is only created if the update successfully affects exactly one row.
  - **Severity:** High

- **Scenario:** Student submitting the same request type rapidly in succession.
  - **Persona:** Attacker / Student
  - **Trace:** The system's rate limiter is applied to sensitive endpoints like login and password resets, but it is absent from the primary request submission endpoint.
  - **Impact:** A user could write a simple script to hammer the request submission endpoint thousands of times a second. Because the duplicate check relies on a slow query parsing data structures, it creates a massive race condition resulting in hundreds of duplicate requests being inserted, causing database bloat and dashboard spam.
  - **Fix:** Apply the existing rate limit decorator to the request submission endpoint and utilize an atomic database transaction for the duplicate verification check.
  - **Severity:** High

- **Scenario:** Catalogue with zero books showing correct empty state.
  - **Persona:** Student
  - **Trace:** The frontend application handles an empty database gracefully, and the backend returns an empty array as expected.
  - **Impact:** Operates correctly.
  - **Severity:** Low
