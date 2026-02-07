from __future__ import annotations

from django.test import override_settings

from zerver.lib.test_classes import ZulipTestCase


class BrandingGuardrailsTest(ZulipTestCase):
    """Tests that protect the fork's branding customizations.

    These are intentionally lightweight: they verify that our standard
    server-rendered pages always have branding information available both
    in the Jinja2 template context ("branding") and in the JSON "page_params"
    consumed by the frontend (page_params.branding.name).

    If these invariants regress, downstream merges tend to reintroduce
    upstream defaults like 'Zulip' in user-visible surfaces.
    """

    @override_settings(BRAND_NAME="TestBrand")
    def test_branding_available_in_templates_and_page_params(self) -> None:
        # Login page (logged-out): uses meta tags that reference `branding.name`.
        result = self.client_get("/login/")
        self.assertEqual(result.status_code, 200)
        html = result.content.decode()
        self.assertIn('property="og:site_name" content="TestBrand"', html)

        page_params = self._get_page_params(result)
        self.assertEqual(page_params["page_type"], "login")
        self.assertEqual(page_params["branding"]["name"], "TestBrand")

        # Home page (logged-in): the base template title references `branding.name`.
        self.login("hamlet")
        result = self.client_get("/")
        self.check_rendered_logged_in_app(result)

        realm_name = self.example_user("hamlet").realm.name
        html = result.content.decode()
        self.assertIn(f"<title>{realm_name} - TestBrand</title>", html)

        page_params = self._get_page_params(result)
        self.assertEqual(page_params["page_type"], "home")
        self.assertEqual(page_params["branding"]["name"], "TestBrand")
