from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urljoin

import requests
from django.http import HttpRequest, HttpResponse
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt

from zerver.actions.channel_folders import (
    check_add_channel_folder,
    do_archive_channel_folder,
    do_change_channel_folder_description,
    do_change_channel_folder_name,
    do_unarchive_channel_folder,
)
from zerver.actions.default_streams import (
    do_add_default_stream,
    do_add_streams_to_default_stream_group,
    do_change_default_stream_group_description,
    do_change_default_stream_group_name,
    do_create_default_stream_group,
    do_remove_default_stream,
    do_remove_default_stream_group,
    do_remove_streams_from_default_stream_group,
)
from zerver.actions.create_user import do_create_user, do_reactivate_user
from zerver.actions.invites import do_create_multiuse_invite_link
from zerver.actions.streams import (
    bulk_add_subscriptions,
    do_change_stream_description,
    do_change_stream_folder,
    do_change_stream_group_based_setting,
    do_change_stream_message_retention_days,
    do_change_stream_permission,
    do_deactivate_stream,
    do_rename_stream,
    do_unarchive_stream,
)
from zerver.actions.user_groups import (
    bulk_add_members_to_user_groups,
    bulk_remove_members_from_user_groups,
    check_add_user_group,
)
from zerver.actions.realm_settings import (
    do_change_realm_permission_group_setting,
    do_set_realm_property,
    do_set_realm_user_default_setting,
)
from zerver.actions.users import do_change_user_role, do_deactivate_user
from zerver.lib.branding import get_branding_context
from zerver.lib.exceptions import AccessDeniedError, JsonableError, ResourceNotFoundError
from zerver.lib.outgoing_http import OutgoingSession
from zerver.lib.response import json_success
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.users import validate_short_name_and_construct_bot_email
from zerver.lib.user_groups import get_recursive_group_members, is_user_in_group
from zerver.models import (
    ChannelFolder,
    Message,
    NamedUserGroup,
    Realm,
    RealmBranding,
    Stream,
    UserGroupMembership,
)
from zerver.models.groups import SystemGroups
from zerver.models.users import RealmUserDefault, UserProfile, get_user_by_delivery_email, get_user_profile_by_id_in_realm

SPONSORS_GROUP_NAME = "Sponsors"

PROJECTS_FOLDER_NAME = "Projects"
PROJECTS_FOLDER_DESCRIPTION = "Project channels managed by Smarty Pants."
DEFAULT_PROJECT_AGENT_CHANNELS = ("smarty-code", "smarty-graph", "smarty-chat")

# Convex control plane base URL (e.g. https://<deployment>.convex.site)
SMARTY_PANTS_CONTROL_PLANE_BASE_URL_ENV_VAR = "SMARTY_PANTS_CONTROL_PLANE_BASE_URL"
# Shared secret validated by Convex `checkZulipFacadeAuth`.
SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET_ENV_VAR = "SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET"

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


def _coerce_int_param(value: object, *, field_name: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    raise JsonableError(_("The '{field_name}' parameter must be an integer.").format(field_name=field_name))


def _is_realm_admin_user(user_profile: UserProfile) -> bool:
    # Owners have full admin powers; include them defensively.
    return bool(getattr(user_profile, "is_realm_admin", False) or getattr(user_profile, "is_realm_owner", False))


def _is_sponsor_user(user_profile: UserProfile) -> bool:
    try:
        sponsors_group = NamedUserGroup.objects.get(
            realm_for_sharding=user_profile.realm,
            name=SPONSORS_GROUP_NAME,
        )
    except NamedUserGroup.DoesNotExist:
        return False

    if sponsors_group.deactivated:
        return False

    return is_user_in_group(sponsors_group.id, user_profile)


def _parse_args_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise JsonableError(_("The 'args' parameter is not valid JSON."))
        if isinstance(parsed, dict):
            return parsed
        raise JsonableError(_("The 'args' parameter must be a JSON object."))
    raise JsonableError(_("The 'args' parameter must be an object."))


@csrf_exempt
def s2s_smarty_pants_authz_check(request: HttpRequest) -> HttpResponse:
    """S2S authorization introspection for Smarty Pants.

    Authentication: shared secret in SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET.

    POST body (form or JSON):
      - realm_id
      - user_id

    Returns:
      - ok
      - is_realm_admin
      - is_sponsor
    """

    _require_smarty_pants_shared_secret(request)

    if request.method != "POST":
        return HttpResponse(status=405)

    payload = _parse_request_payload(request)
    realm = _get_realm_for_s2s_request(payload.get("realm_id") or payload.get("realmId"))
    user_id = _coerce_int_param(payload.get("user_id") or payload.get("userId"), field_name="user_id")

    try:
        user_profile = get_user_profile_by_id_in_realm(user_id, realm)
    except UserProfile.DoesNotExist:
        raise ResourceNotFoundError(_("User not found."))

    return json_success(
        request,
        data={
            "ok": True,
            "realm_id": realm.id,
            "user_id": user_profile.id,
            "is_realm_admin": _is_realm_admin_user(user_profile),
            "is_sponsor": _is_sponsor_user(user_profile),
        },
    )


def _require_project_stream(stream: Stream, *, folder: ChannelFolder) -> None:
    if stream.folder_id != folder.id:
        raise JsonableError(_("Channel is not in the '{folder_name}' folder.").format(folder_name=PROJECTS_FOLDER_NAME))


def _tool_zulip_project_create(
    *,
    realm: Realm,
    invoker: UserProfile,
    args: dict[str, Any],
) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("Project name is required."))
    name = name.strip()

    description = args.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    is_private = _coerce_bool_param(args.get("is_private") or args.get("invite_only"), field_name="is_private")
    invite_only = bool(is_private) if is_private is not None else False

    folder = _ensure_projects_channel_folder(invoker)

    stream, created = create_stream_if_needed(
        realm,
        name,
        invite_only=invite_only,
        stream_description=description,
        folder=folder,
        acting_user=invoker,
    )

    if stream.deactivated:
        raise JsonableError(
            _("Channel '{channel_name}' is archived. Unarchive it to use it as a project.").format(
                channel_name=stream.name
            )
        )

    if not created and stream.folder_id != folder.id:
        do_change_stream_folder(stream, folder, acting_user=invoker)

    return {
        "stream_id": stream.id,
        "stream_name": stream.name,
        "folder_id": folder.id,
        "created": created,
        "invite_only": stream.invite_only,
    }


def _tool_zulip_project_list(*, realm: Realm) -> dict[str, Any]:
    folder = (
        ChannelFolder.objects.filter(realm=realm, name__iexact=PROJECTS_FOLDER_NAME, is_archived=False).first()
        or ChannelFolder.objects.filter(realm=realm, name__iexact=PROJECTS_FOLDER_NAME).order_by("-id").first()
    )

    streams_payload: list[dict[str, Any]] = []
    if folder is not None:
        streams = Stream.objects.filter(realm=realm, folder=folder).order_by("name")
        for stream in streams:
            streams_payload.append(
                {
                    "stream_id": stream.id,
                    "name": stream.name,
                    "description": stream.description,
                    "invite_only": stream.invite_only,
                    "is_archived": stream.deactivated,
                }
            )

    return {
        "folder_id": folder.id if folder is not None else None,
        "folder_is_archived": folder.is_archived if folder is not None else None,
        "projects": streams_payload,
    }


def _tool_zulip_project_archive(
    *,
    realm: Realm,
    invoker: UserProfile,
    args: dict[str, Any],
) -> dict[str, Any]:
    stream_id = _coerce_int_param(args.get("stream_id") or args.get("streamId"), field_name="stream_id")

    try:
        stream = Stream.objects.get(id=stream_id, realm=realm)
    except Stream.DoesNotExist:
        raise ResourceNotFoundError(_("Channel not found."))

    folder = _ensure_projects_channel_folder(invoker)
    _require_project_stream(stream, folder=folder)

    if stream.deactivated:
        return {"stream_id": stream.id, "already_archived": True}

    do_deactivate_stream(stream, acting_user=invoker)
    return {"stream_id": stream.id, "archived": True}


