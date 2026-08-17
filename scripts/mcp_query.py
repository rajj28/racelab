"""Query the experiment through CockroachDB Cloud's Managed MCP Server, for real.

`docs/MCP.md` described how an agent *would* inspect these race runs. This
actually does it: a real MCP session over Streamable HTTP against
`https://cockroachlabs.cloud/mcp` -- initialize, tools/list, tools/call -- so the
claim "the experiment is agent-queryable" is demonstrated rather than asserted.

## Credentials

Reuses the session `ccloud auth login` already established, reading the API key
from the local ccloud credential store. **The key is never printed, logged, or
written anywhere**, including in `--verbose`. This script does not accept a key
as an argument either, so it cannot end up in shell history.

If there is no ccloud session, it says so and stops rather than prompting.

## What it demonstrates

    tools/list        what the server exposes
    select_query      the four RaceLab views, through the server's own tool

The queries are the ones a judge would want to run: how the arms compared, and
which ceiling each arm actually reasoned with -- the ablation, in one statement,
answered by an agent that never touched our code.

Run:  python scripts/mcp_query.py
      python scripts/mcp_query.py --list-tools
      python scripts/mcp_query.py --sql "SELECT * FROM race_arm_comparison"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://cockroachlabs.cloud/mcp"
CRED = pathlib.Path.home() / "AppData" / "Roaming" / ".cockroachdb" / "credentials.json"
PROFILES = pathlib.Path.home() / "AppData" / "Roaming" / ".cockroachdb" / "profiles.json"

DEFAULT_QUERIES = [
    ("How did the five approaches compare?",
     "SELECT * FROM race_arm_comparison"),
    ("Which ceiling did each arm actually reason with? (the ablation)",
     "SELECT arm, inferred_ceiling, count(*) AS decisions "
     "FROM race_agent_decisions WHERE arm IS NOT NULL "
     "GROUP BY arm, inferred_ceiling ORDER BY arm, inferred_ceiling"),
    ("Where did each arm top out?",
     "SELECT arm, max(resulting_total) AS highest_total "
     "FROM race_agent_decisions WHERE arm IS NOT NULL GROUP BY arm ORDER BY arm"),
]


class McpError(RuntimeError):
    pass


def api_key() -> str:
    """Read the key ccloud already holds. Never returned to the caller's output."""
    if not CRED.exists():
        raise McpError(
            f"no ccloud credentials at {CRED}. Run `ccloud auth login` first; "
            f"this script deliberately does not accept a key as an argument."
        )
    try:
        data = json.loads(CRED.read_text(encoding="utf-8"))
        key = data.get("default", {}).get("apiKey")
    except Exception as exc:  # noqa: BLE001
        raise McpError(f"could not read the ccloud credential store: {exc}") from exc
    if not key:
        raise McpError("the ccloud credential store has no apiKey; log in again.")
    return key


def resolve_cluster_id() -> tuple[str | None, str | None]:
    """Find the cluster id for the cluster our DSN actually points at.

    Ties the two CockroachDB tools together rather than hardcoding an id: the
    DSN hostname names the cluster, `ccloud cluster list` maps that name to an
    id, and the MCP server is then scoped to the same cluster the experiment
    ran on. Hardcoding would have been shorter and would silently query the
    wrong cluster the moment the account has two -- which this one now does.
    """
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from racelab.integrations import ccloud
        name = ccloud.cluster_name_from_dsn()
        if not name:
            return None, None
        clusters = ccloud.run("clusters")
        rows = clusters if isinstance(clusters, list) else (clusters or {}).get("clusters", [])
        for c in rows:
            if c.get("name") == name:
                return c.get("id"), name
        return None, name
    except Exception:  # noqa: BLE001 - falls back to an explicit --cluster-id
        return None, None


def database_from_dsn() -> str:
    """The database the experiment writes to, taken from the DSN.

    `select_query` requires it as an argument -- the server does not infer it
    from the cluster -- so it is read from the same DSN the runs used rather
    than hardcoded, for the same reason the cluster id is.
    """
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from racelab.db import dsn_for
        import re
        found = re.search(r":\d+/([^?]+)", dsn_for("crdb"))
        return found.group(1) if found else "defaultdb"
    except Exception:  # noqa: BLE001
        return "defaultdb"


def org_label() -> str | None:
    try:
        return json.loads(PROFILES.read_text(encoding="utf-8")).get(
            "default", {}).get("organizationName")
    except Exception:  # noqa: BLE001
        return None


