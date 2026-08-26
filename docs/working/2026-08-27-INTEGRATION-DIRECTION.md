# Direction: replace file drops with interfaces

## Why

Every month currently begins with a human downloading exports and placing them in
a source pack. That step is the least reliable part of the run, and FY2026 has
already shown both of its failure modes inside a single week:

- **Format drift.** Stripe's export changed from Title Case columns
  (`Created date (UTC)`, `Amount`, `Fee`) to snake_case
  (`created_utc`, `gross`, `fee`, `net`, `reporting_category`).
  `parse_stripe_balance_csv` recognises two shapes and not the third, so
  `bookprep` fails with `KeyError: 'Created (UTC)'`. Nothing changed in this
  repository; the vendor changed under it.
- **Silent omission.** `kontovv_2026.csv` was not in the drop. The pack looked
  complete — a bank PDF and a CAMT XML were present — but the CSV is the
  canonical ledger evidence, and without it the entire cash side of every 2026
  month has nothing to work from.

A file drop cannot detect either. An interface can.

The business is also changing shape: PayPal ends after this period, Woo is being
replaced by Medusa, and Quartermaster may cease to be a vendor. Rebuilding
parsers for departing sources would be wasted work, so the direction below
prioritises what is staying.

## Target shape

| Source | Today | Direction |
| --- | --- | --- |
| Bank | manual CSV/XML/PDF export | statement auto-imported into Simplbooks; bank API also possible |
| Stripe | CSV export | pulled from the Stripe API |
| PayPal | CSV export | **retiring** — this is the last period |
| Woo | CSV export | **retiring** — replaced by Medusa |
| Medusa | — | API into the pipeline, or Medusa ↔ Simplbooks directly |
| Quartermaster | CSV/XLS + monthly PDFs | unchanged; vendor may depart |

## Stripe — proven, do this first

The Stripe API works with the key in `.apikey-stripe`, and returns **more** than
the CSV. A read-only probe of `/v1/balance_transactions` for 2026 returned
charges, refunds *and* payouts; the CSV export held five rows, all `charge`.

```text
txn_3U7YjR…  refund   gross  -39.68  fee 0.00  net  -39.68  eur
txn_3U7YjR…  charge   gross   39.68  fee 0.85  net   38.83  eur
txn_1U09k3…  payout   gross  -35.56  fee 0.00  net  -35.56  eur
```

Why this is the right first move:

- **It removes format drift as a class.** The API contract is versioned; a CSV
  export layout is not.
- **It is more complete.** Payouts and refunds arrive in the same typed ledger
  rather than as separate exports that must agree.
- **Amounts are integer minor units.** No locale parsing, no rounding ambiguity.
- **It is period-addressable.** `created[gte]`/`created[lte]` fetches exactly the
  month being run, instead of slicing an annual file.

Shape: a `scripts/stripe_api.py` alongside `scripts/simplbooks_api.py`, reading
`.apikey-stripe` the way the Simplbooks client reads `.apikey`, writing a
normalized artifact into the source pack so the evidence chain and its hashes are
unchanged downstream. **Read-only.** No Stripe write path should exist.

Open question: whether the fetched payload lands in `source/<year>-pack/` as
captured evidence, or whether `bookprep` calls the API directly. Captured
evidence is preferred — it keeps `source/` the durable record and keeps a rerun
reproducible after the fact, which direct calls would not.

## Bank — highest value, least in our control

Auto-importing the statement into Simplbooks removes the omission failure mode
entirely: nothing is missing because nothing is fetched by hand.

Note what it does **not** change. Imported rows are not auto-matched, so they stay
invisible in the ledger until matched in the GUI, and matching remains the human
step it is today. The statement-import plan
(`artifacts/statement-import/<year>-plan.md`) remains the worklist.

Two routes, not mutually exclusive:

1. **Bank → Simplbooks import**, if the bank and Simplbooks support a direct feed.
   Lowest effort, no code here.
2. **Bank API → this repository**, producing the canonical statement artifact the
   pack expects. More work, but it also fixes coverage detection: a fetched
   statement can assert its own date range instead of relying on a filename.

Prefer 1 where available. Reach for 2 only if the feed cannot be established, or
if the repository needs the statement independently for completeness checking.

## Medusa — design when the migration is real

Two shapes, and the choice matters:

- **Medusa → Simplbooks directly.** Orders become Simplbooks documents without
  passing through this repository. Least code, but it moves the sales basis
  outside the evidence chain — the reconciliation and completeness guarantees in
  `docs/current/bookkeeping-run.md` would no longer cover sales.
- **Medusa API → this repository.** Orders arrive as normalized records, exactly
  where Woo sits today, and every existing guarantee continues to apply.

The second is preferred unless Medusa's own accounting integration is
demonstrably complete, because the first quietly reduces what the pipeline can
check. Decide with the migration in hand, not before.

## Quartermaster

Leave it. CSV/XLS plus monthly PDFs works, and the vendor may depart. Do not
invest in it.

## Sequence

1. **Stripe API client** — proven feasible, removes an active blocker, and the
   work is contained.
2. **Bank auto-import** — highest value; start by establishing whether the direct
   bank→Simplbooks feed exists, since that decides whether any code is needed.
3. **Medusa** — design when the migration date is known.
4. **PayPal and Woo** — no work. They are leaving; carry them on the current
   parsers until they stop appearing.

## Constraints that do not change

- `source/` stays the durable evidence record. Fetched data is captured there;
  `bookprep` does not reach past it. `temp/` remains disposable.
- Every new credential is a file matched by `.apikey*` and never committed. The
  repository is public.
- All new clients are read-only. Simplbooks remains the only system this
  repository writes to, and only through an approved action batch.
- Fetching changes where evidence comes from, not what the pipeline guarantees
  about it. The gates in `docs/current/bookkeeping-run.md` continue to apply
  unchanged.

## Interim

Until the Stripe client exists, 2026 needs either a Stripe re-export in the
charges format (`Created date (UTC)`, `Amount`, `Amount Refunded`, `Fee`), or a
third branch in `parse_stripe_balance_csv` for the snake_case layout. The new
format carries everything the parser needs, so the branch is viable — but with
five rows in a year, a re-export is cheaper if Stripe still offers one.
