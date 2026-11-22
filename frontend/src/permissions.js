export const ROLE_ADMIN = "ADMIN";
export const ROLE_MANAGER = "MANAGER";
export const ROLE_EMPLOYEE = "EMPLOYEE";

export const TASK_PERMISSIONS = Object.freeze({
  create: "tasks.create",
  read: "tasks.read",
  readAll: "tasks.read_all",
  update: "tasks.update",
  updateAll: "tasks.update_all",
  delete: "tasks.delete",
});

export function can(employee, permission) {
  return Boolean(employee?.permissions?.includes(permission));
}

export function canCreateTasks(employee) {
  return can(employee, TASK_PERMISSIONS.create);
}

export function canReadAllTasks(employee) {
  return can(employee, TASK_PERMISSIONS.readAll);
}

export function canUpdateProgress(employee, task) {
  if (!can(employee, TASK_PERMISSIONS.update)) return false;
  if (can(employee, TASK_PERMISSIONS.updateAll)) return true;

  return Number(task?.assigned_to_employee_id) === Number(employee?.id);
}

export function canEditAnyTask(employee) {
  return can(employee, TASK_PERMISSIONS.updateAll);
}

export function canDeleteTasks(employee) {
  return can(employee, TASK_PERMISSIONS.delete);
}

export function canManageEmployees(employee) {
  return can(employee, "employees.manage");
}

export function canManageAdmins(employee) {
  return can(employee, "roles.manage");
}

export function canManageEmployee(employee, target) {
  if (!canManageEmployees(employee)) return false;
  if (target?.role === ROLE_ADMIN && !canManageAdmins(employee)) return false;

  return true;
}

export function assignableRoles(employee, roles = []) {
  if (canManageAdmins(employee)) return roles;

  return roles.filter((role) => role !== ROLE_ADMIN);
}
