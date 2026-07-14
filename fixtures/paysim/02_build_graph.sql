-- PaySim → PostgreSQL 19: build typed node/edge tables from staging, then the
-- property graph. Schema models the PaySim fraud graph (clients, transactions,
-- merchants, banks, and email/phone/SSN identity links).
--
-- Notes:
--  * Transaction id = globalstep (the edge CSVs' transaction_id references it).
--  * Labels are DOUBLE-QUOTED to preserve the naming convention (TitleCase node
--    labels, UPPER_SNAKE edge labels) — unquoted labels fold to lowercase in PG19.
--  * Base tables keep simple lowercase names; edges carry a synthetic bigserial
--    `rid` primary key (the SQL/PGQ edge KEY) so we don't depend on the natural
--    composite key being unique.
--  * The `timestamp` column is exposed as property `ts` to dodge the SQL keyword.
--  * No FKs: PG19 CREATE PROPERTY GRAPH only needs a unique referenced key and
--    tolerates dangling edges.

DROP PROPERTY GRAPH IF EXISTS paysim;
DROP TABLE IF EXISTS
    client, merchant, bank, txn, email, phonenumber, ssn,
    client_perform_transaction, transaction_to_client, transaction_to_merchant,
    transaction_to_bank, has_email, has_phonenumber, has_ssn CASCADE;

-- PG19 rule: a property of the same name must have the SAME type across every
-- label. `id` is exposed on all node labels, so all ids (and the edge columns
-- that reference them) are text — Client/Transaction ids are cast from bigint.

-- ---------- node tables ----------
CREATE TABLE client AS
    SELECT DISTINCT ON (id) id::text AS id, name, (lower(isfraud) = 'true') AS isfraud
    FROM stg_clients WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE client ADD PRIMARY KEY (id);

CREATE TABLE merchant AS
    SELECT DISTINCT ON (id) id, name, (lower(highrisk) = 'true') AS highrisk
    FROM stg_merchants WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE merchant ADD PRIMARY KEY (id);

CREATE TABLE bank AS
    SELECT DISTINCT ON (id) id, name FROM stg_banks WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE bank ADD PRIMARY KEY (id);

CREATE TABLE txn AS
    SELECT DISTINCT ON (globalstep)
        globalstep::text AS id, amount, ts::timestamp AS ts, action, globalstep,
        (lower(isfraud) = 'true') AS isfraud,
        (lower(isflaggedfraud) = 'true') AS isflaggedfraud,
        typedest, typeorig
    FROM stg_transactions WHERE globalstep IS NOT NULL ORDER BY globalstep;
ALTER TABLE txn ADD PRIMARY KEY (id);

CREATE TABLE email AS
    SELECT DISTINCT ON (id) id, name FROM stg_emails WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE email ADD PRIMARY KEY (id);

CREATE TABLE phonenumber AS
    SELECT DISTINCT ON (id) id, name FROM stg_phonenumbers WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE phonenumber ADD PRIMARY KEY (id);

CREATE TABLE ssn AS
    SELECT DISTINCT ON (id) id, name FROM stg_ssns WHERE id IS NOT NULL ORDER BY id;
ALTER TABLE ssn ADD PRIMARY KEY (id);

-- ---------- edge tables (synthetic rid PK) ----------
CREATE TABLE client_perform_transaction (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id text, transaction_id text, ts timestamp);
INSERT INTO client_perform_transaction (client_id, transaction_id, ts)
    SELECT client_id::text, transaction_id::text, ts::timestamp FROM stg_client_perform;

CREATE TABLE transaction_to_client (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id text, client_id text, ts timestamp);
INSERT INTO transaction_to_client (transaction_id, client_id, ts)
    SELECT transaction_id::text, client_id::text, ts::timestamp FROM stg_txn_client;

CREATE TABLE transaction_to_merchant (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id text, merchant_id text, ts timestamp);
INSERT INTO transaction_to_merchant (transaction_id, merchant_id, ts)
    SELECT transaction_id::text, merchant_id, ts::timestamp FROM stg_txn_merchant;

CREATE TABLE transaction_to_bank (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id text, bank_id text, ts timestamp);
INSERT INTO transaction_to_bank (transaction_id, bank_id, ts)
    SELECT transaction_id::text, bank_id, ts::timestamp FROM stg_txn_bank;

CREATE TABLE has_email (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id text, email_id text);
INSERT INTO has_email (client_id, email_id)
    SELECT client_id::text, email_id FROM stg_has_email;

CREATE TABLE has_phonenumber (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id text, phonenumber_id text);
INSERT INTO has_phonenumber (client_id, phonenumber_id)
    SELECT client_id::text, phonenumber_id FROM stg_has_phone;

CREATE TABLE has_ssn (
    rid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id text, ssn_id text);
INSERT INTO has_ssn (client_id, ssn_id)
    SELECT client_id::text, ssn_id FROM stg_has_ssn;

-- ---------- property graph ----------
CREATE PROPERTY GRAPH paysim
    VERTEX TABLES (
        client KEY (id) LABEL "Client" PROPERTIES (id, name, isfraud),
        merchant KEY (id) LABEL "Merchant" PROPERTIES (id, name, highrisk),
        bank KEY (id) LABEL "Bank" PROPERTIES (id, name),
        txn KEY (id) LABEL "Transaction"
            PROPERTIES (id, amount, ts, action, globalstep, isfraud, isflaggedfraud, typedest, typeorig),
        email KEY (id) LABEL "Email" PROPERTIES (id, name),
        phonenumber KEY (id) LABEL "PhoneNumber" PROPERTIES (id, name),
        ssn KEY (id) LABEL "SSN" PROPERTIES (id, name)
    )
    EDGE TABLES (
        client_perform_transaction KEY (rid)
            SOURCE KEY (client_id) REFERENCES client (id)
            DESTINATION KEY (transaction_id) REFERENCES txn (id)
            LABEL "PERFORMS" PROPERTIES (ts, client_id, transaction_id),
        transaction_to_client KEY (rid)
            SOURCE KEY (transaction_id) REFERENCES txn (id)
            DESTINATION KEY (client_id) REFERENCES client (id)
            LABEL "TO_CLIENT" PROPERTIES (ts, transaction_id, client_id),
        transaction_to_merchant KEY (rid)
            SOURCE KEY (transaction_id) REFERENCES txn (id)
            DESTINATION KEY (merchant_id) REFERENCES merchant (id)
            LABEL "TO_MERCHANT" PROPERTIES (ts, transaction_id, merchant_id),
        transaction_to_bank KEY (rid)
            SOURCE KEY (transaction_id) REFERENCES txn (id)
            DESTINATION KEY (bank_id) REFERENCES bank (id)
            LABEL "TO_BANK" PROPERTIES (ts, transaction_id, bank_id),
        has_email KEY (rid)
            SOURCE KEY (client_id) REFERENCES client (id)
            DESTINATION KEY (email_id) REFERENCES email (id)
            LABEL "HAS_EMAIL" PROPERTIES (client_id, email_id),
        has_phonenumber KEY (rid)
            SOURCE KEY (client_id) REFERENCES client (id)
            DESTINATION KEY (phonenumber_id) REFERENCES phonenumber (id)
            LABEL "HAS_PHONE" PROPERTIES (client_id, phonenumber_id),
        has_ssn KEY (rid)
            SOURCE KEY (client_id) REFERENCES client (id)
            DESTINATION KEY (ssn_id) REFERENCES ssn (id)
            LABEL "HAS_SSN" PROPERTIES (client_id, ssn_id)
    );
