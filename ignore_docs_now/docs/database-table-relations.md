# Source-Derived Database Table Relations

## 1. Title And Purpose

This document maps backend table relationships by scanning application source rather than relying on live database introspection. It covers the schemas actively referenced by the codebase:

- `pf_TickleRight_9210`: tenant business data
- `pf_central`: shared platform data
- `pf_admin`: admin/logging data

The goal is to document how the backend treats tables as related, while clearly separating view-defined joins from application-inferred links. This is not a generated ERD and it does not claim database-enforced foreign keys unless schema evidence explicitly shows them.

## 2. Methodology And Confidence Rules

### Sources scanned

- `controllers/`
- `classes/`
- `routes/`
- `migrations/`
- `scripts/createView.php`
- `controllers/userRegistration.php`

### Confidence levels

- `Confidence: schema-enforced`
  Used only when migration or schema SQL explicitly defines the relationship. No core business-table foreign key constraints were confirmed from the scanned legacy schema sources.
- `Confidence: view-defined`
  Used when a `CREATE VIEW` statement explicitly joins one table to another.
- `Confidence: application-inferred`
  Used when controllers, classes, or routes repeatedly join or subquery one table through another, but no FK constraint is shown.

### Cardinality notation

- `Cardinality: 1:1`
- `Cardinality: 1:N`
- `Cardinality: N:N`
- `Cardinality: derived`

### Important note

Most relationships below are `view-defined` or `application-inferred`. The scanned migrations add tables and columns, but they do not provide FK definitions for the main legacy tenant model.

## 3. Schema Overview

- `pf_TickleRight_9210` holds operational data for contacts, inquiries, members, batches, attendance, invoices, payments, and reports.
- `pf_central` holds shared client, user, permission, request, biometric, module, and assessment metadata used across tenant flows.
- `pf_admin` is used mainly for coupon and advertisement availability logging from tenant flows.
- The application depends heavily on SQL views such as `invoice_invoiceitem_view`, `batch_view`, `session_batch_view`, and `batch_employee_time_view`. In practice, these views act as part of the effective read model.

## 4. Core Domains In `pf_TickleRight_9210`

### Contact / Inquiry / Member

Tables covered here: `contact`, `inquiry`, `followup`, `member`, `heard_from`, `category`, `service`

- `member.contact_id -> contact.id`
  `Cardinality: 1:1`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `invoice.contact_id -> contact.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `inquiry.contact_id -> contact.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/inquiry.php`

- `inquiry.heard_from -> heard_from.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/schedulers/total_leads_count.php`, `controllers/schedulers/total_instagram_inq_count.php`, `controllers/schedulers/statistics.php`

- `followup.contact_id -> contact.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/renew.php`

- `followup.parent_id -> followup.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`

- `followup.master_id -> inquiry/payment/invoice item depending on followup.master`
  `Cardinality: derived`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`
  Note: `contact_followup_view` resolves `master_id` polymorphically through `inq_cont_cat_view`, `invoice_invoiceitem_view`, and `payment`. This is an application reference pattern, not a single foreign key.

- `service.category_id -> category.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/renew.php`

- `service.module_id -> module.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`
  Note: this is the tenant `module` table surfaced by `invoice_invoiceitem_view`, not the central `pf_central.module` table.

- `service.topic_id -> topic.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

- `service.session_master_id -> session_master.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

### Batch / Session / Attendance

Tables covered here: `batch`, `batch_time`, `batch_employee`, `session`, `attendance`, `venue`, `branch`, `level`

- `batch.category_id -> category.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

- `batch.bid -> branch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/batch_session.php`

- `batch.venue_id -> venue.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/batch.php`, `controllers/schedulers/ratio_report.php`, `controllers/schedulers/total_mem_count_monthly.php`

- `batch.level_id -> level.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`

- `batch_time.batch_id -> batch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/attendance.php`

- `batch_employee.batch_id -> batch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/batch.php`

- `batch_employee.contact_id -> employee.contact_id`
  `Cardinality: N:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/demo.php`
  Note: `batch_employee` acts as a junction between batches and employees. Some queries surface the same link through `contact.id` because employee records are themselves keyed by `employee.contact_id`.

- `session.batch_id -> batch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

