# Deliberately risky migration — the demo case for sixta-review.
#
# Expected verdicts:
#  * AddIndex renders to a plain CREATE INDEX -> ShareLock, blocks writes
#    (High; SIXTA suggests CREATE INDEX CONCURRENTLY).
#  * The RunSQL backfill compares a column to NULL with `=` -> matches no rows,
#    a silent correctness bug (sixta_analyze_query flags it and rewrites to
#    IS NULL).
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]

    operations = [
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["status"], name="shop_order_status_idx"),
        ),
        migrations.RunSQL(
            sql="UPDATE shop_order SET status = 'new' WHERE status = NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
