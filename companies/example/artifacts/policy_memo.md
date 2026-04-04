# Policy Memo

## Scope

- Company: Example Company OÜ
- Basis: synthetic public example for the repo
- Covered workflow: recurring e-commerce month handling

## Posting Policy

- WooCommerce is the posting basis for recurring online sales invoices.
- Stripe and PayPal exports are settlement evidence, fee evidence, and bank-matching evidence.
- Bank transactions are expected to be imported into Simplbooks before this repo's monthly draft flow is reviewed.
- One-off purchase invoices may be entered manually or created by the skills-flow.
- If a one-off purchase already exists in Simplbooks, the flow should skip duplicate creation and attach later payment evidence when possible.
- One-off sales invoices are created directly in Simplbooks and are outside the recurring Woo sales flow.
- Printful purchase costs belong on the purchase side and may create both purchase and payment drafts when matching bank debits are present.

## Current Boundaries

- Provider-balance adjustments are manual and are not modeled as recurring invoice evidence.
- The publishable example is synthetic and intended to explain the artifact flow, not to prescribe one universal chart-of-accounts setup.