- `session.contact_id -> employee.contact_id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`
  Note: `session_batch_view` resolves `session.contact_id` through `emp_cont_view`.

- `session.session_master_id -> session_master_view.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`

- `attendance.contact_id -> contact.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/attendance.php`

- `attendance.batch_id -> batch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `controllers/batch.php`

- `attendance.session_id -> session_batch_view.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`

### Invoice / Payment

Tables covered here: `invoice`, `invoice_item`, `payment`, `service_makeup`

- `invoice.contact_id -> contact.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `invoice_item.invoice_id -> invoice.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `invoice_item.category_id -> category.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

- `invoice_item.service_id -> service.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `invoice_item.batch_id -> batch.id`
  `Cardinality: 1:N`
  `Confidence: view-defined`
  Evidence: `controllers/userRegistration.php`, `scripts/createView.php`, `controllers/renew.php`

- `payment.invoice_id -> invoice.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/renew.php`, `controllers/payment.php`

- `service_makeup.service_id -> service.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `inc/customPdfBody/invoiceIMFrenchisee.php`, `inc/customPdfBody/invoiceFrenchisee.php`

### Reporting / Configuration

Tables covered here: `report`, `report_column`, `report_filter`, `report_order`, `attendance_settings`, `invoice_settings`, `payment_settings`

- `report_column.report_id -> report.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/report.php`, `controllers/structured_report.php`, `controllers/schedulers/report_email.php`

- `report_filter.report_id -> report.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/report.php`, `controllers/structured_report.php`, `controllers/schedulers/report_email.php`

- `report_order.report_id -> report.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/report.php`, `controllers/structured_report.php`, `controllers/schedulers/report_email.php`

- `attendance_settings.bid -> branch.id`
  `Cardinality: 1:1`
  `Confidence: application-inferred`
  Evidence: `controllers/attendance.php`, `controllers/settings.php`, `controllers/schedulers/attendance_script.php`

- `invoice_settings.bid -> branch.id`
  `Cardinality: 1:1`
  `Confidence: application-inferred`
  Evidence: `controllers/invoice.php`, `controllers/settings.php`, `scripts/userEdit.php`

- `payment_settings.bid -> branch.id`
  `Cardinality: 1:1`
  `Confidence: application-inferred`
  Evidence: `controllers/paymentAcc.php`, `controllers/settings.php`, `controllers/pursuePayment.php`

## 5. Shared Platform Tables In `pf_central`

Tables covered here: `client_db`, `user`, `user_bid`, `user_social`, `user_permission`, `client_module`, `bio_device`, `request`, `module`, `action`, `employee_department`, `assessment_*`

### Client / User / Permission model

- `user.client_id -> client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/biometric.php`, `controllers/renew.php`

- `user.contact_id -> pf_TickleRight_9210.contact.id`
  `Cardinality: 1:1`
  `Confidence: application-inferred`
  Evidence: `controllers/contact.php`, `scripts/userEdit.php`
  Note: this is a cross-schema application link used to bind central users to tenant contacts.

- `user_bid.user_id -> user.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/employee.php`

- `user_bid.bid -> pf_TickleRight_9210.branch.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/employee.php`

- `user_social.user_id -> user.id`
  `Cardinality: 1:1`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/contact.php`, `controllers/biometric.php`

- `user_permission.user_id -> user.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/renew.php`, `controllers/employee.php`

- `user_permission.cm_id -> client_module.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/renew.php`

- `client_module.client_id -> client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/userEdit.php`, `controllers/renew.php`

- `client_module.module_id -> module.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/renew.php`, `classes/dbconnection.class.php`

### Client / Device / Request model

- `bio_device.client_id -> client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/biometric.php`, `controllers/attendance.php`, `controllers/dashboard.php`

- `request.action_id -> action.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/Classess/save_sms_according_to_action.php`, `scripts/Classess/email_according_to_action.php`, `classes/dbconnection.class.php`

- `request.client_id -> client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `scripts/Classess/save_sms_according_to_action.php`, `scripts/Classess/email_according_to_action.php`, `controllers/batch_session.php`

- `request.table_name + request.row_id -> polymorphic application target`
  `Cardinality: derived`
  `Confidence: application-inferred`
  Evidence: `scripts/Classess/save_sms_according_to_action.php`, `scripts/Classess/email_according_to_action.php`, `controllers/batch_session.php`, `controllers/renew.php`
  Note: this is not a single foreign key. The request queue stores a table name plus row id to reference different tenant records such as `session_batch_view` or `invoice_invoiceitem_view`.

