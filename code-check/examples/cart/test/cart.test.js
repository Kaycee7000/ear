import { test } from "node:test";
import assert from "node:assert/strict";
import { addItem, total } from "../src/cart.js";

test("total counts each item's quantity", () => {
  const items = addItem(addItem([], "mug", 9.0, 2), "tea", 4.5, 3);
  assert.equal(total(items), 31.5);
});

test("negative prices are refused", () => {
  assert.throws(() => addItem([], "bad", -1));
});
