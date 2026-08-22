# Task 1 Report: Reviewed Bank-Allocation Contract

## Initial implementation summary

Implemented the generic annual bank-allocation contract for source-bound, reviewed physical-bank decisions.

- Added `bank-allocation.schema.json` and a safe example template.
- Added strict loading, statement identity resolution, period indexing, cent-precise amount handling, normalized-input hash binding, and rebind support in `scripts/bank_allocations.py`.
- Statement identity resolution is ordered archive identifier, account-servicer reference, entry reference, then a deterministic economic composite.
- The loader requires approved review metadata, current record locators, matching period/currency/signed amount, unique identities, and exact normalized-year bindings.
- Rebinding proves the full statement-ID set and every date/currency/signed-amount tuple are unchanged, refreshes only bindings/locators, and marks allocations `needs_review` before the artifact can be loaded again.
- Added `bank_allocations` as an allowed action-batch reference-artifact kind.
- Added a report-only `requires_bank_allocation_binding()` helper for batches that reference physical bank rows. Per the controller ruling, it is not yet added to mandatory existing action-batch binding enforcement.

## Initial files

- Created `scripts/bank_allocations.py`
- Created `schemas/bank-allocation.schema.json`
- Created `templates/bank-allocation.template.json`
- Modified `schemas/action-batch.schema.json`
- Modified `scripts/reference_artifacts.py`
- Created `tests/test_bank_allocations.py`
- Modified `tests/test_schema_contracts.py`
- Modified `tests/test_reference_artifacts.py`

## Initial RED/GREEN evidence

Initial contract run (RED):

```text
python3 -m unittest tests.test_schema_contracts tests.test_bank_allocations -v
FAILED (errors=3)
```

The failures were the expected missing `schemas/bank-allocation.schema.json` and missing `bank_allocations` module.

Rebind economic-identity test (RED):

```text
python3 -m unittest tests.test_bank_allocations.BankAllocationTests.test_rebind_rejects_changed_statement_economics -v
FAILED (failures=1)
```

The test correctly showed that a rebind could proceed after a signed amount change before the economic-tuple guard was added.

Focused GREEN run:

```text
python3 -m unittest tests.test_schema_contracts tests.test_bank_allocations tests.test_reference_artifacts -v
Ran 25 tests
OK
```

## Full-suite evidence

```text
python3 -m unittest discover -s tests -v
Ran 250 tests in 0.525s
OK
```

Additional fresh checks:

```text
git diff --check
python3 -m py_compile scripts/bank_allocations.py scripts/reference_artifacts.py
```

Both completed with exit status 0.

## Initial self-review

- The contract rejects the `ignore` escape hatch and permits only the eight reviewed dispositions from the brief.
- Split allocations require non-empty components and cent-exact agreement with the signed statement amount.
- Allocation locators are checked against current normalized records; a row-number change is handled only through a deliberate rebind.
- Rebinding fails on changed statement sets or changed date/currency/signed amount tuples, and forces a new review.
- No real-company source or artifact data was added.
- The Phase-A controller constraint is preserved: detection exists but existing action-batch submission/check enforcement remains unchanged.

## Initial concerns

None. Later phases need to consume `requires_bank_allocation_binding()` when they introduce mandatory write-capable enforcement.

## Fix Round 1

### Fix Round 1 implementation summary

- Added `bank_allocation_coverage_errors()` and `prove_exact_bank_allocation_coverage()` for deterministic duplicate, missing, and extra physical statement-ID reporting. `load_bank_allocations()` remains deliberately partial-friendly for Phase A.
- Added `bank_ledger_key(record)` for normalized physical `(IBAN, currency)` identity. It requires `source_system == "bank"`, strips IBAN whitespace, uppercases the IBAN and currency, and rejects missing account identifiers.
- Limited bank records used by allocation loading, rebinding, and coverage proof to rows with exact `source_system == "bank"`; legacy wallet/processor rows in `bank_transactions` are ignored and cannot be allocated.
- Enforced annual scope for allocation periods, normalized input periods, and physical statement event dates.
- Tightened the JSON schema so normalized bindings require at least one item and allocation targets require at least one property. Empty `allocations` remains valid.

