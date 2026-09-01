// tsc emits only JavaScript; the UI's static assets are copied alongside it so
// `dist/ui` is a complete, servable directory.
import { cp, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const from = path.join(root, "src", "ts", "ui");
const to = path.join(root, "dist", "ui");
await mkdir(to, { recursive: true });
await cp(from, to, {
  recursive: true,
  filter: (src) => !src.endsWith(".ts"),
});
process.stdout.write(`copied static UI assets to ${to}\n`);
