-- Create the role the API actually connects as.
--
-- POSTGRES_USER makes a superuser, and a superuser bypasses row-level
-- security unconditionally - FORCE ROW LEVEL SECURITY included. Connecting
-- the application as that role would leave migration 0004's policies in
-- place, enabled, forced, and never once consulted. The tenant-isolation
-- tests would still pass, because the application layer filters correctly
-- on its own, and the database half of "two layers, because one is never
-- enough" would be decorative.
--
-- So: `agentworld` owns the schema and runs migrations, `agentworld_app`
-- connects and is subject to the policies. NOSUPERUSER and NOBYPASSRLS are
-- stated explicitly rather than left to defaults, because this file exists
-- for exactly that reason.
--
-- Deployed environments create this role themselves with a real password;
-- the grants it needs are in migration 0005, not here, so that "who may
-- connect" stays an operational concern and "what they may touch" stays a
-- schema one.
--
-- This script runs only on first initialisation of the data volume. After
-- changing it: docker compose down -v && docker compose up -d
CREATE ROLE agentworld_app
  LOGIN PASSWORD 'app'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE agentworld TO agentworld_app;
GRANT USAGE ON SCHEMA public TO agentworld_app;
