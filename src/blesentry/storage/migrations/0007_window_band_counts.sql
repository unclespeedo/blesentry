-- Per-cycle band-count snapshots for crowd/inside baseline (C2 / #132).
-- Replay cache derivable from observations; not a source of truth.
-- Retention: incremental DELETE by observed_at, at most once/day (no VACUUM).

CREATE TABLE window_band_counts (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    count_all INTEGER NOT NULL CHECK (count_all >= 0),
    count_far INTEGER NOT NULL CHECK (count_far >= 0),
    count_near INTEGER NOT NULL CHECK (count_near >= 0),
    count_adjacent INTEGER NOT NULL CHECK (count_adjacent >= 0),
    CHECK (
        count_adjacent <= count_near
        AND count_near <= count_far
        AND count_far <= count_all
    )
);

CREATE INDEX idx_window_band_counts_site_time
    ON window_band_counts (site_id, observed_at);

CREATE INDEX idx_window_band_counts_site_window
    ON window_band_counts (site_id, window_index);
