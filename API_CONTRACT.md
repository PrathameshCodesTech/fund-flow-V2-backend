# Fund Flow V2 — API Contract

Base URL: `/api/v1/`
All endpoints require JWT authentication (`Authorization: Bearer <token>`) unless marked `[public]`.

---

## A. Auth

### A1. Login
```
POST /api/v1/auth/login/
[public]
```
**Purpose:** Authenticate with email + password, return user + tokens.

**Request:**
```json
{ "email": "user@example.com", "password": "..." }
```
**Response 200:**
```json
{
  "user": { "id": 1, "email": "user@example.com", "first_name": "...", "last_name": "..." },
  "access": "<jwt_access_token>",
  "refresh": "<jwt_refresh_token>"
}
```
**Errors:** 401 on bad credentials.

---

### A2. Token Refresh
```
POST /api/v1/auth/refresh/
[public]
```
**Purpose:** Exchange a refresh token for a new access token.

**Request:**
```json
{ "refresh": "<jwt_refresh_token>" }
```
**Response 200:**
```json
{ "access": "<new_jwt_access_token>", "refresh": "<new_jwt_refresh_token>" }
```

---

### A3. Current User
```
GET /api/v1/auth/me/
```
**Purpose:** Return the authenticated user's own profile.

**Response 200:**
```json
{ "id": 1, "email": "user@example.com", "first_name": "...", "last_name": "..." }
```

---

## B. Core / Hierarchy

### B1. Organizations
```
GET    /api/v1/core/organizations/         — list
POST   /api/v1/core/organizations/         — create
GET    /api/v1/core/organizations/{id}/    — retrieve
PUT    /api/v1/core/organizations/{id}/    — update
PATCH  /api/v1/core/organizations/{id}/    — partial update
DELETE /api/v1/core/organizations/{id}/    — delete
```
**Purpose:** Tenant root. All other resources belong under an org.

**Response shape (Organization):**
```json
{ "id": 1, "name": "Acme Corp", "code": "acme", "created_at": "..." }
```

---

### B2. Scope Nodes
```
GET    /api/v1/core/nodes/                  — list (filter by ?org=)
POST   /api/v1/core/nodes/                  — create
GET    /api/v1/core/nodes/{id}/             — retrieve
PUT    /api/v1/core/nodes/{id}/             — update
PATCH  /api/v1/core/nodes/{id}/             — partial update
DELETE /api/v1/core/nodes/{id}/             — delete
GET    /api/v1/core/nodes/{id}/tree/        — subtree as nested tree
GET    /api/v1/core/nodes/{id}/ancestors/   — ordered list to root
GET    /api/v1/core/nodes/{id}/subtree/     — flat ordered list of all descendants
```
**Purpose:** Generic hierarchy tree (Company → Entity → ...). Path and depth are auto-computed on create/update.

**Query params:** `?org=<org_id>` on list.

**Response shape (ScopeNode):**
```json
{
  "id": 2, "org": 1, "parent": 1, "name": "Entity A",
  "code": "ea", "node_type": "entity",
  "path": "/acme/hq/ea", "depth": 1,
  "created_at": "..."
}
```

**Important:** No permission enforcement at the API layer for node CRUD. Downstream services (workflow, invoices) enforce permission at the scope-node level.

---

## C. Access Control

### C1. Roles
```
GET    /api/v1/access/roles/                  — list (filter by ?org=)
POST   /api/v1/access/roles/                 — create
GET    /api/v1/access/roles/{id}/            — retrieve
PUT    /api/v1/access/roles/{id}/            — update
PATCH  /api/v1/access/roles/{id}/            — partial update
DELETE /api/v1/access/roles/{id}/            — delete
```
**Query params:** `?org=<org_id>`.

**Response shape:**
```json
{ "id": 1, "org": 1, "name": "Invoice Approver", "code": "inv_approver" }
```

---

### C2. Permissions (read-only)
```
GET /api/v1/access/permissions/         — list all
GET /api/v1/access/permissions/{id}/     — retrieve
```
**Purpose:** Enum-like. Values are `(action, resource)` pairs seeded in DB.

