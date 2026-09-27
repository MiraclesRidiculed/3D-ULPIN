-- V-CAD database bootstrap.
--
-- Schema ownership: Alembic owns the tables (backend/alembic/versions).
-- This file runs first, on an empty data directory, and is responsible only for
-- the extensions the schema depends on. Tables are created by `alembic upgrade
-- head` so there is exactly one source of schema truth.
--
-- The original DDL that lived here was superseded by migration 0001 after the
-- application models were inspected; that migration documents each deliberate
-- divergence (TEXT primary keys, 2D geometry plus scalar elevations, added
-- columns, generated metric-geometry companions).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
