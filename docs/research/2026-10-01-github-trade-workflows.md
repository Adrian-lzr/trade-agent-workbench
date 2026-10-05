# GitHub Workflow Design Review

Reviewed 2026-10-01. This is an evidence log and design comparison, not a code dependency list.

## Durable execution and idempotency

- [Frappe CRM](https://github.com/frappe/crm/tree/6b37fc70c412fa294ebfd5163082953c339dab86) (`develop`, commit `6b37fc70c412fa294ebfd5163082953c339dab86`, AGPL-3.0) keeps a lead's conversion serialized with a row lock, checks for an existing deal, and distinguishes returning an existing result from failing. It also records status changes and owner assignment.
- [Celery task documentation](https://github.com/celery/celery/blob/ba163551795e547254de52c0e7947f8a15ebe25f/docs/userguide/tasks.rst) (source BSD-3-Clause; documentation CC BY-SA 4.0) explains that late acknowledgements can redeliver work after a worker crash, so tasks must be idempotent and retry/requeue must be distinguished.
- [Temporal Python SDK](https://github.com/temporalio/sdk-python/tree/5bb461265e6c1af29c5f25dbb871a7e67448c61e) (MIT) uses stable workflow IDs, explicit signals/updates, activity timeouts, and deterministic history replay.

The project adopts the concepts, independently implemented in `trade_agent.db.runs`: stable `run_id`, row-locked worker claims, lease expiry, append-only event history, stable event keys, and business-layer idempotency. It does not copy source code or add Frappe, Celery, or Temporal as a runtime dependency. The current M2 boundary still needs an API/worker process in M3.

## Sales domain boundaries

- [ERPNext selling module](https://github.com/frappe/erpnext/tree/4cea6a7f839286b69dd30b38b0991e6ae234dac5/erpnext/selling) (GPLv3) documents a quotation lifecycle with draft/open/replied/ordered/lost/cancelled/expired states and a validity date.
- [Odoo CRM](https://github.com/odoo/odoo/tree/7b934fb3accaf8c61be475cda0329bd13d028af1/addons/crm) and [sales management](https://github.com/odoo/odoo/tree/7b934fb3accaf8c61be475cda0329bd13d028af1/addons/sale_management) are separate modules. Odoo's mail activities attach an assignee, due date, state, note, and optional next activity to a typed business record. The repository license is LGPLv3.
- [ERPNext CRM/selling README](https://github.com/frappe/erpnext/blob/4cea6a7f839286b69dd30b38b0991e6ae234dac5/erpnext/selling/README.md) separates lead/opportunity work from customer/campaign/quotation/sales-order work.

The design implication is to keep RFQ and quote revisions immutable and independent from future lead, customer, order, and activity aggregates. A later follow-up feature should use typed target IDs, assignees, due times with timezones, completion events, and stop conditions rather than adding reminders to the quote state machine.

## Review and export boundary

- [ERPNext quotation controller](https://github.com/frappe/erpnext/tree/4cea6a7f839286b69dd30b38b0991e6ae234dac5/erpnext/selling/doctype/quotation) keeps quotation status and submitted documents as server-side business records; the workflow distinguishes draft/open/replied/ordered/lost/cancelled/expired rather than treating a generated preview as an order.
- [Frappe file doctype](https://github.com/frappe/frappe/tree/develop/frappe/core/doctype/file) models files as private or public objects with an owning document, which is the relevant access-control boundary for downloads.

This project adopts the boundary, not the upstream implementation: a reviewer decision stores the exact quote revision and customer-visible `content_hash`; a renderer creates a private artifact only from that approved immutable snapshot; download checks organization ownership and ready status. The first artifact is a deterministic ReportLab PDF, and no approval endpoint sends email or mutates the quote revision. ERPNext's larger state machine and Frappe's file storage model are outside this first-version scope; the links are design evidence, not runtime dependencies.

### UI and private-file follow-up review

- [Frappe workflow.py](https://github.com/frappe/frappe/blob/592f3d7110f08dd8dd4f450cf790087b25f0a275/frappe/model/workflow.py), commit `592f3d7110f08dd8dd4f450cf790087b25f0a275` (MIT), checks read permission and valid role/condition transitions on the server, rejects self-approval, and records workflow comments. Its bulk approval path also limits batch size and returns per-item outcomes.
- [Frappe file.py](https://github.com/frappe/frappe/blob/592f3d7110f08dd8dd4f450cf790087b25f0a275/frappe/core/doctype/file/file.py), the same MIT commit, rechecks private-file permission on every download and validates safe paths rather than treating a file URL as authorization.
- [ERPNext workflow tree](https://github.com/frappe/erpnext/tree/3257b70f3f58dd7f337a239002334c500de6da76), commit `3257b70f3f58dd7f337a239002334c500de6da76` (GPL-3.0), confirms the product-boundary idea but is not a source-code dependency.

The workbench adopts the behavior only: server-side approval actions remain
authoritative, the UI shows why an action is disabled, and artifact downloads
continue to perform organization, approval, ready-status, and revision checks.
No Frappe or ERPNext source is copied into this MIT project. Odoo community
core was checked at commit `be94cbb53c6790fe57b1ad4bd846f58a7d0a0fa3`; its
approval application boundary is not treated as a stable reusable baseline.

### Inquiry SLA and translation-review expansion

- [ERPNext CRM Lead](https://github.com/frappe/erpnext/tree/14f44bbce58f7e43c8965e47fe33c2b7f109f57c/erpnext/crm/doctype/lead), commit `14f44bbce58f7e43c8965e47fe33c2b7f109f57c`, keeps qualification and lead status separate from quotation state. Its SLA records carry response and resolution deadlines, working-hour calendars, pause/hold time, and first-response timestamps.
- [Frappe Workflow Action](https://github.com/frappe/frappe/tree/592f3d7110f08dd8dd4f450cf790087b25f0a275/frappe/workflow/doctype/workflow_action), commit `592f3d7110f08dd8dd4f450cf790087b25f0a275`, provides a server-side action record tied to a reference document and actor. The same Frappe commit's Communication and Email Queue models preserve sent/received state, message IDs, retry status, and reference links.
- [Odoo mail activity](https://github.com/odoo/odoo/blob/ad0f6679718b2a18616d7ecd212c755d5dc172f9/addons/mail/models/mail_activity.py), commit `ad0f6679718b2a18616d7ecd212c755d5dc172f9`, treats follow-up as a typed target activity with assignee, deadline, timezone-aware user context, attachments, and computed overdue/today/planned/done state.

The selected design for this repository is a separate inquiry aggregate and an
append-only reply revision. Each reply stores the source and target languages,
source/revision hash, translation-review state, reviewer decision, and
idempotency key. Only a reviewer/admin can approve a translation, the author
cannot self-review, and a changed source hash resets the review. These are
independent of quote approval; the references provide behavior and field
boundaries only, not copied GPL/AGPL/LGPL code.

## License boundary

Only publicly described behavior and independently designed interfaces are used here. GPL, AGPL, and LGPL implementation code is not copied into this MIT project; links above are for traceability and future review.
