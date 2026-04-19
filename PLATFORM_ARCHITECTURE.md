# Fund Flow V2 — Platform Architecture

> Production-grade design reference. All locked decisions are in Part 20.
> This document is the source of truth for model implementation.

---

## Part 1 — Foundation

### What We Are Building

A multi-tenant SaaS platform sold to **Organizations**. Each Organization contains a hierarchy of nodes (Companies, Entities, Regions, etc.). Modules (Invoice, Campaign, Vendor, etc.) run inside this hierarchy with configurable approval workflows.

**Core goals:**
- Multi-company, multi-entity enterprise customers
- Multiple modules with configurable approval flows
- Reusable workflow platform, not module-specific logic
- Explicit control over who can act where
- Stable runtime even when configs change mid-flight

---

## Part 2 — Org Hierarchy: Generic ScopeNode

### Why Not Hardcoded Levels

Original design had `org | company | entity` hardcoded. Adding `region`, `branch`, `department`, `cost_center` later would require schema migration.

### Correct Model

```
Organization  (tenant root — fixed, never generic)
    └── ScopeNode (everything below org)
            └── ScopeNode (nested, any depth)
```

**ScopeNode fields:**

| Field | Description |
|-------|-------------|
| `id` | Primary key |
| `name` | Display name |
| `code` | Stable slug, unique among siblings |
| `node_type` | `company \| entity \| region \| branch \| department \| ...` |
| `parent_id` | null if direct child of org |
| `org_id` | always points to root org |
| `path` | materialized path e.g. `/org_1/company_a/entity_x` |
| `depth` | integer, 0 = direct child of org |

### Why Ancestry Fields Matter

The entire system depends on:
- Walk up the tree
- Find nearest ancestor of type X
- Check "at or above instance scope"
- Reassignment authority resolution

Without `path` and `depth`, every lookup becomes a recursive query — expensive and fragile at scale.

### Materialized Path Strategy

```
Org_1
    Company_A  → path = /org_1/company_a           depth = 1
        Entity_X   → path = /org_1/company_a/entity_x  depth = 2
        Entity_Y   → path = /org_1/company_a/entity_y  depth = 2
    Company_B  → path = /org_1/company_b           depth = 1
        Entity_Z   → path = /org_1/company_b/entity_z  depth = 2
```

**Ancestor lookup queries:**
```sql
-- All descendants of Company_A
SELECT * FROM scope_node WHERE path LIKE '/org_1/company_a%';

-- All ancestors of Entity_X
SELECT * FROM scope_node WHERE '/org_1/company_a/entity_x' LIKE path || '%';
```

---

## Part 3 — Users

### 3.1 Split Tables — Why

Old merged table `UserScope(user_id, scope_type, scope_id, role_id)` breaks when:
- User has multiple roles at same node
- User belongs to a node but has no workflow role there
- User has delegated authority at a node they don't belong to

### 3.2 Correct Model

**UserScopeAssignment** — *where* a user belongs
```
user_id
scope_node_id
assignment_type   → primary | additional | delegated

UNIQUE: user_id + scope_node_id + assignment_type
```

**UserRoleAssignment** — *what* a user can do
```
user_id
role_id
scope_node_id

UNIQUE: user_id + role_id + scope_node_id
```

### 3.3 All Cases

| Case | Assignments |
|------|-------------|
| Simple user, one role | 1× ScopeAssignment + 1× RoleAssignment |
| Multiple roles at same node | 1× ScopeAssignment + N× RoleAssignments |
| Delegated authority at another node | 2× ScopeAssignments + 2× RoleAssignments |
| Multi-scope user (3 companies) | 3× ScopeAssignments + 3× RoleAssignments — dashboard shows pending tasks across all three |

### 3.4 Authority vs Visibility

| Concept | Meaning |
|---------|---------|
| **Authority** | What you can **DO** — controlled by `UserRoleAssignment` |
| **Visibility** | What context you can **SEE** — breadcrumbs for navigation |

Entity user can see ancestor breadcrumbs but **cannot act** on parent records and **cannot see** sibling records.

