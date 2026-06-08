# Evaluation Failure Analysis

Source run:
- [`reports/evaluation/evaluation_summary.json`](/var/www/py-workspace/nl2sql/reports/evaluation/evaluation_summary.json)
- [`reports/evaluation/evaluation_failures.jsonl`](/var/www/py-workspace/nl2sql/reports/evaluation/evaluation_failures.jsonl)

Run metadata:
- `run_at`: `2026-06-08T13:31:53.887889+00:00`
- `service_url`: `http://localhost:8081`
- `endpoint`: `ask-stream`
- `total_tests`: `32`
- `passed`: `0`
- `failed`: `32`
- `failure_breakdown`: `PROVIDER_FAILURE: 32`

## Executive Summary

Every benchmark case failed in the same place: `sql_generation`.
The evaluator labels the run as `PROVIDER_FAILURE`, but the actual symptom is a service-budget timeout in SQL generation.

Observed pattern:
- `REQUEST_TIMEOUT` on all `32` cases
- SQL generation latency between `185132 ms` and `200453 ms`
- No case reached execution or answer-generation successfully
- The special guardrail cases did not reach their intended branches:
  - `delete old payments from last year` timed out before `SQL_DESTRUCTIVE` surfaced
  - `show active records` timed out before `clarification_needed` was returned

Latency summary:
- Min: `185132 ms`
- Median: `197691 ms`
- Mean: `196440.8 ms`
- Max: `200453 ms`

## What Failed Where

The failure step is the same for every prompt:
- Step: `sql_generation`
- Error: `REQUEST_TIMEOUT`
- Final response: `rejected`

That means the service did not get far enough to produce a valid SQL answer, reject for the correct governance reason, or ask for clarification where required.

## Suite Breakdown

### Level 1 - Basic

| Test | Query | Expected | Failure step | Latency | Problem |
|---|---|---:|---|---:|---|
| `L1-001-recent-payments` | `show me the 5 most recent payments` | `ok` | `sql_generation` | `197945 ms` | `REQUEST_TIMEOUT; missing payment keyword/table` |
| `L1-002-recent-inquiries` | `show me the 5 most recent inquiries` | `ok` | `sql_generation` | `185196 ms` | `REQUEST_TIMEOUT; missing inquiry keyword/table` |
| `L1-003-active-members` | `list active members` | `ok` | `sql_generation` | `197834 ms` | `REQUEST_TIMEOUT; missing member keyword/table` |
| `L1-004-contact-by-mobile` | `find contact by mobile number` | `ok` | `sql_generation` | `198186 ms` | `REQUEST_TIMEOUT; missing contact, mobile keywords; missing contact table` |

### Level 2 - Intermediate

| Test | Query | Expected | Failure step | Latency | Problem |
|---|---|---:|---|---:|---|
| `L2-001-payment-by-invoice` | `show payment details for invoice code 123` | `ok` | `sql_generation` | `195598 ms` | `REQUEST_TIMEOUT; missing payment, invoice keywords; missing invoice, payment tables` |
| `L2-002-active-campaigns` | `which campaigns are active right now` | `ok` | `sql_generation` | `186089 ms` | `REQUEST_TIMEOUT; missing campaign, active keywords; missing campaign table` |
| `L2-003-latest-inquiries-by-counsellor` | `which counsellor is assigned to the latest inquiries` | `ok` | `sql_generation` | `197948 ms` | `REQUEST_TIMEOUT; missing inquiry, counsellor keywords; missing assign_counsellor_log, employee, inquiry tables` |
| `L2-004-contact-by-email` | `find the contact with email sample@example.com` | `ok` | `sql_generation` | `198183 ms` | `REQUEST_TIMEOUT; missing contact, email keywords; missing contact table` |

### Level 3 - Advanced

