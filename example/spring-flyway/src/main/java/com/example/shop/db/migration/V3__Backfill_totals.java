package com.example.shop.db.migration;

import org.flywaydb.core.api.migration.BaseJavaMigration;
import org.flywaydb.core.api.migration.Context;

/**
 * Java-based migration: renders no SQL offline, so sixta-review flags it for
 * human review (like Django's RunPython) instead of passing it silently.
 */
public class V3__Backfill_totals extends BaseJavaMigration {
    @Override
    public void migrate(Context context) throws Exception {
        try (var stmt = context.getConnection().createStatement()) {
            stmt.execute("UPDATE orders SET total = 0 WHERE total IS NULL");
        }
    }
}