### 3.5 Explicit Only — No Downward Inheritance

Every assignment is an explicit row. Bulk assignment = UI convenience only. Internally creates explicit rows per node.

---

## Part 4 — Roles and Permissions

```
Role
    id, name, code    ← code unique per org
    node_type_scope   → which node types this role is valid for

Permission
    id
    action     → create | read | approve | reject | reassign
                 start_workflow | manage_module | ...
    resource   → invoice | campaign | vendor | workflow | ...

RolePermission
    role_id, permission_id
    UNIQUE: role_id + permission_id
```

---

## Part 5 — Module Activation

### Table

```
ModuleActivation
    id
    module          → invoice | campaign | vendor | budget | ...
    scope_node_id
    is_active
    override_parent → false by default

    UNIQUE: module + scope_node_id
```

### Exact Resolution Contract

```
1. Start at subject's node
2. Does this node have a ModuleActivation row for this module?
       YES + override_parent = true  → obey is_active (stop walking)
       YES + override_parent = false → ignore, walk to parent
       NO                            → walk to parent
3. Repeat until org root
4. First explicit decision wins
5. No decision found → default OFF
```

**Cases:**

| Case | Config | Result |
|------|--------|--------|
| Org ON, no overrides | Org: is_active=true | Entity_X → ON |
| Org ON, entity explicitly OFF | Entity_X: is_active=false, override_parent=true | Entity_X → OFF |
| Org nothing, company ON | Company_A: is_active=true, override_parent=true | Entity_X → ON |
| Nothing anywhere | — | OFF (default) |

---

## Part 6 — WorkflowTemplate + WorkflowTemplateVersion

### Why Separate Tables

Instances must point to a **version row**, not just a version number. Publishing and archiving are cleaner with a dedicated version table.

### Tables

```
WorkflowTemplate
    id
    name
    module
    scope_node_id
    created_by
    created_at

    UNIQUE: module + scope_node_id  ← one template identity per module per node

WorkflowTemplateVersion
    id
    template_id
    version_number
    status          → draft | published | archived
    published_at
    published_by
    created_at

    UNIQUE: template_id + version_number
    CONSTRAINT: only one published version per template at a time
```

### Version Lifecycle

```
New template created → Version 1 (status=draft)
Admin finalizes      → Version 1 (status=published)
Admin edits later    → Version 2 created (status=draft)
Admin publishes V2   → Version 1 auto-archived, Version 2 published
Running instances    → stay locked to Version 1 id
New instances        → pick latest published version
```

### Template Resolution Contract

```
1. Module must be active at resolved node (via ModuleActivation resolver)
2. Start at instance subject's node
3. Walk up tree looking for a WorkflowTemplate matching this module
4. At each node:
       Has a template? → find its latest published version → use it
       No template?    → continue walking up
5. Reach org with no match → no workflow configured
6. Draft and archived versions are invisible to this resolver
```

---

## Part 7 — StepGroups and Steps

Groups and Steps belong to `WorkflowTemplateVersion`, not the base template. Otherwise versioning is only partial.

```
StepGroup
    id                          ← stable, never changes
    template_version_id         ← belongs to version, not base template
    display_order
    name
    parallel_mode               → SINGLE | ALL_MUST_APPROVE | ANY_ONE_APPROVES
    on_rejection_action         → TERMINATE | GO_TO_GROUP
    on_rejection_goto_group_id  ← references StepGroup.id (stable)

WorkflowStep
    id
    group_id
    name
    required_role_id
    scope_resolution_policy     → SUBJECT_NODE | ANCESTOR_OF_TYPE | ORG_ROOT | FIXED_NODE
    ancestor_node_type          ← used only if policy = ANCESTOR_OF_TYPE
    fixed_scope_node_id         ← used only if policy = FIXED_NODE
    default_user_id             ← optional convenience, validated at runtime
```

### Scope Resolution Policies