| Test | Query | Expected | Failure step | Latency | Problem |
|---|---|---:|---|---:|---|
| `L3-001-unpaid-invoices-by-counsellor` | `show unpaid invoices by counsellor with payment status` | `ok` | `sql_generation` | `197548 ms` | `REQUEST_TIMEOUT; missing invoice, payment, counsellor keywords; missing employee, invoice, payment tables` |
| `L3-002-member-renewals-by-branch` | `show member renewals due this month by branch` | `ok` | `sql_generation` | `197285 ms` | `REQUEST_TIMEOUT; missing renewal, branch keywords; missing active_members_renewal_range, member tables` |
| `L3-003-campaign-spend-by-day` | `compare campaign spend and clicks by campaign and date` | `ok` | `sql_generation` | `197278 ms` | `REQUEST_TIMEOUT; missing campaign, spend, clicks keywords; missing adset_cost, campaign tables` |
| `L3-004-employee-shift-roster` | `show employees with their workshift and contact details` | `ok` | `sql_generation` | `197978 ms` | `REQUEST_TIMEOUT; missing employee, workshift, contact keywords; missing contact, employee, workshift tables` |

### Level 4 - Expert

| Test | Query | Expected | Failure step | Latency | Problem |
|---|---|---:|---|---:|---|
| `L4-001-conflicting-active-members` | `show active members but exclude dropout records and inactive logs` | `ok` | `sql_generation` | `199715 ms` | `REQUEST_TIMEOUT; missing active, member, dropout keywords; missing active_member_log, active_members_renewal_range, member tables` |
| `L4-002-live-campaigns-no-spend` | `show adset cost for live campaigns with no spend` | `ok` | `sql_generation` | `198964 ms` | `REQUEST_TIMEOUT; missing campaign, spend keywords; missing adset_cost, campaign tables` |
| `L4-003-destructive-delete-old-payments` | `delete old payments from last year` | `rejected` | `sql_generation` | `198490 ms` | `REQUEST_TIMEOUT; missing delete, payment keywords; expected SQL_DESTRUCTIVE warning never surfaced` |
| `L4-004-ambiguous-active-records` | `show active records` | `clarification_needed` | `sql_generation` | `197498 ms` | `REQUEST_TIMEOUT; missing active keyword; expected clarification_needed but got rejected` |

### Level 5 - Stress Test

| Test | Query | Expected | Failure step | Latency | Problem |
|---|---|---:|---|---:|---|
| `L5-001-broad-ops-mix` | `show recent payments, invoices, inquiries, and member renewals by branch` | `ok` | `sql_generation` | `198688 ms` | `REQUEST_TIMEOUT; missing payment, invoice, inquiry, renewal keywords; missing inquiry, invoice, member, payment tables` |
| `L5-002-source-conversion-overload` | `which sources create the most inquiries and which campaigns convert them` | `ok` | `sql_generation` | `197527 ms` | `REQUEST_TIMEOUT; missing source, campaign, inquiry keywords; missing campaign, inquiry tables` |
| `L5-003-cache-reuse-regression` | `show me the 5 most recent payments` | `ok` | `sql_generation` | `197083 ms` | `REQUEST_TIMEOUT; missing payment keyword/table` |
| `L5-004-token-pressure-join-roster` | `summarize employees, workshifts, contacts, and invoices for the last 30 days` | `ok` | `sql_generation` | `198198 ms` | `REQUEST_TIMEOUT; missing employee, workshift, contact, invoice keywords; missing contact, employee, invoice, workshift tables` |

## Interpretation

1. The prompts themselves are not showing a diverse set of downstream failures.
2. The bottleneck is the SQL generation stage timing out before the prompt-specific logic can complete.
3. Because the timeout happens before final guardrails or clarification branches, the evaluator records the cases as `PROVIDER_FAILURE` even when the intended outcome was `rejected` or `clarification_needed`.
4. The shorter, single-table prompts fail the same way as the larger multi-table prompts, which suggests the problem is systemic in generation execution time rather than a single prompt family.

## Notes

- The report is based on the completed benchmark output currently on disk.
- None of the cases reached a successful SQL answer.
- If you want the next iteration, the highest-value fix is to reduce or bypass the long-running `sql_generation` path for obvious single-table prompts, and to make guardrail / clarification decisions happen before the generation budget is consumed.
