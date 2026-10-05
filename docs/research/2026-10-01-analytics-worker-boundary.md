# Read-only analytics and durable worker evidence

Reviewed 2026-10-01. This note records design evidence used for M3-05 and
M3-06; it is not a dependency decision.

## Worker pattern

- [Celery task acknowledgement documentation](https://github.com/celery/celery/blob/ba163551795e547254de52c0e7947f8a15ebe25f/docs/userguide/tasks.rst), repository
  commit `ba163551795e547254de52c0e7947f8a15ebe25f`, explains that late
  acknowledgements can redeliver a task after a worker crash. A handler must
  therefore make its business side effects idempotent, and retry/requeue must
  be observable rather than hidden in a process queue.
- [Frappe CRM lead conversion](https://github.com/frappe/crm/tree/6b37fc70c412fa294ebfd5163082953c339dab86)
  keeps conversion serialized and returns an existing result when the same
  business object was already converted.

The implementation consequence is a database-backed `RunWorker`: it claims a
row with the existing lock and fencing token, calls the handler outside the
claim transaction, renews the lease explicitly, and requeues expired rows.
The worker does not introduce Celery, Redis, or an in-memory queue. Quote and
revision side effects continue to use the stable business idempotency keys
already enforced by `trade_agent.db.revisions`.

## Analytics query pattern

Analytics uses the same least-privilege principle as reporting systems such as
[Apache Superset's database connections](https://github.com/apache/superset/tree/5.0.0/superset/connectors):
the reporting connection should see only the tables or views required by its
queries. The trade implementation is stricter: it exposes three security
barrier views, excludes payload/cost fields, requires an organization filter,
and renders only static SELECT statements from an enum-based `QueryPlan`.

The upstream `DataAgentFlow` remains an optional conversational classifier,
but `ReadOnlyDataAgentFlowAdapter` maps recognized questions to a hand-authored
plan. It does not pass generated SQL or the upstream local Python
visualization executor into the trade database. Fake mode returns synthetic
rows with `mode=fake`, `source=fake_fixture`, and `is_synthetic=true`; real mode
returns `mode=real` only after executing against the authorized views.
