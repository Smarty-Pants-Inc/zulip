from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import wraps
from typing import Any
from urllib.parse import urljoin

import requests
from django.http import HttpRequest, HttpResponse
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt

from zerver.actions.channel_folders import check_add_channel_folder, do_unarchive_channel_folder
from zerver.actions.create_user import do_create_user, do_reactivate_user
from zerver.actions.streams import bulk_add_subscriptions, bulk_remove_subscriptions, do_change_stream_folder
from zerver.actions.users import do_deactivate_user
from zerver.lib.branding import get_branding_context
from zerver.lib.exceptions import AccessDeniedError, JsonableError, ResourceNotFoundError
from zerver.lib.outgoing_http import OutgoingSession
from zerver.lib.response import json_success
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.user_groups import get_recursive_group_members, is_user_in_group
from zerver.lib.users import validate_short_name_and_construct_bot_email
from zerver.models import ChannelFolder, NamedUserGroup, Realm, RealmBranding, Stream
from zerver.models.realms import InvalidFakeEmailDomainError
from zerver.models.users import UserProfile, get_user_by_delivery_email, get_user_profile_by_id_in_realm

SPONSORS_GROUP_NAME = "Sponsors"

PROJECTS_FOLDER_NAME = "Projects"
PROJECTS_FOLDER_DESCRIPTION = "Project channels managed by Smarty Pants."

# Convex control plane base URL (e.g. https://<deployment>.convex.site)
SMARTY_PANTS_CONTROL_PLANE_BASE_URL_ENV_VAR = "SMARTY_PANTS_CONTROL_PLANE_BASE_URL"
# Shared secret validated by Convex `checkZulipFacadeAuth`.
SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR = "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET"

# smartyd runtime plane
SMARTYD_BASE_URL_ENV_VAR = "SMARTYD_BASE_URL"
SMARTYD_TOKEN_ENV_VAR = "SMARTYD_TOKEN"


class SmartyPantsControlPlaneSession(OutgoingSession):
    def __init__(self, shared_secret: str) -> None:
        # Convex accepts either `x-smarty-pants-secret` or an Authorization bearer token.
        super().__init__(
            role="smarty_pants_control_plane",
            timeout=10,
            headers={
                "x-smarty-pants-secret": shared_secret,
                "Authorization": f"Bearer {shared_secret}",
            },
        )


class SmartydSession(OutgoingSession):
    def __init__(self, token: str | None) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(role="smartyd", timeout=15, headers=headers)


@dataclass
class ProvisionedZulipAgent:
    bot_user: UserProfile


