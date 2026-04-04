# Booksend Reference

## Purpose

This reference keeps the `booksend` skill lean. Use it when you need the concrete runner behavior,
write guardrails, or submission-log semantics.

## Main Entrypoint

- `scripts/booksend.py`
  - loads `actions/<period>.yaml`
  - reads `actions/<period>.check.md`
  - dry-runs locally by default
  - executes approved write batches only with `--mode write --confirm-write`
  - translates repo draft schemas into live Simplbooks `create` payloads before execution
  - updates the action YAML in place with `executed_at`, `response_status`, `response_body`, and `inserted_id`
  - writes `submissions/<period>.json`

## Preconditions

Write mode currently requires all of the following:

- action batch period matches the requested period
- `approval_status` is `approved` or already `submitted`
- the check report contains `- Result: \`pass\``
- the check report `Batch ID` matches the current action batch
- the check report `Action file SHA256` matches the current action file contents
- explicit `--confirm-write`

Dry-run mode does not require the batch to be approved.

## Execution Model

- Actions execute in stable topological order using `depends_on`.
- Duplicate action IDs, missing dependencies, or dependency cycles stop the run.
- Current allowed endpoints are limited to:
  - `invoices/create`
  - `purchases/create`
  - `incomings/create`
  - `payments/create`
- Already successful actions are skipped on rerun.
- Default behavior stops on the first hard failure.
- `--continue-on-error` allows best-effort continuation.
- Legacy `/save` draft endpoints are normalized to the matching `/create` request only for backward-compatible batch loading.

## Submission Log Semantics

`submissions/<period>.json` currently records:

- batch metadata
- the current run mode
- an append-only `request_log`
- a manual-only `rollback_plan`
- summary counts for the current invocation

Each request-log entry includes:

- per-entry `mode`
- `action_idempotency_key`
- `sent_at`
- translated request method, endpoint, and payload
- HTTP status and parsed response body
- detected `inserted_id` when present
- success flag and stop-on-failure marker

## Rollback Plan

- Automatic rollback is intentionally unsupported.
- The batch-level rollback section is a manual reversal aid.
- Successful live actions appear in reverse dependency order.
- Each reversal candidate includes the original endpoint, inserted ID when known, and downstream actions
  that should be reversed first.

## Current Limits

- Dry-run validates request shape locally; it does not probe Simplbooks for a non-mutating server-side validation endpoint.
- Response success detection is heuristic because Simplbooks wrappers are inconsistent.
- Delete or reversal endpoints are not assumed from the public spec, so reversal suggestions remain manual.
- Credit-note submission currently requires a linked prior invoice action in the same batch or an already captured `inserted_id`.
