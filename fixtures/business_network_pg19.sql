-- business_network property graph for PostgreSQL 19 (SQL/PGQ).
-- Mirrors the MockBackend fixture so live results are directly comparable.
-- Labels/properties are unquoted → fold to lowercase consistently in both the
-- DDL and GRAPH_TABLE queries (Kineviz strips backticks, so we must not quote).

DROP PROPERTY GRAPH IF EXISTS business_network;
DROP TABLE IF EXISTS works_at, knows, company, person CASCADE;

CREATE TABLE person (
    person_id bigint PRIMARY KEY,
    name      text NOT NULL,
    email     text
);

CREATE TABLE company (
    company_id bigint PRIMARY KEY,
    name       text NOT NULL
);

CREATE TABLE knows (
    knows_id   bigint PRIMARY KEY,
    src        bigint NOT NULL REFERENCES person(person_id),
    dst        bigint NOT NULL REFERENCES person(person_id),
    since_date date
);

CREATE TABLE works_at (
    works_at_id bigint PRIMARY KEY,
    person_id   bigint NOT NULL REFERENCES person(person_id),
    company_id  bigint NOT NULL REFERENCES company(company_id),
    title       text,
    started_at  date
);

INSERT INTO person VALUES
    (1,'Alice','alice@example.com'),
    (2,'Bob','bob@example.com'),
    (3,'Carol','carol@example.com'),
    (4,'Dave',NULL);

INSERT INTO company VALUES
    (100,'Acme'),
    (200,'Globex');

INSERT INTO knows VALUES
    (1001,1,2,'2021-02-10'),
    (1002,2,3,'2022-06-01'),
    (1003,1,3,'2020-01-15');

INSERT INTO works_at VALUES
    (2001,1,100,'CTO','2019-03-01'),
    (2002,2,100,'Engineer','2020-07-01'),
    (2003,3,200,'Analyst','2021-09-01'),
    (2004,4,200,'Manager','2018-05-01');

CREATE PROPERTY GRAPH business_network
    VERTEX TABLES (
        person  KEY (person_id)  LABEL "Person"  PROPERTIES (person_id AS id, name, email),
        company KEY (company_id) LABEL "Company" PROPERTIES (company_id AS id, name)
    )
    EDGE TABLES (
        knows KEY (knows_id)
            SOURCE      KEY (src) REFERENCES person (person_id)
            DESTINATION KEY (dst) REFERENCES person (person_id)
            LABEL "KNOWS" PROPERTIES (since_date AS since),
        works_at KEY (works_at_id)
            SOURCE      KEY (person_id)  REFERENCES person (person_id)
            DESTINATION KEY (company_id) REFERENCES company (company_id)
            LABEL "WORKS_AT" PROPERTIES (title, started_at)
    );
