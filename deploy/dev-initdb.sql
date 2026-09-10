-- Runs once, on first init of the dev Postgres container's data directory
-- (Postgres images execute everything under /docker-entrypoint-initdb.d on
-- an empty data dir only). Creates a SEPARATE database for the test suite
-- so `make dev`'s data and `make test-db`'s TRUNCATE-happy tests never share
-- a database -- see tests/dbutil.py, which additionally refuses to run
-- against any database whose name doesn't end in "_test".
CREATE DATABASE timekpr_hub_test;