def _tool_zulip_project_unarchive(
    *,
    realm: Realm,
    invoker: UserProfile,
    args: dict[str, Any],
) -> dict[str, Any]:
    stream_id = _coerce_int_param(args.get("stream_id") or args.get("streamId"), field_name="stream_id")

    try:
        stream = Stream.objects.get(id=stream_id, realm=realm)
    except Stream.DoesNotExist:
        raise ResourceNotFoundError(_("Channel not found."))

    folder = _ensure_projects_channel_folder(invoker)
    _require_project_stream(stream, folder=folder)

    if not stream.deactivated:
        return {"stream_id": stream.id, "already_unarchived": True}

    do_unarchive_stream(stream, stream.name, acting_user=invoker)

    # Ensure the stream still lives under the Projects folder after unarchiving.
    stream.refresh_from_db(fields=["folder_id"])
    if stream.folder_id != folder.id:
        do_change_stream_folder(stream, folder, acting_user=invoker)

    return {"stream_id": stream.id, "unarchived": True}


def _tool_zulip_project_rename(
    *,
    realm: Realm,
    invoker: UserProfile,
    args: dict[str, Any],
) -> dict[str, Any]:
    stream_id = _coerce_int_param(args.get("stream_id") or args.get("streamId"), field_name="stream_id")
    new_name = args.get("new_name") or args.get("newName")
    if not isinstance(new_name, str) or not new_name.strip():
        raise JsonableError(_("The 'new_name' parameter is required."))

    try:
        stream = Stream.objects.get(id=stream_id, realm=realm)
    except Stream.DoesNotExist:
        raise ResourceNotFoundError(_("Channel not found."))

    folder = _ensure_projects_channel_folder(invoker)
    _require_project_stream(stream, folder=folder)

    if stream.deactivated:
        raise JsonableError(_("Channel is archived."))

    do_rename_stream(stream, new_name.strip(), invoker)
    stream.refresh_from_db(fields=["name"])
    return {"stream_id": stream.id, "stream_name": stream.name}


def _tool_zulip_stream_list_active(*, realm: Realm) -> dict[str, Any]:
    streams_payload: list[dict[str, Any]] = []
    streams = Stream.objects.filter(realm=realm, deactivated=False).order_by("name")
    for stream in streams:
        streams_payload.append(
            {
                "stream_id": stream.id,
                "name": stream.name,
                "description": stream.description,
                "invite_only": stream.invite_only,
                "folder_id": stream.folder_id,
            }
        )
    return {"streams": streams_payload}


def _tool_zulip_user_list_active(*, realm: Realm) -> dict[str, Any]:
    users_payload: list[dict[str, Any]] = []
    users = (
        UserProfile.objects.filter(realm=realm, is_active=True, is_bot=False)
        .only("id", "delivery_email", "full_name", "role")
        .order_by("id")
    )
    for u in users:
        users_payload.append(
            {
                "user_id": u.id,
                "email": u.delivery_email,
                "full_name": u.full_name,
                "role": u.role,
                "is_realm_admin": _is_realm_admin_user(u),
            }
        )
    return {"users": users_payload}


def _tool_zulip_user_group_list(*, realm: Realm) -> dict[str, Any]:
    groups_payload: list[dict[str, Any]] = []
    groups = (
        NamedUserGroup.objects.filter(realm=realm)
        .only("id", "name", "description", "deactivated", "is_system_group")
        .order_by("name")
    )
    for g in groups:
        groups_payload.append(
            {
                "group_id": g.id,
                "name": g.name,
                "description": g.description,
                "deactivated": g.deactivated,
                "is_system_group": g.is_system_group,
            }
        )
    return {"groups": groups_payload}


def _get_channel_folder_by_name(*, realm: Realm, name: str) -> ChannelFolder | None:
    return ChannelFolder.objects.filter(realm=realm, name__iexact=name.strip()).order_by("-id").first()


def _get_or_create_channel_folder(*, realm: Realm, name: str, description: str, acting_user: UserProfile) -> ChannelFolder:
    existing = _get_channel_folder_by_name(realm=realm, name=name)
    if existing is not None:
        # Prefer unarchiving rather than creating a second folder.
        if existing.is_archived:
            do_unarchive_channel_folder(existing, acting_user=acting_user)
        return existing

    return check_add_channel_folder(realm, name.strip(), description, acting_user=acting_user)


def _tool_zulip_channel_folder_list(*, realm: Realm) -> dict[str, Any]:
    folders_payload: list[dict[str, Any]] = []
    folders = ChannelFolder.objects.filter(realm=realm).only("id", "name", "description", "is_archived").order_by("order")
    for f in folders:
        folders_payload.append(
            {
                "folder_id": f.id,
                "name": f.name,
                "description": f.description,
                "is_archived": f.is_archived,
            }
        )
    return {"folders": folders_payload}


def _tool_zulip_channel_folder_create(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("Folder name is required."))
    name = name.strip()

    description = args.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    existing = _get_channel_folder_by_name(realm=realm, name=name)
    if existing is not None:
        if existing.is_archived:
            do_unarchive_channel_folder(existing, acting_user=invoker)
        return {"folder_id": existing.id, "name": existing.name, "created": False, "is_archived": existing.is_archived}

    folder = check_add_channel_folder(realm, name, description, acting_user=invoker)
    return {"folder_id": folder.id, "name": folder.name, "created": True, "is_archived": folder.is_archived}


def _resolve_channel_folder(*, realm: Realm, args: dict[str, Any]) -> ChannelFolder:
    folder_id_raw = args.get("folder_id") or args.get("folderId")
    folder_name_raw = args.get("folder_name") or args.get("folderName") or args.get("name")

    if folder_id_raw is not None:
        folder_id = _coerce_int_param(folder_id_raw, field_name="folder_id")
        try:
            return ChannelFolder.objects.get(id=folder_id, realm=realm)
        except ChannelFolder.DoesNotExist:
            raise ResourceNotFoundError(_("Folder not found."))

    if isinstance(folder_name_raw, str) and folder_name_raw.strip():
        folder = _get_channel_folder_by_name(realm=realm, name=folder_name_raw)
        if folder is None:
            raise ResourceNotFoundError(_("Folder not found."))
        return folder

    raise JsonableError(_("Provide 'folder_id' or 'folder_name'."))


