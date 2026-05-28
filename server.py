"""LogicMonitor read-only MCP server (Python port).

Ported from the TypeScript project at
https://github.com/monitoringartist/logicmonitor-mcp-server
(c) Monitoring Artist, licensed AGPL-3.0-or-later.

This port re-implements only the READ-ONLY (query) tools of the upstream
server as a FastMCP Python app, packaged for deployment via the Roundhouse
MCP platform ("Deploy from Git"). It is itself licensed AGPL-3.0-or-later.

Configuration (environment variables):
  LM_COMPANY        LogicMonitor account subdomain, e.g. "acme" for
                    https://acme.logicmonitor.com  (required unless LM_BASE_URL)
  LM_BEARER_TOKEN   LogicMonitor API Bearer token (required, mark secret)
  LM_DOMAIN         Portal domain suffix (optional, default "logicmonitor.com").
                    For LM for Government set your gov domain, e.g.
                    "logicmonitorgov.com" -> https://<LM_COMPANY>.<LM_DOMAIN>
  LM_BASE_URL       Full portal base URL override (optional), e.g.
                    "https://acme.logicmonitorgov.com". Wins over
                    LM_COMPANY/LM_DOMAIN; use for gov/custom/on-prem hosts.
  LM_API_TIMEOUT    Per-request timeout in seconds (optional, default 30)

Filtering: list_* tools accept LogicMonitor filter syntax via `filter`,
e.g. filter='hostStatus:alive,displayName~"*web*"'. Use comma (,) for AND
and || for OR. Wildcard values must be quoted: displayName~"*prod*".
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.responses import PlainTextResponse

mcp = FastMCP("logicmonitor")


# --------------------------------------------------------------------------
# LogicMonitor REST v3 client
# --------------------------------------------------------------------------

def _company() -> str:
    company = os.environ.get("LM_COMPANY", "").strip()
    if not company:
        raise ToolError("LM_COMPANY is not configured. Set it to your LogicMonitor account subdomain (e.g. 'acme').")
    return company


def _portal_base() -> str:
    """Portal base URL, e.g. https://acme.logicmonitor.com.

    LM_BASE_URL fully overrides it (gov/custom/on-prem). Otherwise it's built
    from LM_COMPANY + LM_DOMAIN, where LM_DOMAIN defaults to logicmonitor.com
    (set it to your gov domain for LM for Government)."""
    override = os.environ.get("LM_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    domain = os.environ.get("LM_DOMAIN", "").strip() or "logicmonitor.com"
    return f"https://{_company()}.{domain}"


def _base_url() -> str:
    return f"{_portal_base()}/santaba/rest"


def _ui_base() -> str:
    return _portal_base()


def _headers() -> dict[str, str]:
    token = os.environ.get("LM_BEARER_TOKEN", "").strip()
    if not token:
        raise ToolError("LM_BEARER_TOKEN is not configured. Provide a LogicMonitor API Bearer token.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Version": "3",
    }


def _timeout() -> float:
    try:
        return float(os.environ.get("LM_API_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop None values and the catch-all fields='*' (means 'all fields')."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if key == "fields" and value == "*":
            continue
        out[key] = value
    return out


def _get(path: str, **params: Any) -> Any:
    """Authenticated GET against the LogicMonitor REST API."""
    url = _base_url() + path
    try:
        with httpx.Client(timeout=_timeout()) as client:
            resp = client.get(url, headers=_headers(), params=_clean_params(params))
    except httpx.HTTPError as exc:
        raise ToolError(f"LogicMonitor request failed: {exc}") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("errorMessage") or body.get("errmsg") or ""
        except Exception:  # noqa: BLE001 - body may not be JSON
            detail = resp.text[:300]
        raise ToolError(f"LogicMonitor API error {resp.status_code}: {detail or resp.reason_phrase}")
    return resp.json()


def _paginate(path: str, **params: Any) -> dict[str, Any]:
    """Fetch every page of a list endpoint and merge into {total, items}."""
    size = int(params.get("size") or 1000)
    offset = int(params.get("offset") or 0)
    params = {**params, "size": size}
    all_items: list[Any] = []
    total = 0
    first = True
    while True:
        page = _get(path, **{**params, "offset": offset})
        if not isinstance(page, dict) or "items" not in page:
            raise ToolError(f"Unexpected list response from {path}: missing 'items'")
        if first:
            total = int(page.get("total") or 0)
            first = False
        items = page.get("items") or []
        all_items.extend(items)
        if not items or len(all_items) >= total:
            break
        offset += len(items)
    return {"total": total, "items": all_items}


def _list(
    path: str,
    *,
    filter: str | None = None,
    size: int | None = None,
    offset: int | None = None,
    fields: str | None = None,
    autoPaginate: bool = False,
    **extra: Any,
) -> Any:
    params = {"filter": filter, "size": size, "offset": offset, "fields": fields, **extra}
    if autoPaginate:
        return _paginate(path, **params)
    return _get(path, **params)


def _with_query(filter: str | None, query: str | None, search_fields: tuple[str, ...]) -> str | None:
    """Mirror the upstream `query` shortcut: free text becomes an OR filter
    across the given fields, AND-combined with any explicit filter."""
    if not query:
        return filter
    escaped = query.replace('"', '\\"')
    or_group = "||".join(f'{field}~"*{escaped}*"' for field in search_fields)
    return f"{or_group},{filter}" if filter else or_group


def _group_path(endpoint_prefix: str, start_id: Any) -> list[dict[str, Any]]:
    """Walk a group hierarchy upward via parentId, returning root-first.
    `endpoint_prefix` is the group collection path, e.g. '/dashboard/groups'."""
    path: list[dict[str, Any]] = []
    current = start_id
    while current:
        try:
            group = _get(f"{endpoint_prefix}/{current}", fields="id,name,parentId")
        except ToolError:
            break
        path.insert(0, group)
        current = group.get("parentId")
    return path


# ==========================================================================
# Resources / devices
# ==========================================================================

@mcp.tool
def list_resources(
    query: str | None = None,
    filter: str | None = None,
    size: int | None = None,
    offset: int | None = None,
    fields: str | None = None,
    autoPaginate: bool = False,
) -> Any:
    """List monitored resources/devices. Use `query` for free-text search or `filter` for LM syntax (e.g. 'hostStatus:alive,displayName~"*web*"')."""
    combined = _with_query(filter, query, ("displayName", "name", "description"))
    return _list("/device/devices", filter=combined, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_resource(deviceId: int, fields: str | None = None) -> Any:
    """Get full details for a resource/device by its ID."""
    return _get(f"/device/devices/{deviceId}", fields=fields)


@mcp.tool
def list_resource_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List resource/device groups."""
    return _list("/device/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_resource_group(groupId: int, fields: str | None = None) -> Any:
    """Get resource/device group details by ID."""
    return _get(f"/device/groups/{groupId}", fields=fields)


@mcp.tool
def list_resource_properties(
    deviceId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List all properties of a resource/device."""
    return _list(f"/device/devices/{deviceId}/properties", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def list_resource_group_properties(
    groupId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List all properties of a resource/device group."""
    return _list(f"/device/groups/{groupId}/properties", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def list_resource_datasources(
    deviceId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List datasources applied to a resource/device (monitored metric groups)."""
    return _list(f"/device/devices/{deviceId}/devicedatasources", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_resource_datasource(deviceId: int, deviceDataSourceId: int, fields: str | None = None) -> Any:
    """Get details of one datasource applied to a resource/device."""
    return _get(f"/device/devices/{deviceId}/devicedatasources/{deviceDataSourceId}", fields=fields)


@mcp.tool
def list_resource_instances(
    deviceId: int, deviceDataSourceId: int, filter: str | None = None,
    size: int | None = None, offset: int | None = None, fields: str | None = None,
) -> Any:
    """List the instances of a datasource on a resource/device."""
    return _list(f"/device/devices/{deviceId}/devicedatasources/{deviceDataSourceId}/instances", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_resource_instance_data(
    deviceId: int, deviceDataSourceId: int, instanceId: int,
    datapoints: str | None = None, start: int | None = None,
    end: int | None = None, format: str | None = None,
) -> Any:
    """Get time-series metric data for a datasource instance. `start`/`end` are epoch seconds; `datapoints` is a comma-separated list."""
    return _get(
        f"/device/devices/{deviceId}/devicedatasources/{deviceDataSourceId}/instances/{instanceId}/data",
        datapoints=datapoints, start=start, end=end, format=format,
    )


# ==========================================================================
# Alerts
# ==========================================================================

@mcp.tool
def list_alerts(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, needMessage: bool | None = None, autoPaginate: bool = False,
) -> Any:
    """List alerts. Filter e.g. 'severity:4' (critical), 'cleared:false', 'acked:false'."""
    return _list("/alert/alerts", filter=filter, size=size, offset=offset, fields=fields, needMessage=needMessage, autoPaginate=autoPaginate)


@mcp.tool
def get_alert(alertId: str, fields: str | None = None, needMessage: bool | None = None) -> Any:
    """Get alert details by alert ID."""
    return _get(f"/alert/alerts/{alertId}", fields=fields, needMessage=needMessage)


@mcp.tool
def list_alert_rules(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List alert rules (routing of alerts to escalation chains)."""
    return _list("/setting/alert/rules", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_alert_rule(ruleId: int, fields: str | None = None) -> Any:
    """Get alert rule details by ID."""
    return _get(f"/setting/alert/rules/{ruleId}", fields=fields)


@mcp.tool
def list_escalation_chains(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List escalation chains."""
    return _list("/setting/alert/chains", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_escalation_chain(chainId: int, fields: str | None = None) -> Any:
    """Get escalation chain details by ID."""
    return _get(f"/setting/alert/chains/{chainId}", fields=fields)


@mcp.tool
def list_recipients(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List alert recipients."""
    return _list("/setting/recipients", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_recipient(recipientId: int, fields: str | None = None) -> Any:
    """Get recipient details by ID."""
    return _get(f"/setting/recipients/{recipientId}", fields=fields)


@mcp.tool
def list_recipient_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List recipient groups."""
    return _list("/setting/recipientgroups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_recipient_group(groupId: int, fields: str | None = None) -> Any:
    """Get recipient group details by ID."""
    return _get(f"/setting/recipientgroups/{groupId}", fields=fields)


# ==========================================================================
# Collectors
# ==========================================================================

@mcp.tool
def list_collectors(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List collectors (the agents that gather monitoring data)."""
    return _list("/setting/collector/collectors", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_collector(collectorId: int, fields: str | None = None) -> Any:
    """Get collector details by ID."""
    return _get(f"/setting/collector/collectors/{collectorId}", fields=fields)


@mcp.tool
def list_collector_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List collector groups."""
    return _list("/setting/collector/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_collector_group(groupId: int, fields: str | None = None) -> Any:
    """Get collector group details by ID."""
    return _get(f"/setting/collector/groups/{groupId}", fields=fields)


@mcp.tool
def list_collector_versions(size: int | None = None, offset: int | None = None, fields: str | None = None) -> Any:
    """List available collector versions."""
    return _get("/setting/collector/collectors/versions", size=size, offset=offset, fields=fields)


# ==========================================================================
# DataSources / EventSources / ConfigSources
# ==========================================================================

@mcp.tool
def list_datasources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List datasource definitions."""
    return _list("/setting/datasources", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_datasource(dataSourceId: int, fields: str | None = None) -> Any:
    """Get datasource definition details by ID."""
    return _get(f"/setting/datasources/{dataSourceId}", fields=fields)


@mcp.tool
def list_eventsources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List EventSource definitions."""
    return _list("/setting/eventsources", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_eventsource(eventSourceId: int, fields: str | None = None) -> Any:
    """Get EventSource definition details by ID."""
    return _get(f"/setting/eventsources/{eventSourceId}", fields=fields)


@mcp.tool
def list_configsources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List ConfigSource definitions."""
    return _list("/setting/configsources", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_configsource(configSourceId: int, fields: str | None = None) -> Any:
    """Get ConfigSource definition details by ID."""
    return _get(f"/setting/configsources/{configSourceId}", fields=fields)


# ==========================================================================
# Dashboards
# ==========================================================================

@mcp.tool
def list_dashboards(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List dashboards."""
    return _list("/dashboard/dashboards", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_dashboard(dashboardId: int, fields: str | None = None) -> Any:
    """Get dashboard details by ID."""
    return _get(f"/dashboard/dashboards/{dashboardId}", fields=fields)


@mcp.tool
def list_dashboard_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List dashboard groups."""
    return _list("/dashboard/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_dashboard_group(groupId: int, fields: str | None = None) -> Any:
    """Get dashboard group details by ID."""
    return _get(f"/dashboard/groups/{groupId}", fields=fields)


# ==========================================================================
# Reports
# ==========================================================================

@mcp.tool
def list_reports(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List reports."""
    return _list("/report/reports", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_report(reportId: int, fields: str | None = None) -> Any:
    """Get report details by ID."""
    return _get(f"/report/reports/{reportId}", fields=fields)


@mcp.tool
def list_report_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List report groups."""
    return _list("/report/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_report_group(groupId: int, fields: str | None = None) -> Any:
    """Get report group details by ID."""
    return _get(f"/report/groups/{groupId}", fields=fields)


# ==========================================================================
# Websites (synthetic monitoring)
# ==========================================================================

@mcp.tool
def list_websites(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List website/synthetic monitors."""
    return _list("/website/websites", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_website(websiteId: int, fields: str | None = None) -> Any:
    """Get website monitor details by ID."""
    return _get(f"/website/websites/{websiteId}", fields=fields)


@mcp.tool
def list_website_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List website monitor groups."""
    return _list("/website/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_website_group(groupId: int, fields: str | None = None) -> Any:
    """Get website group details by ID."""
    return _get(f"/website/groups/{groupId}", fields=fields)


@mcp.tool
def list_website_checkpoints(fields: str | None = None) -> Any:
    """List the geographic checkpoint locations used for website monitoring."""
    return _get("/website/smcheckpoints", fields=fields)


# ==========================================================================
# Services
# ==========================================================================

@mcp.tool
def list_services(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List services."""
    return _list("/service/services", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_service(serviceId: int, fields: str | None = None) -> Any:
    """Get service details by ID."""
    return _get(f"/service/services/{serviceId}", fields=fields)


@mcp.tool
def list_service_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List service groups."""
    return _list("/service/groups", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_service_group(groupId: int, fields: str | None = None) -> Any:
    """Get service group details by ID."""
    return _get(f"/service/groups/{groupId}", fields=fields)


# ==========================================================================
# Users / roles / access groups / API tokens
# ==========================================================================

@mcp.tool
def list_users(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List user accounts (admins)."""
    return _list("/setting/admins", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_user(userId: int, fields: str | None = None) -> Any:
    """Get user account details by ID."""
    return _get(f"/setting/admins/{userId}", fields=fields)


@mcp.tool
def list_roles(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List roles."""
    return _list("/setting/roles", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_role(roleId: int, fields: str | None = None) -> Any:
    """Get role details by ID."""
    return _get(f"/setting/roles/{roleId}", fields=fields)


@mcp.tool
def list_access_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List access groups."""
    return _list("/setting/accessgroup", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_access_group(accessGroupId: int, fields: str | None = None) -> Any:
    """Get access group details by ID."""
    return _get(f"/setting/accessgroup/{accessGroupId}", fields=fields)


@mcp.tool
def list_api_tokens(
    userId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List API tokens belonging to a user (secrets are not returned)."""
    return _list(f"/setting/admins/{userId}/apitokens", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


# ==========================================================================
# SDTs / OpsNotes / audit logs / netscans / integrations / topology
# ==========================================================================

@mcp.tool
def list_sdts(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List Scheduled Down Times (maintenance windows)."""
    return _list("/sdt/sdts", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_sdt(sdtId: str, fields: str | None = None) -> Any:
    """Get Scheduled Down Time details by ID."""
    return _get(f"/sdt/sdts/{sdtId}", fields=fields)


@mcp.tool
def list_opsnotes(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List OpsNotes (annotations on graphs/timelines)."""
    return _list("/setting/opsnotes", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_opsnote(opsNoteId: str, fields: str | None = None) -> Any:
    """Get OpsNote details by ID."""
    return _get(f"/setting/opsnotes/{opsNoteId}", fields=fields)


@mcp.tool
def list_audit_logs(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List audit/access logs of portal activity."""
    return _list("/setting/accesslogs", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_audit_log(auditLogId: str, fields: str | None = None) -> Any:
    """Get a single audit log entry by ID."""
    return _get(f"/setting/accesslogs/{auditLogId}", fields=fields)


@mcp.tool
def list_netscans(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List NetScan (network discovery) definitions."""
    return _list("/setting/netscans", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_netscan(netscanId: int, fields: str | None = None) -> Any:
    """Get NetScan definition details by ID."""
    return _get(f"/setting/netscans/{netscanId}", fields=fields)


@mcp.tool
def list_integrations(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None, autoPaginate: bool = False,
) -> Any:
    """List integrations (delivery/notification integrations)."""
    return _list("/setting/integrations", filter=filter, size=size, offset=offset, fields=fields, autoPaginate=autoPaginate)


@mcp.tool
def get_integration(integrationId: int, fields: str | None = None) -> Any:
    """Get integration details by ID."""
    return _get(f"/setting/integrations/{integrationId}", fields=fields)


@mcp.tool
def get_topology(fields: str | None = None) -> Any:
    """Get network topology data."""
    return _get("/topology", fields=fields)


# ==========================================================================
# UI link generators (read-only: fetch entity + build a portal URL)
# ==========================================================================

@mcp.tool
def generate_dashboard_link(dashboardId: int) -> dict[str, Any]:
    """Build a LogicMonitor portal URL for a dashboard."""
    dashboard = _get(f"/dashboard/dashboards/{dashboardId}", fields="id,name,groupId,groupName")
    path = _group_path("/dashboard/groups", dashboard.get("groupId"))
    segs = ",".join(f"dashboardGroups-{g['id']}" for g in path)
    tail = f"dashboards-{dashboardId}"
    joined = f"{segs},{tail}" if segs else tail
    return {"url": f"{_ui_base()}/santaba/uiv4/dashboards/{joined}", "dashboard": dashboard, "groupPath": path}


@mcp.tool
def generate_resource_link(deviceId: int) -> dict[str, Any]:
    """Build a LogicMonitor portal URL for a resource/device."""
    device = _get(f"/device/devices/{deviceId}", fields="id,displayName,name,hostGroupIds")
    path: list[dict[str, Any]] = []
    host_group_ids = device.get("hostGroupIds")
    if host_group_ids:
        primary = host_group_ids.split(",")[0].strip()
        path = _group_path("/device/groups", int(primary))
    segs = ",".join(f"resourceGroups-{g['id']}" for g in path)
    tail = f"resources-{deviceId}"
    joined = f"{segs},{tail}" if segs else tail
    url = f"{_ui_base()}/santaba/uiv4/resources/treeNodes?resourcePath={quote(joined, safe='')}"
    return {"url": url, "device": device, "groupPath": path}


@mcp.tool
def generate_alert_link(alertId: str) -> dict[str, Any]:
    """Build a LogicMonitor portal URL for an alert."""
    alert = _get(f"/alert/alerts/{alertId}", fields="id,internalId,type,severity,monitorObjectName")
    return {"url": f"{_ui_base()}/santaba/uiv4/alerts/{alertId}", "alert": alert}


@mcp.tool
def generate_website_link(websiteId: int) -> dict[str, Any]:
    """Build a LogicMonitor portal URL for a website monitor."""
    website = _get(f"/website/websites/{websiteId}", fields="id,name,groupId")
    path = _group_path("/website/groups", website.get("groupId"))
    segs = ",".join(f"websiteGroups-{g['id']}" for g in path)
    tail = f"websites-{websiteId}"
    joined = f"{segs},{tail}" if segs else tail
    return {"url": f"{_ui_base()}/santaba/uiv4/websites/treeNodes#{joined}", "website": website, "groupPath": path}


# --------------------------------------------------------------------------
# Liveness probe (used by Docker HEALTHCHECK + the Roundhouse status badge)
# --------------------------------------------------------------------------

@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request):  # noqa: ANN001 - starlette Request
    return PlainTextResponse("ok", status_code=200)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
        json_response=True,
    )