| Policy | Behaviour |
|--------|-----------|
| `SUBJECT_NODE` | Use exact node the subject belongs to |
| `ANCESTOR_OF_TYPE(type)` | Walk up, find nearest ancestor of that `node_type` |
| `ORG_ROOT` | Always use org root regardless of subject |
| `FIXED_NODE` | Use `fixed_scope_node_id` stored on step |

### Parallel Modes

| Mode | Behaviour |
|------|-----------|
| `SINGLE` | One person — must approve to advance |
| `ALL_MUST_APPROVE` | All must approve; any rejection = group rejects |
| `ANY_ONE_APPROVES` | First approval advances; others get SKIPPED |

> V1 UI: SINGLE only. Model supports all three. No schema change needed when parallel is exposed.

---

## Part 8 — Module-to-Subject-Node Mapping

Every workflowable subject must carry a `scope_node_id` — the anchor for all workflow context derivation.

| Module | Subject node derivation |
|--------|------------------------|
| Invoice | `invoice.scope_node_id` |
| Campaign | `campaign.scope_node_id` |
| Vendor | `vendor.scope_node_id` |
| Budget | `budget.scope_node_id` |

---

## Part 9 — Example: 5-Step Invoice Workflow

```
Vendor submits invoice → TRIGGER (starts workflow instance, not a step)

Group 1 → Marketing Executive
    required_role         = marketing_executive
    scope_resolution      = ANCESTOR_OF_TYPE(company)
    → finds Company_A from Entity_X's path

Group 2 → Regional Manager
    required_role         = regional_manager
    scope_resolution      = SUBJECT_NODE
    → uses Entity_X directly

Group 3 → HO Head
    required_role         = ho_head
    scope_resolution      = ANCESTOR_OF_TYPE(company)
    → finds Company_A

Group 4 → Finance Team
    required_role         = finance_team
    scope_resolution      = ORG_ROOT
    → always Org_1 regardless of company/entity

WorkflowInstance APPROVED
    → Invoice.status = approved
    → Payment process begins (outside workflow engine)
    → Invoice.status = paid  ← business state, not a workflow step
```

**Same template, different company:**
```
Invoice from Company_B / Entity_Y:
    Group 1 → ANCESTOR_OF_TYPE(company) → Company_B → different pool
    Group 2 → SUBJECT_NODE              → Entity_Y  → different pool
    Group 3 → ANCESTOR_OF_TYPE(company) → Company_B → different pool
    Group 4 → ORG_ROOT                  → Org_1     → SAME pool always
```

---

## Part 10 — Picker Logic: Exact Runtime Behaviour

```
Step 1 — Derive context from subject
    invoice.scope_node_id = Entity_X
    Entity_X.path = /org_1/company_a/entity_x
    context = { subject_node: Entity_X, ancestors: [Company_A, Org_1] }

Step 2 — Read step requirements
    required_role = marketing_executive
    policy = ANCESTOR_OF_TYPE(company)

Step 3 — Resolve target node
    Walk up Entity_X's ancestors → first node where node_type = company → Company_A

Step 4 — Query eligible users
    UserRoleAssignment WHERE role = marketing_executive AND scope_node = Company_A

Step 5 — Validate default user
    Is WorkflowStep.default_user in eligible pool?
        YES → prefill
        NO  → flag step, manual assignment required

Step 6 — Admin confirms or overrides
    Can only assign from eligible pool
```

---

## Part 11 — Default User Rule

- **Source of truth** = role + scope_resolution_policy (always)
- **Default user** = optional convenience (never authority)
- Validated at instance creation — if ineligible, step is flagged for manual assignment
- Template must survive user churn; role + policy never goes stale, a specific user can

---

## Part 12 — WorkflowInstance

### Activation Rule

```
DRAFT  → instance created, steps being assigned, no notifications sent
ACTIVE → all steps have valid assigned users, admin confirmed
```

Instance **cannot** transition DRAFT → ACTIVE with any unassigned steps.

### Tables

