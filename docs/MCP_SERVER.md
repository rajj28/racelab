# RaceLab as an MCP server

> **This is one of two MCP documents, and they point in opposite directions.**
> Here, RaceLab is an MCP **server**: any agent gains a write it cannot use to
> break its own policy. For RaceLab as an MCP **client** — inspecting this
> experiment through CockroachDB Cloud's Managed MCP Server — see
> [`MCP.md`](MCP.md).
>
> The first inspects the experiment. The second *is* the experiment's result,
> made callable.

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
  "policy_when_you_decided": {
    "total": 0, "hard_limit": 10000, "ceiling": 60, "binding_limit": 60,
    "policy_status": "compiled", "policy_version": 1, "policy_limit": 60,
    "policy_source_memory": "mcp-policy-1"
  },
  "policy_now": {
    "total": 35, "hard_limit": 10000, "ceiling": 60, "binding_limit": 60,
    "policy_status": "compiled", "policy_version": 1, "policy_limit": 60,
    "policy_source_memory": "mcp-policy-1", "changed_mid_flight": false
  },
  "your_previous_action": "allocate(35)",
  "still_permitted": ["allocate(45)", "allocate(40)"],
  "guidance": "allocate(35) was derived from a total of 0, which is now 35. Choose again from ['allocate(45)', 'allocate(40)'] or abstain, then call decide_and_write once more."
}
```

`changed_mid_flight` is true when the policy **version** moved between the
agent's read and now — not merely when the number differs — so a recompilation
that produced the same ceiling is not reported as a rule change.

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
round 1: 5 concurrent writers -> {'committed': 1, 'reconsider': 4}
```

29/29 checks, driven as a real client over stdio.

## Which rule is enforced

The **compiled** one. `racelab/policy.py` turns a policy document into a
structured constraint once, off the write path; `racelab/policy_gate.py` decides
which version governs; this server enforces it inside the transaction. **No model
runs during a write.**

If there is no current enforceable constraint, `decide_and_write` refuses and
names the state — `uncompiled`, `stale`, `unenforceable` or `mismatched`. All
four mean *nothing will be authorized here*, and `stale` in particular is a state
the previous dollar-figure regex could not have: writing a new policy document
through `remember` makes the account unwritable until someone runs
`scripts/compile_policies.py`, rather than silently enforcing whatever number
appeared in the newest text.

Every committed write reports the `policy_version` it was made under, and that
version is recorded on the `decisions` row that `audit_decisions` returns.

The Lambda gateway resolves policy through the same module. Two write paths with
two readings of one rule would be worse than either.

## Honest scope

This is a **reference server**, not a hardened product. `decide_and_write` acts
on one declared resource — `bindings/*.yaml`, see `racelab/binding.py` — which is
how it reaches tables this repository contains no code for (`--binding refunds`).
Anything beyond that shape means injecting your own read and apply, exactly as
`ConflictAware` already requires: the library is the general form, and this
server is the demonstration that the protocol survives being delivered as a tool.

`racelab` is therefore both an MCP **client** (`scripts/mcp_query.py`, against
CockroachDB Cloud's Managed MCP Server) and an MCP **server**. The first inspects
the experiment; the second is the experiment's result, made callable.