class McpSession:
    """A minimal MCP client over Streamable HTTP.

    Written out rather than pulled from a library so the protocol is visible:
    it is three calls, and the interesting one is `tools/call`.
    """

    def __init__(self, endpoint: str, key: str, cluster_id: str | None = None,
                 verbose: bool = False):
        self.endpoint = endpoint
        self._key = key
        self.cluster_id = cluster_id
        self.verbose = verbose
        self.session_id: str | None = None
        self._id = 0

    def _headers(self) -> dict:
        h = {
            "content-type": "application/json",
            # The server may answer either way on this transport.
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self._key}",
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        if self.cluster_id:
            h["mcp-cluster-id"] = self.cluster_id
        return h

    def _rpc(self, method: str, params: dict | None = None,
             notify: bool = False) -> dict | None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._id += 1
            payload["id"] = self._id

        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise McpError(f"HTTP {exc.code} on {method}: {body}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"cannot reach {self.endpoint}: {exc.reason}") from exc

        if notify or not raw.strip():
            return None
        if self.verbose:
            print(f"    <- {raw[:200]}")
        return _parse(raw)

    def initialize(self) -> dict:
        out = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "racelab-inspector", "version": "1.0"},
        })
        self._rpc("notifications/initialized", {}, notify=True)
        return (out or {}).get("result", {})

    def list_tools(self) -> list[dict]:
        out = self._rpc("tools/list", {})
        return (out or {}).get("result", {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> dict:
        out = self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = (out or {}).get("result")
        if result is None:
            raise McpError(f"{name} returned no result: {json.dumps(out)[:300]}")
        if result.get("isError"):
            raise McpError(f"{name} reported an error: "
                           f"{json.dumps(result.get('content'))[:300]}")
        return result


def _parse(raw: str) -> dict:
    """Accept a JSON body or an SSE frame, since either is legal here."""
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                return json.loads(chunk)
    raise McpError(f"unparseable response: {text[:200]}")


def render(result: dict) -> str:
    """Flatten MCP content blocks into something readable."""
    parts = []
    for block in result.get("content", []) or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        else:
            parts.append(json.dumps(block)[:400])
    if not parts and result.get("structuredContent"):
        parts.append(json.dumps(result["structuredContent"], indent=1)[:2000])
    return "\n".join(parts) if parts else json.dumps(result)[:600]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--cluster-id", default=None,
                    help="scope to one cluster via the mcp-cluster-id header")
    ap.add_argument("--sql", default=None, help="run one statement instead")
    ap.add_argument("--list-tools", action="store_true")
    ap.add_argument("--database", default=None,
                    help="defaults to the database named in the DSN")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        key = api_key()
    except McpError as exc:
        print(exc, file=sys.stderr)
        return 1

    args.database = args.database or database_from_dsn()
    print(f"MCP  {args.endpoint}")
    org = org_label()
    if org:
        print(f"     organization {org} (session from `ccloud auth login`)")

    cluster_id = args.cluster_id
    if not cluster_id:
        cluster_id, cname = resolve_cluster_id()
        if cluster_id:
            print(f"     cluster {cname} ({cluster_id[:8]}...) resolved from the DSN "
                  f"via ccloud")
        else:
            print("     no cluster id resolved; pass --cluster-id", file=sys.stderr)

    session = McpSession(args.endpoint, key, cluster_id, args.verbose)
    try:
        info = session.initialize()
        server = info.get("serverInfo", {})
        print(f"     connected: {server.get('name')} {server.get('version')} "
              f"(protocol {info.get('protocolVersion')})\n")

        tools = session.list_tools()
        if args.list_tools:
            print(f"{len(tools)} tools exposed:")
            for t in tools:
                print(f"  {t.get('name'):<24} {(t.get('description') or '')[:88]}")
            return 0
        print(f"     {len(tools)} tools available; using select_query\n")

        queries = ([("custom", args.sql)] if args.sql else DEFAULT_QUERIES)
        for label, sql in queries:
            print("=" * 78)
            print(label)
            print(f"  {sql}")
            print("-" * 78)
            # Scoping is done once, by the `mcp-cluster-id` header. Passing it
            # again per call is rejected: "cluster_id is set in your MCP config;
            # omit the cluster_id argument". The server is right to refuse --
            # two sources for the same setting is how they end up disagreeing.
            result = session.call("select_query",
                                  {"query": sql, "database": args.database})
            print(render(result))
            print()
    except McpError as exc:
        print(f"\nMCP call failed: {exc}", file=sys.stderr)
        return 1

    print("=" * 78)
    print("Answered by CockroachDB Cloud's Managed MCP Server, over its own")
    print("select_query tool. No RaceLab code took part in producing these rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
