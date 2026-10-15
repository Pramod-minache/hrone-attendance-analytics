# Design Decisions

## 1. Indexes
I created a unique index on employee code to prevent duplicate employees, and a compound unique index on attendance employee code and date to prevent duplicate daily attendance records. I also added indexes for department and employee code, employee join date, and attendance status and date. I would consider an additional date-and-employee-code index if query-plan tests show it is needed.

## 2. Punch-in race
When two identical punch-in requests arrive together, the database must allow only one to create the attendance record. The unique compound index is the final protection against duplicates. One request should succeed and the other should receive HTTP 409 Conflict. This behavior must be verified with concurrent requests.

## 3. Ties
Employees with equal late minutes should have the same rank. Tied employees should be ordered by employee code for predictable results. The leaderboard must rank employees before applying the limit so ties at the cutoff are handled consistently.

## 4. Headcount
The department summary must count employees who joined by the end of the reporting period, even when they have no attendance logs. The aggregation should start from employees or otherwise preserve employees without matching logs.

## 5. Scaling
For 100 times more data, I would inspect execution statistics, add indexes based on measured query plans, and keep filtering and aggregation in MongoDB instead of loading large datasets into Python.