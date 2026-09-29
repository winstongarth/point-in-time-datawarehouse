# `ops.pipeline_run`

One row per pipeline invocation. Every `raw.payload` and `dq.check_result` row traces back to a `run_id` here.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `run_id` | bigint | no |  |
| `pipeline` | text | no |  |
| `started_at` | timestamp with time zone | no |  |
| `ended_at` | timestamp with time zone | yes |  |
| `status` | text | no |  |
| `rows_in` | bigint | yes |  |
| `rows_out` | bigint | yes |  |
| `error` | jsonb | yes |  |
