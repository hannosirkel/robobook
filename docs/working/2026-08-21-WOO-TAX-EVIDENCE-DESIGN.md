# Woo Tax Evidence and Fixed-Gross VAT Design

## Status

Approved design for implementation.

This specification is generic and uses `Example Company OÜ`. Company-specific allocations,
rates selected under reviewed tax policy, product names, quantities, and account decisions must
remain inside the ignored `companies/<company>/` workspace.

## Problem

WooCommerce tax-summary exports can contain annual rows grouped by tax code and configured rate,
with separate order-tax and shipping-tax amounts. They do not contain order IDs or dates. The
existing `bookprep` flow does not recognize this format and therefore manifests it as an
unsupported structured source.

The export is supporting tax evidence, not a second sales source. It must not create revenue or
duplicate processor/Woo sales. It must instead support an auditable allocation of taxable orders
and a fixed-gross VAT recalculation when the shop's configured rate was wrong.

Not every Woo order is necessarily taxable. In particular, an annual tax export can describe
only an EU-taxable subset while other orders in the same year remain zero-rated exports. The
implementation must never apply VAT to every Woo order merely because a tax-summary file exists.

## Selected Approach

Use two explicit stages:

1. Parse and validate the Woo tax-summary CSV as supporting evidence.
2. Build a reviewed annual allocation that links every reported taxable order to a monthly sale,
   then apply the reviewed effective VAT policy while preserving customer-paid gross.

This is preferred to heuristic-only matching or hand-edited action batches. The annual allocation
creates an auditable boundary between source facts and accounting policy and remains deterministic
on reruns.

## Source Detection and Parsing

Add the normalized header set:

- `Tax code`
- `Rate`
- `Total tax`
- `Order tax`
- `Shipping tax`
- `Orders`

A CSV containing all these headers is a Woo source and uses a dedicated
`parse_woo_tax_summary_csv` parser. Detection must be header-based, so spelling in the filename is
not relied upon.

The parser must:

- accept UTF-8 with or without a BOM;
- parse the country prefix from tax codes such as `NL-NL-VAT-1`;
- preserve the complete tax code;
- require a non-negative configured rate and non-negative tax amounts;
- require a positive integral order count;
- verify `Total tax = Order tax + Shipping tax` to the cent;
- preserve the CSV row reference and source hash through the normal manifest;
- infer annual coverage from a year-bearing filename or parent source-pack directory;
- emit structured supporting records without emitting sales, fees, or journal adjustments;
- emit a blocking exception for invalid arithmetic, malformed tax codes, or missing annual
  coverage.

Supporting records use the existing `other` category with event type `woo_tax_summary`. They are
dated at the covered year's final day for artifact compatibility, but their attributes state that
they are annual evidence. They are emitted only in the period containing that date, preventing the
same evidence from being duplicated in all twelve normalized months.

## Annual Allocation Artifact

Add a shared schema and template for:

`companies/<company>/artifacts/vat/<year>-woo-tax-allocation.json`

The artifact contains:

- year and company slug;
- source-file IDs and hashes;
- reviewed policy facts, including OSS use, dispatch-origin treatment, fixed-gross treatment, and
  effective VAT-rate periods;
- one allocation per taxable order, including order ID, event date, country, processor reference,
  configured rate, corrected rate, fixed product gross, fixed shipping gross, corrected product
  VAT, corrected shipping VAT, and source references;
- monthly totals;
- completeness checks tying allocated order counts and original tax components back to every tax
  summary row.

The allocation builder must use order identifiers and geography available from Woo, Stripe,
PayPal, and fulfillment records. A match may be automatic only when unique. Ambiguous or unmatched
rows are blocking and require an explicit company-local reviewed mapping. No country or order may
be guessed from a person's name.

Orders absent from the tax summary remain non-taxable unless another reviewed source explicitly
classifies them as taxable. This rule protects non-EU sales in mixed years.

The full-year runner builds or validates the annual allocation before running monthly
normalization. Monthly `bookprep` reads the validated artifact for the target year and applies only
allocations belonging to its period.

## Fixed-Gross VAT Calculation

Customer-paid gross is immutable. If a configured VAT rate was wrong, the merchant absorbs the
difference.

For each component:

```text
corrected VAT = fixed component gross * corrected rate / (100 + corrected rate)
corrected net = fixed component gross - corrected VAT
```

Round VAT to cents per order component using decimal half-up rounding. Product and shipping are
calculated independently, and their rounded values must reconcile to the order gross.

Where the source provides VAT-exclusive product or shipping values plus configured tax, fixed
component gross is their sum. Where one component must be derived, the derivation is allowed only
when the processor gross and the other observed component reconcile exactly to the cent.

Monthly normalized sales preserve total gross and receive corrected VAT plus explicit product and
shipping component gross/VAT attributes. `bookbuilder` uses those attributes to produce separate
goods and shipping lines without assigning all VAT to the goods line.

## Effective VAT Mapping

Posting policy must support effective-dated sales VAT profiles rather than one static VAT type.
Each profile maps:

- start and optional end date;
- goods VAT type ID;
- shipping/service VAT type ID;
- expected percentage.

The builder selects the profile by accounting date. The checker verifies that the chosen VAT type,
line percentage, and effective date agree. Zero-rated exports continue to use reviewed zero-rate
goods and service mappings.

The applicable legal regime and rates are company-local reviewed policy inputs. The generic code
does not infer OSS registration or dispatch-origin treatment from submitted tax returns.

## Manual Inventory Write-Off Decisions

The public Simplbooks API exposes dated inventory remnants but no inventory-write-off endpoint.
Therefore a reviewed inventory write-off is represented as a company-local manual action containing:

- effective date;
- article ID;
- warehouse ID;
- quantity;
- expense/change-in-inventory account ID;
- reason and approval evidence.

`booksend` must never translate this manual action into an unrelated invoice or purchase call. The
pre-submit checker reports it as required manual work. After it is entered in Simplbooks, a
read-only dated remnant query must confirm the expected quantity before year-close readiness can
pass.

## Failure Handling

Processing blocks when:

- tax-summary arithmetic is invalid;
- a tax row lacks a usable country code or annual coverage;
- allocated counts do not equal source `Orders` counts;
- original allocated product/shipping taxes do not equal the source row totals;
- a taxable order is allocated more than once;
- a fixed-gross calculation changes processor/customer gross;
- a date has no applicable VAT profile;
- a selected VAT type's expected percentage disagrees with the calculated line rate;
- a required manual inventory action lacks completion evidence.

Warnings are insufficient for these conditions because they affect tax or inventory balances.

## Testing

Money-sensitive behavior is test-backed before implementation:

- parser detection by exact header set and BOM handling;
- annual coverage inference from the source-pack directory;
- successful row parsing and source references;
- blocking malformed codes, fractional order counts, and tax-component mismatches;
- allocation completeness and duplicate prevention;
- mixed taxable and zero-rated orders in one year;
- a year where all observed Woo orders are taxable;
- fixed-gross recalculation and component rounding;
- a mid-year VAT-rate transition;
- preservation of monthly and annual customer gross;
- goods and shipping line VAT allocation;
- effective-dated VAT type validation;
- manual inventory action rejection by `booksend` and remnant-based completion checking;
- full-year dry-run acceptance with the annual allocation prerequisite.

## Privacy and Audit Trail

Generic schemas, templates, scripts, and tests may be committed. Real order allocations, company
tax-policy decisions, inventory decisions, API results, and derived totals remain under the ignored
company workspace. Every derived value retains source references and policy provenance.