**Response shape:**
```json
{ "id": 1, "action": "read", "resource": "invoice" }
```

---

### C3. Role-Permission Assignments
```
GET    /api/v1/access/role-permissions/                  — list (filter by ?role=)
POST   /api/v1/access/role-permissions/                  — grant permission to role
GET    /api/v1/access/role-permissions/{id}/             — retrieve
DELETE /api/v1/access/role-permissions/{id}/             — revoke
```
**Query params:** `?role=<role_id>`.

**Request (create):**
```json
{ "role": 1, "permission": 5 }
```

---

### C4. User Scope Assignments
```
GET    /api/v1/access/scope-assignments/                    — list (filter by ?user=, ?scope_node=)
POST   /api/v1/access/scope-assignments/                   — assign user to a scope node
GET    /api/v1/access/scope-assignments/{id}/              — retrieve
DELETE /api/v1/access/scope-assignments/{id}/               — unassign
```
**Query params:** `?user=<user_id>`, `?scope_node=<node_id>`.

**Purpose:** Placement — where a user "lives" in the hierarchy. Used for determining eligible step assignees.

**Request (create):**
```json
{ "user": 3, "scope_node": 5 }
```

---

### C5. User Role Assignments
```
GET    /api/v1/access/role-assignments/                    — list (filter by ?user=, ?role=, ?scope_node=)
POST   /api/v1/access/role-assignments/                   — assign role to user at scope node
GET    /api/v1/access/role-assignments/{id}/              — retrieve
DELETE /api/v1/access/role-assignments/{id}/               — unassign
```
**Query params:** `?user=<user_id>`, `?role=<role_id>`, `?scope_node=<node_id>`.

**Purpose:** Authority — what a user is permitted to do at a scope node (and its descendants via walk-up).

**Request (create):**
```json
{ "user": 3, "role": 1, "scope_node": 5 }
```

---

## D. Module Activation

### D1. Module Activations CRUD
```
GET    /api/v1/modules/activations/                    — list (filter by ?scope_node=, ?module=)
POST   /api/v1/modules/activations/                   — create
GET    /api/v1/modules/activations/{id}/              — retrieve
PUT    /api/v1/modules/activations/{id}/              — update
PATCH  /api/v1/modules/activations/{id}/              — partial update
DELETE /api/v1/modules/activations/{id}/              — delete
```
**Query params:** `?scope_node=<node_id>`, `?module=<module_name>`.

**Request (create/update):**
```json
{ "scope_node": 5, "module": "invoice", "is_active": true, "override_parent": true }
```
**Resolution rule:** Nearest ancestor with `override_parent=True` wins; default is `is_active=False` if no rows exist.

---

### D2. Module Resolve
```
GET /api/v1/modules/resolve/
```
**Purpose:** Walk-up resolve the effective `is_active` state for a module at a given scope node.

**Query params:** `?module=invoice&scope_node=5`

**Response 200:**
```json
{ "module": "invoice", "scope_node": "5", "is_active": true }
```
**Errors:** 400 if params missing, 404 if scope_node not found.

---

## E. Workflow Config

All workflow config endpoints support `?scope_node=<id>` and `?module=<name>` filtering where applicable.

### E1. Workflow Templates
```
GET    /api/v1/workflow/templates/                 — list (filter by ?scope_node=, ?module=)
POST   /api/v1/workflow/templates/                — create
GET    /api/v1/workflow/templates/{id}/            — retrieve
PUT    /api/v1/workflow/templates/{id}/            — update
PATCH  /api/v1/workflow/templates/{id}/            — partial update
DELETE /api/v1/workflow/templates/{id}/            — delete
```
**Request (create):**
```json
{ "name": "Invoice Approval", "module": "invoice", "scope_node": 5 }
```

---

### E2. Workflow Template Versions
```
GET    /api/v1/workflow/versions/                        — list (filter by ?template=)
POST   /api/v1/workflow/versions/                        — create
GET    /api/v1/workflow/versions/{id}/                   — retrieve
PUT    /api/v1/workflow/versions/{id}/                   — update
PATCH  /api/v1/workflow/versions/{id}/                   — partial update
DELETE /api/v1/workflow/versions/{id}/                   — delete
POST   /api/v1/workflow/versions/{id}/publish/           — publish (DRAFT → PUBLISHED)
POST   /api/v1/workflow/versions/{id}/archive/          — archive (PUBLISHED → ARCHIVED)
```
**Query params:** `?template=<template_id>`.