### Fix Round 1 files

- Modified `scripts/bank_allocations.py`
- Modified `schemas/bank-allocation.schema.json`
- Modified `tests/test_bank_allocations.py`
- Modified `tests/test_schema_contracts.py`

### Fix Round 1 RED/GREEN evidence

Initial review-fix RED run:

```text
python3 -m unittest tests.test_bank_allocations tests.test_schema_contracts -v
Ran 29 tests
FAILED (failures=2, errors=5)
```

The failures were the missing ledger/coverage helpers, missing annual scope enforcement, and schema acceptance of empty bindings/targets.

Exact-source-system RED run:

```text
python3 -m unittest tests.test_bank_allocations.BankAllocationTests.test_nonbank_rows_do_not_enter_allocation_or_completeness_proof -v
Ran 1 test
FAILED (failures=1)
```

The pre-fix implementation treated `source_system: "BANK"` as a physical statement row. The production filter was then made exact.

Focused GREEN run:

```text
python3 -m unittest tests.test_bank_allocations tests.test_schema_contracts tests.test_reference_artifacts -v
Ran 32 tests in 0.021s
OK
```

Full-suite GREEN run:

```text
python3 -m unittest discover -s tests -v
Ran 257 tests in 0.598s
OK
```

Fresh final checks:

```text
git diff --check
python3 -m py_compile scripts/bank_allocations.py
```

Both exited 0 before commit.

### Fix Round 1 self-review

- The exact coverage proof is separate from default loading, preserving the report-only Phase-A ability to represent incomplete review work.
- Its ordered errors are deterministic: duplicates, then missing IDs, then extra IDs, with each ID list sorted.
- Only canonical physical rows (`source_system == "bank"`) enter source matching or coverage; this prevents legacy wallet rows from masquerading as physical bank evidence.
- No balance representation was added to the allocation JSON; the `(IBAN, currency)` interface is intentionally limited to the helper required for Tasks 2–3.
- The annual checks cover allocation period, normalized input period, and matched statement date.
- The schema and strict loader now agree on nonempty normalized bindings and targets, while preserving a valid empty annual allocation list.

### Commit

`c5fab24 fix: harden bank allocation coverage contract`

### Fix Round 1 concerns

None. Task 3/6 must call the exact coverage proof when report/write enforcement is introduced.

## Fix Round 2

### Changed files

- `schemas/bank-allocation.schema.json`
- `tests/test_bank_allocations.py`
- `tests/test_schema_contracts.py`

### Covering tests

- `tests/test_schema_contracts.py::SchemaContractTests::test_bank_allocation_schema_requires_a_four_digit_year`
- `tests/test_bank_allocations.py::BankAllocationTests::test_loader_accepts_only_four_digit_years`

### Fix Round 2 RED/GREEN evidence

RED:

```text
python3 -m unittest tests.test_schema_contracts.SchemaContractTests.test_bank_allocation_schema_requires_a_four_digit_year tests.test_bank_allocations.BankAllocationTests.test_loader_accepts_only_four_digit_years -v
Ran 2 tests
FAILED (failures=2)
```

The schema incorrectly accepted years `999` and `10000`; the loader already rejected both.

Focused GREEN:

```text
python3 -m unittest tests.test_bank_allocations tests.test_schema_contracts -v
Ran 31 tests in 0.021s
OK
```

Full-suite GREEN:

```text
python3 -m unittest discover -s tests -v
Ran 259 tests in 0.547s
OK
```

### Fix Round 2 self-review

- Schema and loader now share the exact `1000..9999` integer year domain.
- Both layers are covered for all boundary values: `999`, `1000`, `9999`, and `10000`.
- The schema test validator now evaluates `maximum`, so the contract bound is actively tested rather than only source-inspected.
- No behavior beyond the year-domain alignment changed.

### Fix Round 2 commit

`07d9666 fix: align bank allocation year bounds`

