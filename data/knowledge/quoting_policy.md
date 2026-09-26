# Quoting Policy

Commercial rules for all quotations to external customers. Parameters marked (system) are the
defaults used by the quotation engine; deviations need approval by the sales manager.

## Pricing and margin

- Unit price = cost price / (1 − target margin). Target margin: 18 % of the selling price (system).
- Cost price = direct cost (material, machining, outsourced services, purchased and internal parts)
  + plant overhead 12 % + logistics and duty (DE 1 %, PL 3 %, CN 8 %, for delivery to Germany).
- Raw material is costed with 3 % scrap allowance on the stock weight (system).
- Margin range for approval by the quoting engineer: 12 – 25 %. Below 12 % requires sales manager approval.
- Strategic accounts or first orders from a new customer may be quoted at 15 % with sales approval.
- Prices are in EUR, net, excluding VAT. Default Incoterm: FCA plant; DAP customer site on request.

## Minimum order value

- Minimum order value: €500 per line item and delivery (system). If quantity × unit price is lower,
  the line total is set to €500 and the quote says so.
- Prototype and sample orders (≤ 5 pieces) are quoted with a flat engineering fee of €150 in addition.

## Quote validity

- Quotes are valid for 30 days from the issue date (system).
- Material prices for stainless steel and aluminium are indexed; if the market price changes by more
  than 10 % before order, the material portion may be adjusted.
- Lead times are confirmed at order entry against current capacity.

## Quantity tiers

- Always price every quantity requested by the customer. If only one quantity is given, add the
  next standard tier as an option.
- Standard tiers for proposals: 50, 200, 500, 1000, 2500 pieces.
- Setup time is spread over the batch quantity, so the unit price falls with quantity. A typical
  turned part costs 25 – 40 % less per piece at 500 than at 50 pieces.
- Call-off orders (blanket orders) are priced at the total annual quantity with a maximum of 4 call-offs
  per year; more call-offs are priced at the call-off batch size.

## RFQ completeness

An RFQ line can only be priced when the following are available:

| Required item | If missing | Severity |
|---|---|---|
| Drawing for the line item | ask the customer for the drawing | blocker |
| Material designation | ask for material and standard | blocker |
| Quantity (at least one) | ask for quantities or annual demand | blocker |
| General tolerance | quote on ISO 2768-m, state as assumption | warning |
| Requested delivery date | quote standard lead time | info |

- Blocking items stop the price calculation for that line only; other lines are still quoted.
- The clarification e-mail lists all open points in one message, in the customer's language.
- Special requirements (EN 10204 3.1 certificate, FAI, PPAP, special packaging) are listed as
  separate items or as assumptions if not priced.

## Assemblies

- Assemblies are quoted as one price per assembly, built from the parts list: purchased standard parts
  (catalog price), internal parts (historical cost of the known part number at the assembly plant, DE by
  default, closest batch size), assembly and functional test.
- Parts list items with a new part number that is neither in the standard parts catalog nor in our
  part master and has no drawing attached are listed as "price on request". The rest of the assembly
  is quoted normally; the missing drawing is requested from the customer in the same message.
- A quote with price-on-request items must state clearly that the assembly price is incomplete.
- Customer-supplied parts (free issue) are listed with zero price and the assumption "supplied by customer".
- Assembly quotes use the plant that builds the assembly and produces its make-parts (DE by default);
  a make-part without cost history at that plant is flagged for engineering review.

## Part reuse

When a requested part is nearly identical to a part we have already produced, the existing routing,
inspection plan and CNC programme are reused.

- Criteria: similarity score ≥ 0.92, same material and main dimensions within 5 %.
- The reference part's routing and inspection plan are proposed; the quoting engineer checks the
  drawing differences only (e.g. a changed hole pattern or length).
- Setup cost may be reduced by up to 50 % because programmes and fixtures exist.
- Reuse is a hint, not an automatic price: the new part is still costed from its own drawing.
- Refer to the reference part number in the internal notes, never in the customer quote.

## Price sanity and approval

- If the calculated price deviates by more than 30 % from the scaled price of the most similar
  historical part, the quote needs engineer review before approval.
- Every quote is approved by a named engineer; the approval is logged with date and time.
- Changes to cycle times, operations or margin during review are recorded as feedback and used to
  recalibrate the cost rules.
