1. **Indexes.** Unique `employee_code` and `employee_day` enforce employee identity and one attendance record per day. `department_code` supports department filtering, sorted employee lists and trend input. `joined_department` supports monthly eligible headcount. `date_employee` supports chronological attendance lists and month-wide analytics; `status_date_employee` handles status-filtered lists. `employee_punch` finds the latest eligible punch-in across midnight. The unique employee/day index also serves employee monthly lookups. I rejected an index on `half_day`: low selectivity and no standalone filter justify its write cost.

2. **Punch-in race.** Both requests resolve the employee and calculate the same attendance date. Both try inserting directly. MongoDB's unique employee/day index accepts one insert; the other raises `DuplicateKeyError`, translated to 409. There is no check-then-insert dependency.

3. **Ties.** MongoDB `$rank` ranks total late minutes before the cutoff filter. Every employee with rank at most `limit` is returned, including tied employees. Employee code determines ordering within ties; subsequent ranks skip positions.

4. **Headcount.** Department summaries start with eligible employees, then look up aggregated logs. Missing logs become zero statistics, so every eligible employee still contributes one to headcount. Hour sums and record counts preserve a record-weighted average.

5. **At 100x.** I would add daily summaries, measuring freshness and latency before choosing a sharding strategy.