def _tool_zulip_channel_folder_update(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    folder = _resolve_channel_folder(realm=realm, args=args)

    new_name = args.get("new_name") or args.get("newName")
    description = args.get("description")

    if new_name is not None:
        if not isinstance(new_name, str) or not new_name.strip():
            raise JsonableError(_("The 'new_name' parameter must be a non-empty string."))
        do_change_channel_folder_name(folder, new_name.strip(), acting_user=invoker)

    if description is not None:
        if not isinstance(description, str):
            raise JsonableError(_("The 'description' parameter must be a string."))
        do_change_channel_folder_description(folder, description, acting_user=invoker)

    folder.refresh_from_db(fields=["name", "description", "is_archived"])
    return {"folder_id": folder.id, "name": folder.name, "description": folder.description, "is_archived": folder.is_archived}


def _tool_zulip_channel_folder_archive(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    folder = _resolve_channel_folder(realm=realm, args=args)
    if folder.is_archived:
        return {"folder_id": folder.id, "already_archived": True}
    do_archive_channel_folder(folder, acting_user=invoker)
    return {"folder_id": folder.id, "archived": True}


def _tool_zulip_channel_folder_unarchive(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    folder = _resolve_channel_folder(realm=realm, args=args)
    if not folder.is_archived:
        return {"folder_id": folder.id, "already_unarchived": True}
    do_unarchive_channel_folder(folder, acting_user=invoker)
    return {"folder_id": folder.id, "unarchived": True}


def _tool_zulip_stream_create(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("Channel name is required."))
    name = name.strip()

    description = args.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    is_private = _coerce_bool_param(args.get("is_private") or args.get("invite_only"), field_name="is_private")
    invite_only = bool(is_private) if is_private is not None else False

    folder_name = args.get("folder_name") or args.get("folderName")
    folder: ChannelFolder | None = None
    if isinstance(folder_name, str) and folder_name.strip():
        folder = _get_or_create_channel_folder(
            realm=realm,
            name=folder_name,
            description=f"Channel folder '{folder_name.strip()}' managed by Smarty Pants.",
            acting_user=invoker,
        )

    stream, created = create_stream_if_needed(
        realm,
        name,
        invite_only=invite_only,
        stream_description=description,
        folder=folder,
        acting_user=invoker,
    )

    if stream.deactivated:
        raise JsonableError(_("Channel '{channel_name}' is archived.").format(channel_name=stream.name))

    if folder is not None and stream.folder_id != folder.id:
        do_change_stream_folder(stream, folder, acting_user=invoker)

    return {
        "stream_id": stream.id,
        "name": stream.name,
        "created": created,
        "invite_only": stream.invite_only,
        "folder_id": stream.folder_id,
    }


def _resolve_stream(*, realm: Realm, args: dict[str, Any]) -> Stream:
    stream_id_raw = args.get("stream_id") or args.get("streamId")
    if stream_id_raw is not None:
        stream_id = _coerce_int_param(stream_id_raw, field_name="stream_id")
        try:
            return Stream.objects.get(id=stream_id, realm=realm)
        except Stream.DoesNotExist:
            raise ResourceNotFoundError(_("Channel not found."))

    name_raw = args.get("name")
    if isinstance(name_raw, str) and name_raw.strip():
        stream = Stream.objects.filter(realm=realm, name__iexact=name_raw.strip()).order_by("-id").first()
        if stream is None:
            raise ResourceNotFoundError(_("Channel not found."))
        return stream

    raise JsonableError(_("Provide 'stream_id' or 'name'."))


def _tool_zulip_stream_archive(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    stream = _resolve_stream(realm=realm, args=args)
    if stream.deactivated:
        return {"stream_id": stream.id, "already_archived": True}
    do_deactivate_stream(stream, acting_user=invoker)
    return {"stream_id": stream.id, "archived": True}


def _tool_zulip_stream_unarchive(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    stream = _resolve_stream(realm=realm, args=args)
    if not stream.deactivated:
        return {"stream_id": stream.id, "already_unarchived": True}
    do_unarchive_stream(stream, stream.name, acting_user=invoker)
    return {"stream_id": stream.id, "unarchived": True}


def _tool_zulip_stream_rename(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    stream = _resolve_stream(realm=realm, args=args)
    new_name = args.get("new_name") or args.get("newName")
    if not isinstance(new_name, str) or not new_name.strip():
        raise JsonableError(_("The 'new_name' parameter is required."))
    if stream.deactivated:
        raise JsonableError(_("Channel is archived."))
    do_rename_stream(stream, new_name.strip(), invoker)
    stream.refresh_from_db(fields=["name"])
    return {"stream_id": stream.id, "name": stream.name}


def _tool_zulip_stream_move_to_folder(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    stream = _resolve_stream(realm=realm, args=args)
    folder_name = args.get("folder_name") or args.get("folderName")
    if not isinstance(folder_name, str) or not folder_name.strip():
        raise JsonableError(_("The 'folder_name' parameter is required."))

    folder = _get_or_create_channel_folder(
        realm=realm,
        name=folder_name,
        description=f"Channel folder '{folder_name.strip()}' managed by Smarty Pants.",
        acting_user=invoker,
    )

    if stream.folder_id != folder.id:
        do_change_stream_folder(stream, folder, acting_user=invoker)
    return {"stream_id": stream.id, "folder_id": folder.id, "folder_name": folder.name}


def _resolve_user(*, realm: Realm, args: dict[str, Any]) -> UserProfile:
    user_id_raw = args.get("user_id") or args.get("userId")
    email_raw = args.get("email")

    if user_id_raw is not None:
        user_id = _coerce_int_param(user_id_raw, field_name="user_id")
        return get_user_profile_by_id_in_realm(user_id, realm)

    if isinstance(email_raw, str) and email_raw.strip():
        return get_user_by_delivery_email(email_raw.strip(), realm)

    raise JsonableError(_("Provide 'user_id' or 'email'."))


def _tool_zulip_user_deactivate(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    try:
        user = _resolve_user(realm=realm, args=args)
    except UserProfile.DoesNotExist:
        raise ResourceNotFoundError(_("User not found."))

    if not user.is_active:
        return {"user_id": user.id, "already_deactivated": True}

    do_deactivate_user(user, acting_user=invoker)
    return {"user_id": user.id, "deactivated": True}


def _role_value_from_string(role: str) -> int:
    r = (role or "").strip().lower()
    if r in {"member"}:
        return UserProfile.ROLE_MEMBER
    if r in {"moderator", "mod"}:
        return UserProfile.ROLE_MODERATOR
    if r in {"admin", "administrator", "realm_admin"}:
        return UserProfile.ROLE_REALM_ADMINISTRATOR
    if r in {"owner", "realm_owner"}:
        return UserProfile.ROLE_REALM_OWNER
    raise JsonableError(_("Invalid role. Use one of: member, moderator, admin, owner."))


def _tool_zulip_user_role_set(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    role_raw = args.get("role")
    if not isinstance(role_raw, str) or not role_raw.strip():
        raise JsonableError(_("The 'role' parameter is required."))

    try:
        user = _resolve_user(realm=realm, args=args)
    except UserProfile.DoesNotExist:
        raise ResourceNotFoundError(_("User not found."))

    value = _role_value_from_string(role_raw)
    do_change_user_role(user, value, acting_user=invoker, notify=True)
    user.refresh_from_db(fields=["role"])
    return {"user_id": user.id, "role": user.role}


def _resolve_user_group(*, realm: Realm, args: dict[str, Any]) -> NamedUserGroup:
    group_id_raw = args.get("group_id") or args.get("groupId")
    group_name_raw = args.get("group_name") or args.get("groupName") or args.get("name")

    if group_id_raw is not None:
        group_id = _coerce_int_param(group_id_raw, field_name="group_id")
        try:
            return NamedUserGroup.objects.get(id=group_id, realm=realm)
        except NamedUserGroup.DoesNotExist:
            raise ResourceNotFoundError(_("User group not found."))

    if isinstance(group_name_raw, str) and group_name_raw.strip():
        g = NamedUserGroup.objects.filter(realm=realm, name__iexact=group_name_raw.strip()).order_by("-id").first()
        if g is None:
            raise ResourceNotFoundError(_("User group not found."))
        return g

    raise JsonableError(_("Provide 'group_id' or 'group_name'."))


def _tool_zulip_user_group_create(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("User group name is required."))
    name = name.strip()

    description = args.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    existing = NamedUserGroup.objects.filter(realm=realm, name__iexact=name).order_by("-id").first()
    if existing is not None:
        return {"group_id": existing.id, "name": existing.name, "created": False, "deactivated": existing.deactivated}

    group = check_add_user_group(realm, name, [], acting_user=invoker)
    group.description = description
    group.save(update_fields=["description"])
    return {"group_id": group.id, "name": group.name, "created": True, "deactivated": group.deactivated}


def _resolve_user_ids_list(*, realm: Realm, args: dict[str, Any]) -> list[int]:
    user_ids: list[int] = []

    raw_ids = args.get("user_ids") or args.get("userIds")
    raw_emails = args.get("emails") or args.get("email")

    if raw_ids is not None:
        if isinstance(raw_ids, list):
            for v in raw_ids:
                user_ids.append(_coerce_int_param(v, field_name="user_ids"))
        else:
            user_ids.append(_coerce_int_param(raw_ids, field_name="user_ids"))

    if raw_emails is not None:
        emails: list[str] = []
        if isinstance(raw_emails, list):
            emails = [str(e) for e in raw_emails]
        else:
            emails = [str(raw_emails)]

        for e in emails:
            e = (e or "").strip()
            if not e:
                continue
            try:
                up = get_user_by_delivery_email(e, realm)
            except UserProfile.DoesNotExist:
                raise ResourceNotFoundError(_("User not found: {email}").format(email=e))
            user_ids.append(up.id)

    # De-dupe while preserving order.
    seen: set[int] = set()
    out: list[int] = []
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    if not out:
        raise JsonableError(_("No users provided."))
    return out


def _tool_zulip_user_group_members_add(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_user_group(realm=realm, args=args)
    user_ids = _resolve_user_ids_list(realm=realm, args=args)

    existing = set(
        UserGroupMembership.objects.filter(user_group=group, user_profile_id__in=user_ids).values_list(
            "user_profile_id", flat=True
        )
    )
    to_add = [uid for uid in user_ids if uid not in existing]
    if to_add:
        bulk_add_members_to_user_groups([group], to_add, acting_user=invoker)

    return {"group_id": group.id, "added_user_ids": to_add, "already_members": sorted(existing)}


def _tool_zulip_user_group_members_remove(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_user_group(realm=realm, args=args)
    user_ids = _resolve_user_ids_list(realm=realm, args=args)
    bulk_remove_members_from_user_groups([group], user_ids, acting_user=invoker)
    return {"group_id": group.id, "removed_user_ids": user_ids}


def _tool_zulip_realm_branding_set(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    branding_row, _created = RealmBranding.objects.get_or_create(realm=realm)
    update_fields: list[str] = []

    if "name" in args:
        branding_row.name = _coerce_optional_trimmed_string(args.get("name"), field_name="name")
        update_fields.append("name")

    if "support_email" in args:
        branding_row.support_email = _coerce_optional_trimmed_string(args.get("support_email"), field_name="support_email")
        update_fields.append("support_email")

    urls_payload = args.get("urls")
    if urls_payload is not None and not isinstance(urls_payload, dict):
        raise JsonableError(_("The 'urls' parameter must be an object."))

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
    return {"realm_id": realm.id, "overrides": overrides, "branding": branding}


def _tool_cp_agent_pause(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))

    paused_raw = args.get("paused")
    if not isinstance(paused_raw, bool):
        raise JsonableError(_("The 'paused' parameter must be a boolean."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_PAUSE_AGENT_PATH,
        json_data={
            "realmId": str(realm.id),
            "agentId": agent_id.strip(),
            "paused": paused_raw,
        },
    )


def _project_agent_short_name(channel_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", channel_name.strip().lower()).strip("-")
    if not slug:
        raise JsonableError(_("Invalid channel name for provisioning project agent."))
    return f"{slug}-agent"


def _project_agent_full_name(channel_name: str) -> str:
    pretty = " ".join(word.capitalize() for word in re.split(r"[-_\s]+", channel_name.strip()) if word)
    if not pretty:
        raise JsonableError(_("Invalid channel name for provisioning project agent."))
    return f"Smarty {pretty} Agent"


def _ensure_project_agent_bot_for_stream(*, realm: Realm, stream: Stream, invoker: UserProfile) -> dict[str, Any]:
    short_name = _project_agent_short_name(stream.name)
    full_name = _project_agent_full_name(stream.name)
    _validated_short_name, email = validate_short_name_and_construct_bot_email(short_name, realm)

    bot = UserProfile.objects.filter(realm=realm, delivery_email__iexact=email).order_by("-id").first()
    if bot is not None:
        if not bot.is_bot:
            raise JsonableError(
                _("Expected '{email}' to be a bot user, but found a human user.").format(email=email)
            )
        if not bot.is_active:
            do_reactivate_user(bot, acting_user=invoker)
    else:
        bot = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name=full_name,
            bot_type=UserProfile.DEFAULT_BOT,
            bot_owner=invoker,
            acting_user=invoker,
        )

    if bot.full_name != full_name:
        bot.full_name = full_name
        bot.save(update_fields=["full_name"])

    # Idempotently ensures the bot is subscribed without duplicating rows.
    bulk_add_subscriptions(realm, [stream], [bot], acting_user=invoker)
    bot.refresh_from_db(fields=["id", "delivery_email", "api_key"])

    return {
        "botEmail": bot.delivery_email,
        "botUserId": bot.id,
        "botApiKey": bot.api_key,
    }


def _tool_cp_project_agents_provision_defaults(*, realm: Realm, invoker: UserProfile) -> dict[str, Any]:
    projects_listing = _tool_zulip_project_list(realm=realm)
    listed_projects = projects_listing.get("projects")
    if not isinstance(listed_projects, list):
        listed_projects = []

    by_name: dict[str, dict[str, Any]] = {}
    for project in listed_projects:
        if not isinstance(project, dict):
            continue
        name = project.get("name")
        if not isinstance(name, str):
            continue
        by_name[name.strip().lower()] = project

    provisioned: list[dict[str, Any]] = []
    provisioned_public: list[dict[str, Any]] = []
    for channel_name in DEFAULT_PROJECT_AGENT_CHANNELS:
        project_row = by_name.get(channel_name)
        if project_row is None:
            raise JsonableError(
                _("Required project channel '{channel_name}' was not found in the Projects folder.").format(
                    channel_name=channel_name
                )
            )

        if bool(project_row.get("is_archived")):
            raise JsonableError(
                _("Required project channel '{channel_name}' is archived.").format(channel_name=channel_name)
            )

        stream_id = _coerce_int_param(project_row.get("stream_id"), field_name="stream_id")
        try:
            stream = Stream.objects.get(id=stream_id, realm=realm)
        except Stream.DoesNotExist:
            raise ResourceNotFoundError(_("Channel not found."))

        bot_creds = _ensure_project_agent_bot_for_stream(realm=realm, stream=stream, invoker=invoker)

        # Control plane provisioning needs botApiKey, but we should never return API keys
        # in tool responses (they are secrets).
        provisioned.append(
            {
                "streamId": stream.id,
                "streamName": stream.name,
                **bot_creds,
            }
        )
        provisioned_public.append(
            {
                "streamId": stream.id,
                "streamName": stream.name,
                "botEmail": bot_creds["botEmail"],
                "botUserId": bot_creds["botUserId"],
            }
        )

    control_plane_result = call_control_plane(
        method="POST",
        path=CONTROL_PLANE_PROJECT_AGENTS_PROVISION_DEFAULTS_PATH,
        json_data={
            "realmId": str(realm.id),
            "realmName": realm.string_id,
            "realmUrl": realm.url,
            "projectAgents": provisioned,
        },
    )

    return {
        "projects": provisioned_public,
        "controlPlane": control_plane_result,
    }


def _tool_cp_agent_budget_set(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))

    json_data: dict[str, Any] = {
        "realmId": str(realm.id),
        "agentId": agent_id.strip(),
    }

    if "budgetMonthlyUsd" in args:
        json_data["budgetMonthlyUsd"] = _coerce_optional_number(args.get("budgetMonthlyUsd"))
    if "budgetDailyRuns" in args:
        json_data["budgetDailyRuns"] = _coerce_optional_number(args.get("budgetDailyRuns"))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_SET_AGENT_BUDGET_PATH,
        json_data=json_data,
    )


def _tool_cp_agent_archive(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_ARCHIVE_AGENT_PATH,
        json_data={
            "realmId": str(realm.id),
            "agentId": agent_id.strip(),
        },
    )


def _tool_cp_memory_set(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope")
    if not isinstance(scope, str) or scope.strip() not in {"org", "project", "thread"}:
        raise JsonableError(_("The 'scope' parameter must be one of: org, project, thread."))

    text = args.get("text")
    if not isinstance(text, str):
        raise JsonableError(_("The 'text' parameter must be a string."))

    json_data: dict[str, Any] = {
        "scope": scope.strip(),
        "realmId": str(realm.id),
        "realmName": realm.string_id,
        "text": text,
    }

    # Optional metadata.
    for k in ["streamId", "streamName", "topic", "anchorMessageId"]:
        if k in args:
            json_data[k] = args.get(k)

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_SET_PATH,
        json_data=json_data,
    )


def _tool_cp_memory_get(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope")
    if not isinstance(scope, str) or scope.strip() not in {"org", "project", "thread"}:
        raise JsonableError(_("The 'scope' parameter must be one of: org, project, thread."))

    json_data: dict[str, Any] = {
        "scope": scope.strip(),
        "realmId": str(realm.id),
    }
    for k in ["streamId", "topic"]:
        if k in args:
            json_data[k] = args.get(k)

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_GET_PATH,
        json_data=json_data,
    )


def _tool_cp_memory_blocks_list(*, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_LIST_PATH,
        json_data={"agentId": agent_id.strip()},
    )


def _tool_cp_memory_blocks_create(*, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    label = args.get("label")
    value = args.get("value")
    description = args.get("description")

    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))
    if not isinstance(label, str) or not label.strip():
        raise JsonableError(_("The 'label' parameter is required."))
    if not isinstance(value, str):
        raise JsonableError(_("The 'value' parameter must be a string."))
    if description is not None and not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))

    json_data: dict[str, Any] = {"agentId": agent_id.strip(), "label": label.strip(), "value": value}
    if isinstance(description, str):
        json_data["description"] = description

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_CREATE_PATH,
        json_data=json_data,
    )


def _tool_cp_memory_blocks_update(*, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    block_id = args.get("blockId") or args.get("block_id")
    value = args.get("value")
    description = args.get("description")

    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))
    if not isinstance(block_id, str) or not block_id.strip():
        raise JsonableError(_("The 'blockId' parameter is required."))
    if value is not None and not isinstance(value, str):
        raise JsonableError(_("The 'value' parameter must be a string."))
    if description is not None and not isinstance(description, str):
        raise JsonableError(_("The 'description' parameter must be a string."))
    if value is None and description is None:
        raise JsonableError(_("Provide at least one of 'value' or 'description'."))

    json_data: dict[str, Any] = {"agentId": agent_id.strip(), "blockId": block_id.strip()}
    if isinstance(value, str):
        json_data["value"] = value
    if isinstance(description, str):
        json_data["description"] = description

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_UPDATE_PATH,
        json_data=json_data,
    )


def _tool_cp_memory_blocks_delete(*, args: dict[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agentId") or args.get("agent_id")
    block_id = args.get("blockId") or args.get("block_id")

    if not isinstance(agent_id, str) or not agent_id.strip():
        raise JsonableError(_("The 'agentId' parameter is required."))
    if not isinstance(block_id, str) or not block_id.strip():
        raise JsonableError(_("The 'blockId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_MEMORY_BLOCKS_DELETE_PATH,
        json_data={"agentId": agent_id.strip(), "blockId": block_id.strip()},
    )


def _tool_cp_agents_index(*, realm: Realm) -> dict[str, Any]:
    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_AGENTS_INDEX_PATH,
        json_data={"realmId": str(realm.id)},
    )


def _tool_cp_letta_agents_retrieve(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    runtime_agent_id = _coerce_optional_trimmed_string(
        args.get("runtimeAgentId") or args.get("agentId") or args.get("agent_id"),
        field_name="runtimeAgentId",
    )
    if runtime_agent_id is None:
        raise JsonableError(_("The 'runtimeAgentId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_LETTA_AGENTS_RETRIEVE_PATH,
        json_data={"realmId": str(realm.id), "runtimeAgentId": runtime_agent_id},
    )


def _tool_cp_letta_runs_list(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    runtime_agent_id = _coerce_optional_trimmed_string(
        args.get("runtimeAgentId") or args.get("agentId") or args.get("agent_id"),
        field_name="runtimeAgentId",
    )
    if runtime_agent_id is None:
        raise JsonableError(_("The 'runtimeAgentId' parameter is required."))

    json_data: dict[str, Any] = {"realmId": str(realm.id), "runtimeAgentId": runtime_agent_id}
    for k in ["limit", "after", "before", "order", "conversationId"]:
        if k in args:
            json_data[k] = args.get(k)

    return call_control_plane(method="POST", path=CONTROL_PLANE_LETTA_RUNS_LIST_PATH, json_data=json_data)


def _tool_cp_letta_runs_retrieve(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    run_id = _coerce_optional_trimmed_string(args.get("runId") or args.get("run_id"), field_name="runId")
    runtime_agent_id = _coerce_optional_trimmed_string(
        args.get("runtimeAgentId") or args.get("agentId") or args.get("agent_id"),
        field_name="runtimeAgentId",
    )
    if run_id is None:
        raise JsonableError(_("The 'runId' parameter is required."))
    if runtime_agent_id is None:
        raise JsonableError(_("The 'runtimeAgentId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_LETTA_RUNS_RETRIEVE_PATH,
        json_data={"realmId": str(realm.id), "runtimeAgentId": runtime_agent_id, "runId": run_id},
    )


def _tool_cp_letta_runs_usage_retrieve(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    run_id = _coerce_optional_trimmed_string(args.get("runId") or args.get("run_id"), field_name="runId")
    runtime_agent_id = _coerce_optional_trimmed_string(
        args.get("runtimeAgentId") or args.get("agentId") or args.get("agent_id"),
        field_name="runtimeAgentId",
    )
    if run_id is None:
        raise JsonableError(_("The 'runId' parameter is required."))
    if runtime_agent_id is None:
        raise JsonableError(_("The 'runtimeAgentId' parameter is required."))

    return call_control_plane(
        method="POST",
        path=CONTROL_PLANE_LETTA_RUNS_USAGE_RETRIEVE_PATH,
        json_data={"realmId": str(realm.id), "runtimeAgentId": runtime_agent_id, "runId": run_id},
    )


def _tool_cp_letta_runs_steps_list(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    run_id = _coerce_optional_trimmed_string(args.get("runId") or args.get("run_id"), field_name="runId")
    runtime_agent_id = _coerce_optional_trimmed_string(
        args.get("runtimeAgentId") or args.get("agentId") or args.get("agent_id"),
        field_name="runtimeAgentId",
    )
    if run_id is None:
        raise JsonableError(_("The 'runId' parameter is required."))
    if runtime_agent_id is None:
        raise JsonableError(_("The 'runtimeAgentId' parameter is required."))

    json_data: dict[str, Any] = {"realmId": str(realm.id), "runtimeAgentId": runtime_agent_id, "runId": run_id}
    for k in ["limit", "after"]:
        if k in args:
            json_data[k] = args.get(k)

    return call_control_plane(method="POST", path=CONTROL_PLANE_LETTA_RUNS_STEPS_LIST_PATH, json_data=json_data)


def _invite_as_from_string(role: str) -> int:
    r = (role or "").strip().lower()
    # Values should match PreregistrationUser.INVITE_AS.
    if r in {"member"}:
        return 400
    if r in {"guest", "guest_user", "guest-user"}:
        return 600
    if r in {"moderator", "mod"}:
        return 300
    if r in {"admin", "administrator", "realm_admin"}:
        return 200
    if r in {"owner", "realm_owner"}:
        return 100
    raise JsonableError(_("Invalid invited_as. Use one of: member, guest, moderator, admin, owner."))


def _tool_zulip_invite_link_create(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    invited_as_raw = args.get("invited_as") or args.get("invitedAs")
    invited_as = 400
    if invited_as_raw is not None:
        if not isinstance(invited_as_raw, str):
            raise JsonableError(_("The 'invited_as' parameter must be a string."))
        invited_as = _invite_as_from_string(invited_as_raw)

    expires_raw = args.get("invite_expires_in_minutes") or args.get("inviteExpiresInMinutes")
    invite_expires_in_minutes: int | None = None
    if expires_raw is not None:
        invite_expires_in_minutes = _coerce_int_param(expires_raw, field_name="invite_expires_in_minutes")
        if invite_expires_in_minutes <= 0:
            raise JsonableError(_("invite_expires_in_minutes must be positive."))

    include_defaults_raw = args.get("include_realm_default_subscriptions")
    include_defaults = True
    if include_defaults_raw is not None:
        if not isinstance(include_defaults_raw, bool):
            raise JsonableError(_("include_realm_default_subscriptions must be a boolean."))
        include_defaults = include_defaults_raw

    stream_ids_raw = args.get("stream_ids") or args.get("streamIds") or []
    group_ids_raw = args.get("group_ids") or args.get("groupIds") or []

    stream_ids: list[int] = []
    if isinstance(stream_ids_raw, list):
        stream_ids = [_coerce_int_param(v, field_name="stream_ids") for v in stream_ids_raw]
    elif stream_ids_raw:
        stream_ids = [_coerce_int_param(stream_ids_raw, field_name="stream_ids")]

    group_ids: list[int] = []
    if isinstance(group_ids_raw, list):
        group_ids = [_coerce_int_param(v, field_name="group_ids") for v in group_ids_raw]
    elif group_ids_raw:
        group_ids = [_coerce_int_param(group_ids_raw, field_name="group_ids")]

    streams: list[Stream] = []
    if stream_ids:
        streams = list(Stream.objects.filter(realm=realm, id__in=stream_ids))
        if len(streams) != len(set(stream_ids)):
            raise ResourceNotFoundError(_("One or more channels not found."))

    groups: list[NamedUserGroup] = []
    if group_ids:
        groups = list(NamedUserGroup.objects.filter(realm=realm, id__in=group_ids))
        if len(groups) != len(set(group_ids)):
            raise ResourceNotFoundError(_("One or more user groups not found."))

    welcome_message_custom_text = args.get("welcome_message_custom_text") or args.get("welcomeMessageCustomText")
    if welcome_message_custom_text is not None and not isinstance(welcome_message_custom_text, str):
        raise JsonableError(_("welcome_message_custom_text must be a string."))

    url = do_create_multiuse_invite_link(
        invoker,
        invited_as,
        invite_expires_in_minutes,
        include_defaults,
        streams=streams,
        user_groups=groups,
        welcome_message_custom_text=welcome_message_custom_text,
    )

    return {
        "url": url,
        "invited_as": invited_as,
        "include_realm_default_subscriptions": include_defaults,
        "stream_ids": stream_ids,
        "group_ids": group_ids,
    }


def _system_group(realm: Realm, group_name: str) -> NamedUserGroup:
    g = NamedUserGroup.objects.filter(realm=realm, name=group_name, is_system_group=True).first()
    if g is None:
        raise JsonableError(_("System group '{group_name}' not found.").format(group_name=group_name))
    return g


def _tool_zulip_stream_update(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    stream = _resolve_stream(realm=realm, args=args)
    changes: dict[str, Any] = {"stream_id": stream.id}

    if "description" in args:
        desc = args.get("description")
        if not isinstance(desc, str):
            raise JsonableError(_("The 'description' parameter must be a string."))
        do_change_stream_description(stream, desc, acting_user=invoker)
        changes["description"] = desc

    # Privacy/web-public/history changes.
    privacy_keys = {"is_private", "invite_only", "is_web_public", "history_public_to_subscribers"}
    if any(k in args for k in privacy_keys):
        invite_only = bool(stream.invite_only)
        history_public = bool(stream.history_public_to_subscribers)
        is_web_public = bool(stream.is_web_public)

        if "is_private" in args or "invite_only" in args:
            raw = args.get("is_private") if "is_private" in args else args.get("invite_only")
            if not isinstance(raw, bool):
                raise JsonableError(_("is_private must be a boolean."))
            invite_only = bool(raw)

        if "history_public_to_subscribers" in args:
            raw = args.get("history_public_to_subscribers")
            if not isinstance(raw, bool):
                raise JsonableError(_("history_public_to_subscribers must be a boolean."))
            history_public = bool(raw)

        if "is_web_public" in args:
            raw = args.get("is_web_public")
            if not isinstance(raw, bool):
                raise JsonableError(_("is_web_public must be a boolean."))
            is_web_public = bool(raw)

        # Minimal invariants:
        if is_web_public and invite_only:
            raise JsonableError(_("Web-public channels cannot be private."))
        if is_web_public and not history_public:
            raise JsonableError(_("Web-public channels must have public history."))
        if (not invite_only) and (not is_web_public) and history_public is False:
            # Public (not private) channels must have public history.
            raise JsonableError(_("Public channels must have public history."))

        do_change_stream_permission(
            stream,
            invite_only=invite_only,
            history_public_to_subscribers=history_public,
            is_web_public=is_web_public,
            acting_user=invoker,
        )
        changes.update(
            {
                "invite_only": invite_only,
                "history_public_to_subscribers": history_public,
                "is_web_public": is_web_public,
            }
        )

    if "message_retention_days" in args:
        if not invoker.is_realm_owner:
            raise AccessDeniedError()
        raw = args.get("message_retention_days")
        if raw is None:
            days = None
        else:
            days = _coerce_int_param(raw, field_name="message_retention_days")
            if days <= 0:
                raise JsonableError(_("message_retention_days must be positive."))
        do_change_stream_message_retention_days(stream, invoker, days)
        changes["message_retention_days"] = days

    if "announcement_only" in args:
        raw = args.get("announcement_only")
        if not isinstance(raw, bool):
            raise JsonableError(_("announcement_only must be a boolean."))
        group = _system_group(realm, SystemGroups.ADMINISTRATORS) if raw else _system_group(realm, SystemGroups.EVERYONE)
        do_change_stream_group_based_setting(
            stream,
            "can_send_message_group",
            group,
            acting_user=invoker,
        )
        changes["announcement_only"] = raw
        changes["can_send_message_group"] = group.name

    return {"changes": changes}


def _tool_zulip_realm_settings_update(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    # Generic realm property updates, with a conservative allowlist.
    props = args.get("properties")
    if not isinstance(props, dict) or not props:
        raise JsonableError(_("The 'properties' parameter must be a non-empty object."))

    # Keys must exist in Realm.property_types; we also gate a few sensitive ones to owners.
    owner_only = {
        "invite_required",
        "emails_restricted_to_domains",
        "disallow_disposable_email_addresses",
        "waiting_period_threshold",
        "message_retention_days",
    }

    applied: list[str] = []
    for name, raw in props.items():
        if not isinstance(name, str) or not name:
            continue
        if name not in Realm.property_types:
            raise JsonableError(_("Unsupported realm property: {name}").format(name=name))
        if name in owner_only and not invoker.is_realm_owner:
            raise AccessDeniedError()

        typ = Realm.property_types[name]
        value: Any = raw

        # Best-effort coercions from JSON/form.
        if typ is bool:
            if not isinstance(value, bool):
                raise JsonableError(_("Realm property '{name}' must be boolean.").format(name=name))
        elif typ is int:
            if not isinstance(value, int):
                raise JsonableError(_("Realm property '{name}' must be integer.").format(name=name))
        elif typ is str:
            if not isinstance(value, str):
                raise JsonableError(_("Realm property '{name}' must be string.").format(name=name))
        else:
            # Allow None for optional/int|None types.
            if value is not None and not isinstance(value, typ):
                raise JsonableError(_("Realm property '{name}' has invalid type.").format(name=name))

        do_set_realm_property(realm, name, value, acting_user=invoker)
        applied.append(name)

    return {"applied": applied}


def _tool_zulip_realm_permission_group_settings_update(
    *, realm: Realm, invoker: UserProfile, args: dict[str, Any]
) -> dict[str, Any]:
    settings = args.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise JsonableError(_("The 'settings' parameter must be a non-empty object."))

    # Invitation/security-related settings are owner-only.
    owner_only = {
        "can_invite_users_group",
        "create_multiuse_invite_group",
    }

    applied: list[str] = []
    for name, group_id_raw in settings.items():
        if not isinstance(name, str) or not name:
            continue
        if name not in Realm.REALM_PERMISSION_GROUP_SETTINGS:
            raise JsonableError(_("Unsupported permission setting: {name}").format(name=name))
        if name in owner_only and not invoker.is_realm_owner:
            raise AccessDeniedError()

        group_id = _coerce_int_param(group_id_raw, field_name=name)
        try:
            group = NamedUserGroup.objects.get(id=group_id, realm=realm)
        except NamedUserGroup.DoesNotExist:
            raise ResourceNotFoundError(_("User group not found."))

        do_change_realm_permission_group_setting(realm, name, group, acting_user=invoker)
        applied.append(name)

    return {"applied": applied}


def _tool_zulip_realm_user_defaults_update(*, realm: Realm, invoker: UserProfile, args: dict[str, Any]) -> dict[str, Any]:
    defaults = args.get("defaults")
    if not isinstance(defaults, dict) or not defaults:
        raise JsonableError(_("The 'defaults' parameter must be a non-empty object."))

    realm_user_default, _created = RealmUserDefault.objects.get_or_create(realm=realm)

    applied: list[str] = []
    if "email_address_visibility" in defaults:
        if not invoker.is_realm_owner:
            raise AccessDeniedError()
        value = defaults.get("email_address_visibility")
        value_int = _coerce_int_param(value, field_name="email_address_visibility")
        do_set_realm_user_default_setting(
            realm_user_default,
            "email_address_visibility",
            value_int,
            acting_user=invoker,
        )
        applied.append("email_address_visibility")

    if not applied:
        raise JsonableError(_("No supported defaults provided."))

    return {"applied": applied}


def _resolve_streams_from_ids(realm: Realm, stream_ids: list[int]) -> list[Stream]:
    streams = list(Stream.objects.filter(realm=realm, id__in=stream_ids))
    if len(streams) != len(set(stream_ids)):
        raise ResourceNotFoundError(_("One or more channels not found."))
    return streams


def _tool_zulip_realm_default_streams_add(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("stream_ids") or args.get("streamIds")
    if raw is None:
        raise JsonableError(_("Provide stream_ids."))

    ids: list[int] = []
    if isinstance(raw, list):
        ids = [_coerce_int_param(v, field_name="stream_ids") for v in raw]
    else:
        ids = [_coerce_int_param(raw, field_name="stream_ids")]

    streams = _resolve_streams_from_ids(realm, ids)
    for s in streams:
        do_add_default_stream(s)

    return {"added_stream_ids": ids}


def _tool_zulip_realm_default_streams_remove(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("stream_ids") or args.get("streamIds")
    if raw is None:
        raise JsonableError(_("Provide stream_ids."))

    ids: list[int] = []
    if isinstance(raw, list):
        ids = [_coerce_int_param(v, field_name="stream_ids") for v in raw]
    else:
        ids = [_coerce_int_param(raw, field_name="stream_ids")]

    streams = _resolve_streams_from_ids(realm, ids)
    for s in streams:
        do_remove_default_stream(s)

    return {"removed_stream_ids": ids}


def _resolve_default_stream_group(realm: Realm, args: dict[str, Any]):
    from zerver.models import DefaultStreamGroup

    group_id_raw = args.get("group_id") or args.get("groupId")
    if group_id_raw is not None:
        group_id = _coerce_int_param(group_id_raw, field_name="group_id")
        try:
            return DefaultStreamGroup.objects.get(id=group_id, realm=realm)
        except DefaultStreamGroup.DoesNotExist:
            raise ResourceNotFoundError(_("Default stream group not found."))

    name_raw = args.get("group_name") or args.get("groupName")
    if isinstance(name_raw, str) and name_raw.strip():
        g = DefaultStreamGroup.objects.filter(realm=realm, name__iexact=name_raw.strip()).order_by("-id").first()
        if g is None:
            raise ResourceNotFoundError(_("Default stream group not found."))
        return g

    raise JsonableError(_("Provide group_id or group_name."))


def _tool_zulip_realm_default_stream_group_create(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("group_name") or args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonableError(_("group_name is required."))
    name = name.strip()

    description = args.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JsonableError(_("description must be a string."))

    stream_ids_raw = args.get("stream_ids") or []
    ids: list[int] = []
    if isinstance(stream_ids_raw, list):
        ids = [_coerce_int_param(v, field_name="stream_ids") for v in stream_ids_raw]
    elif stream_ids_raw:
        ids = [_coerce_int_param(stream_ids_raw, field_name="stream_ids")]

    streams = _resolve_streams_from_ids(realm, ids) if ids else []

    do_create_default_stream_group(realm, name, description, streams)
    return {"created": True, "group_name": name}


def _tool_zulip_realm_default_stream_group_update(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_default_stream_group(realm, args)
    new_name = args.get("new_name") or args.get("newName")
    new_desc = args.get("description")

    if new_name is not None:
        if not isinstance(new_name, str) or not new_name.strip():
            raise JsonableError(_("new_name must be a non-empty string."))
        do_change_default_stream_group_name(group, new_name.strip())

    if new_desc is not None:
        if not isinstance(new_desc, str):
            raise JsonableError(_("description must be a string."))
        do_change_default_stream_group_description(group, new_desc)

    return {"updated": True, "group_id": group.id}


def _tool_zulip_realm_default_stream_group_delete(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_default_stream_group(realm, args)
    do_remove_default_stream_group(realm, group)
    return {"deleted": True, "group_id": group.id}


def _tool_zulip_realm_default_stream_group_streams_add(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_default_stream_group(realm, args)
    raw = args.get("stream_ids") or args.get("streamIds")
    if raw is None:
        raise JsonableError(_("Provide stream_ids."))
    ids = [_coerce_int_param(v, field_name="stream_ids") for v in raw] if isinstance(raw, list) else [_coerce_int_param(raw, field_name="stream_ids")]
    streams = _resolve_streams_from_ids(realm, ids)
    do_add_streams_to_default_stream_group(realm, group, streams)
    return {"group_id": group.id, "added_stream_ids": ids}


def _tool_zulip_realm_default_stream_group_streams_remove(*, realm: Realm, args: dict[str, Any]) -> dict[str, Any]:
    group = _resolve_default_stream_group(realm, args)
    raw = args.get("stream_ids") or args.get("streamIds")
    if raw is None:
        raise JsonableError(_("Provide stream_ids."))
    ids = [_coerce_int_param(v, field_name="stream_ids") for v in raw] if isinstance(raw, list) else [_coerce_int_param(raw, field_name="stream_ids")]
    streams = _resolve_streams_from_ids(realm, ids)
    do_remove_streams_from_default_stream_group(realm, group, streams)
    return {"group_id": group.id, "removed_stream_ids": ids}


@csrf_exempt
def s2s_smarty_pants_tools_execute(request: HttpRequest) -> HttpResponse:
    """Execute allowlisted Zulip admin/project tools with anti-spoof checks.

    Authentication: shared secret in SMARTY_PANTS_ZULIP_FACADE_SHARED_SECRET.

    POST body (form or JSON):
      - realm_id
      - invoker_user_id
      - invoker_message_id
      - tool
      - args

    Anti-spoof:
      - invoker_message_id must exist in the realm and be sent by invoker_user_id.
      - invoker_user_id must be an active, non-bot user in the realm.
    """

    _require_smarty_pants_shared_secret(request)

    if request.method != "POST":
        return HttpResponse(status=405)

    payload = _parse_request_payload(request)

    realm = _get_realm_for_s2s_request(payload.get("realm_id") or payload.get("realmId"))
    invoker_user_id = _coerce_int_param(
        payload.get("invoker_user_id") or payload.get("invokerUserId"),
        field_name="invoker_user_id",
    )
    invoker_message_id = _coerce_int_param(
        payload.get("invoker_message_id") or payload.get("invokerMessageId"),
        field_name="invoker_message_id",
    )

    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise JsonableError(_("The 'tool' parameter is required."))
    tool = tool.strip()

    args = _parse_args_object(payload.get("args"))

    # Validate the invoker and the message used as proof-of-identity.
    try:
        invoker = get_user_profile_by_id_in_realm(invoker_user_id, realm)
    except UserProfile.DoesNotExist:
        raise ResourceNotFoundError(_("Invoker not found."))

    if not invoker.is_active:
        raise JsonableError(_("Invoker is deactivated."))
    if invoker.is_bot:
        raise JsonableError(_("Invoker must be a human user (not a bot)."))

    try:
        message = Message.objects.only("id", "sender_id", "realm_id").get(id=invoker_message_id, realm_id=realm.id)
    except Message.DoesNotExist:
        raise ResourceNotFoundError(_("Message not found."))

    if message.sender_id != invoker.id:
        raise AccessDeniedError()

    is_admin = _is_realm_admin_user(invoker)
    is_sponsor = _is_sponsor_user(invoker)

    safe_tools = {
        # Read-only
        "zulip.stream.list_active",
        "zulip.user.list_active",
        "zulip.user_group.list",
        "zulip.channel_folder.list",
        # Projects
        "zulip.project.create",
        "zulip.project.list",
        # Control plane (git-backed memory + Letta memory blocks)
        "cp.memory.get",
        "cp.memory.set",
        "cp.memory.blocks.list",
        "cp.memory.blocks.create",
        "cp.memory.blocks.update",
        "cp.memory.blocks.delete",
        # Control plane (agent observatory / inspect)
        "cp.agents.index",
        "cp.letta.agents.retrieve",
        "cp.letta.runs.list",
        "cp.letta.runs.retrieve",
        "cp.letta.runs.usage.retrieve",
        "cp.letta.runs.steps.list",
    }

    dangerous_tools = {
        # Invites / onboarding
        "zulip.invite.link.create",
        # Stream/channel admin
        "zulip.stream.create",
        "zulip.stream.update",
        "zulip.stream.archive",
        "zulip.stream.unarchive",
        "zulip.stream.rename",
        "zulip.stream.move_to_folder",
        # Channel folders
        "zulip.channel_folder.create",
        "zulip.channel_folder.update",
        "zulip.channel_folder.archive",
        "zulip.channel_folder.unarchive",
        # Projects (dangerous)
        "zulip.project.archive",
        "zulip.project.unarchive",
        "zulip.project.rename",
        # User + group admin
        "zulip.user.deactivate",
        "zulip.user.role.set",
        "zulip.user_group.create",
        "zulip.user_group.members.add",
        "zulip.user_group.members.remove",
        # Realm settings
        "zulip.realm.branding.set",
        "zulip.realm.settings.update",
        "zulip.realm.permission_group_settings.update",
        "zulip.realm.user_defaults.update",
        "zulip.realm.default_streams.add",
        "zulip.realm.default_streams.remove",
        "zulip.realm.default_stream_group.create",
        "zulip.realm.default_stream_group.update",
        "zulip.realm.default_stream_group.delete",
        "zulip.realm.default_stream_group.streams.add",
        "zulip.realm.default_stream_group.streams.remove",
        # Control plane (agent binding)
        "cp.agent.pause",
        "cp.agent.budget.set",
        "cp.agent.archive",
        "cp.project_agents.provision_defaults",
    }

    if tool in safe_tools:
        if not (is_admin or is_sponsor):
            raise AccessDeniedError()
    elif tool in dangerous_tools:
        if not is_admin:
            raise AccessDeniedError()
    else:
        raise JsonableError(_("Unsupported tool: {tool_name}").format(tool_name=tool))

    if tool == "zulip.project.create":
        result = _tool_zulip_project_create(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.project.list":
        result = _tool_zulip_project_list(realm=realm)
    elif tool == "zulip.project.archive":
        result = _tool_zulip_project_archive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.project.unarchive":
        result = _tool_zulip_project_unarchive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.project.rename":
        result = _tool_zulip_project_rename(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.list_active":
        result = _tool_zulip_stream_list_active(realm=realm)
    elif tool == "zulip.user.list_active":
        result = _tool_zulip_user_list_active(realm=realm)
    elif tool == "zulip.user_group.list":
        result = _tool_zulip_user_group_list(realm=realm)
    elif tool == "zulip.channel_folder.list":
        result = _tool_zulip_channel_folder_list(realm=realm)
    elif tool == "zulip.channel_folder.create":
        result = _tool_zulip_channel_folder_create(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.channel_folder.update":
        result = _tool_zulip_channel_folder_update(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.channel_folder.archive":
        result = _tool_zulip_channel_folder_archive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.channel_folder.unarchive":
        result = _tool_zulip_channel_folder_unarchive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.invite.link.create":
        result = _tool_zulip_invite_link_create(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.create":
        result = _tool_zulip_stream_create(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.update":
        result = _tool_zulip_stream_update(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.archive":
        result = _tool_zulip_stream_archive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.unarchive":
        result = _tool_zulip_stream_unarchive(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.rename":
        result = _tool_zulip_stream_rename(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.stream.move_to_folder":
        result = _tool_zulip_stream_move_to_folder(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.user.deactivate":
        result = _tool_zulip_user_deactivate(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.user.role.set":
        result = _tool_zulip_user_role_set(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.user_group.create":
        result = _tool_zulip_user_group_create(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.user_group.members.add":
        result = _tool_zulip_user_group_members_add(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.user_group.members.remove":
        result = _tool_zulip_user_group_members_remove(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.realm.branding.set":
        result = _tool_zulip_realm_branding_set(realm=realm, args=args)
    elif tool == "zulip.realm.settings.update":
        result = _tool_zulip_realm_settings_update(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.realm.permission_group_settings.update":
        result = _tool_zulip_realm_permission_group_settings_update(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.realm.user_defaults.update":
        result = _tool_zulip_realm_user_defaults_update(realm=realm, invoker=invoker, args=args)
    elif tool == "zulip.realm.default_streams.add":
        result = _tool_zulip_realm_default_streams_add(realm=realm, args=args)
    elif tool == "zulip.realm.default_streams.remove":
        result = _tool_zulip_realm_default_streams_remove(realm=realm, args=args)
    elif tool == "zulip.realm.default_stream_group.create":
        result = _tool_zulip_realm_default_stream_group_create(realm=realm, args=args)
    elif tool == "zulip.realm.default_stream_group.update":
        result = _tool_zulip_realm_default_stream_group_update(realm=realm, args=args)
    elif tool == "zulip.realm.default_stream_group.delete":
        result = _tool_zulip_realm_default_stream_group_delete(realm=realm, args=args)
    elif tool == "zulip.realm.default_stream_group.streams.add":
        result = _tool_zulip_realm_default_stream_group_streams_add(realm=realm, args=args)
    elif tool == "zulip.realm.default_stream_group.streams.remove":
        result = _tool_zulip_realm_default_stream_group_streams_remove(realm=realm, args=args)
    elif tool == "cp.agent.pause":
        result = _tool_cp_agent_pause(realm=realm, args=args)
    elif tool == "cp.agent.budget.set":
        result = _tool_cp_agent_budget_set(realm=realm, args=args)
    elif tool == "cp.agent.archive":
        result = _tool_cp_agent_archive(realm=realm, args=args)
    elif tool == "cp.project_agents.provision_defaults":
        result = _tool_cp_project_agents_provision_defaults(realm=realm, invoker=invoker)
    elif tool == "cp.memory.get":
        result = _tool_cp_memory_get(realm=realm, args=args)
    elif tool == "cp.memory.set":
        result = _tool_cp_memory_set(realm=realm, args=args)
    elif tool == "cp.memory.blocks.list":
        result = _tool_cp_memory_blocks_list(args=args)
    elif tool == "cp.memory.blocks.create":
        result = _tool_cp_memory_blocks_create(args=args)
    elif tool == "cp.memory.blocks.update":
        result = _tool_cp_memory_blocks_update(args=args)
    elif tool == "cp.memory.blocks.delete":
        result = _tool_cp_memory_blocks_delete(args=args)
    elif tool == "cp.agents.index":
        result = _tool_cp_agents_index(realm=realm)
    elif tool == "cp.letta.agents.retrieve":
        result = _tool_cp_letta_agents_retrieve(realm=realm, args=args)
    elif tool == "cp.letta.runs.list":
        result = _tool_cp_letta_runs_list(realm=realm, args=args)
    elif tool == "cp.letta.runs.retrieve":
        result = _tool_cp_letta_runs_retrieve(realm=realm, args=args)
    elif tool == "cp.letta.runs.usage.retrieve":
        result = _tool_cp_letta_runs_usage_retrieve(realm=realm, args=args)
    elif tool == "cp.letta.runs.steps.list":
        result = _tool_cp_letta_runs_steps_list(realm=realm, args=args)
    else:  # nocoverage
        raise JsonableError(_("Unsupported tool: {tool_name}").format(tool_name=tool))

    return json_success(
        request,
        data={
            "ok": True,
            "tool": tool,
            "result": result,
        },
    )


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


CONTROL_PLANE_ARCHIVE_AGENT_PATH = "/s2s/zulip/agents/archive"
CONTROL_PLANE_PAUSE_AGENT_PATH = "/s2s/zulip/agents/pause"
CONTROL_PLANE_SET_AGENT_BUDGET_PATH = "/s2s/zulip/agents/budget/set"
CONTROL_PLANE_PROJECT_AGENTS_PROVISION_DEFAULTS_PATH = "/s2s/zulip/project_agents/provision_defaults"
CONTROL_PLANE_MEMORY_GET_PATH = "/s2s/zulip/memory/get"
CONTROL_PLANE_MEMORY_SET_PATH = "/s2s/zulip/memory/set"
CONTROL_PLANE_MEMORY_BLOCKS_LIST_PATH = "/s2s/zulip/memory/blocks/list"
CONTROL_PLANE_MEMORY_BLOCKS_CREATE_PATH = "/s2s/zulip/memory/blocks/create"
CONTROL_PLANE_MEMORY_BLOCKS_UPDATE_PATH = "/s2s/zulip/memory/blocks/update"
CONTROL_PLANE_MEMORY_BLOCKS_DELETE_PATH = "/s2s/zulip/memory/blocks/delete"

CONTROL_PLANE_AGENTS_INDEX_PATH = "/s2s/zulip/agents/index"
CONTROL_PLANE_LETTA_AGENTS_RETRIEVE_PATH = "/s2s/zulip/letta/agents/retrieve"
CONTROL_PLANE_LETTA_RUNS_LIST_PATH = "/s2s/zulip/letta/runs/list"
CONTROL_PLANE_LETTA_RUNS_RETRIEVE_PATH = "/s2s/zulip/letta/runs/retrieve"
CONTROL_PLANE_LETTA_RUNS_USAGE_RETRIEVE_PATH = "/s2s/zulip/letta/runs/usage/retrieve"
CONTROL_PLANE_LETTA_RUNS_STEPS_LIST_PATH = "/s2s/zulip/letta/runs/steps/list"


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


