# LogicMonitor read-only MCP server (Python)

A [FastMCP](https://github.com/jlowin/fastmcp) Python server exposing the
**read-only** (query) tools of the LogicMonitor REST API v3. It is packaged
for one-click deployment via the **Roundhouse** MCP platform ("Deploy from
Git"), but runs as a standalone MCP server anywhere.

This is a Python port of the read-only subset of
**[monitoringartist/logicmonitor-mcp-server](https://github.com/monitoringartist/logicmonitor-mcp-server)**
(© Monitoring Artist). All mutating tools (create/update/delete/acknowledge/
note) from the upstream project are intentionally **excluded**.

## License & attribution

The upstream project is licensed **AGPL-3.0-or-later**. This port is a
derivative work and is therefore also licensed **AGPL-3.0-or-later** (see
[`LICENSE`](./LICENSE)). Note the AGPL network-use clause: if you make this
server available to users over a network, you must offer them the
corresponding source code. For internal-only deployments this is generally
satisfied by keeping this repository accessible to your team.

## Configuration

Set via environment variables (in Roundhouse, add these on the server's env
tab and mark the token **secret**):

| Variable          | Required | Description |
|-------------------|----------|-------------|
| `LM_COMPANY`      | yes      | Account subdomain — the `acme` in `https://acme.logicmonitor.com`. |
| `LM_BEARER_TOKEN` | yes      | LogicMonitor API **Bearer** token. |
| `LM_API_TIMEOUT`  | no       | Per-request timeout in seconds (default `30`). |

The server boots without credentials (so health checks pass); tools return a
clear error until `LM_COMPANY` and `LM_BEARER_TOKEN` are set.

## Deploy into Roundhouse

1. Push this repo to a Git remote Roundhouse can clone.
2. In Roundhouse choose **Deploy from Git**, point it at the repo URL (and a
   `ref` if needed). The repo has `server.py` + `Dockerfile` at its root, so
   it deploys as a code-mode server.
3. On the new server, add env vars `LM_COMPANY` and `LM_BEARER_TOKEN` (secret),
   then redeploy.

The container listens on `:8000` via streamable-HTTP (`stateless_http`,
`json_response`) and serves `/healthz` for the platform status badge —
matching Roundhouse's own generated servers.

## Run locally

```bash
pip install -r requirements.txt
export LM_COMPANY=acme LM_BEARER_TOKEN=...   # your token
python server.py            # serves MCP on http://0.0.0.0:8000
```

## Filtering

`list_*` tools accept LogicMonitor filter syntax via `filter`:

- Equals `name:value`, includes `name~"*value*"`, not-equals `name!:value`
- AND with comma `,`; OR with `||` (do **not** use `&&`)
- Quote wildcard values: `displayName~"*prod*"`
- Example: `filter='hostStatus:alive,displayName~"*web*"'`

`size`/`offset` paginate; `autoPaginate=true` fetches all pages. `fields` is a
comma-separated projection (e.g. `"id,displayName,hostStatus"`).
`list_resources` also accepts a free-text `query` that searches
displayName/name/description.

## Tools (70 read-only)

**Resources/devices:** `list_resources`, `get_resource`, `list_resource_groups`,
`get_resource_group`, `list_resource_properties`, `list_resource_group_properties`,
`list_resource_datasources`, `get_resource_datasource`, `list_resource_instances`,
`get_resource_instance_data`

**Alerts:** `list_alerts`, `get_alert`, `list_alert_rules`, `get_alert_rule`,
`list_escalation_chains`, `get_escalation_chain`, `list_recipients`,
`get_recipient`, `list_recipient_groups`, `get_recipient_group`

**Collectors:** `list_collectors`, `get_collector`, `list_collector_groups`,
`get_collector_group`, `list_collector_versions`

**Sources:** `list_datasources`, `get_datasource`, `list_eventsources`,
`get_eventsource`, `list_configsources`, `get_configsource`

**Dashboards:** `list_dashboards`, `get_dashboard`, `list_dashboard_groups`,
`get_dashboard_group`

**Reports:** `list_reports`, `get_report`, `list_report_groups`, `get_report_group`

**Websites:** `list_websites`, `get_website`, `list_website_groups`,
`get_website_group`, `list_website_checkpoints`

**Services:** `list_services`, `get_service`, `list_service_groups`, `get_service_group`

**Access:** `list_users`, `get_user`, `list_roles`, `get_role`,
`list_access_groups`, `get_access_group`, `list_api_tokens`

**Ops:** `list_sdts`, `get_sdt`, `list_opsnotes`, `get_opsnote`,
`list_audit_logs`, `get_audit_log`, `list_netscans`, `get_netscan`,
`list_integrations`, `get_integration`, `get_topology`

**Portal links:** `generate_dashboard_link`, `generate_resource_link`,
`generate_alert_link`, `generate_website_link`

## Differences from the upstream TypeScript server

- **Read-only only.** The 52 mutating tools are omitted by design.
- **No response field-curation.** Upstream trims list results to a curated
  field set when `fields` is omitted; this port returns the full LM payload.
  Pass `fields` to project specific columns and keep responses small.
- **Filter passthrough.** Filter strings are sent to LM as provided (URL-
  encoded). Quote wildcard values yourself per the syntax above.
- **No MCP-layer auth** is built in (the upstream OAuth/JWT/scope layer is not
  ported). Front it with Roundhouse's token auth or a gateway if needed.