**Request (create):**
```json
{ "template": 1, "version_number": 1 }
```

**Publish rules:** Only one `PUBLISHED` version per template (DB constraint). Published version can be archived (not re-published).

---

### E3. Step Groups
```
GET    /api/v1/workflow/groups/                   — list (filter by ?template_version=)
POST   /api/v1/workflow/groups/                   — create
GET    /api/v1/workflow/groups/{id}/              — retrieve
PUT    /api/v1/workflow/groups/{id}/              — update
PATCH  /api/v1/workflow/groups/{id}/              — partial update
DELETE /api/v1/workflow/groups/{id}/              — delete
```
**Query params:** `?template_version=<version_id>`.

**Request (create):**
```json
{
  "template_version": 1, "name": "Manager Review",
  "display_order": 1, "parallel_mode": "single",
  "on_rejection_action": "terminate"
}
```

`parallel_mode` values: `single`, `all_must_approve`, `any_one_approves`
`on_rejection_action` values: `terminate`, `go_to_group`

---

### E4. Workflow Steps
```
GET    /api/v1/workflow/steps/                   — list (filter by ?group=)
POST   /api/v1/workflow/steps/                   — create
GET    /api/v1/workflow/steps/{id}/              — retrieve
PUT    /api/v1/workflow/steps/{id}/              — update
PATCH  /api/v1/workflow/steps/{id}/              — partial update
DELETE /api/v1/workflow/steps/{id}/              — delete
```
**Query params:** `?group=<group_id>`.

**Request (create):**
```json
{
  "group": 1, "name": "Manager Approval",
  "required_role": 3, "display_order": 1,
  "scope_resolution_policy": "subject_node"
}
```

`scope_resolution_policy` values: `subject_node`, `ancestor_of_type`, `org_root`, `fixed_node`

---

## F. Workflow Runtime

### F1. Instance List / Create
```
GET  /api/v1/workflow/instances/        — list (filter by ?subject_type=, ?subject_id=, ?status=)
POST /api/v1/workflow/instances/        — create draft (generic, non-invoice subjects)
```
**Query params:** `?subject_type=invoice`, `?subject_id=42`, `?status=draft`.

**Request (create):**
```json
{
  "template_version": 1, "subject_type": "invoice",
  "subject_id": 42, "subject_scope_node": 5
}
```

---

### F2. Instance Activate
```
POST /api/v1/workflow/instances/{id}/activate/
```
**Purpose:** Activate a DRAFT instance. Fails if any step has no assignee and no eligible user from role assignment.

**Response 200/400.**

---

### F3. Instance Create from Invoice (DRAFT only)
```
POST /api/v1/workflow/instances/from-invoice/
```
**Purpose:** Create a workflow draft attached to an invoice. Resolves template via walk-up automatically.

**Request:**
```json
{
  "invoice_id": 42,
  "assignments": { "step_id_1": "user_id_3" },  // optional overrides
  "activate": false                               // optional, default false
}
```
**Permission:** Invoice creator OR `START_WORKFLOW` permission on invoice's scope node.

**Response 201:** Full instance with groups and steps.
**Errors:** 403 (no permission), 404 (invoice not found), 422 (module inactive / no template).

---

### F4. Instance Detail
```
GET /api/v1/workflow/instances/{id}/
```
**Response:** Full instance with `instance_groups` and nested `instance_steps`.

---

### F5. Step Approve
```
POST /api/v1/workflow/instance-steps/{id}/approve/
```
**Request:**
```json
{ "note": "LGTM" }
```
**Permission:** Assigned user only.
**Behavior:** Single-mode → step APPROVED, group advances. All-must-approve → waits for others. Any-one-approves → group advances immediately.
**Errors:** 403 (wrong user), 400 (invalid state).

---

