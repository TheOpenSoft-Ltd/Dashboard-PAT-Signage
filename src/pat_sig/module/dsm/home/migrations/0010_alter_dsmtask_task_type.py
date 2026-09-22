# Add the DEFAULT task type (the pushable resting banner).
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0009_alter_dsmtask_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dsmtask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("ALERTHIGHT", "Alert High"),
                    ("ALERTMEDIUM", "Alert Medium"),
                    ("ALERTLOW", "Alert Low"),
                    ("PUBLICRELATION", "Public Relation"),
                    ("DEFAULT", "Default banner"),
                ],
                default="PUBLICRELATION",
                max_length=20,
            ),
        ),
    ]
