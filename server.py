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
  LM_LOG_LEVEL      Log verbosity (optional, default INFO). Standard levels:
                    DEBUG / INFO / WARNING / ERROR / CRITICAL. DEBUG logs the
                    resolved request URL, params, headers (token redacted) and
                    tool arguments.

Filtering: list_* tools accept LogicMonitor filter syntax via `filter`,
e.g. filter='hostStatus:alive,displayName~"*web*"'. Use comma (,) for AND
and || for OR. Wildcard values must be quoted: displayName~"*prod*".
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from starlette.responses import PlainTextResponse

# --------------------------------------------------------------------------
# Logging - verbosity via LM_LOG_LEVEL (standard levels, default INFO). Logs
# go to stdout so they surface in the Roundhouse "Logs" tab. DEBUG adds the
# resolved request URL/params/headers (token redacted) and tool arguments.
# --------------------------------------------------------------------------
log = logging.getLogger("logicmonitor")


def _configure_logging() -> None:
    name = (os.environ.get("LM_LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        level = logging.INFO
    log.setLevel(level)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [logicmonitor] %(message)s"))
        log.addHandler(handler)
        log.propagate = False


_configure_logging()


_SECRET_ARG_HINTS = ("token", "secret", "password", "authorization", "apikey", "api_key")


def _redact_args(arguments: Any) -> Any:
    """Mask values whose key looks secret, so DEBUG arg logs stay safe."""
    if not isinstance(arguments, dict):
        return arguments
    return {
        k: ("***" if any(h in k.lower() for h in _SECRET_ARG_HINTS) else v)
        for k, v in arguments.items()
    }


mcp = FastMCP("logicmonitor")


class LoggingMiddleware(Middleware):
    """Log every tool call: name at INFO, arguments at DEBUG, duration on
    completion, and full error detail (with traceback at DEBUG) on failure.
    Combined with the per-request LM API logging in _get, this gives a full
    picture of what each tool did. Verbosity follows LM_LOG_LEVEL."""

    async def on_call_tool(self, context, call_next):
        msg = context.message
        name = getattr(msg, "name", "?")
        started = time.perf_counter()
        log.info("tool call -> %s", name)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("tool args -> %s: %s", name, _redact_args(getattr(msg, "arguments", None)))
        try:
            result = await call_next(context)
        except Exception as exc:
            dur = (time.perf_counter() - started) * 1000
            log.error("tool error <- %s after %.0fms: %s: %s", name, dur, type(exc).__name__, exc)
            log.debug("tool traceback for %s", name, exc_info=True)
            raise
        dur = (time.perf_counter() - started) * 1000
        log.info("tool ok <- %s (%.0fms)", name, dur)
        return result


mcp.add_middleware(LoggingMiddleware())


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
    clean = _clean_params(params)
    headers = _headers()
    if log.isEnabledFor(logging.DEBUG):
        safe = {**headers, "Authorization": "Bearer ***"}
        log.debug("LM GET %s params=%s headers=%s", url, clean, safe)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=_timeout(), follow_redirects=True) as client:
            resp = client.get(url, headers=headers, params=clean)
    except httpx.HTTPError as exc:
        log.error("LM GET %s network error: %s", url, exc)
        raise ToolError(f"LogicMonitor request failed: {exc}") from exc
    dur_ms = (time.perf_counter() - started) * 1000
    log.info("LM GET %s -> %s (%.0fms, %d bytes)", path, resp.status_code, dur_ms, len(resp.content))
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("errorMessage") or body.get("errmsg") or ""
        except ValueError:  # body may not be JSON
            detail = resp.text[:300]
        log.warning("LM GET %s failed: HTTP %s - %s", url, resp.status_code, detail or resp.reason_phrase)
        raise ToolError(f"LogicMonitor API error {resp.status_code}: {detail or resp.reason_phrase}")
    try:
        return resp.json()
    except ValueError:
        # 2xx but the body isn't JSON - almost always an HTML login/redirect
        # page, i.e. the request never reached the REST API. Surface enough to
        # diagnose instead of a bare "Expecting value" decode error.
        ctype = resp.headers.get("content-type", "?")
        snippet = " ".join(resp.text[:200].split())
        log.warning("LM GET %s returned non-JSON (HTTP %s, ctype=%s): %s", url, resp.status_code, ctype, snippet)
        raise ToolError(
            f"LogicMonitor returned a non-JSON response (HTTP {resp.status_code}, "
            f"content-type {ctype}) from {url}. This usually means the request didn't reach "
            f"the REST API - check LM_BASE_URL / LM_DOMAIN / LM_COMPANY and the API token. "
            f"Body starts: {snippet!r}"
        )


# Largest page a single list request may fetch. Kept low so large
# environments page in small batches instead of timing out on one big call.
MAX_PAGE_SIZE = 100


def _list(
    path: str,
    *,
    filter: str | None = None,
    size: int | None = None,
    offset: int | None = None,
    fields: str | None = None,
    **extra: Any,
) -> Any:
    if size is None or size > MAX_PAGE_SIZE:
        size = MAX_PAGE_SIZE
    params = {"filter": filter, "size": size, "offset": offset, "fields": fields, **extra}
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
) -> Any:
    """List all monitored resources/devices in LogicMonitor.

    Returns: array of resources with id, displayName, name (IP/hostname),
    hostStatus (dead/alive/unknown), preferredCollectorId, deviceType, custom
    properties and group memberships.

    When to use:
    - Get an inventory of all monitored resources/devices
    - Find a specific resource by name / IP / property
    - Check resource health status
    - Get device IDs to feed into other tools

    Two search modes:
    - Simple: pass `query` with free text (e.g. query="production",
      query="web-server") - searches displayName, name and description (OR logic).
    - Advanced: pass `filter` with LM filter syntax for precise control
      (e.g. filter='hostStatus:alive,displayName~"*web*"').
    If both are given, `query` is converted to a filter and AND-combined with `filter`.

    Common filter patterns:
    - By name:      filter='displayName~"*prod*"'
    - By status:    filter='hostStatus:alive'  or  filter='hostStatus:dead'
    - By collector: filter='preferredCollectorId:123'
    - Multiple (AND): filter='hostStatus:alive,displayName~"*web*"'

    Pagination: a negative `total` in the response means results are incomplete -
    page with size/offset to fetch more. In large environments (>1000 devices)
    page in batches to avoid timeouts.

    Related tools: get_resource (details), generate_resource_link (portal URL)."""
    combined = _with_query(filter, query, ("displayName", "name", "description"))
    return _list("/device/devices", filter=combined, size=size, offset=offset, fields=fields)


@mcp.tool
def get_resource(deviceId: int, fields: str | None = None) -> Any:
    """Get full details for a resource/device by its ID.

    Returns: complete device details including displayName, IP/hostname,
    hostStatus, alertStatus, collector assignment, device type, custom
    properties, applied datasources, group memberships, last data time and
    creation date.

    When to use:
    - Get full details after finding a device ID via list_resources
    - Check device configuration and verify collector assignment
    - Review custom properties

    Workflow: use list_resources to find the deviceId, then this tool for the
    full record.

    Related tools: list_resource_datasources (what's monitored),
    list_resource_properties (all properties), generate_resource_link (portal URL)."""
    return _get(f"/device/devices/{deviceId}", fields=fields)


