from django.contrib.auth.hashers import make_password
from django.db import migrations, models

def hash_existing_pins(apps, schema_editor):
    Verification = apps.get_model("home", "NiaCallerVerification")
    for row in Verification.objects.all().iterator():
        raw = str(row.pin_code or "")
        if len(raw) == 4 and raw.isdigit():
            row.pin_code = make_password(raw)
            row.save(update_fields=["pin_code"])

class Migration(migrations.Migration):
    dependencies = [
        ("home", "0048_alter_staffpayment_idempotency_key"),
    ]
    operations = [
        migrations.AlterField(
            model_name="niacallerverification",
            name="pin_code",
            field=models.CharField(max_length=128),
        ),
        migrations.RunPython(hash_existing_pins, migrations.RunPython.noop),
    ]
