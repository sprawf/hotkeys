// Bundle Excalidraw + React into a single offline bundle under ./dist
import esbuild from "esbuild";
import { copyFile, mkdir, readdir, stat, cp } from "node:fs/promises";
import { join, dirname } from "node:path";

const root = new URL(".", import.meta.url).pathname.replace(/^\//, "");
const SRC = join(root, "src");
const DIST = join(root, "dist");

await mkdir(DIST, { recursive: true });

console.log("→ esbuild bundling app.tsx");
await esbuild.build({
  entryPoints: [join(SRC, "app.tsx")],
  bundle: true,
  format: "esm",
  target: ["chrome110"],
  conditions: ["production", "browser", "import", "default"],
  minify: true,
  sourcemap: false,
  outfile: join(DIST, "app.js"),
  loader: {
    ".woff2": "file",
    ".woff":  "file",
    ".ttf":   "file",
    ".png":   "file",
    ".svg":   "file",
    ".jpg":   "file",
  },
  assetNames: "assets/[name]-[hash]",
  define: {
    "process.env.NODE_ENV": '"production"',
    "process.env.IS_PREACT": '"false"',
  },
  // Inline the CSS into app.css next to app.js
  plugins: [
    {
      name: "css-bundle",
      setup(b) {
        b.onEnd(() => console.log("  esbuild done"));
      },
    },
  ],
});

// Bundle the CSS separately (esbuild emits app.css automatically when imported)
// Verify it exists
try {
  await stat(join(DIST, "app.css"));
  console.log("✓ app.css emitted");
} catch {
  console.warn("! app.css not emitted — checking…");
}

// Copy index.html as-is
await copyFile(join(SRC, "index.html"), join(DIST, "index.html"));
console.log("✓ index.html copied");

// Copy Excalidraw's runtime-fetched assets (fonts, locales, data)
const PROD = join(root, "node_modules", "@excalidraw", "excalidraw", "dist", "prod");
for (const sub of ["fonts", "locales", "data"]) {
  try {
    await cp(join(PROD, sub), join(DIST, sub), { recursive: true });
    console.log(`✓ ${sub}/ copied`);
  } catch (e) { console.warn(`! ${sub} skip: ${e.message}`); }
}

console.log("\nBuild complete. Output:");
async function walk(d, depth = 0) {
  for (const f of await readdir(d, { withFileTypes: true })) {
    const p = join(d, f.name);
    if (f.isDirectory()) {
      console.log("  ".repeat(depth) + f.name + "/");
      await walk(p, depth + 1);
    } else {
      const s = await stat(p);
      console.log("  ".repeat(depth) + f.name + "  " + Math.round(s.size / 1024) + "KB");
    }
  }
}
await walk(DIST);
