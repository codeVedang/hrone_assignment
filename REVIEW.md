# Starter review

Line numbers refer to the original starter supplied in the kit, not the replacement file. Missing endpoints are implemented separately from these defects.

| # | Where (function) | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start in the punch's timezone and date rather than the IST attendance day. | UTC 04:35 against a 09:30 IST shift must be 35 minutes late. | Construct aware IST shift bounds using the attendance date. |
| 2 | `compute_late_minutes` | Floors minutes before testing grace, losing seconds at the boundary. | 09:40:01 must yield 10, but yields 0. | Compare elapsed seconds to 600 first, then floor minutes. |
| 3 | `compute_late_minutes` | Cannot use the previous day's start for early-morning overnight punches. | 02:00 for a 22:00–06:00 shift is 240 minutes late. | Use R1's attendance day to anchor shift start. |
| 4 | `compute_work_hours` | Uses binary float and banker's rounding. | 3,618 seconds is 1.005 hours and must round to 1.01. | Divide integer seconds with Decimal and `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Does not advance overnight shift end to the next day. | 06:40 next day after a 22:00 shift must be 40, not 1,480. | Construct the next-day end when shift end is no later than start. |
| 6 | `compute_overtime` | Counts overtime below the 30-minute threshold. | 18:59:59 after an 18:30 end must yield 0. | Floor minutes and apply the minimum of 30. |
| 7 | Time helpers / `punch_in` | Fractional seconds are neither truncated nor normalized before calculations. | 09:40:00.999 must store 09:40:00 and remain within grace. | Convert epoch milliseconds using integer whole seconds. |
| 8 | Mongo client / timestamps | Default naive BSON reads and host-local timestamps make timezone arithmetic unreliable. | Run on a UTC host, or subtract an aware shift end from a naive punch. | Read aware UTC dates, construct IST calendars, use an aware UTC server clock. |
| 9 | Input models | Employee code, name, email and department constraints are absent. | `EMP123`, empty names and malformed emails are accepted. | Match contract patterns and lengths in Pydantic. |
| 10 | Input models | Shift strings and the unequal-shifts requirement are not validated. | `24:00`, `9:30`, or identical start/end are accepted. | Validate HH:MM and reject equal shifts on creation. |
| 11 | Input models / attendance filters | Calendar strings and invalid ranges are not validated. | February 30 or an inverted range is accepted. | Validate exact formats, real dates, month values and range order. |
| 12 | `PunchInIn` | Accepts arbitrary statuses. | A punch-in with `LEAVE` succeeds. | Restrict punch-in status to the three presence statuses. |
| 13 | `PunchInIn` / timestamp conversion | Coerces strings/floats, accepts seconds and huge numbers, and uses truthiness for omission. | Seconds-looking timestamps produce wrong dates; huge values can raise 500. | Strict bounded integer milliseconds; omission alone defaults to now. |
| 14 | `health` | Returns ready without contacting MongoDB. | Stop MongoDB; still returns 200. | Ping and return 503 on database failure; ensure startup indexes are ready. |
| 15 | `create_employee` | Check-then-insert races and no unique index exists. | Parallel creates can insert duplicate codes. | Unique business-code index and duplicate-key-to-409 handling. |
| 16 | `create_employee` | `created_at` uses local naive time and responses expose ISO dates. | On a non-UTC host the stored instant shifts; output is not epoch milliseconds. | UTC server timestamps and recursive millisecond serialization. |
| 17 | `list_employees` | Skip calculation starts page 1 at the second page. | First page omits the first 20 employees. | `(page - 1) * page_size`. |
| 18 | `list_employees` | Total ignores the department filter. | Filter one department and compare total to its members. | Count using the same filter and supporting index. |
| 19 | `list_employees` | No deterministic employee-code ordering. | Insert employees in reverse code order. | Sort in MongoDB by code ascending. |
| 20 | Both list endpoints | No positive-page / 1–100 page-size validation. | Page 0 or page_size 0/101 is accepted. | Validated query parameters shared with explain. |
| 21 | `punch_in` | Unknown employees cause dereferencing of `None`. | Unknown code results in 500. | Resolve employee with an explicit 404. |
| 22 | `punch_in` | Attendance day uses host-local time and ignores overnight allocation. | 02:00 IST belongs to yesterday for a 22:00–06:00 shift. | Convert UTC instant to IST and implement R1's strict earlier-than rule. |
| 23 | `punch_in` | Duplicate check is racy and no employee/day uniqueness is enforced. | Simultaneous punches produce multiple records. | Unique compound index, direct insert, duplicate-key 409. |
| 24 | `punch_in`, `list_attendance` | Invents an `id` in responses, contrary to the natural-key contract. | Inspect response fields. | Whitelist contract fields and omit Mongo identifiers. |
| 25 | Attendance / employee responses | Punches, created time and nested history dates are not converted to epoch milliseconds. | Inspect returned types, including history change values. | Recursive BSON-date serialization. |
| 26 | `list_attendance` | Loads and sorts the entire filtered collection in Python. | A small page over 100,000 logs uses excessive memory and time. | Indexed database sort, skip, limit and count. |
| 27 | `list_attendance` | Sort does not break equal-date ties by employee code. | Seed same-day records in shuffled order. | Sort by date descending, then code ascending. |
| 28 | `list_attendance` | Legacy missing fields are returned missing. | Seed without `history` / `half_day`. | Supply empty history, false half day, and zero optional minute defaults. |
| 29 | Startup | No query or uniqueness indexes are created. | Explain shows collection scans; duplicate business keys are possible. | Idempotent named indexes at startup, shared hints/builders for explain. |
| 30 | Both list filters | Truthiness makes an explicitly empty department/code filter disappear. | `department=` returns all employees rather than exact matches. | Test optional filters with `is not None`. |

Examined and retained: `load_dotenv()` respects real environment variables by default (made explicit with `override=False`); a fixed UTC+05:30 offset is sufficient for the specified IST rules; the work duration formula is correct before rounding; flooring positive overtime minutes is correct before applying the threshold; the attendance slice's `(page - 1)` arithmetic was correct, although it belonged in MongoDB. Synchronous PyMongo with ordinary `def` endpoints is appropriate because FastAPI runs them in its worker thread pool. Read-only derived body fields are intentionally ignored, as the contract requests.
