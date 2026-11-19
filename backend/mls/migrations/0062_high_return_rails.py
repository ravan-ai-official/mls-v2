# Generated manually for high-return homepage rail pins.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mls", "0061_precomproperty_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="is_high_return",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Pin this listing to the homepage High return rail.",
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="high_return_order",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Position in the High return rail (1 = first). Blank sorts after numbered ones.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="high_return_until",
            field=models.DateTimeField(
                blank=True,
                help_text="Stop pinning after this time. Blank = until unticked.",
                null=True,
            ),
        ),
    ]
