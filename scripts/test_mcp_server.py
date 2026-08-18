"""Drive the RaceLab MCP server as a real client, and force a real conflict.

Spawns the server over stdio, speaks JSON-RPC to it, and asserts the outcomes
that matter. The `reconsider` case is the reason this server exists, so it is
produced by an actual competing transaction rather than simulated.

    committed     the write landed, under a named policy version
    refused       the action violated the compiled policy, OR the policy cannot
                  be enforced at all and nothing may be written
    reconsider    another transaction moved the state; nothing was written, and
                  the response carries then-versus-now so the agent can decide
                  again

Section 5 is the one added when the compiler was wired in: an agent that writes
a NEW policy document has, by that act, made the account unwritable until
someone compiles it. The old regex could not have that state -- it read whatever
text was newest and produced a number -- so a policy change nobody noticed still
produced confident enforcement of something.

Run:  python scripts/test_mcp_server.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from racelab.db import connect

ACCOUNT = "mcp-demo-001"
HARD_LIMIT = 100
CEILING = 60

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


class StdioClient:
    """A minimal MCP client over a child process's stdin/stdout."""

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=str(REPO))
        self._id = 0
        # Drain stderr so the child never blocks on a full pipe.
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        for _ in iter(self.proc.stderr.readline, ""):
            pass

    def _send(self, method: str, params: dict | None = None,
              notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the connection")
            line = line.strip()
            if not line:
                continue
            out = json.loads(line)
            if out.get("id") == msg["id"]:
                return out

    def initialize(self) -> dict:
        out = self._send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "racelab-test", "version": "1.0"},
        })
        self._send("notifications/initialized", {}, notify=True)
        return (out or {}).get("result", {})

    def tools(self) -> list[dict]:
        return ((self._send("tools/list", {}) or {})
                .get("result", {}).get("tools", []))

    def call(self, name: str, arguments: dict) -> dict:
        out = self._send("tools/call", {"name": name, "arguments": arguments})
        result = (out or {}).get("result", {})
        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"raw": block["text"]}
        if result.get("structuredContent"):
            return result["structuredContent"]
        return {"raw": json.dumps(out)[:300]}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


POLICY_TEXT = (f"The authorization ceiling for this account is ${CEILING}. "
               f"This is a cumulative total across all allocations, with no "
               f"reset period.")


def setup(seed: int) -> None:
    """A fresh ledger, a policy document, and that policy compiled.

    The compile step is deliberate and separate. It is what the server does NOT
    do at write time: interpretation happens once, here, in a model; enforcement
    happens per write, in SQL, with no model in the loop.

    The wording is also deliberate. "$60 per cycle" does not compile -- a cycle
    can start on any day, so it is not a window the constraint language has --
    and the compiler flags it rather than guessing. That is asserted in section
    5 rather than worked around here.
    """
    import datetime

    from racelab.binding import ResourceBinding
    from racelab.embeddings import get_embedder
    from racelab.memory import MemoryStore
    from racelab.policy import columns_of, compile_policy, store

    with connect("crdb") as conn:
        conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
        conn.execute("DELETE FROM memories WHERE account_id = %s", (ACCOUNT,))
        conn.execute("DELETE FROM policy_constraints WHERE account_id = %s", (ACCOUNT,))
        conn.execute(
            "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s,%s,%s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (ACCOUNT, ACCOUNT, HARD_LIMIT))
        MemoryStore(conn, get_embedder("titan")).add(
            memory_id="mcp-policy-1", account_id=ACCOUNT,
            text=POLICY_TEXT, kind="policy",
            created_at=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1))
        if seed:
            conn.execute(
                "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
                "VALUES (%s,%s,'seed',%s,'mcp-setup')",
                (str(uuid.uuid4()), ACCOUNT, seed))

        binding = ResourceBinding.named("allocations")
        with conn.cursor() as cur:
            cols = columns_of(cur, binding.resource)
        constraint = compile_policy(POLICY_TEXT, schema_columns=cols,
                                    source_memory_id="mcp-policy-1",
                                    **binding.constraint_template())
        if not constraint.enforceable:
            raise SystemExit(
                f"the test policy did not compile to an enforceable constraint: "
                f"{constraint.unsupported}")
        store(conn, ACCOUNT, constraint)


