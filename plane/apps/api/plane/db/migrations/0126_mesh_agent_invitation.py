# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0125_mesh_run_attempt_lifecycle")]

    operations = [
        migrations.AddField(
            model_name="agentregistrationapplication",
            name="default_model",
            field=models.CharField(blank=True, default="", max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="agentregistrationapplication",
            name="invite_id",
            field=models.UUIDField(null=True, unique=True),
        ),
    ]
