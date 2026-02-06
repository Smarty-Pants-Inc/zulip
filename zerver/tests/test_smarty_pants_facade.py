from __future__ import annotations

import os
from typing import Any
from unittest import mock

import orjson
import requests

from zerver.actions.streams import ensure_stream
from zerver.actions.user_groups import check_add_user_group
from zerver.lib.test_classes import ZulipTestCase
from zerver.models.realms import get_realm


class SmartyPantsFacadeTestCase(ZulipTestCase):
    def test_missing_sponsors_group(self) -> None:
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/agents")
        self.assert_json_error(
            result,
            "The 'Sponsors' user group is missing in this organization. Create it (or contact an administrator) to manage Smarty Pants agents.",
        )

    def test_user_not_in_sponsors_group(self) -> None:
        realm = get_realm("zulip")
        othello = self.example_user("othello")
        check_add_user_group(realm, "Sponsors", [othello], acting_user=othello)

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/agents")
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_control_plane_unavailable(self, mock_request: mock.Mock) -> None:
        realm = get_realm("zulip")
        hamlet = self.example_user("hamlet")
        check_add_user_group(realm, "Sponsors", [hamlet], acting_user=hamlet)

        mock_request.side_effect = requests.exceptions.ConnectionError("boom")

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/agents")
        self.assert_json_error_contains(result, "Smarty Pants control plane is unavailable")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants._provision_zulip_objects_for_agent")
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_attach_smarty_pants_agent_idempotent_already_attached(
        self, mock_request: mock.Mock, mock_provision: mock.Mock
    ) -> None:
        realm = get_realm("zulip")
        hamlet = self.example_user("hamlet")
        check_add_user_group(realm, "Sponsors", [hamlet], acting_user=hamlet)

        runtime_agent_id = "runtime-agent-123"

        # First request is list agents, which should return an attached binding.
        list_response = mock.Mock()
        list_response.status_code = 200
        list_response.json.return_value = {
            "result": "success",
            "agents": [
                {
                    "agent": {"id": "agent_1", "runtimeAgentId": runtime_agent_id},
                    "binding": {
                        "id": "binding_1",
                        "zulipBotUserId": 999,
                        "disabledAt": None,
                    },
                }
            ],
        }
        mock_request.return_value = list_response

        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/agents/attach",
            {
                "runtime_agent_id": runtime_agent_id,
                "name": "Already attached",
            },
        )
        response_dict = self.assert_json_success(result)

        # Provisioning should not happen.
        mock_provision.assert_not_called()

        self.assertTrue(response_dict.get("already_attached"))
        self.assertEqual(response_dict.get("agentId"), "agent_1")
        self.assertEqual(response_dict.get("bindingId"), "binding_1")
        self.assertEqual(response_dict.get("zulip_bot_user_id"), 999)


