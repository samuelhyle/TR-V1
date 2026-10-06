#!/usr/bin/env node
/**
 * Bundle the dashboard's ES modules into one minified script.
 *
 * The modules import each other by absolute URL (`/static/views/collection.js`)
 * because that is the only specifier that works unchanged in a browser, where the
 * same files are served from a static host. On disk those paths do not exist, so
 * esbuild needs to be told where `/static/` actually is before it can follow the
 * graph. That mapping is the whole purpose of this file.
 *
 * Invoked by `bin/build_netlify.py`, which falls back to shipping the plain modules
 * if this fails or if esbuild is not installed. A build machine without the tool
 * produces a larger site, never a broken one, so this script never throws: it
 * reports and exits non-zero, and the caller carries on.
 */
import {writeFile, mkdir} from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, '..', 'toriresearch', 'web');

/**
 * Map `/static/<name>` onto the file under `toriresearch/web`.
 *
 * A specifier that does not resolve to a real file is left for esbuild to reject
 * rather than being rewritten to something that does exist: a silently substituted
 * module would produce a bundle that loads and behaves differently from the one the
 * modules describe.
 */
const resolveFromWebRoot = {
  name: 'static-url-to-disk',
  setup(pluginBuild) {
    pluginBuild.onResolve({filter: /^\/static\//}, args => {
      const relative = args.path.replace(/^\/static\//, '');
      return {path: join(web, relative)};
    });
  },
};

async function main() {
  const [entry, outfile] = process.argv.slice(2);
  if (!entry || !outfile) {
    console.error('usage: bundle.mjs <entry> <outfile>');
    process.exit(2);
  }
  let esbuild;
  try {
    esbuild = await import('esbuild');
  } catch {
    console.error('esbuild is not installed');
    process.exit(3);
  }
  await mkdir(dirname(outfile), {recursive: true});
  const result = await esbuild.build({
    entryPoints: [entry],
    outfile,
    bundle: true,
    format: 'esm',
    target: 'es2022',
    minify: true,
    legalComments: 'none',
    plugins: [resolveFromWebRoot],
    logLimit: 0,
    metafile: true,
    write: false,
  });
  const [output] = result.outputFiles;
  await writeFile(outfile, output.contents);
  // The byte count goes to the caller so the build log can report what the bundling
  // actually bought, rather than only that it ran.
  const inputs = Object.keys(result.metafile.inputs).length;
  console.error(`bundled ${inputs} modules into ${output.contents.length} bytes`);
}

main().catch(error => {
  console.error(error.message || String(error));
  process.exit(1);
});
