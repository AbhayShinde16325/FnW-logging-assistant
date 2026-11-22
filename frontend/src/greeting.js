const WELCOME_INSTRUCTIONS =
  "Tell me about the task in your own words. I'll check which details are missing and ask you for them one at a time. You can type or record a voice note.";

export function employeeGreetingName(employee) {
  return String(employee?.name || "").trim();
}

export function welcomeMessage(employee) {
  const name = employeeGreetingName(employee);

  return `${name ? `Hi, ${name}!` : "Hi!"} ${WELCOME_INSTRUCTIONS}`;
}
