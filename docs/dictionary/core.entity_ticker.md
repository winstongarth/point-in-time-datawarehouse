# `core.entity_ticker`

Bitemporal ticker->entity mapping. A brand-new entity's first mapping is backdated to a fixed sentinel (2000-01-01 UTC), not the ingestion date - SEC's ticker map is current-state-only, so there is no true historical assignment date to recover regardless. A genuine reassignment, once detected, opens at real detection time.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `entity_id` | integer | no |  |
| `ticker` | text | no |  |
| `knowledge_from` | timestamp with time zone | no | See table note - not always a true historical date. |
| `knowledge_to` | timestamp with time zone | no | `infinity` means this ticker is the current mapping for the entity. |
