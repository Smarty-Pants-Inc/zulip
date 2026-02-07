from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from django.conf import settings

from zerver.lib.send_email import FromAddress


def get_branding_context() -> dict[str, Any]:
    """Return the server-side branding object used by Jinja2 templates.

    This is intentionally centralized in an isolated module so custom branding
    logic does not need to live in high-churn upstream files like
    zerver/context_processors.py.

    Note: The web app currently only needs `branding.name` in `page_params`; see
    `branding_schema` in web/src/base_page_params.ts.
    """

    og_image_url = settings.BRAND_OG_IMAGE_URL
    if not og_image_url:
        # Use an absolute URL, since OG images are consumed by external crawlers.
        og_image_url = urljoin(
            settings.ROOT_DOMAIN_URI.rstrip("/") + "/",
            "static/images/logo/zulip-icon-128x128.png",
        )

    return {
        "name": settings.BRAND_NAME,
        "support_email": FromAddress.SUPPORT,
        "powered_by_enabled": settings.BRAND_POWERED_BY_ZULIP,
        "urls": {
            "homepage": settings.BRAND_WEBSITE_URL,
            "help": settings.BRAND_HELP_URL,
            "status": settings.BRAND_STATUS_URL,
            "blog": settings.BRAND_BLOG_URL,
            "github": settings.BRAND_GITHUB_URL,
            "twitter": settings.BRAND_TWITTER_URL,
            "mastodon": settings.BRAND_MASTODON_URL,
            "linkedin": settings.BRAND_LINKEDIN_URL,
            "support_project": settings.BRAND_SUPPORT_PROJECT_URL,
            "attribution": settings.BRAND_ATTRIBUTION_URL,
            "self_hosted_billing_login": settings.BRAND_SELF_HOSTED_BILLING_LOGIN_URL,
            "authentication_docs": settings.BRAND_AUTHENTICATION_DOCS_URL,
            "troubleshooting": settings.BRAND_TROUBLESHOOTING_URL,
            "server_installation": settings.BRAND_SERVER_INSTALLATION_URL,
            "server_upgrade": settings.BRAND_SERVER_UPGRADE_URL,
            "og_image": og_image_url,
        },
    }


def get_branding_page_params() -> dict[str, Any]:
    """Return minimal branding info exposed to the web app via `page_params`."""

    return {
        "name": settings.BRAND_NAME,
    }
