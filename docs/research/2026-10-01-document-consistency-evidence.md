# Export Document Consistency Research

Reviewed 2026-10-01. These sources guide fields and controls; they are not
country-specific legal advice and no source code was copied into this MIT
project.

## Official workflow evidence

- [ITA Common Export Documents](https://www.trade.gov/common-export-documents)
  describes the pro forma invoice as the negotiation/quotation starting point,
  the commercial invoice as a legal/customs document, and the packing list as
  an independent package, weight, marks, and dimensions record.
- [ITA Company and Partner Risk](https://www.trade.gov/company-and-partner-risk)
  and the [Consolidated Screening List](https://www.trade.gov/consolidated-screening-list)
  support preserving buyer due-diligence evidence and treating unresolved
  screening results as a transaction blocker.
- [ITA Shipping Basics](https://beta.trade.gov/article?id=Freight-Forwarders)
  says to check packaging, labels, insurance, and export/import documents before
  shipment, with Incoterms assigning freight, insurance, customs, and document
  responsibilities.

The minimum deterministic comparison set is buyer/consignee, document reference,
SKU/description, quantity/UOM, currency and amounts, Incoterm/place, package
count/marks, and net/gross weight with a consistent unit.

## GitHub design references

- [ERPNext Sales Invoice](https://github.com/frappe/erpnext/blob/6b30cef7ac853d1cefb65428dbb59da4d4341b78/erpnext/accounts/doctype/sales_invoice/sales_invoice.py),
  commit `6b30cef7ac853d1cefb65428dbb59da4d4341b78` (2026-09-26), GPL-3.0:
  server-side validation and a submitted/posted document boundary.
- [ERPNext Packing Slip](https://github.com/frappe/erpnext/blob/11da80c9c5429b20a098dae8111e00ab4c1b95b3/erpnext/stock/doctype/packing_slip/packing_slip.py),
  commit `11da80c9c5429b20a098dae8111e00ab4c1b95b3` (2026-06-25), GPL-3.0:
  package rows, weight UOM consistency, and quantity not exceeding the source
  delivery quantity.
- [Odoo account.move](https://github.com/odoo/odoo/blob/86707bc0a6d67150e2106b84b9ae6cf6d4fe5e87/addons/account/models/account_move.py),
  commit `86707bc0a6d67150e2106b84b9ae6cf6d4fe5e87` (2026-09-29), LGPLv3:
  draft/posted/cancelled state and an immutable posted invoice boundary.
- [Odoo stock.picking](https://github.com/odoo/odoo/blob/e8429c4efc0f71e897f86255be3053e71f1d78fd/addons/stock/models/stock_picking.py),
  commit `e8429c4efc0f71e897f86255be3053e71f1d78fd` (2026-09-17), LGPLv3:
  explicit readiness/deadline/completion states and shipment weights.

The repository licenses are recorded for traceability only. GPL/LGPL source is
not a runtime dependency and is not copied into this MIT derivative.

## Selected boundary

Keep the existing approved `QuoteRevision` and private pro forma artifact
immutable. Add an independent document-set aggregate for commercial invoice and
packing-list snapshots, with deterministic field-level mismatch codes. A
commercial invoice or packing list can be issued only after it validates against
the exact approved quote hash; corrections create a new revision or void the old
one. Do not model these documents as extra `quote_artifacts`, because package and
weight facts are shipment-specific and need their own audit boundary.