def write_policy(memory_id: str, text: str) -> None:
    """Write a NEW governing policy document, and deliberately not compile it."""
    import datetime

    from racelab.embeddings import get_embedder
    from racelab.memory import MemoryStore
    with connect("crdb") as conn:
        MemoryStore(conn, get_embedder("titan")).add(
            memory_id=memory_id, account_id=ACCOUNT, text=text, kind="policy",
            supersedes="mcp-policy-1",
            created_at=datetime.datetime.now(datetime.timezone.utc))


def compile_current(memory_id: str, wording: str) -> None:
    """Compile the governing document, as `scripts/compile_policies.py` does."""
    from racelab.binding import ResourceBinding
    from racelab.policy import columns_of, compile_policy, store
    with connect("crdb") as conn:
        binding = ResourceBinding.named("allocations")
        with conn.cursor() as cur:
            cols = columns_of(cur, binding.resource)
        constraint = compile_policy(wording, schema_columns=cols,
                                    source_memory_id=memory_id,
                                    **binding.constraint_template())
        if not constraint.enforceable:
            raise SystemExit(f"resolution did not compile: {constraint.unsupported}")
        store(conn, ACCOUNT, constraint)


def total() -> int:
    with connect("crdb") as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
            (ACCOUNT,)).fetchone()[0])


