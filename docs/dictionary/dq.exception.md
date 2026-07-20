# `dq.exception`

The triage lifecycle (open -> triage -> closed) for a recurring failure, identified by `check_name` (via `check_id`) + `dimension_key` - separate from `check_result`'s per-run log.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `exception_id` | bigint | no |  |
| `check_id` | bigint | no |  |
| `entity_id` | integer | yes |  |
| `dimension_key` | text | no | Groups repeated failures of the same issue across runs (e.g. a ticker, or ticker+period). |
| `severity` | text | no |  |
| `status` | text | no | open -> triage (human-acknowledged) -> closed (manually or auto-resolved on a later pass). |
| `opened_at` | timestamp with time zone | no |  |
| `closed_at` | timestamp with time zone | yes |  |
| `resolution_note` | text | yes |  |
