import assert from "node:assert/strict";
import test from "node:test";

import {
  ROLE_ADMIN,
  ROLE_EMPLOYEE,
  ROLE_MANAGER,
  assignableRoles,
  canCreateTasks,
  canDeleteTasks,
  canManageEmployee,
  canUpdateProgress,
} from "./permissions.js";

const employee = (id, role, permissions) => ({
  id,
  role,
  permissions,
});

const employeeUser = employee(7, ROLE_EMPLOYEE, [
  "tasks.create",
  "tasks.read",
  "tasks.update",
  "tasks.delete",
]);
const managerUser = employee(8, ROLE_MANAGER, [
  "tasks.create",
  "tasks.read",
  "tasks.read_all",
  "tasks.update",
  "tasks.update_all",
  "tasks.delete",
  "employees.read",
  "employees.manage",
]);
const adminUser = employee(9, ROLE_ADMIN, [
  "tasks.create",
  "tasks.read",
  "tasks.read_all",
  "tasks.update",
  "tasks.update_all",
  "tasks.delete",
  "employees.read",
  "employees.manage",
  "roles.manage",
]);

test("employees can create and delete their own tasks", () => {
  assert.equal(canCreateTasks(employeeUser), true);
  assert.equal(canDeleteTasks(employeeUser), true);
});

test("employees can edit only their assigned task", () => {
  assert.equal(
    canUpdateProgress(employeeUser, { assigned_to_employee_id: 7 }),
    true
  );
  assert.equal(
    canUpdateProgress(employeeUser, { assigned_to_employee_id: 8 }),
    false
  );
});

test("managers can manage tasks and employees but not administrators", () => {
  assert.equal(canCreateTasks(managerUser), true);
  assert.equal(canDeleteTasks(managerUser), true);
  assert.equal(
    canManageEmployee(managerUser, { id: 7, role: ROLE_EMPLOYEE }),
    true
  );
  assert.equal(
    canManageEmployee(managerUser, { id: 9, role: ROLE_ADMIN }),
    false
  );
  assert.deepEqual(
    assignableRoles(managerUser, [ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE]),
    [ROLE_MANAGER, ROLE_EMPLOYEE]
  );
});

test("administrators can manage administrator accounts and roles", () => {
  assert.equal(
    canManageEmployee(adminUser, { id: 9, role: ROLE_ADMIN }),
    true
  );
  assert.deepEqual(
    assignableRoles(adminUser, [ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE]),
    [ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE]
  );
});