```
WorkflowInstance
    id
    template_id
    template_version_id         ← locked at creation, never changes
    subject_type                ← e.g. "invoice", "campaign"
    subject_id                  ← PK of subject record
    subject_scope_node_id       ← denormalized for fast lookups
    status                      → DRAFT | ACTIVE | APPROVED | REJECTED | FROZEN | STUCK
    current_group_id
    started_by, started_at, completed_at

WorkflowInstanceGroup
    id, instance_id
    step_group_id
    display_order
    status  → WAITING | IN_PROGRESS | APPROVED | REJECTED | RESET

WorkflowInstanceStep
    id, instance_group_id, workflow_step_id
    assigned_user_id
    status  → WAITING | IN_PROGRESS | APPROVED | REJECTED | SKIPPED | ORPHANED | REASSIGNED
    acted_at, note
    reassigned_from_user_id, reassigned_at, reassigned_by
```

### Execution State Machine

```
ACTIVE — current group IN_PROGRESS
│
├── SINGLE approved
│       → group APPROVED → next group IN_PROGRESS → notify users
│
├── ALL_MUST_APPROVE — one approved
│       → all approved? YES → group APPROVED → advance
│                      NO  → wait
│
├── ANY_ONE_APPROVES — one approved
│       → group APPROVED immediately
│       → remaining steps = SKIPPED → advance
│
└── Any rejection
        → TERMINATE    → instance REJECTED
        → GO_TO_GROUP  → reset all groups between target and current
                         target group = IN_PROGRESS
                         full audit trail preserved

Last group APPROVED → instance APPROVED → subject status updated
```

---

## Part 13 — Rejection Routing

- Rejection targets reference **`group_id`** (stable), never `display_order`
- Full reset within rollback range; groups before target stay APPROVED

```
Example — 4 groups: Group_A → Group_B → Group_C → Group_D

Group_B rejects → goto Group_A
    Group_A, Group_B = RESET → Group_A = IN_PROGRESS

Group_D rejects → goto Group_A
    Group_A, Group_B, Group_C, Group_D = RESET → Group_A = IN_PROGRESS

Group_D rejects → goto Group_C
    Group_C, Group_D = RESET
    Group_A, Group_B stay APPROVED
    Group_C = IN_PROGRESS
```

---

## Part 14 — Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| User deactivated, step IN_PROGRESS | Step = ORPHANED → instance = STUCK; admin at or above instance node notified |
| User deactivated, step WAITING | Pre-flagged ORPHANED; STUCK when that group activates |
| User deactivated, step APPROVED | No impact; audit preserved; workflow continues |
| Default user became ineligible | Detected at instance creation; manual assignment required |
| No eligible user for role + resolved scope | Hard block at instance creation |
| Node deactivated | All instances at that node = FROZEN; resume on reactivation |
| Same user in multiple steps | Allowed in V1; logged in audit trail; no block |

---

## Part 15 — Reassignment Authority

User can reassign if they have `reassign` permission on `workflow` at:
- The instance subject's node, **OR**
- Any ancestor node (checked via `path`)

```
Org admin     → can reassign anything
Company admin → can reassign within company and its children
Entity admin  → can reassign within that entity only

Permission check:
    UserRoleAssignment WHERE role has permission(reassign, workflow)
    AND scope_node_id is on the path of instance.subject_scope_node_id
```

---

## Part 16 — Who Can Start a Workflow Instance

| Action | Permission required |
|--------|-------------------|
| Create subject | `create` on subject resource at subject node or ancestor |
| Activate DRAFT → ACTIVE | `start_workflow` or `manage_module` at subject node or ancestor |

Same user can create and start if they hold both permissions.

---

## Part 17 — Notification Events

```
WorkflowEvent
    id
    instance_id
    event_type  → STEP_ASSIGNED | STEP_APPROVED | STEP_REJECTED | STEP_ORPHANED
                   STEP_REASSIGNED | INSTANCE_STUCK | INSTANCE_FROZEN
                   INSTANCE_APPROVED | INSTANCE_REJECTED
    actor_user_id
    target_user_id
    metadata    (json)
    created_at

NotificationDelivery
    event_id
    channel  → in_app | email | slack | ...
    status   → pending | sent | failed
    sent_at
```

> Events = business logic. Channels = delivery detail.
> V1: in-app only. Email/Slack added as channels — no new event logic needed.

