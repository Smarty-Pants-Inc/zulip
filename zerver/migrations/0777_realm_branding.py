from __future__ import annotations

from django.db import migrations, models
from django.db.models import CASCADE


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0776_realm_default_avatar_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="RealmBranding",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(blank=True, max_length=100, null=True),
                ),
                (
                    "support_email",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "homepage_url",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "help_url",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "status_url",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "blog_url",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "github_url",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "realm",
                    models.OneToOneField(
                        on_delete=CASCADE,
                        related_name="realm_branding",
                        to="zerver.realm",
                    ),
                ),
            ],
        ),
    ]
