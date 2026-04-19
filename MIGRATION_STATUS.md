# Fund Flow V2 — Migration Status

Last updated: 2026-04-16

## Status Legend
- **Done** — Backend API fully implemented, tested, and documented in `API_CONTRACT.md`.
- **Partial** — Some functionality exists but is incomplete or has known gaps.
- **Not Started** — No backend implementation yet.
- **Blocked** — Cannot proceed due to upstream dependency.

---

## Feature / Flow Tracking

| Feature / Flow | V1 Status | V2 Backend | V2 Frontend | UAT Status | Notes / Blockers |
|---|---|---|---|---|---|
| auth/login | Done | Done | Done | — | JWT via SimpleJWT. Login, refresh, me endpoints exist. |
| token refresh | Done | Done | Done | — | `/auth/refresh/` POST endpoint. |
| current-user/me | Done | Done | Done | — | `GET /auth/me/` returns user profile. |
| organization management | Done | Done | Done | — | Full CRUD on `/core/organizations/`. |
| scope node management | Done | Done | Done | — | Full CRUD + `/nodes/{id}/tree/`, `/ancestors/`, `/subtree/`. Path/depth auto-managed. |
| role management | Done | Done | Done | — | Full CRUD on `/access/roles/`. |
| permission management | Done | Done | Done | — | Read-only list at `/access/permissions/`. |
| role-permission assignments | Done | Done | Done | — | Grant/revoke on `/access/role-permissions/`. |
| user scope assignment | Done | Done | Done | — | Placement assignments at `/access/scope-assignments/`. |
| user role assignment | Done | Done | Done | — | Authority assignments at `/access/role-assignments/`. Walk-up lookup in services. |
| module activation | Done | Done | Done | — | CRUD at `/modules/activations/`. Walk-up resolve at `/modules/resolve/`. Default OFF. |
| workflow template config | Done | Done | Done | — | Full CRUD at `/workflow/templates/`. Group/step creation dialogs for draft versions. |
| workflow version publish/archive | Done | Done | Done | — | `/versions/{id}/publish/` and `/archive/` actions. Single-published- version DB constraint enforced. |
| workflow draft creation | Done | Done | Done | — | Generic `/instances/` POST. Invoice-specific via `/instances/from-invoice/` (draft only). |
| workflow draft assignment plan | Done | Done | Done | — | `GET /instances/{id}/assignment-plan/` returns groups, steps, eligible users. |
| workflow draft step assignment | Done | Done | Done | — | `POST /instance-steps/{id}/assign/` assigns eligible user to draft step. |
| workflow draft assignment UX | Not Started | Not Started | Done | — | `/workflow-drafts/:id/assign` page with per-step assignee picker. |
| workflow activation | Done | Done | Done | — | `/instances/{id}/activate/` blocks on unassigned steps. |
| workflow approve/reject/reassign | Done | Done | Done | — | `/instance-steps/{id}/approve/`, `/reject/`, `/reassign/`. Reassign requires `REASSIGN` permission. |
| task inbox | Done | Done | Done | — | `GET /workflow/tasks/me/` returns user's actionable steps across all active instances. |
| invoice create/list/detail | Done | Done | Done | — | Full CRUD. List/detail scoped by permission (creator OR read-permission at scope/ancestor). |
| invoice -> workflow from-invoice flow | Done | Done | Done | — | `POST /instances/from-invoice/` creates DRAFT (optionally auto-activates). Template resolved via walk-up. |
| notification events/in-app delivery | Done | Done | Not Started | — | `WorkflowEvent` + `NotificationDelivery` rows created on step assignment, approve, reject, reassign. `NotificationDelivery.status=PENDING`. No delivery mechanism implemented yet (push, email, websocket). |
| campaigns workflow integration | Done | Partial | Not Started | — | Schema exists. No API/service layer. URL router is empty (`urlpatterns = []`). |
| vendors workflow integration | Done | Partial | Not Started | — | Schema exists. No API/service layer. URL router is empty (`urlpatterns = []`). |
| budgets workflow integration | Done | Partial | Not Started | — | Schema exists. No API/service layer. URL router is empty (`urlpatterns = []`). |

---

## High-Level Summary

| Category | Status |
|---|---|
| Auth | Done |
| Core (org + scope nodes) | Done |
| Access control (roles, permissions, assignments) | Done |
| Module activation resolver | Done |
| Workflow config (templates, versions, groups, steps) | Done |
| Workflow runtime (instances, activation, step actions) | Done |
| Workflow draft assignment plan + per-step assignment UX | Done |
| Invoice CRUD + permission scoping | Done |
| Invoice → Workflow integration | Done |
| Task inbox | Done |
| Workflow events + notifications (DB rows) | Done |
| Campaigns module | Partial (schema done, API not started) |
| Vendors module | Partial (schema done, API not started) |
| Budgets module | Partial (schema done, API not started) |
| Frontend (any feature) | Partial (Phase B.1–B.8: auth, scope nodes, access control, workflow config + builder, invoices, draft assignment UX, runtime actions, module activation) |

---

## Key Gaps to Resolve Before UAT

1. **Notification delivery mechanism** — `NotificationDelivery` rows are created with `status=PENDING` but no worker/websocket/polling mechanism delivers them to users. Frontend cannot show real-time notifications without this.

2. **Campaigns / Vendors / Budgets** — Empty routers. V1 equivalents are unknown; need requirements before V2 backend can be built.

3. **User search endpoint** — `GET /api/v1/users/` now implemented (was missing). User picker now available in Access Control and Task Reassign.

4. **V1 data migration** — No migration scripts or plan for migrating V1 data into V2 schema. V1 status column above assumes V1 was functional, not that V1→V2 migration path exists.
