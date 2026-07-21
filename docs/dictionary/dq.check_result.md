# `dq.check_result`

One row per check per run, including passes - a check that only records failures cannot support a coverage metric.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `check_id` | bigint | no |  |
| `check_name` | text | no |  |
| `dataset` | text | no |  |
| `run_id` | bigint | no |  |
| `severity` | text | no |  |
| `status` | text | no |  |
| `observed` | jsonb | yes | Actual values the check computed. |
| `expected` | jsonb | yes | The threshold/shape the check compared against. |
| `evaluated_at` | timestamp with time zone | no |  |
