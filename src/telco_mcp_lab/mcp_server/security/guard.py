"""Tenant guard: every ID a tool touches must belong to the caller's tenant.

Two rules:
1. **The account comes from CallerContext, never from arguments.** If the
   model passes an `account_id`, it's only a *selector* among the caller's own
   accounts. A value outside that set is refused.
2. **Every other ID (subscription, service, order, draft) is checked by
   ownership after fetching:** the resource's `account_id` must be one of the
   caller's accounts.

Refusals look **exactly like "not found"**. Otherwise a caller could probe
which IDs exist in other tenants (an existence oracle).

Java/Spring equivalent: a `@PreAuthorize`/`PermissionEvaluator` or a service
method that loads the entity and checks `entity.accountId in principal.accounts`.
"""

from mcp.server.mcpserver.exceptions import ToolError

from telco_mcp_lab.mcp_server.security.caller import CallerContext


class AccessDenied(ToolError):  # noqa: N818 - reads naturally
    """A tenant/scope refusal. Subclasses ToolError so the model sees a normal
    tool error, while audit logging can tell denials apart."""


def not_found(kind: str, example: str) -> str:
    return (
        f"No {kind} with that ID is available to you. {kind.capitalize()} IDs look like "
        f"{example}. Do not guess IDs: use the list tools or ask the user."
    )


NO_CUSTOMER = (
    "No customer is selected for this request, so no account data is available. "
    "The agent application must identify the customer first; do not retry with an account_id."
)


def resolve_account(caller: CallerContext, account_id: str | None) -> str:
    if not caller.account_ids:
        raise AccessDenied(NO_CUSTOMER)
    if account_id is None:
        if len(caller.account_ids) == 1:
            return next(iter(caller.account_ids))
        choices = ", ".join(sorted(caller.account_ids))
        raise ToolError(
            f"This user has several accounts ({choices}). Ask which one they mean, "
            "then pass it as account_id."
        )
    if account_id not in caller.account_ids:
        raise AccessDenied(not_found("account", "ACC-1001"))
    return account_id


def ensure_owned(caller: CallerContext, resource: dict, kind: str, example: str) -> dict:
    if not caller.account_ids:
        raise AccessDenied(NO_CUSTOMER)
    if resource.get("account_id") not in caller.account_ids:
        raise AccessDenied(not_found(kind, example))
    return resource
