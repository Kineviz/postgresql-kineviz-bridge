#!/usr/bin/env bash
# Load the PaySim dataset into PostgreSQL 19 and build the `paysim` property graph.
#
#   ./scripts/load_paysim.sh                       # uses the bundled dataset
#   PAYSIM_DIR=/path/to/paysim ./scripts/load_paysim.sh   # use your own copy
#
# The repo ships a full synthetic PaySim dataset, stored compressed in
# fixtures/paysim/paysim_data.zip; with no PAYSIM_DIR set, this script unzips it
# once and loads it, so a fresh clone works out of the box.
# Assumes the postgres:19beta1 container `pg19beta` is running with db `appdb`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DS="${PAYSIM_DIR:-}"
if [[ -z "$DS" ]]; then
    DS="$HERE/fixtures/paysim"
    if [[ ! -f "$DS/data/raw/clients.csv" ]]; then
        echo "== unzipping bundled PaySim data (fixtures/paysim/paysim_data.zip) =="
        unzip -oq "$DS/paysim_data.zip" -d "$DS"
    fi
    echo "== using bundled PaySim dataset (set PAYSIM_DIR to load your own copy) =="
fi
RAW="$DS/data/raw"
PROC="$DS/data/processed"
PSQL=(docker exec -i pg19beta psql -U postgres -d appdb -v ON_ERROR_STOP=1)

copy() {  # copy <table> <csv>
    echo "  COPY $1  <-  $(basename "$2")"
    "${PSQL[@]}" -c "\copy $1 FROM STDIN WITH (FORMAT csv, HEADER true)" < "$2"
}

echo "== 1. staging tables =="
"${PSQL[@]}" < "$HERE/fixtures/paysim/01_staging.sql"

echo "== 2. load CSVs =="
copy stg_clients        "$RAW/clients.csv"
copy stg_merchants      "$RAW/merchants.csv"
copy stg_banks          "$PROC/banks.csv"
copy stg_transactions   "$PROC/transactions_cleaned.csv"
copy stg_emails         "$PROC/emails.csv"
copy stg_phonenumbers   "$PROC/phonenumbers.csv"
copy stg_ssns           "$PROC/ssns.csv"
copy stg_client_perform "$PROC/Client_Perform_Transaction.csv"
copy stg_txn_client     "$PROC/Transaction_To_Client.csv"
copy stg_txn_merchant   "$PROC/Transaction_To_Merchant.csv"
copy stg_txn_bank       "$PROC/Transaction_To_Bank.csv"
copy stg_has_email      "$PROC/Has_Email.csv"
copy stg_has_phone      "$PROC/Has_Phonenumber.csv"
copy stg_has_ssn        "$PROC/Has_SSN.csv"

echo "== 3. build node/edge tables + property graph =="
"${PSQL[@]}" < "$HERE/fixtures/paysim/02_build_graph.sql"

echo "== 4. counts =="
"${PSQL[@]}" -c "
SELECT 'Client' n, count(*) FROM client UNION ALL
SELECT 'Merchant', count(*) FROM merchant UNION ALL
SELECT 'Bank', count(*) FROM bank UNION ALL
SELECT 'Transaction', count(*) FROM txn UNION ALL
SELECT 'Email', count(*) FROM email UNION ALL
SELECT 'PhoneNumber', count(*) FROM phonenumber UNION ALL
SELECT 'SSN', count(*) FROM ssn UNION ALL
SELECT 'PERFORMS', count(*) FROM client_perform_transaction UNION ALL
SELECT 'TO_CLIENT', count(*) FROM transaction_to_client UNION ALL
SELECT 'TO_MERCHANT', count(*) FROM transaction_to_merchant UNION ALL
SELECT 'TO_BANK', count(*) FROM transaction_to_bank UNION ALL
SELECT 'HAS_EMAIL', count(*) FROM has_email UNION ALL
SELECT 'HAS_PHONE', count(*) FROM has_phonenumber UNION ALL
SELECT 'HAS_SSN', count(*) FROM has_ssn
ORDER BY 1;"

echo "== done =="
