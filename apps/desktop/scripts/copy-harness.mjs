// 打包前把 PyInstaller 产物复制进 resources/harness，供 electron-builder 装入 extraResources。
import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(here, "../../../artifacts/harness/quant-agent-harness.exe");
const destDir = path.resolve(here, "../resources/harness");
const dest = path.join(destDir, "quant-agent-harness.exe");

mkdirSync(destDir, { recursive: true });
copyFileSync(src, dest);
console.log(`copied ${src} -> ${dest}`);
