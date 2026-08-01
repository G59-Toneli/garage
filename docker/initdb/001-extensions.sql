-- Runs once, when the postgres container initialises an empty data directory.
-- The pgvector image ships the extension files; nothing installs it into the database itself.
CREATE EXTENSION IF NOT EXISTS vector;
