// Rollup config for Decky plugin bundling.
// The @decky/rollup package handles all the heavy lifting — it configures
// TypeScript compilation, React JSX transform, and outputs a single
// dist/index.js file that Decky Loader can consume.
import deckyPlugin from "@decky/rollup";

export default deckyPlugin({});
