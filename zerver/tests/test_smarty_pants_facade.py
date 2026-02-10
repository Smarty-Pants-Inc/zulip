from __future__ import annotations

import os
from unittest import mock

import orjson

from zerver.lib.test_classes import ZulipTestCase
from zerver.models.realms import get_realm


class SmartyPantsRealmBrandingS2STestCase(ZulipTestCase):
    """Tests for the S2S realm branding endpoint.

    Endpoint:
    - GET/POST /api/s2s/smarty_pants/realm/branding

    This endpoint is intended for provisioning/automation and is authenticated
    via the shared secret configured in SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET.
    """

    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")

    def _headers(self, *, token: str | None) -> dict[str, str]:
        if token is None:
            return {}
        return {"x-smarty-pants-secret": token}

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_get_requires_shared_secret(self) -> None:
        result = self.client_get(
            f"/api/s2s/smarty_pants/realm/branding?realm_id={self.realm.id}",
        )
        self.assertEqual(result.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_get_returns_effective_branding(self) -> None:
        result = self.client_get(
            f"/api/s2s/smarty_pants/realm/branding?realm_id={self.realm.id}",
            headers=self._headers(token="test-secret"),
        )
        payload = self.assert_json_success(result)
        self.assertEqual(payload["realm_id"], self.realm.id)
        self.assertIn("branding", payload)
        self.assertIn("overrides", payload)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_post_sets_and_clears_name_override(self) -> None:
        # Set.
        post_payload = {
            "realm_id": self.realm.id,
            "branding": {"name": "RealmBrand"},
        }
        result = self.client_post(
            "/api/s2s/smarty_pants/realm/branding",
            orjson.dumps(post_payload),
            content_type="application/json",
            headers=self._headers(token="test-secret"),
        )
        payload = self.assert_json_success(result)
        self.assertEqual(payload["overrides"]["name"], "RealmBrand")
        self.assertEqual(payload["branding"]["name"], "RealmBrand")

        # Clear.
        clear_payload = {
            "realm_id": self.realm.id,
            "branding": {"name": ""},
        }
        result = self.client_post(
            "/api/s2s/smarty_pants/realm/branding",
            orjson.dumps(clear_payload),
            content_type="application/json",
            headers=self._headers(token="test-secret"),
        )
        payload = self.assert_json_success(result)
        self.assertEqual(payload["overrides"], {})