@mcp.tool
def list_resource_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List resource/device groups (folders).

    Returns: array of groups with id, name, parentId, full path, description,
    number of devices, number of subgroups and custom properties.

    What they are: organizational folders for devices, like directories in a file
    system - organize by location, environment, customer or any logical
    structure. Custom properties set on a group are inherited by every device in
    it (handy for credentials, location tags).

    Common filter patterns:
    - By name:     filter='name~"*Production*"'
    - Root groups: filter='parentId:1'
    - Non-empty:   filter='numOfDirectDevices>0'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_resource_group (details), list_resource_group_properties
    (group properties), list_resources (devices in group)."""
    return _list("/device/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_resource_group(groupId: int, fields: str | None = None) -> Any:
    """Get resource/device group details by ID.

    Returns: full group details - name, full path, parentId, description, custom
    properties, number of devices (direct and total), number of subgroups, alert
    status and SDT status.

    Key fields:
    - fullPath: complete hierarchy, e.g. "/Production/Web Servers/US-East"
    - customProperties: inherited by ALL devices in the group (e.g. ssh.user,
      env, team)
    - numOfDirectDevices vs numOfHosts: direct members vs total including subgroups
    - alertStatus: rollup alert status for the whole group

    Workflow: use list_resource_groups to find groupId, then this tool for full
    details including inherited properties.

    Related tools: list_resource_groups (find groups),
    list_resource_group_properties (properties), list_resources (devices in group)."""
    return _get(f"/device/groups/{groupId}", fields=fields)


@mcp.tool
def list_resource_properties(
    deviceId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None,
) -> Any:
    """List all properties (system and user-defined) of a resource/device.

    Returns: array of properties with name, value, source (device-level vs
    inherited from group) and type (system vs custom).

    Property types:
    - System (auto-populated): system.hostname, system.devicetype, system.ips,
      system.categories (auto-detected technologies, e.g. "AWS/EC2")
    - Custom (user-defined): credentials (ssh.user, snmp.community, wmi.user),
      tags (env, owner, location), integration IDs, business metadata

    Inheritance: device level (highest priority) -> group -> parent group.
    Datasource appliesTo logic uses these properties to decide what to monitor.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Workflow: use list_resources to find deviceId, then this tool to see all
    properties including inherited ones.

    Related tools: get_resource (summary), list_resource_group_properties
    (group-level properties)."""
    return _list(f"/device/devices/{deviceId}/properties", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def list_resource_group_properties(
    groupId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None,
) -> Any:
    """List all properties of a resource/device group.

    Properties set at the group level are inherited by every device in the group.

    Returns: array of properties with name, value, type (custom vs system) and
    inheritance source.

    Common group properties:
    - Credentials: ssh.user, ssh.pass, snmp.community, wmi.user, wmi.pass
    - Tags: env (production/staging), location, owner (team name)
    - Business metadata: cost.center, sla.tier, compliance.level

    Inheritance: group properties apply to all devices in the group; child groups
    inherit from parents; device-level properties override group ones. Used by
    datasource appliesTo logic and authentication.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Workflow: use list_resource_groups to find groupId, then this tool to review
    inherited settings.

    Related tools: get_resource_group (group details), list_resource_properties
    (device-level properties)."""
    return _list(f"/device/groups/{groupId}/properties", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def list_resource_datasources(
    deviceId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None,
) -> Any:
    """List datasources applied to a resource/device (the monitored metric groups).

    Returns: array of datasources actively monitoring this device with id
    (deviceDataSourceId), dataSourceName, dataSourceDisplayName, status, alert
    status, instance count and last poll time.

    What you discover: which datasources are active (e.g. WinCPU, WinMemory,
    SNMP_Network_Interfaces), how many instances each has, collection status and
    any active alerts.

    This is step 1 of retrieving metric data:
    1. this tool -> get deviceDataSourceId for the datasource you want
    2. list_resource_instances -> get instanceId for a specific instance
    3. get_resource_instance_data -> get the actual metric values

    Troubleshooting: "Why no CPU data?" -> check the WinCPU datasource is applied
    and collecting; inspect the status field for errors.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_resource_datasource (details), list_resource_instances
    (next step), get_resource_instance_data (metrics)."""
    return _list(f"/device/devices/{deviceId}/devicedatasources", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_resource_datasource(deviceId: int, deviceDataSourceId: int, fields: str | None = None) -> Any:
    """Get details of one datasource applied to a resource/device.

    Returns: full device-datasource details - dataSourceName, status, alert
    status, instance count, monitoring configuration, stopMonitoring flag, custom
    properties and graphs.

    Key fields:
    - instanceNumber: how many instances (e.g. 4 network interfaces)
    - status: collection status (normal vs error)
    - alertStatus: any active alerts from this datasource
    - stopMonitoring: whether the datasource is disabled on this device

    Workflow: use list_resource_datasources to find deviceDataSourceId, then this
    tool for detailed status.

    Related tools: list_resource_datasources (find it), list_resource_instances
    (its instances)."""
    return _get(f"/device/devices/{deviceId}/devicedatasources/{deviceDataSourceId}", fields=fields)


@mcp.tool
def list_resource_instances(
    deviceId: int, deviceDataSourceId: int, filter: str | None = None,
    size: int | None = None, offset: int | None = None, fields: str | None = None,
) -> Any:
    """List the instances of a datasource on a resource/device.

    Returns: array of instances with id, name, displayName, description, status,
    alert status and last collection time.

    What instances are: the individual components a datasource monitors - e.g.
    individual disks (C:, D:, E:), network interfaces (eth0, eth1), database
    tables or processes.

    Complete workflow to get metrics:
    1. list_resource_datasources -> get deviceDataSourceId
    2. this tool -> get instanceId
    3. get_resource_instance_data -> get the actual metric values

    Related tools: list_resource_datasources (first step),
    get_resource_instance_data (get metrics)."""
    return _list(f"/device/devices/{deviceId}/devicedatasources/{deviceDataSourceId}/instances", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_resource_instance_data(
    deviceId: int, deviceDataSourceId: int, instanceId: int,
    datapoints: str | None = None, start: int | None = None,
    end: int | None = None, format: str | None = None,
) -> Any:
    """Get time-series metric data (datapoints) for a datasource instance, e.g.
    CPU/memory/network utilization.

    Returns: time-series data with timestamps and values for the requested
    datapoints.

    Required workflow (3 steps):
    1. list_resource_datasources -> deviceDataSourceId for the datasource (e.g. WinCPU)
    2. list_resource_instances  -> instanceId for the specific instance
    3. this tool -> the actual metric values

    Parameters:
    - deviceId / deviceDataSourceId / instanceId: from the steps above
    - datapoints: comma-separated metric names (e.g. "CPUBusyPercent,MemoryUsedPercent");
      omit for all
    - start / end: time range in epoch SECONDS (start must be before now); if
      omitted LM returns a recent default window

    Related tools: list_resource_datasources, list_resource_instances."""
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
    fields: str | None = None, needMessage: bool | None = None,
) -> Any:
    """List alerts in LogicMonitor.

    Returns: array of alerts with id (alertId), severity, resource name,
    datasource, datapoint, alert message, start time (startEpoch),
    acknowledgement status (acked) and the alert rule applied.

    When to use:
    - Get all critical alerts
    - Find unacknowledged alerts needing attention
    - Check CPU/memory alerts on a device
    - Generate alert reports

    Common filter patterns:
    - By severity:    filter='severity:4' (critical)
    - Unacknowledged: filter='acked:false'
    - Not cleared:    filter='cleared:false'
    - Specific device: filter='monitorObjectName~"*prod-web-01*"'
    - Recent:         filter='startEpoch>1730851200' (epoch seconds)
    - Combined (AND): filter='severity:4,acked:false'

    Note: the alert API does NOT support the OR operator (||) - use comma for AND
    only; for OR conditions make multiple calls. Set needMessage=true to include
    the full alert message text.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_alert (full details), generate_alert_link (portal URL)."""
    return _list("/alert/alerts", filter=filter, size=size, offset=offset, fields=fields, needMessage=needMessage)


@mcp.tool
def get_alert(alertId: str, fields: str | None = None, needMessage: bool | None = None) -> Any:
    """Get detailed information about a specific alert by its ID.

    Returns: complete alert details - alert message, severity, threshold crossed,
    current value, alert history, escalation chain triggered, acknowledgement
    details, resource details, datasource/datapoint info and the alert rule
    applied. Set needMessage=true to include the full message text.

    When to use:
    - Investigate a specific alert after getting its ID from list_alerts
    - Check the threshold and current values
    - Review alert history and escalation

    Workflow: use list_alerts to find the alertId, then this tool for the full
    investigation details.

    Related tools: list_alerts (find alerts), generate_alert_link (share URL)."""
    return _get(f"/alert/alerts/{alertId}", fields=fields, needMessage=needMessage)


@mcp.tool
def list_alert_rules(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all alert rules - the routing logic that sends alerts to escalation chains.

    Returns: array of alert rules with id, name, priority, enabled status,
    matching conditions (device/datasource/severity filters), the escalation
    chain assigned and suppression settings.

    What they are: alert rules are traffic directors - "IF an alert matches these
    conditions, THEN send it to this escalation chain." Rules are evaluated in
    priority order and the first match wins.

    When to use:
    - Audit who gets notified for different alert types
    - Understand notification routing logic
    - Troubleshoot "why didn't I get alerted?" (does the alert match a rule? is
      the rule enabled? is the chain configured?)

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_alert_rule (detailed conditions), list_escalation_chains
    (destination chains)."""
    return _list("/setting/alert/rules", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_alert_rule(ruleId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific alert rule by ID.

    Returns: complete rule details - name, priority, enabled status, detailed
    matching conditions (device groups, datasources, datapoints, instance
    filters, severity levels), escalation chain assignment, suppression windows
    and notification settings.

    Matching conditions explained:
    - deviceGroups: which device folders the rule applies to
    - datasources: which datasources trigger it (e.g. CPU, Memory, AWS_EC2)
    - datapoints: specific metrics (e.g. CPUBusyPercent)
    - instances: filter by instance name (e.g. C: drive only)
    - severity: alert levels (critical, error, warn)
    - escalatingChainId: where matching alerts are routed

    Workflow: use list_alert_rules to find ruleId, then this tool to review the
    complete matching logic and routing.

    Related tools: list_alert_rules (find rules), get_escalation_chain (check the
    notification chain)."""
    return _get(f"/setting/alert/rules/{ruleId}", fields=fields)


@mcp.tool
def list_escalation_chains(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all escalation chains.

    Returns: array of escalation chains with id, name, description, escalation
    stages, recipients at each stage, timing/delays and enabled status.

    What they are: escalation chains define HOW and WHO gets notified when alerts
    trigger. They are multi-stage workflows: Stage 1 notifies immediately ->
    Stage 2 notifies after X minutes if still open -> Stage 3 escalates further.

    When to use:
    - Audit notification routing
    - Review who gets notified for critical alerts
    - Verify on-call escalation paths

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_escalation_chain (detailed stages), list_alert_rules
    (which rules use a chain), list_recipients (available notification targets)."""
    return _list("/setting/alert/chains", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_escalation_chain(chainId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific escalation chain by its ID.

    Returns: complete chain details - name, description and all stages, each with
    its recipients, notification methods (email/SMS/webhook), time delays between
    stages, rate limiting and business-hours restrictions.

    Per stage you get: stage number, delay before it triggers (minutes),
    recipients/groups notified, notification methods, and schedule (24/7 vs
    business hours only).

    When to use:
    - Review the detailed notification workflow
    - Verify who gets notified at each stage and the timing between escalations
    - Troubleshoot why notifications weren't received

    Workflow: use list_escalation_chains to find chainId, then this tool for the
    complete workflow.

    Related tools: list_escalation_chains (find chains), list_recipients (see
    recipients)."""
    return _get(f"/setting/alert/chains/{chainId}", fields=fields)


@mcp.tool
def list_recipients(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all alert recipients (individual notification targets).

    Returns: array of recipients with id, type (email/SMS/voice/webhook), contact
    information, method (email address, phone number, webhook URL), name and status.

    What they are: individual notification endpoints used inside escalation
    chains - email addresses, SMS/phone numbers, webhook URLs, or integration
    endpoints (Slack, PagerDuty, etc.). Recipients are single targets; recipient
    groups bundle several together to notify a whole team at once.

    When to use:
    - Audit who can receive alerts
    - Verify contact information is current
    - Review notification endpoints

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_recipient (details), list_recipient_groups (group
    management), list_escalation_chains (who gets notified)."""
    return _list("/setting/recipients", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_recipient(recipientId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific recipient by ID.

    Returns: complete recipient details - type, name, contact information
    (email/phone/URL), notification method, timezone, schedule restrictions and
    rate-limiting settings.

    Details returned:
    - Contact info: exact email/phone/webhook URL
    - Schedule: when notifications are sent (always vs business hours)
    - Rate limit: max notifications per period (prevents notification fatigue)
    - Method: delivery mechanism (SMTP, Twilio, webhook)

    Workflow: use list_recipients to find recipientId, then this tool for the
    full configuration.

    Related tools: list_recipients (find recipient), list_escalation_chains (usage)."""
    return _get(f"/setting/recipients/{recipientId}", fields=fields)


@mcp.tool
def list_recipient_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all recipient groups.

    Returns: array of recipient groups with id, name, description, member count
    and recipients list.

    What they are: collections of recipients treated as a single notification
    target, so an escalation chain can notify an entire team at once (e.g. a
    "Database Team" group containing 5 members). Updating the group updates every
    chain that uses it.

    When to use:
    - Audit team notification lists
    - Review group membership before changes

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_recipient_group (details), list_recipients (individual
    members), list_escalation_chains (usage)."""
    return _list("/setting/recipientgroups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_recipient_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific recipient group by ID.

    Returns: complete group details - name, description, the list of all members
    (recipients) with their contact info, and the escalation chains that use this
    group.

    When to use:
    - Review group membership before changes (removing a member affects every
      chain that uses the group)
    - Verify who gets notified through this group

    Workflow: use list_recipient_groups to find groupId, then this tool to review
    membership.

    Related tools: list_recipient_groups (find groups), list_escalation_chains
    (where it's used)."""
    return _get(f"/setting/recipientgroups/{groupId}", fields=fields)


# ==========================================================================
# Collectors
# ==========================================================================

@mcp.tool
def list_collectors(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all collectors - the agents that gather monitoring data.

    Returns: array of collectors with id, description (collector name), hostname,
    platform (Windows/Linux), status (alive/dead), build version, number of
    monitored devices and last heartbeat time.

    What they are: lightweight agents installed on-premise or in cloud that
    collect metrics from devices. Each device is assigned to one collector.

    When to use:
    - Check collector health before assigning devices
    - Find available collectors for new device assignments
    - Monitor collector capacity and load
    - Identify offline/dead collectors

    Common filter patterns:
    - Alive:        filter='status:alive'
    - By platform:  filter='platform:Linux'  or  filter='platform:Windows'
    - By name:      filter='description~"*prod*"'
    - Low capacity: filter='numberOfHosts<100'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_collector (details), list_collector_groups (browse
    groups), list_collector_versions (check updates)."""
    return _list("/setting/collector/collectors", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_collector(collectorId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific collector by its ID.

    Returns: complete collector details - description (name), hostname, platform,
    status, build version, number of devices monitored, free disk space,
    CPU/memory usage, last heartbeat and configuration.

    Health indicators to check:
    - status: "alive" (healthy) vs "dead" (offline/problem)
    - numberOfHosts: how many devices it monitors (capacity planning)
    - freeDiskSpace: space available for data buffering
    - build: version (compare with list_collector_versions for updates)
    - lastHeartbeatTime: recent = healthy, old = potential issue

    Workflow: use list_collectors to find collectorId, then this tool for a
    detailed health check.

    Related tools: list_collectors (find collector), list_collector_versions
    (check updates), list_resources (assigned devices)."""
    return _get(f"/setting/collector/collectors/{collectorId}", fields=fields)


@mcp.tool
def list_collector_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all collector groups (folders).

    Returns: array of collector groups with id, name, parentId, full path,
    description, number of collectors and number of subgroups.

    What they are: organizational folders for collectors, similar to device
    groups - categorize collectors by location, environment, customer,
    datacenter or function.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_collector_group (details), list_collectors (collectors in
    group)."""
    return _list("/setting/collector/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_collector_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific collector group by ID.

    Returns: complete group details - name, full path, parentId, description,
    number of collectors (direct and total) and number of subgroups.

    Workflow: use list_collector_groups to find groupId, then this tool for the
    full details.

    Related tools: list_collector_groups (find groups), list_collectors
    (collectors in group)."""
    return _get(f"/setting/collector/groups/{groupId}", fields=fields)


@mcp.tool
def list_collector_versions(size: int | None = None, offset: int | None = None, fields: str | None = None) -> Any:
    """List available collector versions.

    Returns: array of collector versions with version number, release date,
    stability level (GA/EA/RC), changelog summary, download size, platform
    support and mandatory/recommended flags.

    Version types:
    - GA (Generally Available): production-ready, stable, recommended
    - EA (Early Adopter): beta/new features, use in non-production first
    - RC (Release Candidate): pre-GA testing version
    - Mandatory: critical security/bug fixes, upgrade required

    When to use:
    - Check for collector updates and review the changelog
    - Find a specific version for rollback
    - Verify platform compatibility before upgrading

    Compare the latest version here against the `build` field from get_collector
    to see which collectors are behind.

    Related tools: get_collector (current version on a collector), list_collectors
    (find collectors to upgrade)."""
    return _get("/setting/collector/collectors/versions", size=size, offset=offset, fields=fields)


# ==========================================================================
# DataSources / EventSources / ConfigSources
# ==========================================================================

@mcp.tool
def list_datasources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all datasource definitions.

    Returns: array of datasources with id, name, displayName, description,
    appliesTo (which devices it monitors), collection method and the
    datapoints/metrics collected.

    What they are: templates that define WHAT to monitor (CPU, memory, disk), HOW
    to collect it (SNMP, WMI, API, script) and WHEN to alert. LogicMonitor ships
    2000+ pre-built datasources for common technologies.

    When to use:
    - Find the datasource for a technology (e.g. "AWS_EC2", "VMware_vCenter")
    - Discover what can be monitored
    - Browse monitoring capabilities

    Common filter patterns:
    - By name:        filter='name~"*CPU*"'
    - Cloud providers: filter='name~"*AWS*"'  or  filter='name~"*Azure*"'
    - Database:       filter='name~"*MySQL*"'
    - Network:        filter='name~"*Cisco*"'  or  filter='name~"*SNMP*"'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_datasource (details), list_resource_datasources (what's
    applied to a specific device)."""
    return _list("/setting/datasources", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_datasource(dataSourceId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific datasource definition by its ID.

    Returns: complete datasource details - name, displayName, description,
    appliesTo logic, collection method, datapoints (metrics), thresholds, alert
    rules and polling interval.

    Key fields:
    - appliesTo: logic determining which devices get this datasource
      (e.g. 'system.hostname =~ "*prod*"')
    - dataSourceType: collection method (SNMP, WMI, JDBC, API, script)
    - dataPoints: metrics collected (e.g. CPUBusyPercent, MemoryUsedPercent)
    - alertExpr: threshold formulas (when to alert)
    - collectInterval: how often data is collected (seconds)

    Workflow: use list_datasources to find dataSourceId, then this tool to
    understand how it works.

    Related tools: list_datasources (find datasource), list_resource_datasources
    (devices using it)."""
    return _get(f"/setting/datasources/{dataSourceId}", fields=fields)


@mcp.tool
def list_eventsources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all EventSource definitions.

    Returns: array of EventSources with id, name, displayName, description,
    appliesTo logic and event collection method.

    What they are: EventSources collect and process event data - Windows event
    logs, syslog, SNMP traps, application logs, cloud events. Distinct from
    DataSources (metrics) and ConfigSources (configs); used for log monitoring
    and event correlation.

    Common EventSources: Windows_Application_EventLog, Windows_Security_EventLog,
    Linux_Syslog, SNMP_Traps, VMware_Events.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_eventsource (details)."""
    return _list("/setting/eventsources", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_eventsource(eventSourceId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific EventSource by its ID.

    Returns: complete EventSource details - name, displayName, description,
    appliesTo logic, collection method, filter rules, severity mapping and alert
    settings.

    Key fields:
    - appliesTo: which devices get event monitoring
    - filters: rules for parsing/matching events
    - severityMapping: maps event levels (INFO/WARN/ERROR) to LM alert levels
    - schedule: when event collection runs

    Workflow: use list_eventsources to find eventSourceId, then this tool for the
    full configuration.

    Related tools: list_eventsources (find EventSource)."""
    return _get(f"/setting/eventsources/{eventSourceId}", fields=fields)


@mcp.tool
def list_configsources(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all ConfigSource definitions.

    Returns: array of ConfigSources with id, name, displayName, description,
    appliesTo logic and collection method.

    What they are: ConfigSources track configuration-file changes for compliance
    and change management - like datasources, but for configs instead of metrics.
    Alert when a config changes unexpectedly.

    What can be tracked: network device configs (router/switch/firewall), Linux
    /etc files and SSH keys, Windows registry/policies, cloud security groups and
    IAM policies.

    Common ConfigSources: Cisco_IOS_Config, F5_LTM_Config, Palo_Alto_Config,
    Linux_Config_Files.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_configsource (details)."""
    return _list("/setting/configsources", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_configsource(configSourceId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific ConfigSource by its ID.

    Returns: complete ConfigSource details - name, displayName, description,
    appliesTo logic (which devices), collection method (CLI/SNMP/API), the
    collection script and alert settings.

    Key fields:
    - appliesTo: logic determining which devices get config tracking
    - collectMethod: how the config is retrieved (CLI commands, SNMP, API)
    - configAlerts: settings for when to alert on changes
    - lineageId: built-in (LogicMonitor) vs custom ConfigSource

    Workflow: use list_configsources to find configSourceId, then this tool to
    understand how it works.

    Related tools: list_configsources (find ConfigSource)."""
    return _get(f"/setting/configsources/{configSourceId}", fields=fields)


# ==========================================================================
# Dashboards
# ==========================================================================

@mcp.tool
def list_dashboards(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all dashboards.

    Returns: array of dashboards with id, name, description, groupId, groupName,
    widget count and owner.

    When to use:
    - Find AWS/Azure/infrastructure dashboards
    - Discover available pre-built dashboards
    - Get dashboard IDs for generating shareable links

    Common filter patterns:
    - By name:  filter='name~"*AWS*"'
    - By group: filter='groupId:5'  or  filter='groupName~"*Cloud*"'
    - By owner: filter='owner:john.doe'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_dashboard (details), generate_dashboard_link (portal URL),
    list_dashboard_groups (browse hierarchy)."""
    return _list("/dashboard/dashboards", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_dashboard(dashboardId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific dashboard by its ID.

    Returns: complete dashboard details - name, description, groupId, owner,
    widgets configuration, widget count, sharing settings, template variables and
    last-modified time.

    What you get:
    - widgetsConfig: configuration of all widgets (chart types, metrics, thresholds)
    - widgetTokens: template variables (e.g. defaultDeviceGroup for dynamic filtering)
    - groupId/groupName: which folder the dashboard is in
    - sharable: whether the dashboard is public/private

    Workflow: use list_dashboards to find dashboardId, then this tool for details,
    then generate_dashboard_link for a shareable URL.

    Related tools: list_dashboards (find dashboard), generate_dashboard_link
    (portal URL), list_dashboard_groups (browse folders)."""
    return _get(f"/dashboard/dashboards/{dashboardId}", fields=fields)


@mcp.tool
def list_dashboard_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all dashboard groups (folders).

    Returns: array of dashboard groups with id, name, parentId, full path,
    description, number of dashboards, number of subgroups and owner.

    What they are: organizational folders for dashboards, like directories -
    organize by team, environment, application or cloud provider.

    Workflow: use this tool to browse the hierarchy, then list_dashboards
    filtered by groupId to see dashboards in a specific folder.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_dashboard_group (details), list_dashboards (dashboards in
    group)."""
    return _list("/dashboard/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_dashboard_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific dashboard group by its ID.

    Returns: complete group details - name, full path, parentId, description,
    number of dashboards (direct and total), number of subgroups, owner and
    permissions.

    Workflow: use list_dashboard_groups to find groupId, then this tool for the
    full details.

    Related tools: list_dashboard_groups (find groups), list_dashboards
    (dashboards in group)."""
    return _get(f"/dashboard/groups/{groupId}", fields=fields)


# ==========================================================================
# Reports
# ==========================================================================

@mcp.tool
def list_reports(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all reports (scheduled and on-demand).

    Returns: array of reports with id, name, type (alert/availability/
    capacity/performance), description, schedule, recipients, format
    (PDF/HTML/CSV) and last run time.

    What they are: scheduled or on-demand documents summarizing monitoring data -
    metrics, alerts, availability/SLA statistics, capacity-planning forecasts -
    optionally emailed to stakeholders.

    Report types: Alert (counts by severity, MTTR, top alerting devices),
    Availability (uptime/SLA, outages), Capacity Planning (growth trends,
    forecasting), Performance (metric trends, top consumers), and Custom.

    When to use:
    - Find existing reports before creating duplicates
    - Review report schedules and who receives them
    - Audit reporting configuration

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_report (details), list_report_groups (organization)."""
    return _list("/report/reports", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_report(reportId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific report by its ID.

    Returns: complete report details - name, type, description, schedule
    (daily/weekly/monthly), recipients, format, data sources (which
    devices/groups), date range, customization settings, last-run timestamp and
    delivery status.

    Configuration details:
    - Schedule: when the report runs (e.g. "Every Monday at 8am")
    - Recipients: who receives it by email
    - Format: PDF (management), HTML (web), CSV (data analysis)
    - Scope: which devices/groups are included
    - Date range: last 7 days, last month, custom period

    Workflow: use list_reports to find reportId, then this tool for the full
    configuration.

    Related tools: list_reports (find reports), list_report_groups (organization)."""
    return _get(f"/report/reports/{reportId}", fields=fields)


@mcp.tool
def list_report_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all report groups (folders).

    Returns: array of report groups with id, name, parentId, full path,
    description, number of reports and number of subgroups.

    What they are: organizational folders for reports - categorize by audience
    (executive/operations/customer), frequency, department or type.

    Workflow: use this tool to browse the hierarchy, then list_reports filtered by
    groupId to see reports in a specific folder.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_report_group (details), list_reports (reports in group)."""
    return _list("/report/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_report_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific report group by ID.

    Returns: complete group details - name, full path, parentId, description,
    number of reports (direct and total) and number of subgroups.

    Workflow: use list_report_groups to find groupId, then this tool for the full
    details.

    Related tools: list_report_groups (find groups), list_reports (reports in
    group)."""
    return _get(f"/report/groups/{groupId}", fields=fields)


# ==========================================================================
# Websites (synthetic monitoring)
# ==========================================================================

@mcp.tool
def list_websites(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all website monitors (synthetic checks).

    Returns: array of website monitors with id, name, type (webcheck/pingcheck),
    domain/URL, status, checkpoint locations, response time and availability
    percentage.

    What they are: synthetic checks that test URL/service availability from
    multiple global locations - like "ping from the internet" to verify your
    services are reachable.

    Monitor types:
    - webcheck: full HTTP/HTTPS check (status code, response time, content
      validation, SSL cert)
    - pingcheck: simple ICMP ping test (faster, simpler)

    Common filter patterns:
    - By domain: filter='domain~"*example.com*"'
    - By type:   filter='type:webcheck'  or  filter='type:pingcheck'
    - By status: filter='overallAlertStatus:critical'  (find down sites)
    - By name:   filter='name~"*production*"'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_website (details), generate_website_link (portal URL),
    list_website_checkpoints (available locations)."""
    return _list("/website/websites", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_website(websiteId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific website monitor by its ID.

    Returns: complete monitor details - name, type (webcheck/pingcheck),
    domain/URL, monitoring configuration, checkpoint locations, response-time
    thresholds, SSL settings, authentication, custom headers and alert status.

    Configuration details returned:
    - steps: multi-step transaction monitoring (for complex workflows)
    - checkpoints: which global locations perform the checks
    - schema: HTTP vs HTTPS
    - testLocation: internal (from a collector) vs external (from the cloud)
    - responseTimeThreshold: alert if slower than X ms
    - sslCertExpirationDays: alert X days before the cert expires

    When to use:
    - Review monitoring configuration and checkpoint locations
    - Verify URL and SSL certificate monitoring settings
    - Troubleshoot failed checks

    Workflow: use list_websites to find websiteId, then this tool for the full
    configuration.

    Related tools: list_websites (find website), generate_website_link (portal
    URL), list_website_checkpoints (available locations)."""
    return _get(f"/website/websites/{websiteId}", fields=fields)


@mcp.tool
def list_website_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all website groups (folders).

    Returns: array of website groups with id, name, parentId, full path,
    description, number of websites and number of subgroups.

    What they are: organizational folders for website monitors, similar to device
    groups - categorize monitored URLs/services by application, environment,
    location or customer.

    Workflow: use this tool to browse the hierarchy, then list_websites filtered
    by groupId to see monitors in a specific folder.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_website_group (details), list_websites (websites in group)."""
    return _list("/website/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_website_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific website group by its ID.

    Returns: complete group details - name, full path, parentId, description,
    number of websites (direct and total), number of subgroups and alert status.

    Workflow: use list_website_groups to find groupId, then this tool for the
    full details.

    Related tools: list_website_groups (find groups), list_websites (websites in
    group)."""
    return _get(f"/website/groups/{groupId}", fields=fields)


@mcp.tool
def list_website_checkpoints(fields: str | None = None) -> Any:
    """List available checkpoint locations for website monitoring.

    Returns: array of checkpoint locations with id, name, geographic region,
    status and type (internal/external).

    What they are: the global vantage points from which LogicMonitor runs
    synthetic website checks - think "test my website from New York, London,
    Tokyo."
    - External (cloud): LM-managed locations worldwide (US-East, EU-West,
      Asia-Pacific, etc.)
    - Internal (collector-based): tests run from your own collectors (for
      internal apps, VPNs, private networks)

    When to use:
    - Check available locations before reviewing/creating website monitors
    - Verify geographic coverage for multi-region/SLA monitoring

    Related tools: list_websites (existing monitors), get_website (verify a
    monitor's checkpoint configuration)."""
    return _get("/website/smcheckpoints", fields=fields)


# ==========================================================================
# Services
# ==========================================================================

@mcp.tool
def list_services(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all business services.

    Returns: array of services with id, name, description, health status,
    dependencies, monitored resources, service level objectives (SLOs) and
    availability percentage.

    What they are: business-level constructs that aggregate multiple resources
    into a single health status - representing customer-facing services,
    applications or business processes. Example: an "E-Commerce Platform" service
    rolls up web servers, databases, load balancers and APIs into one health
    indicator, so stakeholders see "is the application working?" instead of "is
    server X working?"

    When to use:
    - Monitor business-service health vs individual device health
    - Track SLA compliance for customer-facing services
    - Understand service dependencies

    Common filter patterns:
    - By status: filter='status:normal'  or  filter='status:dead'
    - By name:   filter='name~"*production*"'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_service (details and dependencies), list_service_groups
    (organization)."""
    return _list("/service/services", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_service(serviceId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific service by ID.

    Returns: complete service details - name, description, health status, the
    dependency tree (all resources comprising the service), SLA/SLO
    configuration, availability statistics, alert rules and the service group.

    Key information:
    - Dependency tree: all resources that comprise the service
    - Health calculation: how the status is derived (e.g. "if ANY web server is
      down, the service is degraded")
    - Current status: operational / degraded / down
    - SLA metrics: uptime percentage and outage history

    Troubleshooting: service shows "down" -> check the dependency tree -> identify
    which resource(s) failed -> address those; the service auto-recovers when
    dependencies are healthy.

    Workflow: use list_services to find serviceId, then this tool for the full
    dependency analysis.

    Related tools: list_services (find service), list_resources (health of
    dependent resources)."""
    return _get(f"/service/services/{serviceId}", fields=fields)


@mcp.tool
def list_service_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all service groups (folders).

    Returns: array of service groups with id, name, parentId, full path,
    description, number of services and number of subgroups.

    What they are: organizational folders for business services, similar to
    device groups - categorize services by business unit, customer, region or
    SLA tier.

    Workflow: use this tool to browse the hierarchy, then list_services filtered
    by groupId to see services in a specific folder.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_service_group (details), list_services (services in group)."""
    return _list("/service/groups", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_service_group(groupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific service group by ID.

    Returns: complete group details - name, full path, parentId, description,
    number of services (direct and total) and number of subgroups.

    Workflow: use list_service_groups to find groupId, then this tool for the
    full details.

    Related tools: list_service_groups (find groups), list_services (services in
    group)."""
    return _get(f"/service/groups/{groupId}", fields=fields)


# ==========================================================================
# Users / roles / access groups / API tokens
# ==========================================================================

@mcp.tool
def list_users(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all user accounts (admins).

    Returns: array of users with id, username, email, roles, status
    (active/suspended), last login time, created date and API token count.

    When to use:
    - Audit user access and check who has admin rights
    - Find user IDs for API-token review
    - Identify inactive users for compliance

    Common filter patterns:
    - Active users:    filter='status:active'
    - By email:        filter='email~"*@company.com"'
    - By role:         filter='roles:"*administrator*"'
    - Never logged in: filter='lastLoginOn:0'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_user (details), list_roles (available roles),
    list_api_tokens (a user's API tokens)."""
    return _list("/setting/admins", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_user(userId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific user by their ID.

    Returns: complete user details - username, email, firstName, lastName, roles
    (permissions), status (active/suspended), last login time, created date,
    phone, timezone, API token count and two-factor-auth status.

    Key fields:
    - roles: array of role names (defines permissions)
    - status: "active" (can log in) vs "suspended" (access revoked)
    - lastLoginOn: epoch timestamp (identify inactive accounts)
    - apiTokens: number of active API tokens
    - twoFAEnabled: whether 2FA is configured

    Security audit use cases: find users inactive 90+ days, review who has admin
    roles, check whether former employees still have access.

    Workflow: use list_users to find userId, then this tool for the full profile.

    Related tools: list_users (find user), list_roles (available roles),
    list_api_tokens (the user's tokens)."""
    return _get(f"/setting/admins/{userId}", fields=fields)


@mcp.tool
def list_roles(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all roles (permission sets).

    Returns: array of roles with id, name, description, custom flag, associated
    user count and permissions (view/manage/delete for
    resources/alerts/reports/settings).

    What they are: permission templates assigned to users that control who can
    view/modify/delete resources, alerts, dashboards and settings - the basis of
    RBAC. Built-in examples: administrator (full access), readonly (view-only),
    manager (manage resources/alerts but not settings). Organizations also create
    custom roles (e.g. "database-team-role").

    When to use:
    - Discover available roles
    - Audit the permission structure
    - Find role IDs for review

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_role (detailed permissions), list_users (who has each role)."""
    return _list("/setting/roles", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_role(roleId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific role by its ID.

    Returns: complete role details - name, description, custom flag and the
    detailed permission matrix (view/manage/delete/acknowledge for each area:
    resources/devices, alerts, dashboards, reports, settings, users).

    Permission granularity:
    - Resources: view/add/modify/delete devices
    - Alerts: view/acknowledge/manage alert rules
    - Dashboards: view/create/edit/delete
    - Reports: view/create/schedule
    - Settings: modify datasources/collectors/integrations
    - Users: manage other users/roles

    Use cases: security audits ("can this role delete production devices?"),
    least-privilege selection, and documenting role permissions for compliance.

    Workflow: use list_roles to find roleId, then this tool to review the exact
    permissions.

    Related tools: list_roles (find roles), list_users (who has this role)."""
    return _get(f"/setting/roles/{roleId}", fields=fields)


@mcp.tool
def list_access_groups(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all access groups.

    Returns: array of access groups with id, name, description, tenant ID, number
    of associated resources and number of users.

    What they are: permission boundaries that control WHICH resources users can
    see and manage. Used in multi-tenant (MSP) environments to isolate customer
    data, or to segment access by team/department.

    Access groups vs roles (important distinction):
    - Access groups control WHAT you can see (visibility / data isolation)
    - Roles control WHAT actions you can perform (view/edit/delete)
    - Users need both: a role (what they can do) + an access group (what they can see)

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_access_group (details), list_users (user assignments),
    list_resources (resources associated with groups)."""
    return _list("/setting/accessgroup", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_access_group(accessGroupId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific access group by its ID.

    Returns: complete access-group details - name, description, tenant ID, the
    list of associated resources (which device groups/resources are in this access
    group) and the list of users assigned to it.

    Key information:
    - Resources: which device groups/resources users in this group can see
    - Users: who is assigned to this access group
    - Tenant ID: multi-tenant identifier (MSP environments)

    Impact analysis before changes: removing a resource hides it from the group's
    users; removing a user revokes their visibility to everything in the group.

    Workflow: use list_access_groups to find accessGroupId, then this tool to
    review the full configuration.

    Related tools: list_access_groups (find groups), list_users (user access)."""
    return _get(f"/setting/accessgroup/{accessGroupId}", fields=fields)


@mcp.tool
def list_api_tokens(
    userId: int, filter: str | None = None, size: int | None = None,
    offset: int | None = None, fields: str | None = None,
) -> Any:
    """List API tokens belonging to a specific user (secret keys are NOT returned).

    Returns: array of tokens for the given userId with id, note (description),
    created date, last-used date, status (active/inactive), access ID and the
    roles inherited from the user.

    What they are: authentication credentials for the LogicMonitor REST API - an
    alternative to username/password for programmatic access. Each token inherits
    its user's permissions and does not expire automatically (must be revoked
    manually).

    When to use:
    - Audit API access per user
    - Find unused/stale tokens (check lastUsedOn) for security cleanup
    - Inventory which integrations use a user's tokens

    Security workflow: list_users -> for each user, this tool -> review lastUsedOn
    (>90 days = candidate for revocation) and the note field for purpose.

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: list_users (find userId)."""
    return _list(f"/setting/admins/{userId}/apitokens", filter=filter, size=size, offset=offset, fields=fields)


# ==========================================================================
# SDTs / OpsNotes / audit logs / netscans / integrations / topology
# ==========================================================================

@mcp.tool
def list_sdts(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all Scheduled Down Times (SDTs) - maintenance windows.

    Returns: array of SDTs with id, type (DeviceSDT/DeviceGroupSDT/etc),
    device/group name, start/end times, duration, comment, creator and status
    (active/scheduled/expired).

    What they are: maintenance windows that suppress alerting to prevent false
    alarms during planned work. No alerts fire during an SDT period.

    SDT types: DeviceSDT (all monitoring on a device), DeviceGroupSDT (all devices
    in a group), DeviceDataSourceSDT (one datasource on a device),
    DeviceDataSourceInstanceSDT (one instance only, e.g. C: drive).

    Common filter patterns:
    - Active now:  filter='isEffective:true'
    - Future:      filter='startDateTime>{epoch}'
    - By device:   filter='deviceDisplayName~"*prod-web*"'
    - By creator:  filter='admin:john.doe'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_sdt (details)."""
    return _list("/sdt/sdts", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_sdt(sdtId: str, fields: str | None = None) -> Any:
    """Get detailed information about a specific Scheduled Down Time (SDT) by ID.

    Returns: complete SDT details - type, device/group affected, start/end times,
    duration, comment, who created it, status and recurrence settings.

    Status meanings:
    - scheduled: future window (not started yet)
    - active: currently in the window (alerts suppressed now)
    - expired: window completed (historical record)

    Workflow: use list_sdts to find the SDT ID, then this tool for the full
    details.

    Related tools: list_sdts (find SDTs)."""
    return _get(f"/sdt/sdts/{sdtId}", fields=fields)


@mcp.tool
def list_opsnotes(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all operational notes (OpsNotes).

    Returns: array of OpsNotes with id, note text, timestamp (epoch), creator,
    tags, scope (which devices/groups it applies to) and related SDTs.

    What they are: timestamped operational annotations shown on graphs and
    dashboards as vertical lines at the time they occurred. Document deployments,
    changes, maintenance and incidents - anything that might affect metrics - so
    you can correlate metric changes with operational events ("latency jumped at
    2pm" -> OpsNote: "deploy at 2pm").

    Common filter patterns:
    - By time:   filter='happenedOn>1730851200'
    - By tags:   filter='tags~"*deployment*"'
    - By device: filter='monitorObjectName~"*prod-web*"'

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_opsnote (details)."""
    return _list("/setting/opsnotes", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_opsnote(opsNoteId: str, fields: str | None = None) -> Any:
    """Get detailed information about a specific operational note by ID.

    Returns: complete OpsNote details - note text, timestamp, creator, tags,
    scope (devices/groups affected), related SDTs and linked resources.

    Workflow: use list_opsnotes to find the note ID, then this tool for the full
    details.

    Related tools: list_opsnotes (find notes)."""
    return _get(f"/setting/opsnotes/{opsNoteId}", fields=fields)


@mcp.tool
def list_audit_logs(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List audit/access logs of portal activity (for compliance and security).

    Returns: array of audit-log entries with id, username, IP address, timestamp
    (happenedOn, in epoch SECONDS), a description of the action performed and
    sessionId.

    When to use:
    - Investigate changes: "who deleted this device?"
    - Track a user's activity over a time window
    - Monitor API usage and login attempts
    - Compliance audits and security investigations

    Common filter patterns:
    - By user:        filter='username:john.doe'
    - By time:        filter='happenedOn>1640995200'  (epoch SECONDS, not ms!)
    - By action type: filter='description~"*Delete*"'  (or *Create*, *Update*)
    - By resource:    filter='description~"*device*"'
    - By IP:          filter='ip:192.168.1.100'
    - Combined (AND): filter='username:admin,happenedOn>1640995200,description~"*device*"'

    Critical notes:
    - Time is epoch SECONDS (not milliseconds like some other LM APIs)
    - The audit-log API does NOT support the OR operator (||), only AND (comma)

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_audit_log (details of a specific entry)."""
    return _list("/setting/accesslogs", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_audit_log(auditLogId: str, fields: str | None = None) -> Any:
    """Get detailed information about a specific audit-log entry by its ID.

    Returns: complete entry details - username, IP address, exact timestamp, the
    full description of the action, session ID, affected resources and
    before/after values (for updates).

    When to use:
    - Get full details after finding a log ID via list_audit_logs
    - Review exact changes made (old vs new values)
    - Investigate a specific incident with full context

    Workflow: use list_audit_logs with filters to find relevant entries, then
    this tool with the log ID for complete details.

    Related tools: list_audit_logs (search logs)."""
    return _get(f"/setting/accesslogs/{auditLogId}", fields=fields)


@mcp.tool
def list_netscans(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all NetScan (network discovery) definitions.

    Returns: array of netscans with id, name, description, scan method
    (nmap/script/ICMP/SNMP/cloud), schedule, target networks (IP ranges/subnets),
    collector and last-run time.

    What they are: automated network discovery that finds devices on your network
    and onboards them into monitoring - instead of adding devices one by one,
    netscan discovers them by IP range/subnet (or cloud API) and applies
    properties and datasources automatically.

    Scan methods: nmap (comprehensive), ICMP ping (fast reachability), SNMP walk
    (network gear), script (custom logic), and AWS/Azure/GCP (cloud
    auto-discovery).

    When to use:
    - Audit existing discovery configurations
    - Check which networks are being scanned and on what schedule
    - Troubleshoot why a device wasn't auto-discovered

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_netscan (configuration details)."""
    return _list("/setting/netscans", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_netscan(netscanId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific netscan by ID.

    Returns: complete netscan details - name, description, scan method, schedule,
    target networks/IPs, credentials, filters (include/exclude rules), device
    properties to apply, collector assignment, duplicate-detection settings and
    last-execution results.

    Configuration details:
    - Targets: IP ranges/subnets or cloud filters
    - Schedule: how often the scan runs
    - Credentials: which properties are used for auth (ssh.user, snmp.community)
    - Filters: include/exclude rules
    - Device properties: auto-applied to discovered devices
    - Duplicate handling: how devices found in multiple scans are treated

    Troubleshooting: "why wasn't a device discovered?" -> check the target range
    and exclude filters; "wrong credentials?" -> verify the credential properties.

    Workflow: use list_netscans to find netscanId, then this tool to review the
    full configuration.

    Related tools: list_netscans (find netscan)."""
    return _get(f"/setting/netscans/{netscanId}", fields=fields)


@mcp.tool
def list_integrations(
    filter: str | None = None, size: int | None = None, offset: int | None = None,
    fields: str | None = None,
) -> Any:
    """List all third-party integrations configured in LogicMonitor.

    Returns: array of integrations with id, name, type
    (Slack/PagerDuty/ServiceNow/Jira/etc), status (active/inactive),
    configuration summary and authentication status.

    What they are: connections to external platforms for alert notifications,
    ticket creation, chat messages and incident management - extending LM
    alerting beyond email/SMS. Categories include incident management (PagerDuty,
    Opsgenie), ticketing (ServiceNow, Jira, Zendesk), collaboration (Slack, Teams)
    and automation (webhooks, API).

    When to use:
    - Find integration IDs used by escalation chains
    - Verify integrations are active and authenticated
    - Audit external connections

    Pagination: a negative `total` means incomplete results - page with
    size/offset.

    Related tools: get_integration (configuration details), list_escalation_chains
    (where integrations are used)."""
    return _list("/setting/integrations", filter=filter, size=size, offset=offset, fields=fields)


@mcp.tool
def get_integration(integrationId: int, fields: str | None = None) -> Any:
    """Get detailed information about a specific integration by ID.

    Returns: complete integration details - name, type, configuration (API keys,
    webhooks, URLs), authentication status, last successful notification, error
    logs and which escalation chains use it.

    Configuration varies by type: Slack (webhook URL, channels), PagerDuty
    (integration key, service mappings), ServiceNow (instance URL, credentials,
    table mapping), Jira (project keys, issue type, field mapping), Webhook
    (target URL, auth headers, payload format).

    Troubleshooting: authentication failed -> check API keys/credentials; not
    receiving notifications -> verify escalation-chain configuration; review error
    logs for failed attempts.

    Workflow: use list_integrations to find integrationId, then this tool for
    detailed configuration and troubleshooting.

    Related tools: list_integrations (find integrations)."""
    return _get(f"/setting/integrations/{integrationId}", fields=fields)


@mcp.tool
def get_topology(fields: str | None = None) -> Any:
    """Get network topology data.

    Returns: network topology data - device relationships, network connections,
    parent-child hierarchies and Layer 2 / Layer 3 connectivity maps.

    What it is: an automatically discovered map of how devices connect to each
    other. LogicMonitor builds it using SNMP, CDP (Cisco Discovery Protocol),
    LLDP (Link Layer Discovery Protocol) and other methods.

    Includes:
    - Physical connections: switch ports, router interfaces
    - Logical relationships: gateway -> firewall -> switches -> servers
    - Layer 2: MAC tables, VLANs, switch-port connections
    - Layer 3: IP routing, subnets, default gateways

    Use cases: network visualization, impact analysis ("if this switch fails,
    what loses connectivity?"), capacity planning and troubleshooting.

    Related tools: list_resources (view devices), get_resource (device details
    including connections)."""
    return _get("/topology", fields=fields)


# ==========================================================================
# UI link generators (read-only: fetch entity + build a portal URL)
# ==========================================================================

@mcp.tool
def generate_dashboard_link(dashboardId: int) -> dict[str, Any]:
    """Generate a direct, shareable portal URL for a LogicMonitor dashboard.

    Returns: a dict with the complete dashboard `url` (including the full group
    hierarchy path so it opens in the correct navigation context), the
    `dashboard` details (id, name, groupName) and the `groupPath` array. The URL
    follows the pattern <portal>/santaba/uiv4/dashboards/dashboardGroups-{path},
    dashboards-{id} and is resolved against the configured portal host
    (LM_BASE_URL / LM_COMPANY+LM_DOMAIN), so it works for gov/custom hosts too.

    When to use: share dashboard links in Slack/email/tickets, embed them in
    runbooks, or build reports with clickable links.

    Workflow: use list_dashboards to find the dashboard ID, then this tool to
    generate the link.

    Related tools: list_dashboards (find dashboard), get_dashboard (details)."""
    dashboard = _get(f"/dashboard/dashboards/{dashboardId}", fields="id,name,groupId,groupName")
    path = _group_path("/dashboard/groups", dashboard.get("groupId"))
    segs = ",".join(f"dashboardGroups-{g['id']}" for g in path)
    tail = f"dashboards-{dashboardId}"
    joined = f"{segs},{tail}" if segs else tail
    return {"url": f"{_ui_base()}/santaba/uiv4/dashboards/{joined}", "dashboard": dashboard, "groupPath": path}


@mcp.tool
def generate_resource_link(deviceId: int) -> dict[str, Any]:
    """Generate a direct, shareable portal URL for a LogicMonitor resource/device.

    Returns: a dict with the complete resource `url` (including the full group
    hierarchy so it opens in the correct folder context), the `device` details
    (id, name, displayName) and the `groupPath` array. The URL follows the
    pattern <portal>/santaba/uiv4/resources/treeNodes?resourcePath=
    resourceGroups-{path},resources-{id} and is resolved against the configured
    portal host, so it works for gov/custom hosts too.

    When to use: share device links in incident tickets, alert notifications or
    documentation.

    Workflow: find the device via list_resources, then this tool with deviceId to
    generate the link.

    Related tools: list_resources (find device), get_resource (details),
    generate_alert_link (link to a device's alerts)."""
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
    """Generate a direct, shareable portal URL for a LogicMonitor alert.

    Returns: a dict with the alert `url` (pattern
    <portal>/santaba/uiv4/alerts/{alertId}, resolved against the configured
    portal host so it works for gov/custom hosts) and the `alert` summary.

    When to use: include alert links in Slack/PagerDuty notifications, share alert
    context with teammates, or reference an alert in an incident ticket.

    Workflow: get the alertId from list_alerts, then this tool to generate the
    link.

    Related tools: list_alerts (find alerts), get_alert (details)."""
    alert = _get(f"/alert/alerts/{alertId}", fields="id,internalId,type,severity,monitorObjectName")
    return {"url": f"{_ui_base()}/santaba/uiv4/alerts/{alertId}", "alert": alert}


@mcp.tool
def generate_website_link(websiteId: int) -> dict[str, Any]:
    """Generate a direct, shareable portal URL for a LogicMonitor website monitor.

    Returns: a dict with the complete website `url` (including the full folder
    hierarchy path so it opens in the correct context), the `website` details
    (id, name, groupId) and the `groupPath` array. The URL follows the pattern
    <portal>/santaba/uiv4/websites/treeNodes#websiteGroups-{path},
    websites-{id} and is resolved against the configured portal host, so it works
    for gov/custom hosts too.

    When to use: share a monitor with the team (Slack/email/tickets), reference it
    in incident docs/runbooks, or build reports with clickable links.

    Workflow: find the monitor via list_websites, then this tool with websiteId to
    generate the link.

    Related tools: list_websites (find website), get_website (details),
    generate_dashboard_link / generate_resource_link / generate_alert_link
    (links for other entity types)."""
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
    try:
        _portal = _portal_base()
    except ToolError:
        _portal = "<unconfigured: set LM_COMPANY or LM_BASE_URL>"
    log.info(
        "LogicMonitor MCP starting: portal=%s level=%s",
        _portal, logging.getLevelName(log.level),
    )
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
        json_response=True,
    )
