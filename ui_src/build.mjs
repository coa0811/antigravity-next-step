import { build } from "esbuild";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const cssPath = path.join(__dirname, "side_panel.css");
const entryPath = path.join(__dirname, "side_panel.ts");
const outDir = path.resolve(__dirname, "..", "ui");
const outPath = path.join(outDir, "side-panel.html");

const cssText = readFileSync(cssPath, "utf8");

const result = await build({
  entryPoints: [entryPath],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2022"],
  minify: true,
  write: false,
  logLevel: "info",
});

const jsBundle = result.outputFiles[0].text
  .replace(/<\/script/gi, "<\\/script")
  .replace(/<!--/g, "<\\!--");

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Next Step HUD</title>
<style>
${cssText}
</style>
</head>
<body>
<div id="app"></div>
<script>
${jsBundle}
</script>
</body>
</html>
`;

mkdirSync(outDir, { recursive: true });
writeFileSync(outPath, html, "utf8");
console.log(`Built ${outPath} (${Buffer.byteLength(html, "utf8")} bytes)`);
