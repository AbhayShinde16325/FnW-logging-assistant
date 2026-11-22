import assert from "node:assert/strict";
import test from "node:test";

import { welcomeMessage } from "./greeting.js";

test("greets the authenticated employee by name", () => {
  assert.match(welcomeMessage({ name: "QWERTY" }), /^Hi, QWERTY! /);
});

test("uses the backend employee name, never another user supplied value", () => {
  assert.match(welcomeMessage({ name: " Rahul " }), /^Hi, Rahul! /);
});

test("falls back to a neutral greeting when no employee name is available", () => {
  assert.match(welcomeMessage(null), /^Hi! /);
  assert.match(welcomeMessage({ name: "   " }), /^Hi! /);
});
