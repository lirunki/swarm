# Swarm feature ideas

Swarm already handles individual agent lifecycles. The next improvements should
make large fleets easier to organize, coordinate and monitor without creating
uncontrolled agent loops.

## 1. Durable agent-to-agent messaging

Extend the existing parent-to-child messaging into a persistent inbox/outbox,
rather than merely pasting messages into another terminal.

A message should include:

- Sender, recipient, subject and body.
- Optional file and product references.
- A unique message ID and reply/thread ID.
- State: **queued → delivered → acknowledged**.

“Sent to tmux” must not mean “read by the agent.” Recipients should explicitly
acknowledge messages or reply.

### Behavior and safeguards

- Messages survive controller restarts.
- Stopped or dormant agents can receive messages without automatically waking.
- Senders may explicitly request a wake, subject to permissions.
- Running agents receive messages at a safe point, not as surprise text mixed
  into their current input.
- Peers in the same project can communicate, not only parents and children.
- Use stable IDs and deduplication for retries; do not blindly replay side effects.
- Start within one Swarm, then extend messaging across federated Swarms.

## 2. Projects, tags and ordering

The org chart answers **“who created whom?”** Projects should answer **“what work
belongs together?”**

Add:

- **Project:** one main group, such as “Parser rewrite.”
- **Tags:** multiple labels, such as `review`, `research` and `CI`.
- Pinning and manual ordering.
- Collapse/expand an entire project.
- Views: **Hierarchy / Project / Flat**.
- Sorting by manual order, activity, status or recently updated.

Keep project membership separate from parentage. Moving agents between groups
must not change ownership or who created them.

Group actions could include pausing automatic wakes, stopping agents and
archiving completed agents, with confirmation.

## 3. An Attention queue

Provide a compact, actionable list so users do not need to inspect every tile.

Include:

- Questions awaiting a human answer.
- Failed wakeups or process launches.
- New messages and deliverables.
- Children that finished.
- Agents with no recent checkpoint.

Each item should explain **why it needs attention**, with direct actions to open
the terminal, reply or acknowledge it. This is more useful than adding more
colors to cards.

## 4. Dependencies and structured handoffs

Support workflows such as:

```text
Implement parser → Review parser → Run integration tests
```

An agent could declare:

- Wait for agent X to finish.
- Continue when agent X publishes a product.
- Blocked on a human decision.

A downstream agent should receive a structured handoff containing the checkpoint,
branch, worktree, file references and product links.

A dependency completing must not silently authorize a merge, deployment or other
external side effect.

## 5. Shared project brief

Provide a durable, versioned project document containing:

- Goal and acceptance criteria.
- Constraints and decisions.
- Relevant repositories and links.
- Current plan.

Fresh agents can start with this brief instead of inheriting a huge conversation.
Updating it should notify affected agents rather than silently changing their
instructions.

## 6. Fleet limits and cleanup

As delegation grows, add:

- Per-project concurrency limits.
- Optional time, token and cost budgets where usage data is available.
- Persistent storage for important uploaded inputs.
- Archiving completed cards without deleting sessions or worktrees.
- Explicit cleanup of abandoned worktrees and artifacts.

## Recommended implementation order

1. **Projects/tags and manual ordering:** immediate dashboard benefit.
2. **Durable messaging inbox/outbox:** reliable collaboration foundation.
3. **Attention queue:** manageable fleet monitoring.
4. **Dependencies and handoffs:** build on messaging.
5. **Shared project briefs and resource controls.**

## Design principle

Avoid an unrestricted system where agents chat with everyone and wake each other
freely. Prefer **explicit recipients, durable messages, controlled wakeups and
clear ownership** to enable collaboration without noisy or runaway agent loops.
