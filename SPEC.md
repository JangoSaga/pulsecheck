# PulseCheck --- Uptime Monitoring Platform

## 1. Overview

PulseCheck is a backend service that monitors websites and HTTP APIs at
configurable intervals. It records health-check results, detects outages
and recoveries, and exposes uptime analytics.

**Example:** A user registers `https://api.example.com/health` with a
60-second interval. PulseCheck periodically sends an HTTP request and
stores the status code, response time, and outcome.

## 2. Goals

-   Allow users to create and manage endpoint monitors.
-   Execute scheduled HTTP health checks.
-   Record check history and response times.
-   Detect downtime and recovery.
-   Notify users about incidents.
-   Calculate uptime and availability statistics.

## 3. Suggested technology stack

-   Python 3.12+
-   FastAPI
-   PostgreSQL
-   SQLAlchemy 2.x (async)
-   Alembic
-   Redis
-   `httpx`
-   Background scheduler and workers (start with APScheduler; evolve to
    a distributed scheduler if needed)
-   Pydantic v2
-   Pytest
-   Docker Compose

## 4. Functional requirements

### 4.1 Monitor management

Users can: - Create a monitor with a name, URL, HTTP method, interval
and timeout. - Configure expected status codes. - List and inspect their
monitors. - Update monitor settings. - Pause, resume and delete
monitors.

Validate URLs and restrict unsafe destinations. Do not allow checks
against localhost, private network ranges, link-local addresses or cloud
metadata endpoints unless explicitly permitted in a controlled
development environment. Re-check DNS resolution to reduce SSRF and
DNS-rebinding risks.

### 4.2 Health checks

For each due monitor: 1. Claim or schedule a check. 2. Send an HTTP
request with a configured timeout. 3. Record status code, latency and
outcome. 4. Classify the result as healthy or unhealthy. 5. Update
monitor state and incident state.

Support configurable methods and expected status codes. Avoid following
redirects by default, or validate every redirect destination.

### 4.3 Incident management

-   Open an incident when a monitor crosses the configured failure
    threshold.
-   Resolve the incident when the endpoint recovers according to the
    recovery policy.
-   Avoid creating duplicate open incidents for the same monitor.
-   Store start time, resolution time and failure reason.

Use configurable consecutive-failure thresholds to avoid declaring
downtime after one transient failure.

### 4.4 Notifications

Initially support email notifications: - Incident opened. - Incident
resolved.

Track notification attempts and outcomes. Avoid sending duplicate
notifications for the same state transition. Add retry handling for
temporary delivery failures.

### 4.5 Analytics

Expose: - Current monitor status. - Uptime percentage over a selected
period. - Average and percentile response times. - Total successful and
failed checks. - Incident count and duration.

Document the uptime calculation and treatment of missing checks.

## 5. Data model

### User

-   `id` --- UUID primary key
-   `email` --- unique
-   `password_hash`
-   `created_at`
-   `updated_at`

### Monitor

-   `id` --- UUID primary key
-   `user_id` --- foreign key
-   `name`
-   `target_url`
-   `http_method`
-   `interval_seconds`
-   `timeout_seconds`
-   `expected_status_codes` --- JSON or normalized configuration
-   `failure_threshold`
-   `recovery_threshold`
-   `is_active`
-   `created_at`
-   `updated_at`

### MonitorCheck

-   `id` --- UUID primary key
-   `monitor_id` --- foreign key
-   `outcome` --- healthy/unhealthy
-   `status_code` --- nullable
-   `response_time_ms` --- nullable
-   `error_type` --- nullable
-   `error_message` --- nullable and sanitized
-   `checked_at`

### Incident

-   `id` --- UUID primary key
-   `monitor_id` --- foreign key
-   `status` --- open/resolved
-   `started_at`
-   `resolved_at` --- nullable
-   `failure_reason`

### Notification

-   `id` --- UUID primary key
-   `incident_id` --- foreign key
-   `channel`
-   `status`
-   `attempt_count`
-   `last_error` --- nullable
-   `sent_at` --- nullable
-   `created_at`

Add indexes for monitor ownership, active monitors, check history by
`(monitor_id, checked_at)`, and open incidents. Define retention for
check history so the database does not grow without bounds.

## 6. API design

  -------------------------------------------------------------------------------------------
  Method                  Endpoint                                    Purpose
  ----------------------- ------------------------------------------- -----------------------
  POST                    `/api/v1/monitors`                          Create a monitor

  GET                     `/api/v1/monitors`                          List the current user's
                                                                      monitors

  GET                     `/api/v1/monitors/{monitor_id}`             Get monitor details

  PATCH                   `/api/v1/monitors/{monitor_id}`             Update monitor settings

  DELETE                  `/api/v1/monitors/{monitor_id}`             Delete a monitor

  POST                    `/api/v1/monitors/{monitor_id}/pause`       Pause monitoring

  POST                    `/api/v1/monitors/{monitor_id}/resume`      Resume monitoring

  GET                     `/api/v1/monitors/{monitor_id}/checks`      Get check history

  GET                     `/api/v1/monitors/{monitor_id}/incidents`   Get incident history

  GET                     `/api/v1/analytics/uptime`                  Get uptime analytics
  -------------------------------------------------------------------------------------------

All monitor operations must enforce ownership checks. Add pagination and
date-range validation to history and analytics endpoints.

## 7. Architecture

Suggested components:

-   **API:** FastAPI routes, authentication and request validation.
-   **Database:** PostgreSQL for users, monitor configuration, checks
    and incidents.
-   **Scheduler:** Finds monitors due for a check.
-   **Check workers:** Execute HTTP requests independently of API
    processes.
-   **Notification worker:** Delivers incident notifications.
-   **Redis:** Optional coordination, queueing and short-lived
    scheduling state.

Do not run long health checks inside API request handlers. The API
should enqueue or schedule work and return promptly.

## 8. Development phases

1.  Create project structure, settings and Docker Compose.
2.  Configure PostgreSQL, SQLAlchemy and Alembic.
3.  Implement authentication and monitor CRUD.
4.  Implement a single health-check service and unit tests.
5.  Add scheduling and independent check workers.
6.  Persist check results and calculate monitor state.
7.  Implement incident creation and recovery.
8.  Add notification delivery and retry handling.
9.  Implement analytics and pagination.
10. Add integration tests, security checks and documentation.

## 9. Testing requirements

Test: - Valid and invalid monitor URLs. - Successful, failing and
timed-out endpoints. - Unexpected status codes. - Consecutive-failure
and recovery thresholds. - Duplicate incident prevention. - Paused and
deleted monitors. - Concurrent scheduling and check claiming. -
Notification retry and deduplication. - User ownership and
authorization. - SSRF protections.

Use mocked HTTP responses for unit tests and controlled local test
servers for integration tests.

## 10. Non-functional requirements

-   Use async I/O for network operations.
-   Apply explicit connection and request timeouts.
-   Limit concurrent checks.
-   Use structured logs and correlation IDs.
-   Keep secrets out of logs.
-   Make worker operations safe to retry.
-   Define data retention and cleanup jobs.
-   Document scheduler and worker recovery behavior.

## 11. Definition of done

-   A user can register, pause, resume and delete monitors.
-   Checks execute independently at the configured interval.
-   Results are persisted and queryable.
-   Incidents open and resolve according to thresholds.
-   Notifications are recorded and retried safely.
-   Analytics return documented calculations.
-   Automated tests pass.
-   The service starts through Docker Compose.
-   README explains setup, configuration, architecture and API usage.
