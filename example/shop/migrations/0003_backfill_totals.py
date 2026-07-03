# Contains RunPython: emits no SQL, so sixta-review flags it for human review
# instead of passing it silently.
from django.db import migrations


def backfill_totals(apps, schema_editor):
    Order = apps.get_model("shop", "Order")
    for order in Order.objects.filter(total=0).iterator():
        order.total = 1
        order.save(update_fields=["total"])


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_order_index_and_backfill")]

    operations = [migrations.RunPython(backfill_totals, migrations.RunPython.noop)]
