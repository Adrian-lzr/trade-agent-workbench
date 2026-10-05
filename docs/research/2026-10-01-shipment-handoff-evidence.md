# Shipment Handoff and Freight Evidence Research

Reviewed 2026-10-01. The sources describe common controls, not universal legal
requirements. No GPL/LGPL implementation code was copied into this MIT project.

## Official workflow evidence

- [ITA Shipping Basics](https://beta.trade.gov/article?id=Freight-Forwarders)
  recommends comparing forwarder cost/service, assigning freight/insurance/
  duties/customs responsibilities through Incoterms, and checking packaging,
  labels, insurance, booking, and export/import documents before shipment.
- [ITA Common Export Documents](https://www.trade.gov/common-export-documents)
  distinguishes the pro forma, commercial invoice, and packing list; customs
  compares descriptions, quantities, weights, marks, and dimensions.

## GitHub design references

- [ERPNext Shipment](https://github.com/frappe/erpnext/tree/a64466561f6df2cfc513fedea0490ba6e5782410/erpnext/stock/doctype/shipment),
  commit `a64466561f6df2cfc513fedea0490ba6e5782410` (2026-06-13), GPL-3.0:
  shipment party/address, parcel rows, value/weight, carrier/booking/tracking,
  Incoterm, and Draft/Submitted/Booked/Completed/Cancelled boundaries.
- [Odoo stock.picking](https://github.com/odoo/odoo/blob/e8429c4efc0f71e897f86255be3053e71f1d78fd/addons/stock/models/stock_picking.py),
  commit `e8429c4efc0f71e897f86255be3053e71f1d78fd` (2026-09-17), LGPLv3:
  computed read-only state, availability/deadline blockers, scheduled and
  completed dates, package counts, and shipping weights.

The source projects are design references only. Their permissions and legal
assumptions are not copied; this project keeps writes behind its own org and
reviewer/admin identity boundary.

## Selected minimum slice

Create an org-scoped `shipment_handoff` aggregate linked to an approved quote
revision and exact content hash. Keep append-only evidence/checklist records for
freight quote, insurance (scope and expiry), packing/labels, export/import
documents, and bill of lading/AWB. A deterministic gate should block
`ready_for_booking` when required evidence, document consistency, or screening
clearance is unresolved. Use explicit status transitions, idempotency, and an
audited reviewer/admin override with a reason; do not let a model infer a
destination-country legal requirement.