---

## Part 18 — Uniqueness Constraints

| Model | Constraint |
|-------|-----------|
| `ScopeNode` | `UNIQUE (org, parent, code)` — no duplicate siblings |
| `UserScopeAssignment` | `UNIQUE (user, scope_node, assignment_type)` |
| `UserRoleAssignment` | `UNIQUE (user, role, scope_node)` |
| `Role` | `UNIQUE (org, code)` |
| `RolePermission` | `UNIQUE (role, permission)` |
| `ModuleActivation` | `UNIQUE (module, scope_node)` |
| `WorkflowTemplate` | `UNIQUE (module, scope_node)` |
| `WorkflowTemplateVersion` | `UNIQUE (template, version_number)` + only one published per template |
| `StepGroup` | `UNIQUE (template_version, display_order)` |
| `WorkflowInstance` | No unique constraint — same subject can have multiple instances |
| `WorkflowInstanceStep` | `UNIQUE (instance_group, workflow_step)` |

---

## Part 19 — What Was Wrong, What Was Fixed

| Original | Problem | Fix |
|----------|---------|-----|
| Hardcoded org/company/entity | Schema migration for new types | Generic `ScopeNode` with `node_type` |
| Merged `UserScope` table | Conflated placement and authority | Split `UserScopeAssignment` + `UserRoleAssignment` |
| `default_user_id` as source of truth | Breaks on user churn | Role + policy is truth, default is convenience |
| Authority = visibility | Entity user confused | Authority separated from visibility |
| Module activation without semantics | Ambiguous child behaviour | `override_parent` flag + exact resolver |
| `on_rejection_goto_group_order` | Brittle on reorder | Stable `group_id` reference |
| `required_node_type` | Too simple | `scope_resolution_policy` enum |
| Paid as workflow step | State not step | Business state on subject record |
| Template + version in one table | Messy publishing/archiving | `WorkflowTemplate` + `WorkflowTemplateVersion` split |
| Groups/steps on base template | Versioning partial | Groups/steps belong to version row |
| No ancestry support | Recursive queries at scale | `path` + `depth` on `ScopeNode` |
| No subject-to-node mapping | Policy cannot be implemented | Every subject carries `scope_node_id` |
| No uniqueness constraints | Config drift | Full constraint list defined |

---

## Part 20 — All Locked Decisions

| Topic | Decision |
|-------|---------|
| Tenant root | `Organization` (fixed) |
| Hierarchy | Generic `ScopeNode` with `node_type` |
| Ancestry | Materialized path + depth on `ScopeNode` |
| User placement | `UserScopeAssignment` |
| User authority | `UserRoleAssignment` (separate table) |
| Scope inheritance | Explicit only |
| Authority vs visibility | Separated |
| Module activation | `override_parent` flag, walk-up resolver |
| Template identity | `WorkflowTemplate` |
| Template versioning | `WorkflowTemplateVersion` (separate table) |
| Groups/steps | Belong to version, not base template |
| Template resolution | Walk-up, most specific published version wins |
| Step scope | `scope_resolution_policy` enum |
| Subject-to-node mapping | Every subject carries `scope_node_id` |
| Default user | Optional convenience, runtime validated |
| Instance version lock | Points to `WorkflowTemplateVersion.id` |
| Instance activation | All steps must be assigned before ACTIVE |
| StepGroup identity | Stable `id`, separate `display_order` |
| Rejection target | References `group_id` |
| Rejection reset | Full reset within rollback range |
| Parallel model | Supported; SINGLE only in V1 UI |
| Vendor | Trigger, not step |
| Paid | Business state on subject |
| User deactivated | ORPHANED → STUCK |
| Node deactivated | FROZEN, resume on reactivation |
| Reassignment | Any admin at or above instance node |
| Who starts instance | `create` + `start_workflow` permission at subject node or ancestor |
| Conflict of interest | Allowed in V1, logged |
| Notifications V1 | In-app only, event model ready for channels |
| Uniqueness constraints | Full list in Part 18 |
