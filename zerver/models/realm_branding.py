from __future__ import annotations

from django.db import models
from django.db.models import CASCADE

from zerver.models.realms import Realm


class RealmBranding(models.Model):
    """Per-realm branding overrides for downstream forks.

    This model is intentionally separate from `Realm` to minimize merge
    conflicts with upstream Zulip and to keep fork-specific concerns
    isolated.

    All fields are optional; unset/NULL values fall back to server-level
    BRAND_* settings.
    """

    realm = models.OneToOneField(Realm, on_delete=CASCADE, related_name="realm_branding")

    name = models.CharField(max_length=100, null=True, blank=True)
    support_email = models.CharField(max_length=255, null=True, blank=True)

    homepage_url = models.TextField(null=True, blank=True)
    help_url = models.TextField(null=True, blank=True)
    status_url = models.TextField(null=True, blank=True)
    blog_url = models.TextField(null=True, blank=True)
    github_url = models.TextField(null=True, blank=True)

