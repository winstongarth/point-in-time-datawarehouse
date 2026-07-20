# `raw.payload`

Immutable landing zone. Every vendor response is stored verbatim; append-only, enforced by a `BEFORE UPDATE OR DELETE` trigger (invariant 6). Every `core` row traces back here via `payload_id`.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `payload_id` | bigint | no |  |
| `source` | text | no |  |
| `endpoint` | text | no |  |
| `request_params` | jsonb | no |  |
| `fetched_at` | timestamp with time zone | no |  |
| `http_status` | integer | no |  |
| `content_sha256` | character | no | Indexed with `source` - lets downstream steps detect an unchanged fetch. |
| `body` | bytea | no |  |
| `run_id` | bigint | no |  |
