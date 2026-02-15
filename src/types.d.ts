// Type declarations for importing static assets (images) in TypeScript.
// Without these, TypeScript would complain about `import logo from "./logo.png"`
// because it doesn't know how to handle non-code imports.
// The bundler (Rollup) handles the actual import at build time.

declare module "*.svg" {
  const content: string;
  export default content;
}

declare module "*.png" {
  const content: string;
  export default content;
}

declare module "*.jpg" {
  const content: string;
  export default content;
}

declare module "*.webp" {
  const content: string;
  export default content;
}
