"""Employee-profile MCP tools with existing RBAC boundaries."""

from __future__ import annotations

from auth.permissions import EMPLOYEES_READ
from auth.service import employee_by_id, list_employees
from mcp_server.registry.tool_registry import MCPToolSpec
from mcp_server.tools.common import error, object_schema, ok


def _profile(employee):
    if employee is None:
        return None

    return employee.to_payload()


def get_current_employee(arguments, context):
    return ok(employee=_profile(context.employee))


def get_employee_profile(arguments, context):
    target_id = arguments.get("employee_id")

    if target_id is None:
        return ok(employee=_profile(context.employee))

    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return error("employee_id must be an integer.")

    if target_id != context.employee.id and not context.employee.has_permission(
        EMPLOYEES_READ
    ):
        return error("The employee was not found.", status="not_found")

    employee = employee_by_id(target_id)

    if employee is None:
        return error("The employee was not found.", status="not_found")

    return ok(employee=_profile(employee))


def list_employee_profiles(arguments, context):
    if not context.employee.has_permission(EMPLOYEES_READ):
        return error(
            "You do not have permission to list employees.", status="forbidden"
        )

    rows = list_employees()
    return ok(
        count=len(rows),
        employees=[employee.to_payload() for employee in rows],
    )


def get_employee_permissions(arguments, context):
    target_id = arguments.get("employee_id")

    if target_id is None:
        employee = context.employee
    else:
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return error("employee_id must be an integer.")

        if target_id == context.employee.id:
            employee = context.employee
        else:
            if not context.employee.has_permission(EMPLOYEES_READ):
                return error("The employee was not found.", status="not_found")

            employee = employee_by_id(target_id)

            if employee is None:
                return error("The employee was not found.", status="not_found")

    return ok(
        employee_id=employee.id,
        role=employee.role,
        permissions=sorted(employee.permissions),
    )


def build_employee_tools() -> list[MCPToolSpec]:
    return [
        MCPToolSpec(
            name="get_current_employee",
            title="Get current employee",
            description=(
                "Return the authenticated employee's profile. Identity comes "
                "from the MCP bearer token, never from a tool argument."
            ),
            input_schema=object_schema({}),
            permissions=frozenset(),
            handler=get_current_employee,
        ),
        MCPToolSpec(
            name="get_employee_profile",
            title="Get an employee profile",
            description=(
                "Return the current employee, or another employee when the "
                "caller has employees.read."
            ),
            input_schema=object_schema({"employee_id": {"type": "integer"}}),
            permissions=frozenset(),
            handler=get_employee_profile,
        ),
        MCPToolSpec(
            name="list_employees",
            title="List employees",
            description="List employees. Requires employees.read.",
            input_schema=object_schema({}),
            permissions=frozenset({EMPLOYEES_READ}),
            handler=list_employee_profiles,
        ),
        MCPToolSpec(
            name="get_employee_permissions",
            title="Get employee permissions",
            description=(
                "Return one employee's role and permissions. Employees may "
                "read their own; managers and administrators may read others."
            ),
            input_schema=object_schema({"employee_id": {"type": "integer"}}),
            permissions=frozenset(),
            handler=get_employee_permissions,
        ),
    ]