def decision_versions() -> set:
    """The policy versions recorded against this account's committed decisions."""
    with connect("crdb") as conn:
        rows = conn.execute(
            "SELECT DISTINCT d.policy_version FROM decisions d "
            "JOIN allocations a ON a.run_id = d.run_id "
            "WHERE a.account_id = %s", (ACCOUNT,)).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    print("RaceLab MCP server, driven as a real client over stdio")
    print("=" * 76)
    print(f"  account {ACCOUNT}  ceiling ${CEILING} (in retrieved text)  "
          f"hard limit ${HARD_LIMIT}")
    print()

    client = StdioClient([sys.executable, "-m",
                          "racelab.integrations.mcp_server", "--allow-writes"])
    try:
        print("1. Protocol")
        info = client.initialize()
        srv = info.get("serverInfo", {})
        check("server initialized", bool(srv.get("name")),
              f"{srv.get('name')} {srv.get('version')}")
        names = [t["name"] for t in client.tools()]
        check("all four tools advertised",
              {"decide_and_write", "recall", "remember", "audit_decisions"}
              <= set(names), str(names))
        check("instructions tell the model what to do with 'reconsider'",
              "reconsider" in (info.get("instructions") or ""))

        print("\n2. Memory is real: recall goes through the vector index")
        got = client.recall = client.call("recall", {
            "account_id": ACCOUNT, "query": "what is the spending ceiling?"})
        setup(0)
        got = client.call("recall", {
            "account_id": ACCOUNT, "query": "what is the spending ceiling?"})
        texts = " ".join(m.get("text", "") for m in got.get("memories", []))
        check("the policy is retrievable", f"${CEILING}" in texts,
              f"{len(got.get('memories', []))} memories")

        print("\n3. A write that fits commits, under a named policy version")
        out = client.call("decide_and_write",
                          {"account_id": ACCOUNT, "amount": 45,
                           "agent_id": "agent-a"})
        print(f"     status={out.get('status')} total_now={out.get('total_now')} "
              f"policy_version={out.get('policy_version')}")
        check("status is committed", out.get("status") == "committed", str(out)[:90])
        check("the ledger reflects it", total() == 45, f"${total()}")
        check("the response names the policy version it committed under",
              isinstance(out.get("policy_version"), int),
              f"v{out.get('policy_version')}")
        check("the decision row records that version",
              decision_versions() == {out.get("policy_version")},
              f"decisions.policy_version = {decision_versions()}")

        print("\n4. A write that breaks the compiled ceiling is refused")
        out = client.call("decide_and_write",
                          {"account_id": ACCOUNT, "amount": 45,
                           "agent_id": "agent-b"})
        print(f"     status={out.get('status')}  still_permitted="
              f"{out.get('still_permitted')}")
        check("status is refused", out.get("status") == "refused", str(out)[:90])
        check("nothing was written", total() == 45, f"${total()} unchanged")
        check("the response names the ceiling", "60" in json.dumps(out))
        check("the refusal cites the compiled policy, not a parsed dollar figure",
              "policy limit" in json.dumps(out) or "policy v" in json.dumps(out),
              str(out.get("why"))[:100])
        check("it tells the agent what would fit",
              "still_permitted" in out and "guidance" in out)

        print("\n5. A policy that has moved and not been recompiled blocks writes")
        print("   (the state the old dollar-figure regex could not have)")
        write_policy("mcp-policy-2",
                     "Authorization ceiling reduced to $40 per billing cycle, "
                     "effective immediately, superseding the prior ceiling.")
        out = client.call("decide_and_write",
                          {"account_id": ACCOUNT, "amount": 35,
                           "agent_id": "agent-c"})
        print(f"     status={out.get('status')} "
              f"policy_status={(out.get('policy_now') or {}).get('policy_status')}")
        check("a superseding, uncompiled policy refuses the write",
              out.get("status") == "refused", str(out)[:100])
        check("it reports the reason as 'stale', not as a ceiling breach",
              (out.get("policy_now") or {}).get("policy_status") == "stale",
              str((out.get("policy_now") or {}).get("policy_status")))
        check("nothing was written", total() == 45, f"${total()} unchanged")
        check("it offers no alternative action, because none would be allowed",
              out.get("still_permitted") == [], str(out.get("still_permitted")))
        check("the guidance says to compile, not to retry",
              "compile" in (out.get("guidance") or "").lower(),
              str(out.get("guidance"))[:90])

        # And the same document, compiled, is enforceable again -- so the refusal
        # above is a missing compilation, not a dead end.
        print("\n   after compiling it, the new ceiling is the one enforced")
        compile_current("mcp-policy-2",
                        "The authorization ceiling for this account is $40. This is "
                        "a cumulative total across all allocations, with no reset "
                        "period.")
        out = client.call("decide_and_write",
                          {"account_id": ACCOUNT, "amount": 35,
                           "agent_id": "agent-d"})
        check("the recompiled, lower ceiling now refuses a write that used to fit",
              out.get("status") == "refused", str(out)[:100])
        check("and it is enforced as a limit, not as a missing policy",
              (out.get("policy_now") or {}).get("policy_status") == "compiled",
              str((out.get("policy_now") or {}).get("policy_status")))
        check("the version advanced with the policy",
              (out.get("policy_now") or {}).get("policy_version") == 2,
              f"v{(out.get('policy_now') or {}).get('policy_version')}")

        print("\n6. Genuine contention returns 'reconsider', not an error")
        # Worth being precise about which case this is. A stale AGENT -- one that
        # recalled, thought, and called the tool after the total moved -- gets
        # `refused`, because the server reads current state and the agent's
        # amount no longer fits. That is section 4, and it is the common case.
        #
        # `reconsider` is the other case: the server's OWN transaction loses a
        # race. That needs concurrent writers rather than a slow agent, because
        # the server's read-to-write window is microseconds -- the agent has
        # already decided by the time it calls. An earlier version of this test
        # raced a single competitor and always saw the COMPETITOR take the 40001,
        # which is the correct outcome and the wrong experiment.
        #
        # So: several independent MCP clients writing at once, with the ceiling
        # raised so policy does not refuse them first and mask the contention.
        with connect("crdb") as conn:
            conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
            conn.execute("DELETE FROM memories WHERE account_id = %s", (ACCOUNT,))
            conn.execute("UPDATE accounts SET hard_limit = 10000 WHERE account_id = %s",
                         (ACCOUNT,))

        # Two rounds at most. With five writers racing it is entirely possible
        # for ALL of them to lose and none to commit -- that is what contention
        # looks like, not a defect -- and a single unlucky round should not fail
        # the suite. Retrying the round is honest; weakening the assertion so
        # that "nothing committed" counts as success would not be.
        outcomes: list[dict] = []
        kinds: dict = {}
        for round_no in range(2):
            with connect("crdb") as conn:
                conn.execute("DELETE FROM allocations WHERE account_id = %s",
                             (ACCOUNT,))
            racers = [StdioClient([sys.executable, "-m",
                                   "racelab.integrations.mcp_server",
                                   "--allow-writes"])
                      for _ in range(5)]
            outcomes = []
            lock = threading.Lock()
            gate = threading.Event()

            def racer(cl, i: int) -> None:
                cl.initialize()
                gate.wait()
                try:
                    out = cl.call("decide_and_write",
                                  {"account_id": ACCOUNT, "amount": 35,
                                   "agent_id": f"racer-{i}"})
                except Exception as exc:  # noqa: BLE001
                    out = {"status": "client-error", "why": str(exc)[:80]}
                with lock:
                    outcomes.append(out)

            try:
                threads = [threading.Thread(target=racer, args=(cl, i))
                           for i, cl in enumerate(racers)]
                for t in threads:
                    t.start()
                time.sleep(2.0)      # let every client finish its handshake
                gate.set()
                for t in threads:
                    t.join(timeout=90)
            finally:
                for cl in racers:
                    cl.close()

            kinds = {}
            for o in outcomes:
                kinds[o.get("status")] = kinds.get(o.get("status"), 0) + 1
            print(f"     round {round_no + 1}: {len(outcomes)} concurrent "
                  f"writers -> {kinds}")
            if kinds.get("reconsider", 0) >= 1 and kinds.get("committed", 0) >= 1:
                break

        seen = next((o for o in outcomes if o.get("status") == "reconsider"), None)
        if seen:
            print(f"     then={seen.get('observed_when_you_decided')} "
                  f"now={seen.get('observed_now')}")
            print(f"     guidance: {str(seen.get('guidance'))[:110]}")

        check("concurrent writers produced status='reconsider'", seen is not None,
              f"outcomes: {kinds}")
        check("at least one write still committed",
              kinds.get("committed", 0) >= 1,
              f"{kinds.get('committed', 0)} committed")
        if seen:
            check("it reports the state at decision time and now",
                  seen.get("observed_when_you_decided") is not None
                  and seen.get("observed_now") is not None,
                  f"{seen.get('observed_when_you_decided')} -> {seen.get('observed_now')}")
            check("it reports the policy at decision time and now",
                  "policy_now" in seen and "policy_when_you_decided" in seen)
            check("it names the action that is now stale",
                  seen.get("your_previous_action") == "allocate(35)")
            check("it tells the agent to choose again, not to retry",
                  "again" in (seen.get("guidance") or "").lower())
            check("nothing was written for the reconsidered call",
                  "nothing was written" in (seen.get("why") or ""))

        print("\n7. Read-only mode refuses writes")
        ro = StdioClient([sys.executable, "-m",
                          "racelab.integrations.mcp_server"])
        try:
            ro.initialize()
            out = ro.call("decide_and_write",
                          {"account_id": ACCOUNT, "amount": 35})
            check("writes are forbidden without --allow-writes",
                  out.get("status") == "forbidden", str(out)[:80])
        finally:
            ro.close()
    finally:
        client.close()

    print("\n" + "=" * 76)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nAny MCP client -- Claude Code, Cursor, VS Code, a LangChain MCP")
    print("adapter -- gets a write it cannot use to violate its own policy, and")
    print("a 'reconsider' result the protocol has no other vocabulary for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
