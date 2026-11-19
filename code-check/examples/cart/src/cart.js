// A tiny shopping cart used by the code-check self-test. The total has a deliberate bug:
// it adds each item's price once and ignores the quantity.
export function total(items) {
  let sum = 0;
  for (const item of items) {
    sum += item.price;
  }
  return Math.round(sum * 100) / 100;
}

export function addItem(items, name, price, quantity = 1) {
  if (!(price >= 0)) throw new Error("price must be zero or more");
  return [...items, { name, price, quantity }];
}
