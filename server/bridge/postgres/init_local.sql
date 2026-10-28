-- Run as the PostgreSQL administrator, using psql (not an application migration).
-- Creates an empty local database only. Existing SQLite data is not imported.
-- Afterwards set the login password interactively with: \password "fall-relay"
\set ON_ERROR_STOP on
\connect postgres

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fall-relay') THEN
        CREATE ROLE "fall-relay" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    ELSIF EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'fall-relay'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolcanlogin)
    ) THEN
        RAISE EXCEPTION 'Existing fall-relay role has unexpected privileges; inspect it before continuing';
    END IF;
    IF EXISTS (
        SELECT FROM pg_database WHERE datname = 'fall-relay'
        AND (pg_get_userbyid(datdba) <> 'fall-relay' OR pg_encoding_to_char(encoding) <> 'UTF8')
    ) THEN
        RAISE EXCEPTION 'Existing fall-relay database has an unexpected owner or encoding';
    END IF;
END
$$;

SELECT 'CREATE DATABASE "fall-relay" OWNER "fall-relay" TEMPLATE template0 ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'fall-relay')
\gexec

REVOKE ALL ON DATABASE "fall-relay" FROM PUBLIC;
\connect fall-relay
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO "fall-relay";

SELECT current_database() AS database, pg_get_userbyid(datdba) AS owner,
       pg_encoding_to_char(encoding) AS encoding
FROM pg_database WHERE datname = current_database();
