# `core.price_fact`

The bitemporal core for daily prices. Multiple `source`s (yfinance, tiingo) may hold independent, simultaneously-valid rows for the same entity/date - CLAUDE.md's M6 cross-vendor check reconciles them, this table doesn't merge them itself.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `fact_id` | bigint | no |  |
| `entity_id` | integer | no |  |
| `trade_date` | date | no |  |
| `open` | numeric | yes |  |
| `high` | numeric | yes |  |
| `low` | numeric | yes |  |
| `close` | numeric | yes |  |
| `volume` | bigint | yes |  |
| `adj_close` | numeric | yes | Diverges from `close` after a split/dividend - the mechanism for detecting retroactive adjustment. |
| `source` | text | no |  |
| `knowledge_from` | timestamp with time zone | no |  |
| `knowledge_to` | timestamp with time zone | no | `infinity` means this is the currently-believed-true value. |
| `payload_id` | bigint | no |  |
