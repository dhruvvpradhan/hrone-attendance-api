# Design Decisions

This document explains some of the implementation choices I made while building the Employee Attendance & Analytics API.

## 1. Keeping attendance calculations on the server

I decided not to accept calculated values such as late minutes, work hours or overtime from the client.

The client provides the punch timestamps, while the backend calculates the derived attendance fields. This makes the stored data more consistent and prevents a client from sending values that do not match the actual punches.

## 2. Using IST for the attendance date

The attendance date is a business concept, so I used IST when deciding which date a punch belongs to.

MongoDB stores timestamps as BSON datetime values, while the application converts them when it needs to calculate the local attendance date.

This is particularly important around midnight and for shifts that cross the normal day boundary.

## 3. Preventing duplicate attendance records

An employee should have at most one attendance record for a particular attendance date.

I used a unique MongoDB index on:

```text
emp_code + date
```

The application also checks for an existing record before inserting a new one. The database constraint is still important because two requests could arrive at nearly the same time.

If MongoDB detects a duplicate, the API returns a conflict response instead of creating another record.

## 4. Punch-out validation

For punch-out, the API looks for the relevant existing punch-in rather than blindly accepting any timestamp.

The punch-out time must be after the punch-in and cannot be unreasonably far from it. This prevents invalid attendance records caused by reversed or incorrect timestamps.

The update also checks the current database state before changing the record.

## 5. Attendance regularization and history

I kept regularization history inside the attendance document instead of creating another collection.

When a record is manually corrected, the history stores information about the previous values, the changes, the reason and who made the correction.

This keeps the current attendance state easy to query while still preserving an audit trail.

## 6. Clearing punches for non-presence statuses

Statuses such as `ABSENT` and `LEAVE` should not retain punch times that imply the employee was present.

Therefore, when regularization changes an attendance record to one of these statuses, the punch fields are cleared and the derived attendance values are recalculated.

## 7. Using MongoDB aggregation for analytics

For analytics endpoints, I chose MongoDB aggregation pipelines instead of retrieving all attendance records into Python and calculating everything there.

This allows filtering, grouping and calculations to happen closer to the data and reduces the amount of data that needs to be transferred to the application.

## 8. Department headcount

For department-level analytics, headcount should not depend only on attendance records.

An employee may have joined the company but have no attendance record for a particular period. The department summary therefore uses the employee collection to determine which employees were part of the department during the selected period.

## 9. Leaderboard ranking

The late-arrival leaderboard uses MongoDB's window-function support to rank employees based on their total late minutes.

Using a database-side ranking operation also allows employees with equal values to receive the same rank.

## 10. Filling missing dates in department trends

A department may not have an attendance record for every calendar date.

For the trend endpoint, I used MongoDB's `$densify` stage so that the output can contain the complete date range, including dates where there was no attendance document.

This makes the trend easier to consume because the API does not silently omit dates.

## 11. Seven-day moving average

The department trend includes a seven-day moving average.

I calculated this as a rolling value over the generated daily trend rather than calculating it separately in application code. This keeps the trend-related processing inside the aggregation pipeline.

## 12. Pagination at the database level

The attendance listing uses database-side sorting, skipping and limiting.

This means the API does not need to load every attendance document into Python just to return one page of results.

The page size is also bounded so that a client cannot request an unnecessarily large response.

## 13. Query performance checks

I added explain endpoints so that MongoDB execution statistics can be inspected for the main analytics queries.

This provides a way to verify query execution and index usage instead of assuming that an index is being used simply because it exists.

## 14. Keeping the implementation in one main application file

The assignment specifically expects the application implementation in `app/main.py`.

I therefore kept the API routes, validation models, helper functions and MongoDB operations together in that file rather than introducing additional application modules that were not required for the task.

This also makes the submitted implementation straightforward to review and run.