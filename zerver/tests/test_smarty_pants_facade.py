from __future__ import annotations

import os
from unittest import mock

import orjson

from zerver.lib.test_classes import ZulipTestCase
from zerver.models import Recipient, Subscription
from zerver.models.realms import get_realm
from zerver.models.users import get_user_by_delivery_email


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


class SmartyPantsToolsS2STestCase(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")
        self.admin = self.example_user("iago")
        self.sponsor = self.example_user("hamlet")
        self.subscribe(self.admin, "Denmark")
        self.subscribe(self.sponsor, "Denmark")

    def _headers(self) -> dict[str, str]:
        return {"x-smarty-pants-secret": "test-secret"}

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_project_agents_provision_defaults_requires_admin(self, mock_call_control_plane: mock.Mock) -> None:
        message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": message_id,
            "tool": "cp.project_agents.provision_defaults",
            "args": {},
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        self.assertEqual(result.status_code, 403)
        mock_call_control_plane.assert_not_called()

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_project_agents_provision_defaults_creates_bots_and_subscriptions(
        self, mock_call_control_plane: mock.Mock
    ) -> None:
        mock_call_control_plane.return_value = {"ok": True, "created": 3}

        folder_result = self.client_post(
            "/api/v1/channel_folders/create",
            {
                "name": "Projects",
                "description": "Project channels managed by Smarty Pants.",
            },
            subdomain="zulip",
        )
        self.assert_json_success(folder_result)

        for channel_name in ["smarty-code", "smarty-graph", "smarty-chat"]:
            create_result = self.client_post(
                "/json/users/me/subscriptions",
                {"subscriptions": orjson.dumps([{"name": channel_name}]).decode()},
                subdomain="zulip",
            )
            self.assert_json_success(create_result)

        message_id = self.send_stream_message(self.admin, "Denmark", topic_name="sp")
        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.admin.id,
            "invoker_message_id": message_id,
            "tool": "cp.project_agents.provision_defaults",
            "args": {},
        }

        first = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        first_json = self.assert_json_success(first)
        self.assertEqual(first_json["tool"], "cp.project_agents.provision_defaults")
        project_rows = first_json["result"]["projects"]
        self.assert_length(project_rows, 3)

        expected_emails = {
            "smarty-code-agent-bot@zulip.testserver",
            "smarty-graph-agent-bot@zulip.testserver",
            "smarty-chat-agent-bot@zulip.testserver",
        }
        self.assertEqual({row["botEmail"] for row in project_rows}, expected_emails)

        for row in project_rows:
            bot = get_user_by_delivery_email(row["botEmail"], self.realm)
            self.assertTrue(bot.is_bot)
            self.assertTrue(bot.is_active)
            self.assertTrue(
                Subscription.objects.filter(
                    user_profile_id=bot.id,
                    recipient__type_id=row["streamId"],
                    recipient__type=Recipient.STREAM,
                    active=True,
                ).exists()
            )

        second = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        second_json = self.assert_json_success(second)
        second_rows = second_json["result"]["projects"]

        self.assertEqual(
            {(row["streamId"], row["botEmail"], row["botUserId"]) for row in second_rows},
            {(row["streamId"], row["botEmail"], row["botUserId"]) for row in project_rows},
        )
        self.assertEqual(mock_call_control_plane.call_count, 2)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_cp_agents_index_allowed_for_sponsor(self, mock_call_control_plane: mock.Mock) -> None:
        mock_call_control_plane.return_value = {"ok": True, "agents": []}

        message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": message_id,
            "tool": "cp.agents.index",
            "args": {},
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        self.assert_json_success(result)
        mock_call_control_plane.assert_called_once()

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_cp_letta_agents_retrieve_allowed_for_sponsor(self, mock_call_control_plane: mock.Mock) -> None:
        mock_call_control_plane.return_value = {"ok": True, "id": "agent-123"}

        message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": message_id,
            "tool": "cp.letta.agents.retrieve",
            "args": {"runtimeAgentId": "agent-123"},
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        self.assert_json_success(result)
        mock_call_control_plane.assert_called_once()
