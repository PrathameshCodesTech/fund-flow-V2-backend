# Scoped Budget Revisions API

## Purpose

Budget allocation changes are made one budget at a time. A revision is created
first and only changes live `Budget` and `BudgetLine` values when published.

Both manual edits and Excel uploads use the same revision/history model.

## Template

`GET /api/v1/budgets/{budget_id}/revision-template/`

Returns an `.xlsx` workbook for the selected budget only. It contains:

```text
Category Code *
Category Name
Subcategory Code
Subcategory Name
Current Allocation
New Allocation *
```

The scope, financial year, and budget are determined by the selected budget;
they are not supplied in every Excel row.

## Create Manual Revision

`POST /api/v1/budgets/revisions/manual/`

```json
{
  "budget": 17,
  "change_reason": "Reallocate FY27 marketing plan",
  "lines": [
    {"category": 7, "subcategory": 15, "allocated_amount": "450000.00"},
    {"category": 16, "subcategory": null, "allocated_amount": "50000.00"}
  ]
}
```

`lines` is the complete proposed allocation plan for the selected budget.
An existing line omitted from the payload is treated as a logical removal with
zero allocation. The live row remains for audit and ledger integrity.

## Create Excel Revision

`POST /api/v1/budgets/revisions/excel/`

Multipart form fields:

```text
budget=<budget id>
change_reason=<required reason>
file=<.xlsx or .xls workbook>
```

Required workbook columns:

```text
Category Code
New Allocation
```

`Subcategory Code` is optional for category-level allocations.

## Read History

```text
GET /api/v1/budgets/revisions/?budget={budget_id}
GET /api/v1/budgets/revisions/{revision_id}/
```

Each revision includes full before/after snapshots and line-level changes:

```text
added | updated | removed | unchanged
```

## Publish Revision

`POST /api/v1/budgets/revisions/{revision_id}/publish/`

Publishing is atomic. It rejects changes when:

- the revision is no longer validated
- the budget or its lines changed after the revision was created
- a proposed allocation is below a line's reserved plus consumed amount
- a category/subcategory is invalid, inactive, duplicated, or belongs to a different organisation

On success it updates the live budget line allocations, recomputes the budget
header allocated amount, and marks the revision as `published`.

## Cancel Revision

`POST /api/v1/budgets/revisions/{revision_id}/cancel/`

Only `draft` or `validated` revisions can be cancelled. Published history is
immutable.

## Permissions

All revision create, publish, and cancel actions require the existing budget
`update` permission at the budget scope or an ancestor scope.
