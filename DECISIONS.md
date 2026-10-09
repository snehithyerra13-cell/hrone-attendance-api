# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` ensures customer ID uniqueness; index on `(department, joined_on)` accelerates department filtering.
   - `attendance_logs`: Unique compound index on `(emp_code, date)` guarantees at most one record per employee per day. `(date: -1, emp_code: 1)` backs paginated listing without in-memory sort. `(emp_code: 1, punch_in: -1)` serves fast punch-out session lookup. `(date: 1, late_minutes: -1)` and `(date: 1, status: 1)` back analytics pipelines.
   - *Rejected:* A TTL index was considered and rejected because attendance logs and history audit trails must persist permanently.

2. **Punch-in race.**
   Two concurrent punch-ins for the same employee and date proceed simultaneously. Both attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` allows exactly one document to be written (returning HTTP 201). The competing insert raises `DuplicateKeyError`, which the API catches and returns as HTTP 409 Conflict.

3. **Ties.**
   The aggregation uses `$rank` window ranking (competition ranking: `1, 2, 2, 4`). The `$match: {"rank": {"$lte": limit}}` stage filters after ranking. Tied employees at the cutoff share the same rank and are both returned (response may exceed `limit` items), ordered by `total_late_minutes DESC, emp_code ASC`.

4. **Headcount.**
   The pipeline queries `employees` first, filtering by `joined_on <= month_end` and counting headcount (`$sum: 1`). It brings in `attendance_logs` using `$lookup` with `preserveNullAndEmptyArrays: True`, ensuring employees and departments with zero logs are preserved and counted.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily attendance totals into a dedicated `daily_department_summary` collection via change streams or transactional updates on punch-out/regularization, replacing heavy real-time aggregation queries over hundreds of millions of raw log documents with $O(1)$ index reads.
