# Discovery Findings

## Scope

- Company: Example Company OÜ
- Year: 2024
- Discovery overview: `companies/example/artifacts/discovery/2024-overview.json`

## Confirmed Patterns

- The example company uses WooCommerce as the recurring invoice basis.
- Stripe and PayPal operate as settlement layers rather than separate revenue ledgers.
- Processor fees and Printful costs are represented on the purchase side.

## Suspicious Patterns

- None. This is a synthetic worked example with one clean month.

## Data Quality Concerns

- The file is illustrative, not live-discovered.
- Real companies may need more complex mappings for VAT, warehouses, and prior-period continuity.

## Implications For Later Skills

- `bookprep`: preserve Woo as the sales posting basis while retaining Stripe and PayPal as settlement evidence.
- `bookrecon`: require Woo, processor, Printful, and bank evidence before approving the example month for build.
- `bookbuilder`: emit one monthly invoice summary, processor fee purchases, one Printful purchase, and matching cash-settlement drafts.
- `bookaudit`: compare source-derived totals to the final month state instead of trusting draft actions alone.