### Assessment metadata used from tenant flows

- `assessment_task.group_id -> assessment_group.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `assessment_template_task.template_id -> assessment_template.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `assessment_template_task.task_id -> pf_central.assessment_task.id`
  `Cardinality: N:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`
  Note: `assessment_template_task` behaves as the junction between templates and tasks.

- `assessment_task_value.task_id -> assessment_task.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `assessment_log.template_id -> assessment_template.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `assessment_log_value.log_id -> assessment_log.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `assessment_log_value.task_id -> pf_central.assessment_task.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/assessment.php`

- `employee_department.id <- emp_cont_view.department_id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/calendar.php`

## 6. Admin Logging Links In `pf_admin`

Only admin tables directly referenced from application logic are listed here.

- `coupon_avail_log.client_id -> pf_central.client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/coupon.php`

- `coupon_avail_log.contact_id -> pf_TickleRight_9210.contact.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/coupon.php`

- `advertisement_avail_log.client_id -> pf_central.client_db.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/ads.php`

- `advertisement_avail_log.contact_id -> pf_TickleRight_9210.contact.id`
  `Cardinality: 1:N`
  `Confidence: application-inferred`
  Evidence: `controllers/ads.php`

## 7. Important Views Appendix

### View: `invoice_invoiceitem_view`

- Type: core operational read model
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `invoice`
  - `invoice_item`
  - `category`
  - `service`
  - `contact`
  - `member`
  - `batch`
  - `batch_view`
  - `module`
  - `topic`
  - `session_master`
- Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

### View: `batch_view`

- Type: helper view for batch scheduling and display
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `batch`
  - `batch_time`
  - `batch_employee`
  - `employee`
  - `contact`
  - `category`
- Evidence: `controllers/userRegistration.php`

### View: `session_batch_view`

- Type: core operational read model for sessions
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `session`
  - `batch`
  - `emp_cont_view`
  - `session_master_view`
  - `branch`
- Evidence: `controllers/userRegistration.php`

### View: `batch_employee_time_view`

- Type: helper view for attendance, staffing, and scheduling
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `batch_time`
  - `batch`
  - `batch_employee`
  - `category`
  - `session`
  - `session_master_view`
  - `employee`
  - `contact`
  - `level`
- Evidence: `controllers/userRegistration.php`, `scripts/createView.php`

### View: `invoice_invoiceitem_module_view`

- Type: helper view for module-scoped invoice coverage
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `invoice`
  - `invoice_item`
  - `category`
  - `service_module_view`
  - `contact`
  - `member`
  - `batch_view`
  - `session_master`
  - `session`
- Evidence: `controllers/userRegistration.php`

### View: `attendance_cont_view`

- Type: helper view for attendance reporting
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `attendance`
  - `contact`
  - `batch`
  - `session_batch_view`
- Evidence: `controllers/userRegistration.php`

### View: `contact_followup_view`

- Type: helper view for CRM followup timelines
- `Cardinality: derived`
- `Confidence: view-defined`
- Derived from:
  - `followup`
  - `contact`
  - `emp_cont_view`
  - `followup` as parent followup
  - `inq_cont_cat_view`
  - `invoice_invoiceitem_view`
  - `payment`
- Evidence: `controllers/userRegistration.php`

## 8. Limitations / Open Questions

- Live MySQL schema inspection was not available in this environment, so the document is derived from source and migrations instead of validated against `INFORMATION_SCHEMA`.
- No scanned migration or schema file proved foreign key constraints for the main legacy tenant relationships above. As a result, core links are intentionally marked `view-defined` or `application-inferred`.
- `request.table_name` plus `row_id` is a polymorphic queue reference pattern, not a relational foreign key.
- `followup.master_id` is also polymorphic and resolves differently based on `followup.master`.
- `attendance_settings`, `invoice_settings`, and `payment_settings` appear branch-scoped through `bid`, but the schema files scanned do not show an enforced constraint to `branch.id`.
- Additional `pf_admin` tables may exist, but only `coupon_avail_log` and `advertisement_avail_log` were directly confirmed from the scanned application logic.
