from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import timedelta
from unittest import mock

import orjson
import time_machine
from django.core.cache import cache
from django.utils.timezone import now as timezone_now

from zerver.lib.test_classes import ZulipTestCase
from zerver.models import Message, Recipient, Stream, Subscription
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


class SmartyPantsSignedAuthS2STestCase(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()

    def _signed_headers(
        self,
        *,
        method: str,
        path: str,
        nonce: str | None = None,
        timestamp_ms: int | None = None,
        secret: str = "test-secret",
    ) -> dict[str, str]:
        if nonce is None:
            nonce = uuid.uuid4().hex
        if timestamp_ms is None:
            timestamp_ms = int(timezone_now().timestamp() * 1000)

        payload = "\n".join([method.upper(), path, str(timestamp_ms), nonce])
        signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "X-SP-S2S-Timestamp": str(timestamp_ms),
            "X-SP-S2S-Nonce": nonce,
            "X-SP-S2S-Signature": signature,
        }

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "signed",
        },
        clear=False,
    )
    def test_signed_auth_allows_request_without_legacy_secret(self) -> None:
        path = "/api/s2s/smarty_pants/realm/branding"
        result = self.client_get(
            f"{path}?realm_id={self.realm.id}",
            headers=self._signed_headers(method="GET", path=path),
        )
        self.assert_json_success(result)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "signed",
        },
        clear=False,
    )
    def test_signed_auth_rejects_legacy_secret_only(self) -> None:
        path = "/api/s2s/smarty_pants/realm/branding"
        result = self.client_get(
            f"{path}?realm_id={self.realm.id}",
            headers={"x-smarty-pants-secret": "test-secret"},
        )
        self.assertEqual(result.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "signed",
        },
        clear=False,
    )
    def test_signed_auth_rejects_payload_secret_only(self) -> None:
        path = "/api/s2s/smarty_pants/realm/branding"
        result = self.client_post(
            path,
            orjson.dumps({"realm_id": self.realm.id, "branding": {"name": "X"}, "secret": "test-secret"}),
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "signed",
        },
        clear=False,
    )
    def test_signed_auth_rejects_replay_nonce(self) -> None:
        path = "/api/s2s/smarty_pants/realm/branding"
        nonce = "nonce-replay"
        timestamp_ms = int(timezone_now().timestamp() * 1000)
        headers = self._signed_headers(method="GET", path=path, nonce=nonce, timestamp_ms=timestamp_ms)

        first = self.client_get(f"{path}?realm_id={self.realm.id}", headers=headers)
        self.assert_json_success(first)

        second = self.client_get(f"{path}?realm_id={self.realm.id}", headers=headers)
        self.assertEqual(second.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "signed",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.time.time")
    def test_signed_auth_rejects_expired_timestamp(self, mock_time: mock.Mock) -> None:
        # zerver.views.smarty_pants uses time.time() to validate +/- 5 minutes.
        mock_time.return_value = 1000.0
        now_ms = int(mock_time.return_value * 1000)
        timestamp_ms = now_ms - (6 * 60 * 1000)

        path = "/api/s2s/smarty_pants/realm/branding"
        headers = self._signed_headers(method="GET", path=path, nonce="nonce-expired", timestamp_ms=timestamp_ms)
        result = self.client_get(f"{path}?realm_id={self.realm.id}", headers=headers)
        self.assertEqual(result.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
            "SMARTY_PANTS_S2S_AUTH_MODE": "both",
        },
        clear=False,
    )
    def test_both_mode_does_not_fall_back_to_legacy_if_signed_headers_present(self) -> None:
        path = "/api/s2s/smarty_pants/realm/branding"

        # Valid legacy secret but invalid signature.
        headers = {
            "x-smarty-pants-secret": "test-secret",
            "X-SP-S2S-Timestamp": str(int(timezone_now().timestamp() * 1000)),
            "X-SP-S2S-Nonce": "nonce-bad-sig",
            "X-SP-S2S-Signature": "0" * 64,
        }

        result = self.client_get(f"{path}?realm_id={self.realm.id}", headers=headers)
        self.assertEqual(result.status_code, 403)


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

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_s2s_send_stream_as_user_allows_sponsor_as_self(self) -> None:
        invoker_message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        denmark = Stream.objects.get(name="Denmark", realm=self.realm)

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": invoker_message_id,
            "sender_user_id": self.sponsor.id,
            "stream_id": denmark.id,
            "topic": "sp",
            "content": "hello from s2s",
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/messages/send_stream_as_user",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        response_json = self.assert_json_success(result)

        msg = Message.objects.get(id=response_json["id"], realm_id=self.realm.id)
        self.assertEqual(msg.sender_id, self.sponsor.id)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_s2s_send_stream_as_user_rejects_sponsor_impersonation(self) -> None:
        invoker_message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        denmark = Stream.objects.get(name="Denmark", realm=self.realm)

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": invoker_message_id,
            "sender_user_id": self.admin.id,
            "stream_id": denmark.id,
            "topic": "sp",
            "content": "spoof",
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/messages/send_stream_as_user",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        self.assertEqual(result.status_code, 403)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_s2s_send_stream_as_user_allows_admin_to_send_as_other_human(self) -> None:
        invoker_message_id = self.send_stream_message(self.admin, "Denmark", topic_name="sp")
        denmark = Stream.objects.get(name="Denmark", realm=self.realm)

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.admin.id,
            "invoker_message_id": invoker_message_id,
            "sender_user_id": self.sponsor.id,
            "stream_id": denmark.id,
            "topic": "sp",
            "content": "admin can replay",
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/messages/send_stream_as_user",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        response_json = self.assert_json_success(result)
        msg = Message.objects.get(id=response_json["id"], realm_id=self.realm.id)
        self.assertEqual(msg.sender_id, self.sponsor.id)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_s2s_send_stream_topic_batch_allows_sponsor_self_and_bot(self) -> None:
        invoker_message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        denmark = Stream.objects.get(name="Denmark", realm=self.realm)
        bot = self.create_test_bot("s2s-bot", self.sponsor)

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": invoker_message_id,
            "stream_id": denmark.id,
            "topic": "sp",
            "messages": [
                {
                    "sender_user_id": self.sponsor.id,
                    "content": "hello from s2s batch (self)",
                },
                {
                    "sender_user_id": bot.id,
                    "content": "hello from s2s batch (bot)",
                },
            ],
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/messages/send_stream_topic_batch",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        response_json = self.assert_json_success(result)
        self.assertTrue(response_json["ok"])
        self.assertEqual(len(response_json["results"]), 2)

        msg0 = Message.objects.get(id=response_json["results"][0]["id"], realm_id=self.realm.id)
        msg1 = Message.objects.get(id=response_json["results"][1]["id"], realm_id=self.realm.id)
        self.assertEqual(msg0.sender_id, self.sponsor.id)
        self.assertEqual(msg1.sender_id, bot.id)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_s2s_send_stream_topic_batch_rejects_sponsor_impersonation(self) -> None:
        invoker_message_id = self.send_stream_message(self.sponsor, "Denmark", topic_name="sp")
        denmark = Stream.objects.get(name="Denmark", realm=self.realm)

        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.sponsor.id,
            "invoker_message_id": invoker_message_id,
            "stream_id": denmark.id,
            "topic": "sp",
            "messages": [
                {
                    "sender_user_id": self.admin.id,
                    "content": "spoof",
                },
            ],
        }

        result = self.client_post(
            "/api/s2s/smarty_pants/messages/send_stream_topic_batch",
            orjson.dumps(payload),
            content_type="application/json",
            headers=self._headers(),
        )
        self.assertEqual(result.status_code, 403)

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
        self.assertEqual(first_json["deduped"], False)
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
        self.assertEqual(second_json["deduped"], True)
        self.assertEqual(mock_call_control_plane.call_count, 1)

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
        response_json = self.assert_json_success(result)
        self.assertEqual(response_json["deduped"], False)
        mock_call_control_plane.assert_called_once()

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_s2s_tools_execute_rejects_old_invoker_message(self, mock_call_control_plane: mock.Mock) -> None:
        message_id = self.send_stream_message(self.admin, "Denmark", topic_name="sp")
        payload = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.admin.id,
            "invoker_message_id": message_id,
            "tool": "cp.agents.index",
            "args": {},
        }

        with time_machine.travel(timezone_now() + timedelta(minutes=11), tick=False):
            result = self.client_post(
                "/api/s2s/smarty_pants/tools/execute",
                orjson.dumps(payload),
                content_type="application/json",
                headers=self._headers(),
            )

        self.assert_json_error(result, "Invoker message is too old.")
        mock_call_control_plane.assert_not_called()

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.call_control_plane")
    def test_s2s_tools_execute_idempotency_normalizes_args(self, mock_call_control_plane: mock.Mock) -> None:
        mock_call_control_plane.return_value = {"ok": True, "agents": [{"id": "a1"}]}

        message_id = self.send_stream_message(self.admin, "Denmark", topic_name="sp")
        payload_one = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.admin.id,
            "invoker_message_id": message_id,
            "tool": "cp.letta.runs.list",
            "args": {"z": 1, "a": {"c": 3, "b": 2}},
        }
        payload_two = {
            "realm_id": self.realm.id,
            "invoker_user_id": self.admin.id,
            "invoker_message_id": message_id,
            "tool": "cp.letta.runs.list",
            "args": {"a": {"b": 2, "c": 3}, "z": 1},
        }

        first = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload_one),
            content_type="application/json",
            headers=self._headers(),
        )
        first_json = self.assert_json_success(first)
        self.assertEqual(first_json["deduped"], False)

        second = self.client_post(
            "/api/s2s/smarty_pants/tools/execute",
            orjson.dumps(payload_two),
            content_type="application/json",
            headers=self._headers(),
        )
        second_json = self.assert_json_success(second)
        self.assertEqual(second_json["deduped"], True)
        self.assertEqual(second_json["result"], first_json["result"])
        self.assertEqual(mock_call_control_plane.call_count, 1)

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