### F6. Step Reject
```
POST /api/v1/workflow/instance-steps/{id}/reject/
```
**Request:**
```json
{ "note": "Needs revision" }
```
**Permission:** Assigned user only.
**Behavior:** `terminate` → instance REJECTED. `go_to_group` → resets groups within display-order range; all reset steps go back to WAITING.
**Errors:** 403 (wrong user), 400 (invalid state).

---

### F7. Step Reassign
```
POST /api/v1/workflow/instance-steps/{id}/reassign/
```
**Request:**
```json
{ "user_id": 7, "note": "Reassigning to backup" }
```
**Permission:** Requires `REASSIGN` permission on the step's target scope node.
**Validation:** New user must be in the eligible pool (scope assignment at or above step's target node).
**Errors:** 403 (no permission), 404 (user not found), 400 (not eligible or invalid state).

---

### F8. My Tasks
```
GET /api/v1/workflow/tasks/me/
```
**Purpose:** Personal inbox — all actionable (WAITING) steps assigned to the requesting user across all active instances.

**Response:**
```json
[
  {
    "instance_step_id": 10,
    "instance_id": 5,
    "subject_type": "invoice",
    "subject_id": 42,
    "subject_scope_node_id": 5,
    "instance_status": "active",
    "group_name": "Manager Review",
    "group_order": 1,
    "step_name": "Manager Approval",
    "step_order": 1,
    "assigned_user_id": 3,
    "status": "waiting",
    "created_at": "..."
  }
]
```

---

### F9. Instance Groups
```
GET /api/v1/workflow/instance-groups/          — list (filter by ?instance=)
GET /api/v1/workflow/instance-groups/{id}/    — retrieve
```

---

## G. Invoices

### G1. Invoice List
```
GET /api/v1/invoices/
```
**Query params:** `?scope_node=<node_id>`, `?status=<status>`.

**Permission behavior:**
- Returns invoices where user is the creator, OR
- User has `read` permission at the invoice's scope node or any ancestor.
- Users without permission see empty list (not 403).

**Response:** Paginated. Use `response.data['results']` for the list.
```json
{ "count": 1, "next": null, "previous": null, "results": [...] }
```

---

### G2. Invoice Create
```
POST /api/v1/invoices/
```
**Request:**
```json
{ "scope_node": 5, "title": "Invoice #1", "amount": "1000.00", "currency": "INR" }
```
**Permission:** `CREATE` permission on target scope node.
**Errors:** 403 if no permission.

---

### G3. Invoice Detail
```
GET /api/v1/invoices/{id}/
```
**Permission behavior:** Returns 404 (not 403) if user cannot read — prevents enumeration.
- Accessible if user is creator OR has `read` permission at scope node or ancestor.

---

### G4. Invoice Update
```
PUT /api/v1/invoices/{id}/
PATCH /api/v1/invoices/{id}/
```
**Permission:** `UPDATE` permission on invoice's scope node, or user is the creator.
**Errors:** 403 if unauthorized.

---

## H. Campaigns, Vendors, Budgets

**Status: NOT IMPLEMENTED.** URLs are mounted at `/api/v1/campaigns/`, `/api/v1/vendors/`, `/api/v1/budgets/` but all three apps have empty `urlpatterns = []`. No views, serializers, or models are wired up. These are out of scope for the current V2 backend scope.

---

## Notes & Caveats

1. **No row-level field security.** Permission checks are at the object/scope level only.
2. **Invoice status is derived from workflow runtime.** `draft` stays draft; workflow ACTIVE → `in_review`; APPROVED → `approved`; REJECTED → `rejected`. Changing invoice status directly is not an API feature.
3. **Workflow template resolution is gated on module activation.** Before a workflow can be created for an invoice, the `invoice` module must be activated at the invoice's scope node (or an ancestor with `override_parent=True`).
4. **JWT tokens use SimpleJWT.** Access token lifetime is short (configurable, default 15 min). Refresh token is used to obtain new access tokens.
5. **All list endpoints are filtered server-side** to the authenticated user's accessible scope. There is no super-admin bypass in the API layer.
6. **Activation blocks on unassigned steps.** A draft can only be activated when every step in the first group has an assigned user or an eligible user pool from the role assignment.