class SmartyPantsMemoryEndpointsTestCase(ZulipTestCase):
    """Tests for the GET /api/v1/smarty_pants/memory and POST /api/v1/smarty_pants/memory endpoints."""

    def setUp(self) -> None:
        super().setUp()
        # Set up a sponsor user for authentication tests
        realm = get_realm("zulip")
        self.hamlet = self.example_user("hamlet")
        self.othello = self.example_user("othello")
        check_add_user_group(realm, "Sponsors", [self.hamlet], acting_user=self.hamlet)

        # Create a test stream for project/thread scopes
        self.test_stream = ensure_stream(realm, "test stream", acting_user=self.hamlet)

    # ===== GET endpoint tests =====

    def test_get_memory_missing_sponsors_group(self) -> None:
        """Non-sponsor user cannot GET memory."""
        self.login("othello")
        result = self.client_get("/json/smarty_pants/memory?scope=org")
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    def test_get_memory_missing_scope_param(self) -> None:
        """GET memory requires scope parameter."""
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory")
        self.assert_json_error(
            result,
            "The 'scope' parameter is required and must be one of: org, project, thread",
        )

    def test_get_memory_invalid_scope(self) -> None:
        """GET memory rejects invalid scope values."""
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=invalid")
        self.assert_json_error(
            result,
            "The 'scope' parameter is required and must be one of: org, project, thread",
        )

    def test_get_memory_project_scope_missing_stream_id(self) -> None:
        """GET memory with project scope requires stream_id."""
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=project")
        self.assert_json_error(
            result,
            "The 'stream_id' parameter is required for 'project' scope.",
        )

    def test_get_memory_thread_scope_missing_stream_id(self) -> None:
        """GET memory with thread scope requires stream_id."""
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=thread&topic=test-topic")
        self.assert_json_error(
            result,
            "The 'stream_id' parameter is required for 'thread' scope.",
        )

    def test_get_memory_thread_scope_missing_topic(self) -> None:
        """GET memory with thread scope requires topic."""
        self.login("hamlet")
        result = self.client_get(f"/json/smarty_pants/memory?scope=thread&stream_id={self.test_stream.id}")
        self.assert_json_error(
            result,
            "The 'topic' parameter is required for 'thread' scope.",
        )

    def test_get_memory_invalid_stream_id(self) -> None:
        """GET memory rejects non-integer stream_id."""
        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=project&stream_id=not-a-number")
        self.assert_json_error(
            result,
            "The 'stream_id' parameter must be an integer.",
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_get_memory_org_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can GET org-scoped memory."""
        # Mock the control plane response
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": "success",
            "memory": {
                "text": "Organization memory content",
                "updatedAt": "2024-01-01T00:00:00Z",
            },
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=org")
        response_dict = self.assert_json_success(result)

        # Verify response structure
        self.assertEqual(response_dict["text"], "Organization memory content")
        self.assertEqual(response_dict["updatedAt"], "2024-01-01T00:00:00Z")

        # Verify the control plane was called with correct parameters
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/get", call_args[0][1])
        
        # Verify JSON payload
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "org")
        self.assertEqual(json_data["realmId"], str(self.hamlet.realm_id))
        self.assertNotIn("streamId", json_data)
        self.assertNotIn("topic", json_data)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_get_memory_project_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can GET project-scoped memory."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "memory": {
                "text": "Project memory content",
                "updatedAt": "2024-01-02T00:00:00Z",
            },
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get(
            f"/json/smarty_pants/memory?scope=project&stream_id={self.test_stream.id}"
        )
        response_dict = self.assert_json_success(result)

        self.assertEqual(response_dict["text"], "Project memory content")
        self.assertEqual(response_dict["updatedAt"], "2024-01-02T00:00:00Z")

        # Verify control plane call
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "project")
        self.assertEqual(json_data["streamId"], str(self.test_stream.id))
        self.assertNotIn("topic", json_data)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_get_memory_thread_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can GET thread-scoped memory."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "memory": {
                "text": "Thread memory content",
                "updatedAt": "2024-01-03T00:00:00Z",
            },
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get(
            f"/json/smarty_pants/memory?scope=thread&stream_id={self.test_stream.id}&topic=test-topic"
        )
        response_dict = self.assert_json_success(result)

        self.assertEqual(response_dict["text"], "Thread memory content")
        self.assertEqual(response_dict["updatedAt"], "2024-01-03T00:00:00Z")

        # Verify control plane call
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "thread")
        self.assertEqual(json_data["streamId"], str(self.test_stream.id))
        self.assertEqual(json_data["topic"], "test-topic")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_get_memory_empty_memory(self, mock_request: mock.Mock) -> None:
        """GET memory returns empty string when memory is not set."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"memory": {}}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=org")
        response_dict = self.assert_json_success(result)

        self.assertEqual(response_dict["text"], "")
        self.assertIsNone(response_dict["updatedAt"])

    # ===== POST endpoint tests =====

    def test_set_memory_missing_sponsors_group(self) -> None:
        """Non-sponsor user cannot POST memory."""
        self.login("othello")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "org", "text": "test"},
        )
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    def test_set_memory_missing_scope_param(self) -> None:
        """POST memory requires scope parameter."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"text": "test"},
        )
        self.assert_json_error(
            result,
            "The 'scope' parameter is required and must be one of: org, project, thread",
        )

    def test_set_memory_invalid_scope(self) -> None:
        """POST memory rejects invalid scope values."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "invalid", "text": "test"},
        )
        self.assert_json_error(
            result,
            "The 'scope' parameter is required and must be one of: org, project, thread",
        )

    def test_set_memory_missing_text(self) -> None:
        """POST memory requires text parameter."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "org"},
        )
        self.assert_json_error(
            result,
            "The 'text' parameter is required and must be a string.",
        )

    def test_set_memory_text_not_string(self) -> None:
        """POST memory requires text to be a string."""
        self.login("hamlet")
        # Pass an integer instead of string
        result = self.client_post(
            "/json/smarty_pants/memory",
            orjson.dumps({"scope": "org", "text": 123}).decode(),
            content_type="application/json",
        )
        self.assert_json_error(
            result,
            "The 'text' parameter is required and must be a string.",
        )

    def test_set_memory_project_scope_missing_stream_id(self) -> None:
        """POST memory with project scope requires stream_id."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "project", "text": "test"},
        )
        self.assert_json_error(
            result,
            "The 'stream_id' parameter is required for 'project' scope.",
        )

    def test_set_memory_thread_scope_missing_stream_id(self) -> None:
        """POST memory with thread scope requires stream_id."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "thread", "text": "test", "topic": "test-topic"},
        )
        self.assert_json_error(
            result,
            "The 'stream_id' parameter is required for 'thread' scope.",
        )

    def test_set_memory_thread_scope_missing_topic(self) -> None:
        """POST memory with thread scope requires topic."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "thread", "text": "test", "stream_id": str(self.test_stream.id)},
        )
        self.assert_json_error(
            result,
            "The 'topic' parameter is required for 'thread' scope.",
        )

    def test_set_memory_invalid_stream_id(self) -> None:
        """POST memory rejects non-integer stream_id."""
        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "project", "text": "test", "stream_id": "not-a-number"},
        )
        self.assert_json_error(
            result,
            "The 'stream_id' parameter must be an integer.",
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_set_memory_org_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can POST org-scoped memory."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": "success",
            "memory": {
                "text": "Updated org memory",
                "updatedAt": "2024-01-01T12:00:00Z",
            },
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {"scope": "org", "text": "Updated org memory"},
        )
        response_dict = self.assert_json_success(result)

        # Verify the control plane was called with correct parameters
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/set", call_args[0][1])

        # Verify JSON payload
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "org")
        self.assertEqual(json_data["text"], "Updated org memory")
        self.assertEqual(json_data["realmId"], str(self.hamlet.realm_id))
        self.assertNotIn("streamId", json_data)
        self.assertNotIn("topic", json_data)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_set_memory_project_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can POST project-scoped memory."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {
                "scope": "project",
                "text": "Updated project memory",
                "stream_id": str(self.test_stream.id),
            },
        )
        self.assert_json_success(result)

        # Verify control plane call
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "project")
        self.assertEqual(json_data["text"], "Updated project memory")
        self.assertEqual(json_data["streamId"], str(self.test_stream.id))
        self.assertNotIn("topic", json_data)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_set_memory_thread_scope_success(self, mock_request: mock.Mock) -> None:
        """Sponsor user can POST thread-scoped memory."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            {
                "scope": "thread",
                "text": "Updated thread memory",
                "stream_id": str(self.test_stream.id),
                "topic": "test-topic",
            },
        )
        self.assert_json_success(result)

        # Verify control plane call
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["scope"], "thread")
        self.assertEqual(json_data["text"], "Updated thread memory")
        self.assertEqual(json_data["streamId"], str(self.test_stream.id))
        self.assertEqual(json_data["topic"], "test-topic")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_set_memory_json_content_type(self, mock_request: mock.Mock) -> None:
        """POST memory accepts JSON content type."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(
            "/json/smarty_pants/memory",
            orjson.dumps({
                "scope": "org",
                "text": "Memory via JSON",
            }).decode(),
            content_type="application/json",
        )
        self.assert_json_success(result)

        # Verify control plane call
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["text"], "Memory via JSON")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_memory_control_plane_error(self, mock_request: mock.Mock) -> None:
        """Memory endpoints handle control plane errors correctly."""
        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {
            "result": "error",
            "msg": "Internal server error",
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=org")
        self.assert_json_error(result, "Internal server error")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_memory_control_plane_connection_error(self, mock_request: mock.Mock) -> None:
        """Memory endpoints handle connection errors gracefully."""
        mock_request.side_effect = requests.exceptions.RequestException("Connection timeout")

        self.login("hamlet")
        result = self.client_get("/json/smarty_pants/memory?scope=org")
        self.assert_json_error_contains(result, "Smarty Pants control plane is unavailable")


class SmartyPantsMemoryBlocksFacadeEndpointsTestCase(ZulipTestCase):
    """Tests for the Smarty Pants memory blocks facade endpoints.

    Endpoints:
    - GET /api/v1/smarty_pants/agents/<agent_id>/memory/blocks
    - POST /api/v1/smarty_pants/agents/<agent_id>/memory/blocks
    - PATCH /api/v1/smarty_pants/agents/<agent_id>/memory/blocks/<block_id>
    - DELETE /api/v1/smarty_pants/agents/<agent_id>/memory/blocks/<block_id>
    """

    def setUp(self) -> None:
        super().setUp()
        realm = get_realm("zulip")
        self.hamlet = self.example_user("hamlet")
        self.othello = self.example_user("othello")
        check_add_user_group(realm, "Sponsors", [self.hamlet], acting_user=self.hamlet)

        self.agent_id = "agent_123"
        self.block_id = "block_456"

    def _memory_blocks_url(self, agent_id: str | None = None) -> str:
        if agent_id is None:
            agent_id = self.agent_id
        return f"/json/smarty_pants/agents/{agent_id}/memory/blocks"

    def _memory_block_url(self, agent_id: str | None = None, block_id: str | None = None) -> str:
        if agent_id is None:
            agent_id = self.agent_id
        if block_id is None:
            block_id = self.block_id
        return f"/json/smarty_pants/agents/{agent_id}/memory/blocks/{block_id}"

    def test_list_agent_memory_blocks_requires_sponsors_group(self) -> None:
        self.login("othello")
        result = self.client_get(self._memory_blocks_url())
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    def test_create_agent_memory_block_requires_sponsors_group(self) -> None:
        self.login("othello")
        result = self.client_post(self._memory_blocks_url(), {"label": "foo", "value": "bar"})
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    def test_update_agent_memory_block_requires_sponsors_group(self) -> None:
        self.login("othello")
        result = self.client_patch(self._memory_block_url(), {"label": "foo"})
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    def test_delete_agent_memory_block_requires_sponsors_group(self) -> None:
        self.login("othello")
        result = self.client_delete(self._memory_block_url())
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_list_agent_memory_blocks_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "blocks": [
                {"id": "b1", "label": "persona", "value": "..."},
            ]
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_get(self._memory_blocks_url())
        response_dict = self.assert_json_success(result)
        self.assertEqual(response_dict["blocks"][0]["id"], "b1")

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/blocks/list", call_args[0][1])
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["realmId"], str(self.hamlet.realm_id))
        self.assertEqual(json_data["agentId"], self.agent_id)

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_create_agent_memory_block_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": "success",
            "block": {"id": "new_block"},
        }
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(
            self._memory_blocks_url(),
            {"label": "  persona  ", "value": "hello", "description": "desc"},
        )
        self.assert_json_success(result)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/blocks/create", call_args[0][1])
        json_data = call_args[1]["json"]
        self.assertEqual(
            json_data,
            {
                "realmId": str(self.hamlet.realm_id),
                "agentId": self.agent_id,
                "label": "persona",
                "value": "hello",
                "description": "desc",
            },
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_update_agent_memory_block_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_patch(
            self._memory_block_url(),
            orjson.dumps({"label": " new ", "value": "v2"}).decode(),
            content_type="application/json",
        )
        self.assert_json_success(result)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/blocks/update", call_args[0][1])
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["realmId"], str(self.hamlet.realm_id))
        self.assertEqual(json_data["agentId"], self.agent_id)
        self.assertEqual(json_data["blockId"], self.block_id)
        self.assertEqual(json_data["label"], "new")
        self.assertEqual(json_data["value"], "v2")

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_delete_agent_memory_block_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_delete(self._memory_block_url())
        self.assert_json_success(result)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/memory/blocks/delete", call_args[0][1])
        json_data = call_args[1]["json"]
        self.assertEqual(
            json_data,
            {
                "realmId": str(self.hamlet.realm_id),
                "agentId": self.agent_id,
                "blockId": self.block_id,
            },
        )


