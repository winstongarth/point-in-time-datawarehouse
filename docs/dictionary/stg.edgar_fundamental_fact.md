# `stg.edgar_fundamental_fact`

Parsed EDGAR XBRL datapoints with the metric map already applied. Truncated and rebuilt on every `pdw parse` run; not deduplicated and has no constraints beyond column types by design (CLAUDE.md 5).

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `cik` | character | yes |  |
| `entity_name` | text | yes |  |
| `metric_code` | text | yes |  |
| `period_start` | date | yes |  |
| `period_end` | date | yes |  |
| `fiscal_year` | integer | yes |  |
| `fiscal_period` | text | yes |  |
| `value` | numeric | yes |  |
| `unit` | text | yes |  |
| `vendor_native_tag` | text | yes |  |
| `form_type` | text | yes |  |
| `accession_no` | text | yes |  |
| `filed_date` | date | yes |  |
| `payload_id` | bigint | yes |  |
