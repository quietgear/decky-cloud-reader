// Rollup config for Decky plugin bundling.
// The @decky/rollup package handles all the heavy lifting — it configures
// TypeScript compilation, React JSX transform, and outputs a single
// dist/index.js file that Decky Loader can consume.
//
// We add @rollup/plugin-replace to inject the plugin version from package.json
// at build time, replacing __PLUGIN_VERSION__ with the actual version string.
import deckyPlugin from "@decky/rollup";
import replace from "@rollup/plugin-replace";
import { readFileSync } from "fs";

const pkg = JSON.parse(readFileSync("package.json", "utf-8"));

const config = deckyPlugin({});
config.plugins = [
  replace({ preventAssignment: true, __PLUGIN_VERSION__: JSON.stringify(pkg.version) }),
  ...(config.plugins || []),
];
export default config;