class SmartyPantsPauseFacadeEndpointsTestCase(ZulipTestCase):
    """Tests for the Smarty Pants pause/unpause facade endpoint.

    Endpoint:
    - POST /api/v1/smarty_pants/agents/<agent_id>/pause
    """

    def setUp(self) -> None:
        super().setUp()
        realm = get_realm("zulip")
        self.hamlet = self.example_user("hamlet")
        self.othello = self.example_user("othello")
        check_add_user_group(realm, "Sponsors", [self.hamlet], acting_user=self.hamlet)
        self.agent_id = "agent_123"

    def _pause_url(self, agent_id: str | None = None) -> str:
        if agent_id is None:
            agent_id = self.agent_id
        return f"/json/smarty_pants/agents/{agent_id}/pause"

    def test_pause_requires_sponsors_group(self) -> None:
        self.login("othello")
        result = self.client_post(self._pause_url(), {"paused": "true"})
        self.assert_json_error(
            result,
            "You must be a member of the 'Sponsors' user group to manage Smarty Pants agents.",
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_pause_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(self._pause_url(), {"paused": "true"})
        self.assert_json_success(result)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("/s2s/zulip/agents/pause", call_args[0][1])
        json_data = call_args[1]["json"]
        self.assertEqual(
            json_data,
            {
                "realmId": str(self.hamlet.realm_id),
                "agentId": self.agent_id,
                "paused": True,
            },
        )

    @mock.patch.dict(
        os.environ,
        {
            "SMARTY_PANTS_CONTROL_PLANE_BASE_URL": "http://example.com",
            "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET": "test-secret",
        },
        clear=False,
    )
    @mock.patch("zerver.views.smarty_pants.SmartyPantsControlPlaneSession.request")
    def test_unpause_wires_to_control_plane(self, mock_request: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}
        mock_request.return_value = mock_response

        self.login("hamlet")
        result = self.client_post(self._pause_url(), {"paused": "false"})
        self.assert_json_success(result)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        json_data = call_args[1]["json"]
        self.assertEqual(json_data["paused"], False)
