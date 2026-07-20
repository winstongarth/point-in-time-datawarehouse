DROP TRIGGER IF EXISTS payload_append_only ON raw.payload;
DROP FUNCTION IF EXISTS raw.forbid_payload_mutation();
DROP TABLE IF EXISTS raw.payload;