def require_sponsors_group_member(
    view_func: Any,
) -> Any:
    """Enforce membership in the realm's 'Sponsors' user group.

    This is MVP-only access control for the Smarty Pants facade.
    """

    @wraps(view_func)
    def _wrapped_view_func(request: HttpRequest, user_profile: UserProfile, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            sponsors_group = NamedUserGroup.objects.get(
                realm_for_sharding=user_profile.realm,
                name=SPONSORS_GROUP_NAME,
            )
        except NamedUserGroup.DoesNotExist:
            raise JsonableError(
                _(
                    "The '{group_name}' user group is missing in this organization. "
                    "Create it (or contact an administrator) to manage Smarty Pants agents."
                ).format(group_name=SPONSORS_GROUP_NAME)
            )

        if sponsors_group.deactivated:
            raise JsonableError(
                _(
                    "The '{group_name}' user group is deactivated in this organization. "
                    "Reactivate it to manage Smarty Pants agents."
                ).format(group_name=SPONSORS_GROUP_NAME)
            )

        if not is_user_in_group(sponsors_group.id, user_profile):
            raise JsonableError(
                _(
                    "You must be a member of the '{group_name}' user group to manage Smarty Pants agents."
                ).format(group_name=SPONSORS_GROUP_NAME)
            )

        return view_func(request, user_profile, *args, **kwargs)

    return _wrapped_view_func


def get_control_plane_config() -> tuple[str, str]:
    base_url = os.environ.get(SMARTY_PANTS_CONTROL_PLANE_BASE_URL_ENV_VAR)
    shared_secret = os.environ.get(SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR)

    if not base_url or not shared_secret:
        raise JsonableError(
            _(
                "Smarty Pants control plane is not configured on this Zulip server. "
                "Missing {base_url_var} and/or {secret_var} environment variables."
            ).format(
                base_url_var=SMARTY_PANTS_CONTROL_PLANE_BASE_URL_ENV_VAR,
                secret_var=SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR,
            )
        )

    return base_url.rstrip("/"), shared_secret


def get_smartyd_config(*, required: bool) -> tuple[str | None, str | None]:
    base_url = os.environ.get(SMARTYD_BASE_URL_ENV_VAR)
    token = os.environ.get(SMARTYD_TOKEN_ENV_VAR) or os.environ.get("SMARTYD_API_KEY")

    if required and not base_url:
        raise JsonableError(
            _(
                "smartyd is not configured on this Zulip server. Missing {base_url_var}. "
                "Set it to the base URL printed by `smarty connect` (e.g. http://127.0.0.1:8788)."
            ).format(base_url_var=SMARTYD_BASE_URL_ENV_VAR)
        )

    return (base_url.rstrip("/") if base_url else None), token


def _parse_request_payload(request: HttpRequest) -> dict[str, Any]:
    """Best-effort JSON/form payload parsing.

    Zulip's public API largely uses form-encoded requests, but internal
    surfaces sometimes prefer application/json.

    We intentionally accept arbitrary payload keys and forward them to the
    control plane; the control plane is responsible for validating inputs.
    """

    if request.content_type is not None and request.content_type.startswith("application/json"):
        if not request.body:
            return {}
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            raise JsonableError(_("Request body is not valid JSON."))

        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise JsonableError(_("Request JSON must be an object."))
        return payload

    data: dict[str, Any] = {}
    for key in request.POST:
        values = request.POST.getlist(key)
        data[key] = values[0] if len(values) == 1 else values
    return data


def _get_smarty_pants_shared_secret() -> str:
    secret = os.environ.get(SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR)
    if not secret:
        raise JsonableError(
            _(
                "Smarty Pants facade shared secret is not configured on this Zulip server. Missing {secret_var}."
            ).format(secret_var=SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR)
        )
    return secret


def _extract_smarty_pants_secret_from_request(request: HttpRequest) -> str | None:
    header = request.headers.get("x-smarty-pants-secret")
    if header is not None:
        token = header.strip()
        if token:
            return token

    auth = request.headers.get("Authorization")
    if auth is not None:
        auth = auth.strip()
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token:
                return token

    return None


def _require_smarty_pants_shared_secret(request: HttpRequest) -> None:
    expected = _get_smarty_pants_shared_secret()
    provided = _extract_smarty_pants_secret_from_request(request)
    if provided is None or not constant_time_compare(provided, expected):
        raise AccessDeniedError()


def _coerce_optional_trimmed_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JsonableError(
            _("The '{field_name}' parameter must be a string.").format(field_name=field_name)
        )
    trimmed = value.strip()
    return trimmed if trimmed else None


def _realm_branding_overrides_dict(branding: RealmBranding | None) -> dict[str, Any]:
    if branding is None:
        return {}

    overrides: dict[str, Any] = {}
    if branding.name:
        overrides["name"] = branding.name
    if branding.support_email:
        overrides["support_email"] = branding.support_email

    urls: dict[str, str] = {}
    if branding.homepage_url:
        urls["homepage"] = branding.homepage_url
    if branding.help_url:
        urls["help"] = branding.help_url
    if branding.status_url:
        urls["status"] = branding.status_url
    if branding.blog_url:
        urls["blog"] = branding.blog_url
    if branding.github_url:
        urls["github"] = branding.github_url

    if urls:
        overrides["urls"] = urls

    return overrides


def _get_realm_for_s2s_request(realm_id_raw: object) -> Realm:
    if not isinstance(realm_id_raw, str):
        realm_id_raw = str(realm_id_raw) if realm_id_raw is not None else ""

    realm_id_raw = realm_id_raw.strip()
    if not realm_id_raw:
        raise JsonableError(_("The 'realm_id' parameter is required."))

    if not realm_id_raw.isdigit():
        raise JsonableError(_("The 'realm_id' parameter must be an integer."))

    try:
        return Realm.objects.get(id=int(realm_id_raw))
    except Realm.DoesNotExist:
        raise ResourceNotFoundError(_("Realm not found."))


@csrf_exempt
def s2s_realm_branding(request: HttpRequest) -> HttpResponse:
    """Get or set per-realm branding overrides.

    Authentication: requires the shared secret configured in
    SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET.

    Supported override keys:
      - name
      - support_email
      - urls: homepage/help/status/blog/github

    GET parameters:
      - realm_id (or realmId)

    POST body (form or JSON):
      - realm_id (or realmId)
      - branding: {name?, support_email?, urls?}

    Empty strings (or null) clear a field.
    """

    _require_smarty_pants_shared_secret(request)

    if request.method == "GET":
        realm = _get_realm_for_s2s_request(request.GET.get("realm_id") or request.GET.get("realmId"))
        branding_row = RealmBranding.objects.filter(realm=realm).first()
        overrides = _realm_branding_overrides_dict(branding_row)
        branding = get_branding_context(realm)
        return json_success(request, data={"realm_id": realm.id, "overrides": overrides, "branding": branding})

    if request.method != "POST":
        return HttpResponse(status=405)

    payload = _parse_request_payload(request)
    realm = _get_realm_for_s2s_request(payload.get("realm_id") or payload.get("realmId"))

    branding_payload: dict[str, Any] | None = None
    raw_branding = payload.get("branding")
    if isinstance(raw_branding, dict):
        branding_payload = raw_branding
    else:
        # Convenience: allow passing override keys at top-level.
        supported_keys = {"name", "support_email", "urls"}
        branding_payload = {k: payload[k] for k in supported_keys if k in payload}

    if not branding_payload:
        raise JsonableError(_("Request must include a 'branding' object."))

    urls_payload = branding_payload.get("urls")
    if urls_payload is not None and not isinstance(urls_payload, dict):
        raise JsonableError(_("The 'urls' parameter must be an object."))

    branding_row, _created = RealmBranding.objects.get_or_create(realm=realm)
    update_fields: list[str] = []

    if "name" in branding_payload:
        branding_row.name = _coerce_optional_trimmed_string(branding_payload.get("name"), field_name="name")
        update_fields.append("name")

    if "support_email" in branding_payload:
        branding_row.support_email = _coerce_optional_trimmed_string(
            branding_payload.get("support_email"),
            field_name="support_email",
        )
        update_fields.append("support_email")

    if isinstance(urls_payload, dict):
        url_field_map = {
            "homepage": ("homepage_url", "urls.homepage"),
            "help": ("help_url", "urls.help"),
            "status": ("status_url", "urls.status"),
            "blog": ("blog_url", "urls.blog"),
            "github": ("github_url", "urls.github"),
        }
        for key, (field, field_name) in url_field_map.items():
            if key not in urls_payload:
                continue
            setattr(
                branding_row,
                field,
                _coerce_optional_trimmed_string(urls_payload.get(key), field_name=field_name),
            )
            update_fields.append(field)

    if update_fields:
        branding_row.save(update_fields=update_fields)

    # Avoid cluttering the DB with empty override rows.
    if (
        branding_row.name is None
        and branding_row.support_email is None
        and branding_row.homepage_url is None
        and branding_row.help_url is None
        and branding_row.status_url is None
        and branding_row.blog_url is None
        and branding_row.github_url is None
    ):
        branding_row.delete()
        branding_row = None

    overrides = _realm_branding_overrides_dict(branding_row)
    branding = get_branding_context(realm)
    return json_success(request, data={"realm_id": realm.id, "overrides": overrides, "branding": branding})


def _control_plane_url(base_url: str, path: str) -> str:
    # urljoin behavior depends on trailing slashes.
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _normalize_control_plane_success_payload(payload: object) -> dict[str, Any]:
    # If the control plane already wraps results in Zulip-style envelope,
    # unwrap it so we don't nest result/msg.
    if isinstance(payload, dict) and payload.get("result") == "success":
        return {k: v for k, v in payload.items() if k not in {"result", "msg"}}

    if isinstance(payload, dict):
        return payload

    return {"data": payload}


def call_control_plane(
    *,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url, shared_secret = get_control_plane_config()
    url = _control_plane_url(base_url, path)

    session = SmartyPantsControlPlaneSession(shared_secret)

    try:
        response = session.request(method, url, params=params, json=json_data)
    except requests.exceptions.RequestException as e:
        raise JsonableError(
            _(
                "Smarty Pants control plane is unavailable. "
                "Please try again later. ({exception_type})"
            ).format(exception_type=type(e).__name__)
        )

    try:
        payload: object = response.json()
    except ValueError:
        raise JsonableError(_("Smarty Pants control plane returned invalid JSON."))

    # Respect a Zulip-style error payload if provided.
    if isinstance(payload, dict) and payload.get("result") == "error":
        message = payload.get("msg")
        if not isinstance(message, str) or not message:
            message = _("Smarty Pants control plane returned an error.")
        raise JsonableError(message)

    if response.status_code >= 400:
        message: str | None = None
        if isinstance(payload, dict):
            for key in ["msg", "error", "message"]:
                value = payload.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        if message is None:
            message = _("Smarty Pants control plane returned HTTP {status_code}.").format(
                status_code=response.status_code
            )
        raise JsonableError(message)

    return _normalize_control_plane_success_payload(payload)


CONTROL_PLANE_LIST_AGENTS_PATH = "/s2s/zulip/agents/list"
CONTROL_PLANE_CREATE_AGENT_PATH = "/s2s/zulip/agents/create"
CONTROL_PLANE_ATTACH_RUNTIME_AGENT_PATH = "/s2s/zulip/agents/attach_runtime"
CONTROL_PLANE_ARCHIVE_AGENT_PATH = "/s2s/zulip/agents/archive"
CONTROL_PLANE_PAUSE_AGENT_PATH = "/s2s/zulip/agents/pause"
CONTROL_PLANE_SET_AGENT_BUDGET_PATH = "/s2s/zulip/agents/budget/set"
CONTROL_PLANE_MEMORY_GET_PATH = "/s2s/zulip/memory/get"
CONTROL_PLANE_MEMORY_SET_PATH = "/s2s/zulip/memory/set"
CONTROL_PLANE_MEMORY_BLOCKS_LIST_PATH = "/s2s/zulip/memory/blocks/list"
CONTROL_PLANE_MEMORY_BLOCKS_CREATE_PATH = "/s2s/zulip/memory/blocks/create"
CONTROL_PLANE_MEMORY_BLOCKS_UPDATE_PATH = "/s2s/zulip/memory/blocks/update"
CONTROL_PLANE_MEMORY_BLOCKS_DELETE_PATH = "/s2s/zulip/memory/blocks/delete"


def _coerce_optional_number(value: object) -> int | float | None:
    """Parse numbers coming from JSON or form-encoded payloads.

    - JSON numbers are accepted as-is.
    - Form values arrive as strings; we accept ints/floats.
    - Empty string clears the value (None).
    """

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        # Accept either integer or float representations.
        try:
            if re.fullmatch(r"[-+]?\d+", raw):
                return int(raw)
            return float(raw)
        except ValueError:
            raise JsonableError(_("Budget values must be numbers."))
    raise JsonableError(_("Budget values must be numbers."))


def _parse_bool_param(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _make_bot_short_name(runtime_agent_id: str) -> str:
    # Zulip bot short names must be alphanumeric-ish; avoid leaking full IDs.
    suffix = re.sub(r"[^a-zA-Z0-9]", "", runtime_agent_id)[-12:] or "agent"
    return f"smarty-pants-{suffix}".lower()


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip()


# NOTE (2026-02-08): Per-agent channel folders/streams were an MVP prototyping layout.
# We now treat Zulip streams as Projects (workspaces) and use DMs for 1:1.
# Agents are attached to Projects by subscribing their bot user to the relevant stream.


def _coerce_bool_param(value: object, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_bool_param(value)
    raise JsonableError(_("The '{field_name}' parameter must be a boolean.").format(field_name=field_name))


def _ensure_projects_channel_folder(user_profile: UserProfile) -> ChannelFolder:
    realm = user_profile.realm

    folder = ChannelFolder.objects.filter(
        realm=realm,
        name__iexact=PROJECTS_FOLDER_NAME,
        is_archived=False,
    ).first()
    if folder is not None:
        return folder

    # Prefer unarchiving a previous Projects folder over creating a second one.
    archived = ChannelFolder.objects.filter(
        realm=realm,
        name__iexact=PROJECTS_FOLDER_NAME,
        is_archived=True,
    ).order_by("-id").first()
    if archived is not None:
        do_unarchive_channel_folder(archived, acting_user=user_profile)
        return archived

    return check_add_channel_folder(
        realm,
        PROJECTS_FOLDER_NAME,
        PROJECTS_FOLDER_DESCRIPTION,
        acting_user=user_profile,
    )


def _get_zulip_bot_user_id_for_agent_id(user_profile: UserProfile, *, agent_id: str) -> int:
    # We currently do not persist the control-plane agentId<->bot mapping in Zulip.
    # So we resolve it via the control plane.
    list_result = _call_control_plane_list_agents(user_profile, include_disabled=True)
    raw_agents = list_result.get("agents")

    if isinstance(raw_agents, list):
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            agent = item.get("agent")
            binding = item.get("binding")
            if not isinstance(agent, dict) or agent.get("id") != agent_id:
                continue
            if not isinstance(binding, dict):
                break
            bot_user_id = binding.get("zulipBotUserId")
            if isinstance(bot_user_id, int):
                return bot_user_id
            break

    raise JsonableError(_("Agent not found or missing Zulip bot binding."))


def _create_runtime_agent_via_smartyd() -> str:
    base_url, token = get_smartyd_config(required=True)
    assert base_url is not None

    session = SmartydSession(token)
    url = _control_plane_url(base_url, "/v1/sessions/open")

    try:
        response = session.request("POST", url, json={"new_agent": True})
    except requests.exceptions.RequestException as e:
        raise JsonableError(
            _(
                "smartyd is unavailable. Please verify {base_url_var} is reachable from the Zulip server. ({exception_type})"
            ).format(base_url_var=SMARTYD_BASE_URL_ENV_VAR, exception_type=type(e).__name__)
        )

    try:
        payload: object = response.json()
    except ValueError:
        raise JsonableError(_("smartyd returned invalid JSON."))

    if response.status_code >= 400:
        message = None
        if isinstance(payload, dict):
            for key in ["message", "error", "msg"]:
                value = payload.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        if message is None:
            message = _("smartyd returned HTTP {status_code}.").format(status_code=response.status_code)
        raise JsonableError(message)

    if not isinstance(payload, dict):
        raise JsonableError(_("smartyd returned an unexpected response."))

    runtime_agent_id = payload.get("agentId") or payload.get("agent_id")
    if not isinstance(runtime_agent_id, str) or not runtime_agent_id:
        raise JsonableError(_("smartyd did not return an agentId."))

    return runtime_agent_id


# (removed) _ensure_channel_folder: no longer provision per-agent folders


def _ensure_agent_bot_user(
    user_profile: UserProfile,
    *,
    agent_name: str,
    runtime_agent_id: str,
) -> UserProfile:
    short_name = _make_bot_short_name(runtime_agent_id)
    try:
        _short_name, email = validate_short_name_and_construct_bot_email(short_name, user_profile.realm)
    except InvalidFakeEmailDomainError:
        raise JsonableError(
            _(
                "Can't create agent bots until FAKE_EMAIL_DOMAIN is correctly configured. Please contact your server administrator."
            )
        )

    try:
        existing = get_user_by_delivery_email(email, user_profile.realm)
        if not existing.is_bot:
            raise JsonableError(
                _(
                    "A user with email {email} already exists and is not a bot. "
                    "Resolve this conflict before attaching the agent."
                ).format(email=email)
            )
        if not existing.is_active:
            do_reactivate_user(existing, acting_user=user_profile)
        return existing
    except UserProfile.DoesNotExist:
        pass

    return do_create_user(
        email=email,
        password=None,
        realm=user_profile.realm,
        full_name=agent_name,
        bot_type=UserProfile.DEFAULT_BOT,
        bot_owner=user_profile,
        acting_user=user_profile,
    )


def _provision_zulip_objects_for_agent(
    user_profile: UserProfile,
    *,
    agent_name: str,
    runtime_agent_id: str,
) -> ProvisionedZulipAgent:
    # New model: do not provision per-agent channels.
    # Agents participate in Projects by being subscribed to those project streams,
    # and 1:1 interaction happens via DMs.
    bot_user = _ensure_agent_bot_user(user_profile, agent_name=agent_name, runtime_agent_id=runtime_agent_id)
    return ProvisionedZulipAgent(bot_user=bot_user)


def _call_control_plane_list_agents(user_profile: UserProfile, *, include_disabled: bool) -> dict[str, Any]:
    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_LIST_AGENTS_PATH,
        json_data={
            "realmId": str(user_profile.realm_id),
            "includeDisabled": include_disabled,
        },
    )


@require_sponsors_group_member
def list_smarty_pants_agents(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    include_disabled = _parse_bool_param(request.GET.get("include_disabled"))
    result = _call_control_plane_list_agents(user_profile, include_disabled=include_disabled)

    raw_agents = result.get("agents")
    agents: list[dict[str, Any]] = []

    if isinstance(raw_agents, list):
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            agent = item.get("agent")
            binding = item.get("binding")
            usage = item.get("usage")
            if not isinstance(agent, dict):
                continue
            entry: dict[str, Any] = dict(agent)

            # Optional fields from the control plane.
            if usage is not None:
                entry["usage"] = usage
            if isinstance(binding, dict):
                entry["binding"] = binding
                paused_at = binding.get("pausedAt")
                if isinstance(paused_at, (int, float)):
                    entry["pausedAt"] = paused_at

                # Prefer flattening the most commonly-used budget fields.
                budget_monthly_usd = binding.get("budgetMonthlyUsd")
                if budget_monthly_usd is not None:
                    entry["budgetMonthlyUsd"] = budget_monthly_usd
                budget_daily_runs = binding.get("budgetDailyRuns")
                if budget_daily_runs is not None:
                    entry["budgetDailyRuns"] = budget_daily_runs
            agents.append(entry)

    return json_success(request, data={"org": result.get("org"), "agents": agents})


@require_sponsors_group_member
def create_smarty_pants_agent(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    payload = _parse_request_payload(request)
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("Agent name is required."))

    runtime_agent_id = payload.get("runtimeAgentId") or payload.get("runtime_agent_id")
    if runtime_agent_id is not None:
        # Primarily for dev/test environments where smartyd is not reachable
        # from the Zulip server, so the client can supply a pre-provisioned
        # runtime agent id.
        if not isinstance(runtime_agent_id, str) or not runtime_agent_id.strip():
            raise JsonableError(_("runtimeAgentId must be a non-empty string."))
        runtime_agent_id = runtime_agent_id.strip()
    else:
        runtime_agent_id = _create_runtime_agent_via_smartyd()

    provisioned = _provision_zulip_objects_for_agent(
        user_profile,
        agent_name=name.strip(),
        runtime_agent_id=runtime_agent_id,
    )

    try:
        control_plane_result = call_control_plane(
            method="POST",
            path=CONTROL_PLANE_ATTACH_RUNTIME_AGENT_PATH,
            json_data={
                "realmId": str(user_profile.realm_id),
                "realmName": user_profile.realm.name,
                "realmUrl": user_profile.realm.url,
                "agent": {
                    "name": name.strip(),
                    "runtimeAgentId": runtime_agent_id,
                },
                "binding": {
                    "zulipBotUserId": provisioned.bot_user.id,
                    "zulipBotEmail": provisioned.bot_user.delivery_email,
                    "zulipBotApiKey": provisioned.bot_user.api_key,
                },
            },
        )
    except Exception:
        # Best-effort rollback: don't leave an active bot with no control-plane binding.
        do_deactivate_user(provisioned.bot_user, acting_user=user_profile)
        raise

    return json_success(
        request,
        data={
            **control_plane_result,
            "zulip_bot_user_id": provisioned.bot_user.id,
        },
    )


@require_sponsors_group_member
def attach_smarty_pants_agent(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    payload = _parse_request_payload(request)
    runtime_agent_id = payload.get("runtime_agent_id")
    if not isinstance(runtime_agent_id, str) or not runtime_agent_id.strip():
        raise JsonableError(_("Runtime agent id is required."))

    runtime_agent_id = runtime_agent_id.strip()

    # Idempotency: before provisioning Zulip objects, check if this runtime agent is
    # already attached with an active binding.
    list_result = _call_control_plane_list_agents(user_profile, include_disabled=True)
    raw_agents = list_result.get("agents")
    if isinstance(raw_agents, list):
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            agent = item.get("agent")
            binding = item.get("binding")
            if not isinstance(agent, dict):
                continue
            if agent.get("runtimeAgentId") != runtime_agent_id:
                continue
            if not isinstance(binding, dict):
                continue
            # Consider it attached if binding exists and is not disabled.
            if binding.get("disabledAt") is not None:
                continue

            # Only short-circuit if we have a complete Zulip bot binding.
            zulip_bot_user_id = binding.get("zulipBotUserId")
            zulip_bot_email = binding.get("zulipBotEmail")
            zulip_bot_api_key = binding.get("zulipBotApiKey")
            if zulip_bot_user_id is None or zulip_bot_email is None or zulip_bot_api_key is None:
                continue

            data: dict[str, Any] = {
                "already_attached": True,
                "agentId": agent.get("id"),
                "zulip_bot_user_id": zulip_bot_user_id,
            }
            binding_id = binding.get("id")
            if binding_id is not None:
                data["bindingId"] = binding_id
            return json_success(request, data=data)

    name = payload.get("name")
    agent_name = name.strip() if isinstance(name, str) and name.strip() else f"Agent {runtime_agent_id[:8]}"

    provisioned = _provision_zulip_objects_for_agent(
        user_profile,
        agent_name=agent_name,
        runtime_agent_id=runtime_agent_id,
    )

    try:
        # Attach existing runtime agent id (idempotent by runtimeAgentId).
        control_plane_result = call_control_plane(
            method="POST",
            path=CONTROL_PLANE_ATTACH_RUNTIME_AGENT_PATH,
            json_data={
                "realmId": str(user_profile.realm_id),
                "realmName": user_profile.realm.name,
                "realmUrl": user_profile.realm.url,
                "agent": {
                    "name": agent_name,
                    "runtimeAgentId": runtime_agent_id,
                },
                "binding": {
                    "zulipBotUserId": provisioned.bot_user.id,
                    "zulipBotEmail": provisioned.bot_user.delivery_email,
                    "zulipBotApiKey": provisioned.bot_user.api_key,
                },
            },
        )
    except Exception:
        do_deactivate_user(provisioned.bot_user, acting_user=user_profile)
        raise

    return json_success(
        request,
        data={
            **control_plane_result,
            "zulip_bot_user_id": provisioned.bot_user.id,
        },
    )


@require_sponsors_group_member
def archive_smarty_pants_agent(request: HttpRequest, user_profile: UserProfile, agent_id: str) -> HttpResponse:
    # Resolve bot user id from the control plane so we can deactivate it locally.
    list_result = _call_control_plane_list_agents(user_profile, include_disabled=True)
    bot_user_id: int | None = None
    if isinstance(list_result.get("agents"), list):
        for item in list_result["agents"]:
            if not isinstance(item, dict):
                continue
            agent = item.get("agent")
            binding = item.get("binding")
            if isinstance(agent, dict) and agent.get("id") == agent_id and isinstance(binding, dict):
                value = binding.get("zulipBotUserId")
                if isinstance(value, int):
                    bot_user_id = value
                break

    archive_result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_ARCHIVE_AGENT_PATH,
        json_data={"realmId": str(user_profile.realm_id), "agentId": agent_id},
    )

    bot_deactivated = False
    bot_deactivation_error: str | None = None
    if bot_user_id is not None:
        try:
            bot_user = get_user_profile_by_id_in_realm(bot_user_id, user_profile.realm)
            if bot_user.is_bot:
                do_deactivate_user(bot_user, acting_user=user_profile)
                bot_deactivated = True
        except Exception as e:
            bot_deactivation_error = f"{type(e).__name__}: {e}"

    return json_success(
        request,
        data={
            **archive_result,
            "zulip_bot_user_id": bot_user_id,
            "bot_deactivated": bot_deactivated,
            "bot_deactivation_error": bot_deactivation_error,
        },
    )


@require_sponsors_group_member
def pause_smarty_pants_agent(request: HttpRequest, user_profile: UserProfile, agent_id: str) -> HttpResponse:
    payload = _parse_request_payload(request)
    paused_raw = payload.get("paused")

    # Zulip public API typically submits form-encoded values, so booleans arrive as strings.
    if paused_raw is None:
        raise JsonableError(_("The 'paused' parameter is required."))

    paused: bool
    if isinstance(paused_raw, bool):
        paused = paused_raw
    elif isinstance(paused_raw, str):
        paused = _parse_bool_param(paused_raw)
    else:
        raise JsonableError(_("The 'paused' parameter must be a boolean."))

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_PAUSE_AGENT_PATH,
        json_data={
            "realmId": str(user_profile.realm_id),
            "agentId": agent_id,
            "paused": paused,
        },
    )

    return json_success(request, data=result)


@require_sponsors_group_member
def set_smarty_pants_agent_budget(request: HttpRequest, user_profile: UserProfile, agent_id: str) -> HttpResponse:
    payload = _parse_request_payload(request)

    # Only forward recognized keys; control plane validates semantics.
    monthly_raw = payload.get("budgetMonthlyUsd")
    daily_runs_raw = payload.get("budgetDailyRuns")

    json_data: dict[str, Any] = {
        "realmId": str(user_profile.realm_id),
        "agentId": agent_id,
    }

    if "budgetMonthlyUsd" in payload:
        json_data["budgetMonthlyUsd"] = _coerce_optional_number(monthly_raw)
    if "budgetDailyRuns" in payload:
        json_data["budgetDailyRuns"] = _coerce_optional_number(daily_runs_raw)

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_SET_AGENT_BUDGET_PATH,
        json_data=json_data,
    )

    return json_success(request, data=result)


@require_sponsors_group_member
def create_smarty_pants_project(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Create (or ensure) a project stream under the "Projects" channel folder.

    Request body (form or JSON):
      - name: project/stream name (required)
      - description: stream description (optional)
      - is_private: boolean (optional; default false)

    Response:
      - stream_id
      - stream_name
      - folder_id
      - created: whether the stream was newly created
      - invite_only
    """

    payload = _parse_request_payload(request)

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("Project name is required."))
    name = name.strip()

    description = payload.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    is_private_value: object | None = None
    if "is_private" in payload:
        is_private_value = payload.get("is_private")
    elif "invite_only" in payload:
        is_private_value = payload.get("invite_only")

    is_private = _coerce_bool_param(is_private_value, field_name="is_private")
    invite_only = bool(is_private) if is_private is not None else False

    folder = _ensure_projects_channel_folder(user_profile)

    stream, created = create_stream_if_needed(
        user_profile.realm,
        name,
        invite_only=invite_only,
        stream_description=description,
        folder=folder,
        acting_user=user_profile,
    )

    if stream.deactivated:
        raise JsonableError(
            _("Channel '{channel_name}' is archived. Unarchive it to use it as a project.").format(
                channel_name=stream.name
            )
        )

    # If the stream already existed, ensure it lives under the Projects folder.
    if not created and stream.folder_id != folder.id:
        do_change_stream_folder(stream, folder, acting_user=user_profile)

    return json_success(
        request,
        data={
            "stream_id": stream.id,
            "stream_name": stream.name,
            "folder_id": folder.id,
            "created": created,
            "invite_only": stream.invite_only,
        },
    )


@require_sponsors_group_member
def subscribe_smarty_pants_agent_to_project(
    request: HttpRequest,
    user_profile: UserProfile,
    stream_id: int,
    agent_id: str,
) -> HttpResponse:
    """Subscribe an agent's bot user to the given project stream."""

    try:
        stream = Stream.objects.get(id=stream_id, realm=user_profile.realm)
    except Stream.DoesNotExist:
        raise ResourceNotFoundError(_("Channel not found."))

    if stream.deactivated:
        raise JsonableError(_("Channel is archived."))

    bot_user_id = _get_zulip_bot_user_id_for_agent_id(user_profile, agent_id=agent_id)
    bot_user = get_user_profile_by_id_in_realm(bot_user_id, user_profile.realm)
    if not bot_user.is_bot:
        raise JsonableError(_("Bound user is not a bot."))

    subs_added, already_subscribed = bulk_add_subscriptions(
        realm=user_profile.realm,
        streams=[stream],
        users=[bot_user],
        acting_user=user_profile,
    )

    return json_success(
        request,
        data={
            "stream_id": stream.id,
            "agent_id": agent_id,
            "zulip_bot_user_id": bot_user.id,
            "subscribed": len(subs_added) > 0,
            "already_subscribed": len(already_subscribed) > 0,
        },
    )


@require_sponsors_group_member
def unsubscribe_smarty_pants_agent_from_project(
    request: HttpRequest,
    user_profile: UserProfile,
    stream_id: int,
    agent_id: str,
) -> HttpResponse:
    """Unsubscribe an agent's bot user from the given project stream."""

    try:
        stream = Stream.objects.get(id=stream_id, realm=user_profile.realm)
    except Stream.DoesNotExist:
        raise ResourceNotFoundError(_("Channel not found."))

    bot_user_id = _get_zulip_bot_user_id_for_agent_id(user_profile, agent_id=agent_id)
    bot_user = get_user_profile_by_id_in_realm(bot_user_id, user_profile.realm)
    if not bot_user.is_bot:
        raise JsonableError(_("Bound user is not a bot."))

    removed, not_subscribed = bulk_remove_subscriptions(
        realm=user_profile.realm,
        users=[bot_user],
        streams=[stream],
        acting_user=user_profile,
    )

    return json_success(
        request,
        data={
            "stream_id": stream.id,
            "agent_id": agent_id,
            "zulip_bot_user_id": bot_user.id,
            "unsubscribed": len(removed) > 0,
            "already_unsubscribed": len(not_subscribed) > 0,
        },
    )


@require_sponsors_group_member
def get_smarty_pants_memory(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Retrieve memory from the Control Plane.
    
    Query parameters:
    - scope: one of "org", "project", or "thread" (required)
    - stream_id: Zulip stream ID (required for project and thread scope)
    - topic: Zulip topic name (required for thread scope)
    """
    scope = request.GET.get("scope")
    if not scope or scope not in {"org", "project", "thread"}:
        raise JsonableError(
            _("The 'scope' parameter is required and must be one of: org, project, thread")
        )

    stream_id = request.GET.get("stream_id")
    topic = request.GET.get("topic")

    # Validate required parameters based on scope
    if scope in {"project", "thread"} and not stream_id:
        raise JsonableError(_("The 'stream_id' parameter is required for '{scope}' scope.").format(scope=scope))
    
    if scope == "thread" and not topic:
        raise JsonableError(_("The 'topic' parameter is required for 'thread' scope."))

    # Build the request payload
    json_data: dict[str, Any] = {
        "realmId": str(user_profile.realm_id),
        "scope": scope,
    }

    if stream_id:
        # Control plane expects `streamId` as a string externalId.
        stream_id_str = str(stream_id)
        if not stream_id_str.isdigit():
            raise JsonableError(_("The 'stream_id' parameter must be an integer."))
        json_data["streamId"] = stream_id_str

    if topic:
        json_data["topic"] = topic

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_GET_PATH,
        json_data=json_data,
    )

    memory = result.get("memory") if isinstance(result, dict) else None
    text = memory.get("text") if isinstance(memory, dict) and isinstance(memory.get("text"), str) else ""
    updated_at = memory.get("updatedAt") if isinstance(memory, dict) else None

    # Keep the UI contract simple: return {text, updatedAt}.
    return json_success(request, data={"text": text, "updatedAt": updated_at})


@require_sponsors_group_member
def set_smarty_pants_memory(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Save memory to the Control Plane.
    
    JSON body parameters:
    - scope: one of "org", "project", or "thread" (required)
    - stream_id: Zulip stream ID (required for project and thread scope)
    - topic: Zulip topic name (required for thread scope)
    - text: memory content (required)
    """
    payload = _parse_request_payload(request)

    scope = payload.get("scope")
    if not scope or scope not in {"org", "project", "thread"}:
        raise JsonableError(
            _("The 'scope' parameter is required and must be one of: org, project, thread")
        )

    text = payload.get("text")
    if not isinstance(text, str):
        raise JsonableError(_("The 'text' parameter is required and must be a string."))

    stream_id = payload.get("stream_id")
    topic = payload.get("topic")

    # Validate required parameters based on scope
    if scope in {"project", "thread"} and stream_id is None:
        raise JsonableError(_("The 'stream_id' parameter is required for '{scope}' scope.").format(scope=scope))
    
    if scope == "thread" and not topic:
        raise JsonableError(_("The 'topic' parameter is required for 'thread' scope."))

    # Build the request payload
    json_data: dict[str, Any] = {
        "realmId": str(user_profile.realm_id),
        "scope": scope,
        "text": text,
    }

    if stream_id is not None:
        # Control plane expects `streamId` as a string externalId.
        stream_id_str = str(stream_id)
        if not stream_id_str.isdigit():
            raise JsonableError(_("The 'stream_id' parameter must be an integer."))
        json_data["streamId"] = stream_id_str
    
    if topic:
        if not isinstance(topic, str):
            raise JsonableError(_("The 'topic' parameter must be a string."))
        json_data["topic"] = topic

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_SET_PATH,
        json_data=json_data,
    )

    return json_success(request, data=result)


@require_sponsors_group_member
def list_agent_memory_blocks(request: HttpRequest, user_profile: UserProfile, agent_id: str) -> HttpResponse:
    """List all memory blocks for a specific agent.
    
    Path parameters:
    - agent_id: The agent ID
    """
    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_LIST_PATH,
        json_data={
            "realmId": str(user_profile.realm_id),
            "agentId": agent_id,
        },
    )

    blocks = result.get("blocks") if isinstance(result, dict) else []
    return json_success(request, data={"blocks": blocks})


@require_sponsors_group_member
def create_agent_memory_block(request: HttpRequest, user_profile: UserProfile, agent_id: str) -> HttpResponse:
    """Create a new memory block for a specific agent.
    
    Path parameters:
    - agent_id: The agent ID
    
    JSON body parameters:
    - label: The block label (required)
    - value: The block value (required)
    - description: The block description (optional)
    """
    payload = _parse_request_payload(request)

    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise JsonableError(_("The 'label' parameter is required and must be a non-empty string."))

    value = payload.get("value")
    if not isinstance(value, str):
        raise JsonableError(_("The 'value' parameter is required and must be a string."))

    json_data: dict[str, Any] = {
        "realmId": str(user_profile.realm_id),
        "agentId": agent_id,
        "label": label.strip(),
        "value": value,
    }

    # Forward optional description if provided.
    # NOTE: Keep payload/response shape backwards compatible for existing UI and e2e tests.
    description = payload.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise JsonableError(_("The 'description' parameter must be a string if provided."))
        json_data["description"] = description

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_CREATE_PATH,
        json_data=json_data,
    )

    return json_success(request, data=result)


@require_sponsors_group_member
def update_agent_memory_block(request: HttpRequest, user_profile: UserProfile, agent_id: str, block_id: str) -> HttpResponse:
    """Update an existing memory block for a specific agent.
    
    Path parameters:
    - agent_id: The agent ID
    - block_id: The block ID
    
    JSON body parameters:
    - label: The block label (optional)
    - value: The block value (optional)
    - description: The block description (optional)
    """
    payload = _parse_request_payload(request)

    json_data: dict[str, Any] = {
        "realmId": str(user_profile.realm_id),
        "agentId": agent_id,
        "blockId": block_id,
    }

    label = payload.get("label")
    if label is not None:
        if not isinstance(label, str) or not label.strip():
            raise JsonableError(_("The 'label' parameter must be a non-empty string if provided."))
        json_data["label"] = label.strip()

    value = payload.get("value")
    if value is not None:
        if not isinstance(value, str):
            raise JsonableError(_("The 'value' parameter must be a string if provided."))
        json_data["value"] = value

    # Forward optional description if provided.
    # NOTE: Keep payload/response shape backwards compatible for existing UI and e2e tests.
    description = payload.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise JsonableError(_("The 'description' parameter must be a string if provided."))
        json_data["description"] = description

    if "label" not in json_data and "value" not in json_data and "description" not in json_data:
        raise JsonableError(_("At least one of 'label', 'value', or 'description' must be provided."))

    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_UPDATE_PATH,
        json_data=json_data,
    )

    return json_success(request, data=result)


@require_sponsors_group_member
def delete_agent_memory_block(request: HttpRequest, user_profile: UserProfile, agent_id: str, block_id: str) -> HttpResponse:
    """Delete a memory block for a specific agent.
    
    Path parameters:
    - agent_id: The agent ID
    - block_id: The block ID
    """
    result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_DELETE_PATH,
        json_data={
            "realmId": str(user_profile.realm_id),
            "agentId": agent_id,
            "blockId": block_id,
        },
    )

    return json_success(request, data=result)
