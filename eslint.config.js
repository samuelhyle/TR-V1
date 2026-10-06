// Lint for the dashboard's ES modules.
//
// There was no linter, formatter or type checker on this side at all, which is
// how `web/components.js` ended up with six exports the app never called and
// `web/dashboard.js` reached two thousand lines with nothing able to read it.
// The rule set is narrow on purpose: these are the mistakes that actually
// happen when a UI is built by string concatenation into `innerHTML`, and a
// report nobody reads is worse than no report.
//
// `no-undef` matters most. A typo in a global function name in a 2,000-line
// module is otherwise only found by opening the page.

export default [
  {
    files: ["toriresearch/web/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: {
        document: "readonly",
        window: "readonly",
        console: "readonly",
        fetch: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        // The provenance panel re-stamps its "read at" time on a timer while the
        // dialog is open and clears it on close. These were absent from this list,
        // so `no-undef` reported working code as broken -- and the honest response
        // to a linter that cries wolf is to stop reading it. A missing global
        // here reads as an error but means nothing at runtime, which is the
        // failure mode this file exists to prevent.
        setInterval: "readonly",
        clearInterval: "readonly",
        EventSource: "readonly",
        AbortController: "readonly",
        URLSearchParams: "readonly",
        location: "readonly",
        history: "readonly",
        localStorage: "readonly",
        navigator: "readonly",
        Response: "readonly",
        Request: "readonly",
        Headers: "readonly",
        getComputedStyle: "readonly",
        matchMedia: "readonly",
        ResizeObserver: "readonly",
        requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly",
        HTMLElement: "readonly",
        Node: "readonly",
        URL: "readonly",
        Blob: "readonly",
        FormData: "readonly",
        crypto: "readonly",
        TextEncoder: "readonly",
        TextDecoder: "readonly",
      },
    },
    rules: {
      // `no-undef` blocks the build: a typo in a global name in a 2,000-line
      // module is otherwise only found by opening the page.
      "no-undef": "error",
      // `no-unused-vars` blocks. This warned for the whole view extraction, on
      // the understanding it would be promoted once the extraction landed -- and
      // it did. The six dead bindings it was reporting are gone: two unused
      // imports, an unreferenced `prefillPurchase` that the server-side prefill
      // replaced, a write-only `operationsData` cache, and two workflow
      // functions whose buttons were never wired. That last pair is why this
      // stayed a warning: the functions were correct and complete, so "unused"
      // meant "unreachable", not "unfinished", and deleting them would have
      // hidden a bug rather than fixed one.
      "no-unused-vars": "error",
      "no-var": "error",
      "prefer-const": "off", // the view modules use `let` for reassigned state
      eqeqeq: ["error", "smart"],
      "no-console": "off", // the dashboard reports collection state to the console
    },
  },
  // The hosted edge: the Netlify function, its Blobs services and the signed
  // gateway to the research backend.
  //
  // These were never linted. The npm script pointed at `toriresearch/web`, so the
  // function that holds the session store, the throttle that guards the login form
  // and the HMAC contract between two runtimes went unchecked for as long as they
  // existed -- and they are the files where a typo is a security defect rather than
  // a broken panel.
  {
    files: ["netlify/**/*.mjs", "bin/*.mjs"],
    languageOptions: {
      ecmaVersion: 2025, // Node 22 supports JSON imports with import attributes.
      sourceType: "module",
      globals: {
        console: "readonly",
        process: "readonly",
        Buffer: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        Headers: "readonly",
        Request: "readonly",
        Response: "readonly",
        AbortController: "readonly",
        AbortSignal: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        fetch: "readonly",
        structuredClone: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      // An underscore prefix states that an argument is deliberately ignored, which is
      // a different thing from having missed it. The throttle keys all take the same
      // pair so the loop can call them uniformly, and a key that reads the wrong one
      // would key the per-address limit on the username -- so the unused one is named
      // rather than removed and made to look accidental.
      "no-unused-vars": ["error", {argsIgnorePattern: "^_", varsIgnorePattern: "^_"}],
      "no-var": "error",
      eqeqeq: ["error", "smart"],
      // A comparison of secrets is only constant-time if it really is. The auth
      // module does its own comparisons with `timingSafeEqual`; this catches the
      // case where one is replaced with an ordinary equality and nothing notices.
      "no-self-compare": "error",
    },
  },
  // The build scripts. These run in Node, take their arguments from a shell, and
  // write to the filesystem, so the mistakes worth catching are the ones that would
  // publish the wrong thing rather than throw.
  {
    files: ["bin/bundle.mjs", "bin/*.mjs"],
    rules: {
      "no-console": "off",
    },
  },
];
