# Export Workflow Evidence

Reviewed 2026-10-01. The sources describe common export practices; they do not establish a universal legal rule for every country, product, or contract.

## Sources and observed facts

- [U.S. International Trade Administration: Common Export Documents](https://www.trade.gov/common-export-documents) describes the pro forma invoice as a negotiation/start-of-quotation document, the commercial invoice as a legal/customs document, and the packing list as a separate shipment document used to check packages, weights, marks, and dimensions. It notes that some destinations require certificates of origin or other special documents.
- [ITA: Company and Partner Risk](https://www.trade.gov/company-and-partner-risk) recommends vetting foreign entities before a transaction because financial, legal, and reputation risk can result from inadequate due diligence.
- [ITA: Consolidated Screening List](https://www.trade.gov/consolidated-screening-list) provides a consolidated search across government screening lists for parties subject to export, re-export, or transfer restrictions.
- [ITA: Methods of Payment](https://www.trade.gov/methods-payment) compares payment methods and explains the seller risk of open-account terms and consignments. Risk allocation depends on both buyer needs and seller protection, such as insurance or guarantees.
- [HubSpot: Create and edit sequences](https://knowledge.hubspot.com/sequences/create-and-edit-sequences) documents scheduled, targeted email steps, task reminders, and automatic sequence exit after a contact replies or books a meeting.
- [ITA: Shipping Basics](https://beta.trade.gov/article?id=Freight-Forwarders) describes comparing freight-forwarder cost/service, assigning freight/insurance/duty/customs responsibilities through Incoterms, checking packaging/labels/export-import documents before shipment, and routing bills of lading or special documents for the relevant payment method.
- [ITA: Direct Exporting](https://beta.trade.gov/article?id=Direct-Exporting) describes the additional people, time, and market/channel work required for direct export, and distinguishes overseas representatives and distributors. It calls out territory, exclusivity, commission, authority to commit the company, duration, termination, and dispute terms as contract concerns.
- [ITA: Responding to Inquiries](https://beta.trade.gov/article?id=Responding-to-Enquiries) describes the need for a system to process overseas inquiries, research the foreign company, preserve inquiry records, respond promptly and clearly with specifications/prices/terms, and treat the sender as a possible end customer, representative, or distributor. It also cautions that machine translation should not be sent directly in a commercial transaction.
- [ITA: After-Sales Service](https://beta.trade.gov/article?id=Providing-After-Sales-Service) describes delivery, manuals, service facilities, maintenance, repair/replacement, distributor support, and ongoing customer communication as part of export strategy; expectations vary by market and industrial buyers often evaluate service before buying.
- [ITA: Labeling](https://beta.trade.gov/article?id=Labeling) describes shipment marks such as shipper/consignee identity, origin, weight, package count, handling symbols, destination-language warnings, port, dangerous-goods marks, and composition where required. Buyer requirements and destination/product rules determine the exact checklist.

## Product implications

1. Keep immutable quote/customer-visible snapshots and derive pro forma invoice, commercial invoice, and packing-list fields from approved data. Cross-document mismatches should be a visible blocking issue.
2. Add a qualification evidence record with source, check time, reviewer, result, and attachments. An unresolved screening hit should block the relevant stage and require an authorized review.
3. Add payment method, term, deposit/balance conditions, insurance or guarantee evidence, and due-date tasks. Do not infer creditworthiness or payment promises from an email.
4. Add assigned, timezone-aware follow-up activities with manual-send approval and explicit stop-on-reply, stop-on-meeting, and stop-on-deal conditions.
5. Keep these capabilities behind M3/M5 gates until their API permissions, retention rules, and country/product-specific policies are specified.

## Additional workflow backlog from the shipping and channel evidence

6. Add a shipment handoff record with the selected Incoterm, cost/responsibility split, freight-forwarder quote revision, insurance scope and expiry, packing/label checks, required document checklist, booking reference, and ETA. A shipment gate should block when a required document or insurance check is unresolved; this is an operational control, not a claim about any specific country's law.
7. Add a channel record distinguishing direct sale, representative, and distributor. Store territory, exclusive/non-exclusive status, commission, authority limits, start/end dates, termination path, dispute forum, samples sent, trial-period outcome, and next review task. The approval boundary must prevent a representative from being treated as authorized to make commitments unless that authority is explicitly recorded.
8. Add inquiry handling fields for source channel, original language, human translation review, customer role, received timestamp/timezone, response SLA, versioned reply and attachments, and a nurture/overdue queue for inquiries that do not convert. Add an after-sales record for warranty/SLA, localized manuals/training, repair/replacement policy, distributor support, and post-delivery follow-up. These are workflow controls derived from the guidance, not mandatory universal deadlines or warranty law.
9. Add a destination/product-specific packing and labeling checklist with a versioned label proof and buyer approval evidence. The shipment gate should show missing marks and route exceptions to an authorized reviewer; it must not infer a universal destination rule from a model-generated answer.

Alibaba RFQ pages and search snippets were considered only as a platform-flow reference for fields such as MOQ, packaging, Incoterms, destination, and inspection. They are not treated as an authoritative legal or universal business specification.

The ITA sources above are U.S. government guidance and may be outdated or
country/product specific. They guide data fields, checklists, and audit
boundaries; they do not replace a customs broker, freight forwarder, legal
review, or destination-country requirements.
