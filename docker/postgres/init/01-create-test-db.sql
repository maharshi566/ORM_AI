-- Runs once, when the Postgres volume is first created.
-- A separate database for integration tests, so tests never touch your dev data.
CREATE DATABASE orm_ai_test;
