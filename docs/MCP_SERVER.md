# RaceLab as an MCP server

Point any MCP client at this and the agent gains a write it **cannot use to
violate the policy it retrieved itself** — no library import, no framework, no
change to how the agent is built. Works with Claude Code, Cursor, VS Code, and
any LangChain MCP adapter.

```json
{
  "mcpServers": {
    "racelab": {
      "command": "python",
      "args": ["-m", "racelab.integrations.mcp_server", "--allow-writes"]
    }
  }
}
```

Already shipped as `.mcp.json` in this repository, so opening it in Claude Code
is enough.

## Why a server you run, and not an API we host

The obvious product is a hosted endpoint you POST a proposed action to, which
answers allowed or denied. **It cannot provide this guarantee**, and shipping it
would contradict the result the rest of this project establishes.

The soundness argument is that the check runs *inside the transaction that does
the write*. Under SERIALIZABLE, the state verified before `COMMIT` is the state
that becomes durable, or the commit is refused with a `40001`. An advisory
service sits outside any transaction, so between its "allowed" and your `COMMIT`
another agent can move the state — which is exactly the racy check this project
measured as insufficient.

So whoever enforces must own the transaction. This server owns it, running
against **your** cluster with **your** DSN from **your** environment. It never
holds anyone else's credentials, which is a reason to prefer stdio rather than an
accident of it.

## The tools

| Tool | What it does |
|---|---|
| `decide_and_write` | the guarded write: `committed`, `refused`, or `reconsider` |
| `recall` | semantic search over policy memory, via CockroachDB's vector index |
| `remember` | write a fact or policy into agentic memory, effective immediately |
| `audit_decisions` | what agents decided, and against which constraint |

Writes are **off by default**. Without `--allow-writes` this is a read-only
inspection surface, and `decide_and_write` answers `forbidden`.

## `reconsider`: the result MCP has no vocabulary for

MCP has tool success and tool error. It has no way to say

> your last answer was derived from state that has since changed — here is the
> new state, decide again.

That gap is real, and it is the same one LangChain has. `decide_and_write`
returns it as a first-class result — not a failure, but a *teaching* response:

```json
{
  "status": "reconsider",
  "why": "another transaction changed the state your decision rested on; nothing was written",
  "observed_when_you_decided": 0,
  "observed_now": 35,
  "policy_when_you_decided": { "ceiling": 60, "source_memory": "mcp-policy-1" },
  "policy_now": { "ceiling": 60, "source_memory": "mcp-policy-1", "changed_mid_flight": false },
  "your_previous_action": "allocate(35)",
  "still_permitted": ["allocate(45)", "allocate(40)"],
  "guidance": "allocate(35) was derived from a total of 0, which is now 35. Choose again from ['allocate(45)', 'allocate(40)'] or abstain, then call decide_and_write once more."
}
```

The server does **not** retry for you. That is deliberate: retrying here would
replay the same amount, which is precisely the failure this project measures.
The agent's own model re-decides, in its own context, with the refreshed policy
quoted back at it — and the write lands only if the new choice satisfies the
constraint at commit time.

The server's `instructions` field tells the model this on connect, so a
well-behaved client handles it without any prompting from you.

## Which result you get, and when

Worth being precise, because the two failure results mean different things:

- **`refused`** — the common case. A stale *agent*: it recalled, thought, and
  called the tool after the total moved, so its amount no longer fits. The server
  read current state and the policy said no.
- **`reconsider`** — the server's *own* transaction lost a race. This needs
  concurrent writers rather than a slow agent, because the server's
  read-to-write window is microseconds: the agent has already decided by the
  time it calls.

An earlier version of the test raced a single competitor against the tool and
always saw the *competitor* take the `40001` — the correct database outcome and
the wrong experiment. `scripts/test_mcp_server.py` now runs five independent MCP
clients writing at once, which produces both:

```
round 1: 5 concurrent writers -> {'committed': 2, 'reconsider': 3}
```

18/18 checks, driven as a real client over stdio.

## Honest scope

This is a **reference server**, not a hardened product. `decide_and_write`
implements the allocation shape this project measures — a summed ledger against a
ceiling parsed from retrieved policy text. Generalising it means injecting your
own read and apply, exactly as `ConflictAware` already requires: the library is
the general form, and this server is the demonstration that the protocol survives
being delivered as a tool.

`racelab` is therefore both an MCP **client** (`scripts/mcp_query.py`, against
CockroachDB Cloud's Managed MCP Server) and an MCP **server**. The first inspects
the experiment; the second is the experiment's result, made callable.
