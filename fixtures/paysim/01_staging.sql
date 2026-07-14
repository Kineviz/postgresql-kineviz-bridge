-- PaySim → PostgreSQL 19: staging tables (raw CSV shapes).
-- Loaded by scripts/load_paysim.sh via \copy. Tricky columns (booleans,
-- timestamps) are kept as text here and cast in 02_build_graph.sql to keep the
-- bulk COPY bullet-proof. Column order matches each CSV header exactly.

DROP TABLE IF EXISTS
    stg_clients, stg_merchants, stg_banks, stg_transactions,
    stg_emails, stg_phonenumbers, stg_ssns,
    stg_client_perform, stg_txn_client, stg_txn_merchant, stg_txn_bank,
    stg_has_email, stg_has_phone, stg_has_ssn CASCADE;

-- raw/clients.csv:  email,id,isfraud,name,phonenumber,ssn
CREATE TABLE stg_clients (email text, id bigint, isfraud text, name text, phonenumber text, ssn text);

-- raw/merchants.csv: highrisk,id,name
CREATE TABLE stg_merchants (highrisk text, id text, name text);

-- processed/banks.csv: id,name
CREATE TABLE stg_banks (id text, name text);

-- processed/transactions_cleaned.csv:
-- action,amount,globalstep,iddest,idorig,isflaggedfraud,isfraud,namedest,nameorig,typedest,typeorig,timestamp
CREATE TABLE stg_transactions (
    action text, amount numeric, globalstep bigint, iddest text, idorig text,
    isflaggedfraud text, isfraud text, namedest text, nameorig text,
    typedest text, typeorig text, ts text
);

-- processed/{emails,phonenumbers,ssns}.csv: id,name
CREATE TABLE stg_emails (id text, name text);
CREATE TABLE stg_phonenumbers (id text, name text);
CREATE TABLE stg_ssns (id text, name text);

-- processed/Client_Perform_Transaction.csv: client_id,transaction_id,timestamp
CREATE TABLE stg_client_perform (client_id bigint, transaction_id bigint, ts text);

-- processed/Transaction_To_Client.csv: transaction_id,client_id,timestamp
CREATE TABLE stg_txn_client (transaction_id bigint, client_id bigint, ts text);

-- processed/Transaction_To_Merchant.csv: transaction_id,merchant_id,timestamp
CREATE TABLE stg_txn_merchant (transaction_id bigint, merchant_id text, ts text);

-- processed/Transaction_To_Bank.csv: transaction_id,bank_id,timestamp
CREATE TABLE stg_txn_bank (transaction_id bigint, bank_id text, ts text);

-- processed/Has_Email.csv: client_id,email_id
CREATE TABLE stg_has_email (client_id bigint, email_id text);
-- processed/Has_Phonenumber.csv: client_id,phonenumber_id
CREATE TABLE stg_has_phone (client_id bigint, phonenumber_id text);
-- processed/Has_SSN.csv: client_id,ssn_id
CREATE TABLE stg_has_ssn (client_id bigint, ssn_id text);