### Fix Round 2 concerns

None.

## Private Integration Fix Round

### Private integration implementation summary

- Preserved `statement_identity(record)` exactly, while changing allocation identity to the canonical `(statement_id, normalized IBAN, uppercase currency)` tuple.
- Added required `iban` to allocation validation, the JSON schema, template fixture, and test fixtures. IBAN normalization removes all whitespace and uppercases the value.
- Applied the canonical key to normalized-row indexing, allocation uniqueness, current-row validation, exact coverage, rebinding, and period allocation indexing. A shared archive identifier can now represent distinct EUR/USD physical rows; only a duplicate full key is rejected.
- Updated physical-bank reconciliation to index and look up allocation values by that same key, so same-archive EUR/USD entries remain two physical rows. Balance scope, clearing, and action generation are unchanged.

### Files changed

- `schemas/bank-allocation.schema.json`
- `scripts/bank_allocations.py`
- `scripts/bookrecon.py`
- `templates/bank-allocation.template.json`
- `tests/test_bank_allocations.py`
- `tests/test_bookrecon.py`
- `tests/test_schema_contracts.py`

### Private integration RED/GREEN evidence

RED (before production changes):

```text
python3 -m unittest tests.test_schema_contracts.SchemaContractTests.test_bank_allocation_schema_requires_iban tests.test_bank_allocations.BankAllocationTests.test_same_archive_rows_in_distinct_currencies_load_and_prove_complete tests.test_bank_allocations.BankAllocationTests.test_duplicate_full_allocation_key_is_rejected tests.test_bank_allocations.BankAllocationTests.test_wrong_allocation_iban_is_rejected_by_loading_and_coverage tests.test_bank_allocations.BankAllocationTests.test_rebind_retains_same_archive_rows_for_each_currency tests.test_bank_allocations.BankAllocationTests.test_period_allocations_indexes_each_statement_once tests.test_bookrecon.BookreconTests.test_same_archive_rows_in_each_currency_are_two_exact_physical_rows -v
Ran 7 tests in 0.007s
FAILED (failures=5, errors=2)
```

The failures showed that `iban` was not required, normalized rows with the same archive identity were rejected, period allocations used statement ID alone, and reconciliation reported the paired rows as incomplete.

Focused GREEN:

```text
python3 -m unittest tests.test_bank_allocations tests.test_bookrecon tests.test_schema_contracts tests.test_reference_artifacts -v
Ran 65 tests in 0.035s
OK

python3 -m py_compile scripts/bank_allocations.py scripts/bookrecon.py
git diff --check
```

Both follow-up checks exited 0.

Required statement-ID-only mutation check (temporary lookup mutation):

```text
python3 -m unittest tests.test_bookrecon.BookreconTests.test_same_archive_rows_in_each_currency_are_two_exact_physical_rows -v
Ran 1 test in 0.001s
FAILED (failures=1)
```

The mutation changed `canonical_allocations.get(key)` to `canonical_allocations.get(statement_id)` and produced `warn` rather than the required `pass`. The lookup was restored before final verification.

Full-suite GREEN:

```text
python3 -m unittest discover -s tests -v
Ran 292 tests in 0.547s
OK

python3 -m py_compile scripts/bank_allocations.py scripts/bookrecon.py
git diff --check
```

Both final checks exited 0.

### Private integration self-review

- The immutable archive/account-servicer/entry/composite statement-identity ordering is unchanged.
- A wrong allocation IBAN is rejected during loading and exact-coverage proof reports the unmatched full key; matching statement ID, currency, and amount cannot bypass it.
- Rebinding compares and refreshes each full key independently, so unchanged same-archive EUR/USD rows retain both locators and get only the expected binding/locator/review refresh.
- The reconciliation regression test proves that two rows with the same archive ID count as two exact physical rows, and the required statement-ID-only mutation makes that test fail.
- No `companies/plepic` data was accessed or included.

### Private integration commit

`fix: key bank allocations by ledger and currency`

### Private integration concerns

None.
