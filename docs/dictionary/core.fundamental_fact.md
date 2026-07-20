# `core.fundamental_fact`

The bitemporal core for the 6 tracked fundamental metrics. A restatement never updates a row - it closes the prior row's `knowledge_to` and inserts a successor with `supersedes` set.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `fact_id` | bigint | no |  |
| `entity_id` | integer | no |  |
| `metric_code` | text | no |  |
| `period_start` | date | yes | NULL for instant concepts (e.g. Assets, StockholdersEquity). |
| `period_end` | date | no |  |
| `fiscal_year` | integer | yes |  |
| `fiscal_period` | text | yes |  |
| `value` | numeric | no |  |
| `unit` | text | no |  |
| `source` | text | no |  |
| `vendor_native_tag` | text | yes | The actual XBRL tag used - visible when a filer switches tags mid-history. |
| `form_type` | text | yes |  |
| `accession_no` | text | yes |  |
| `filed_date` | date | no |  |
| `knowledge_from` | timestamp with time zone | no |  |
| `knowledge_to` | timestamp with time zone | no | `infinity` means this is the currently-believed-true value. |
| `supersedes` | bigint | yes | Points to the fact_id this row restates, if any. |
| `payload_id` | bigint | no |  |
| `ingested_at` | timestamp with time zone | no |  |
