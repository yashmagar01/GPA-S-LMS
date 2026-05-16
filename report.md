# QA Architect Adversarial Audit Report

## A. Student Borrowing Lifecycle
✅ **What works:**
- Borrow limit enforcement (checked against `MAX_BOOKS_PER_STUDENT`)
- Duplicate request prevention for identical `book_id`
- Filtering out "Pass Out" students from making new requests

❌ **What is missing or broken:**
- **Scenario: Waitlisted book request limits**
  - *Persona*: Student
  - *Current behavior*: The `api_submit_request` endpoint checks `borrow_records` for active loans and borrow limits, but it completely ignores the number of *pending* `book_request` requests in the `requests` table. A student can submit 10 separate book requests (for 10 different books) even if their limit is 5.
  - *Why it's a problem*: A student can spam the librarian with more requests than they are allowed to borrow. When the librarian approves them, they may either bypass the limit or fail silently at approval time.
  - *Fix*: In `student_portal.py` `api_submit_request()`, add a query counting pending `book_request`s in the `requests` table. Add this to the current active loans count before comparing against `limit`.
  - *Severity*: High

- **Scenario: Requesting cancellation before approval (but immediately after submission)**
  - *Persona*: Student
  - *Current behavior*: `api_cancel_request` verifies status is 'pending', then updates to 'cancelled'. However, it does NOT trigger any sync to the local DB (`main.py`) or remove the request from the desktop UI immediately. If the SyncManager hasn't run, the librarian might see and approve a cancelled request.
  - *Why it's a problem*: Conflicting state between student UI and librarian UI.
  - *Fix*: In `student_portal.py`, `api_cancel_request`, call `_push_to_cloud` or an equivalent sync mechanism to ensure the cancellation reaches the local desktop DB immediately (or is queued properly).
  - *Severity*: Medium

⚠️ **What is partially implemented:**
- **Scenario: Collecting book deadline**
  - *Persona*: Both
  - *Current behavior*: When a book request is approved, an email is sent. But there is no background job or "deadline" logic in the DB to auto-cancel the reservation if the student doesn't collect it.
  - *Why it's a problem*: Book remains in "reserved" state infinitely, blocking other students.
  - *Fix*: Add an `expiration_date` or `reserved_until` column to `requests` or `borrow_records`, and a cron/background job to auto-cancel stale reservations.
  - *Severity*: High

## B. Renewal Lifecycle
✅ **What works:**
- Duplicate pending renewal prevention
- Due date calculation extends from `max(current_due, datetime.now())` + 7 days.

❌ **What is missing or broken:**
- **Scenario: Renewal when at the maximum renewal count**
  - *Persona*: Student
  - *Current behavior*: The `api_submit_request` and `api_admin_approve_request` endpoints do *not* check or increment a `renewal_count` on the `borrow_records` table. A student can request renewals infinitely. The UI mentions a policy of "2 Renewals per book", but this is hardcoded in `api_me()` and never enforced.
  - *Why it's a problem*: Students can keep a high-demand book indefinitely.
  - *Fix*: Add a `renewal_count` column to `borrow_records`. In `api_submit_request`, check if the count >= max. In `api_admin_approve_request`, increment the count when extending the due date.
  - *Severity*: High

- **Scenario: Renewal when another student is on the waitlist**
  - *Persona*: Both
  - *Current behavior*: The backend logic for renewal (`api_admin_approve_request`) blind-extends the date without checking if another student has a pending `book_request` for the same book (or if available_copies <= 0).
  - *Why it's a problem*: Unfair distribution; waitlisted students never get the book.
  - *Fix*: In `api_admin_approve_request` (and ideally in `api_submit_request` to block it early), query the `requests` table for pending `book_request`s for the same `book_id`. If > 0, reject the renewal.
  - *Severity*: Medium

## C. Return & Fine Lifecycle
✅ **What works:**
- Calculating fine based on days late during return (`main.py` -> `database.py` `return_book()`).

❌ **What is missing or broken:**
- **Scenario: Fine payment acknowledgment / Fine appearing on dashboard**
  - *Persona*: Student / Librarian
  - *Current behavior*: The `borrow_records` table has a `fine` column which is updated upon return. However, there is no system to track if the fine is *paid* vs *unpaid* (e.g., a `fine_status` column). The dashboard has no API endpoint to list unpaid fines or block borrowing.
  - *Why it's a problem*: The college cannot actually collect fines systematically; they disappear into the DB.
  - *Fix*: Add a `fine_status` (unpaid/paid/waived) to `borrow_records`. Create an endpoint `api/fines` for the student portal, and block new `book_request`s in `api_submit_request` if `fine_status == 'unpaid'`.
  - *Severity*: Critical

