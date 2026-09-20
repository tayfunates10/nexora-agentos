-- Hybrid retrieval adds a language-neutral full-text index over chunk content.
-- The explicit 'simple' dictionary avoids English-only stemming in a multi-language platform.
CREATE INDEX rag_chunks_lexical_gin
    ON rag_chunks USING gin (to_tsvector('simple', content));
