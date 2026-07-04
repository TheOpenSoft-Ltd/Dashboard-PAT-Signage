# Add the "stop" status (manual hard-stop while playing).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("home", "0008_outboxreport"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dsmtask",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("downloaded", "Downloaded"),
                    ("playing", "playing"),
                    ("skiping", "skiping"),
                    ("stop", "Stop"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