- **Scenario: Damaged/Lost book**
  - *Persona*: Librarian
  - *Current behavior*: There is no flow in `main.py` to mark a book as "Lost" or "Damaged". The only action is `return_book()`, which marks it 'returned' and makes the copy available again.
  - *Why it's a problem*: Inventory corruption. A lost book becomes "available" in the catalogue.
  - *Fix*: Add a "Mark Lost/Damaged" button in `main.py` that updates the `status` in `borrow_records` to 'lost' and *decrements* the `total_copies` / `available_copies` in the `books` table.
  - *Severity*: High

## D. Waitlist Lifecycle
❌ **What is missing or broken:**
- **Scenario: Real Waitlist functionality**
  - *Persona*: Student
  - *Current behavior*: There is an endpoint `/api/books/<book_id>/notify`, but there is no mechanism tying it to a true waitlist queue. When a book is returned in `database.py` `return_book()`, it does NOT check the `notifications` or `waitlist` tables to alert the next student.
  - *Why it's a problem*: The "Notify Me" button is a placebo.
  - *Fix*: In `database.py` `return_book()`, query the portal DB for users who requested notification for `book_id`. Insert a row into `user_notifications` and trigger an email.
  - *Severity*: High

## E. Account & Identity Scenarios
✅ **What works:**
- Checking for Pass Out status before allowing requests
- First login password change flow (updates `is_first_login = 0`)

❌ **What is missing or broken:**
- **Scenario: Account deletion with active loans**
  - *Persona*: Librarian
  - *Current behavior*: In `api_admin_approve_request` (or similar deletion logic), there is no check preventing the approval of an account deletion request if the student has active `status='borrowed'` records in `borrow_records`.
  - *Why it's a problem*: Books disappear from circulation tracking permanently.
  - *Fix*: In the deletion approval endpoint, query `borrow_records` for `status='borrowed'`. Return a 409 conflict if > 0.
  - *Severity*: Critical

## F. Librarian Daily Operations
✅ **What works:**
- Issuing a book at the counter via desktop app
- Processing returns

❌ **What is missing or broken:**
- **Scenario: Approving a request for a book with 0 available copies**
  - *Persona*: Librarian
  - *Current behavior*: `api_admin_approve_request` processes `book_request` but does not actually issue the book (insert into `borrow_records` and decrement `available_copies`). Wait, looking at the code, it doesn't even handle `book_request` approval logic for issuing! It just marks it 'approved'.
  - *Why it's a problem*: The book is never actually issued in the DB via the portal! The desktop app (`main.py`) handles issuing directly, but the portal approval just changes a status. If it's expected to issue, it's missing. If it's just a reservation, it lacks the counter step.
  - *Fix*: Clarify the approval workflow. If approval means "Reservation confirmed", the counter UI needs a way to "Issue Reserved Book".
  - *Severity*: Critical

## G. Sync & Data Integrity
✅ **What works:**
- Syncing core tables via SyncManager

❌ **What is missing or broken:**
- **Scenario: Sync Conflict / available_copies drifting**
  - *Persona*: System
  - *Current behavior*: `available_copies` is heavily reliant on manual updates or desktop triggers. If a student portal action (like an API waitlist) relies on `available_copies`, it might read stale data. Furthermore, concurrent issuing on Desktop A vs Desktop B can create negative `available_copies`.
  - *Fix*: Instead of relying purely on the `available_copies` column, calculate it dynamically via `total_copies - COUNT(borrow_records WHERE status='borrowed')` or use database triggers to prevent it dropping below 0.
  - *Severity*: High

## H. Deployment & Infrastructure
✅ **What works:**
- Access-log system hook

❌ **What is missing or broken:**
- **Scenario: Unauthenticated access to study materials**
  - *Persona*: Student/Attacker
  - *Current behavior*: `/api/study-materials/<int:material_id>/download` might not check `session['student_id']` correctly, or allows direct URL access to the file path.
  - *Fix*: Ensure the download endpoint strictly checks session auth and uses `send_file` securely, preventing path traversal.
  - *Severity*: High

## I. Notification & Email Pipeline
✅ **What works:**
- The `generate_email_template` and `trigger_notification_email` are called on request submission.

❌ **What is missing or broken:**
- **Scenario: Email failing silently delaying the API**
  - *Persona*: Student
  - *Current behavior*: `trigger_notification_email` is called synchronously in `api_submit_request`. If the SMTP server is slow, the student's web request hangs.
  - *Fix*: Push `trigger_notification_email` into a background thread or task queue (e.g., using `threading.Thread` or Celery).
  - *Severity*: Medium

## J. Edge Cases & Stress Scenarios
❌ **What is missing or broken:**
- **Scenario: Student submitting the same request type 20 times rapidly**
  - *Persona*: Student
  - *Current behavior*: No rate limiting on `/api/request`. A script could submit thousands of requests, spamming the librarian and crashing the email server.
  - *Fix*: Implement Flask-Limiter or a simple session-based cooldown (e.g., `last_request_time`) to restrict `/api/request` to 1 request per 10 seconds.
  - *Severity*: High
