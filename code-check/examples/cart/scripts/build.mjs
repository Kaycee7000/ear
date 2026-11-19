// Writes the "app" the self-test collects as an artifact.
import { mkdirSync, writeFileSync } from "node:fs";
mkdirSync("dist", { recursive: true });
writeFileSync("dist/cart-app.txt", "cart app build\n");
console.log("built dist/cart-app.txt");
