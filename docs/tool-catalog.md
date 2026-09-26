# Tool catalog (generated, do not edit)

> Generated from the live tool definitions by `make catalog`. A test fails if
> this file is out of date. This is the contract external agents integrate against.

* Server version: `0.3.0`
* Catalog hash: `sha256:cdaa8d6a0bb2be1b4cd19294630e17101ae52b79a2e52c996248195993c72dee`
* Tools: 5

| Tool | Scope | Read-only | Summary |
|---|---|---|---|
| [`get_account_summary`](#get_account_summary) | `read` | yes | Get a one-call overview of the user's customer account: status and type, the holder's name, when it was opened, how many mobile lines (subscriptions) it has in each status, which plans the active lines are on, and account notes. |
| [`list_subscriptions`](#list_subscriptions) | `read` | yes | List the mobile lines (subscriptions) on the user's account: subscription ID, phone number, plan and status for each. |
| [`get_service_details`](#get_service_details) | `read` | yes | Get the technical and feature details of ONE mobile line: network, data allowance, roaming on/off, voicemail, active add-ons, SIM type and line notes. |
| [`get_order_status`](#get_order_status) | `read` | yes | Get the current status of ONE order by its ID (read-only; it never changes or cancels anything). |
| [`list_orders`](#list_orders) | `read` | yes | List the user's orders, newest first (read-only). |

## get_account_summary

**Get account summary** · scope `read` · annotations `{"openWorldHint": false, "readOnlyHint": true}`

```text
Get a one-call overview of the user's customer account: status and type, the
holder's name, when it was opened, how many mobile lines (subscriptions) it
has in each status, which plans the active lines are on, and account notes.

Use this when the user asks about their account in general, for example
"what's on my account?", "is my account active?", "how many lines do I have?",
"which plans am I on?".

Do NOT use this for the list of individual lines or phone numbers (use
list_subscriptions), SIM/roaming/add-on details of one line (use
get_service_details), or orders (use get_order_status / list_orders).

account_id is optional; omit it unless the user has several accounts.
```

| Parameter | Type | | Constraints | Description |
|---|---|---|---|---|
| `account_id` | string | optional | pattern `^ACC-\d{4}$` | Optional. Which of the user's accounts, e.g. ACC-1001. Omit it when the user has one account. It only selects among the user's OWN accounts. |

Output fields: `account_id`, `account_type`, `status`, `holder_name`, `customer_since`, `subscriptions`, `active_plans`, `notes`

## list_subscriptions

**List mobile lines** · scope `read` · annotations `{"openWorldHint": false, "readOnlyHint": true}`

```text
List the mobile lines (subscriptions) on the user's account: subscription ID,
phone number, plan and status for each. Paginated.

Use this for "what numbers/lines do I have?", "which plan is each line on?",
"show my suspended lines" (status=SUSPENDED), or to find the subscription_id
that get_service_details needs.

Do NOT use this for account-level totals (use get_account_summary) or for the
roaming/SIM/add-on details of one line (use get_service_details).

Pagination: if next_cursor is not null, call again with cursor=next_cursor to
get more. Only fetch more pages if the user's question needs them.
```

| Parameter | Type | | Constraints | Description |
|---|---|---|---|---|
| `account_id` | string | optional | pattern `^ACC-\d{4}$` | Optional. Which of the user's accounts, e.g. ACC-1001. Omit it when the user has one account. It only selects among the user's OWN accounts. |
| `status` | string | optional | one of ACTIVE, SUSPENDED, TERMINATED |  |
| `limit` | integer | optional | 1–20 | Page size, 1-20. Default 10. |
| `cursor` | string | optional | pattern `^[A-Za-z0-9_-]{1,200}$` | Opaque next_cursor from the previous page. Omit for the first page. |

Output fields: `account_id`, `items`, `next_cursor`

## get_service_details

**Get line details** · scope `read` · annotations `{"openWorldHint": false, "readOnlyHint": true}`

```text
Get the technical and feature details of ONE mobile line: network, data
allowance, roaming on/off, voicemail, active add-ons, SIM type and line notes.

Use this for "is roaming on for my number?", "what add-ons does line
SUB-1001-01 have?", "how much data do I get?".

Needs a subscription_id (e.g. SUB-1001-01). If you don't have it, call
list_subscriptions first. Do NOT guess it.
```

| Parameter | Type | | Constraints | Description |
|---|---|---|---|---|
| `subscription_id` | string | **required** | pattern `^SUB-\d{4}-\d{2}$` | Subscription (mobile line) ID, e.g. SUB-1001-01. Get it from list_subscriptions; never guess. |

Output fields: `subscription_id`, `service_id`, `msisdn`, `status`, `network`, `data_allowance_gb`, `roaming_enabled`, `voicemail_enabled`, `addons`, `sim_type`, `notes`

## get_order_status

**Get order status** · scope `read` · annotations `{"openWorldHint": false, "readOnlyHint": true}`

```text
Get the current status of ONE order by its ID (read-only; it never changes or
cancels anything).

Use this for "what's the status of order 123?", "has my plan change gone
through?", when the user gives an order number.

Order IDs look like ORD-000123. If the user says "order 123", use ORD-000123.
If the user doesn't know the ID, use list_orders instead.
```

| Parameter | Type | | Constraints | Description |
|---|---|---|---|---|
| `order_id` | string | **required** | pattern `^ORD-\d{6}$` | Order ID: 'ORD-' + 6 digits, e.g. ORD-000123. If the user says 'order 123', pad to 6 digits: ORD-000123. |

Output fields: `order_id`, `status`, `action`, `target`, `subscription_id`, `created_at`

## list_orders

**List orders** · scope `read` · annotations `{"openWorldHint": false, "readOnlyHint": true}`

```text
List the user's orders, newest first (read-only). Paginated.

Use this for "what orders do I have?", "did I change anything recently?", or
to find an order ID. For one known order, prefer get_order_status.
Pagination: pass next_cursor as cursor to get older orders.
```

| Parameter | Type | | Constraints | Description |
|---|---|---|---|---|
| `account_id` | string | optional | pattern `^ACC-\d{4}$` | Optional. Which of the user's accounts, e.g. ACC-1001. Omit it when the user has one account. It only selects among the user's OWN accounts. |
| `limit` | integer | optional | 1–20 | Page size, 1-20. Default 10. |
| `cursor` | string | optional | pattern `^[A-Za-z0-9_-]{1,200}$` | Opaque next_cursor from the previous page. Omit for the first page. |

Output fields: `account_id`, `items`, `next_cursor`
