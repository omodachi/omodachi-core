import {defineConfig} from 'vite';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {APP_NAME} from './src/lib/name.js';

// LITE-1c: the app's name lives in src/lib/name.js only. The build writes it
// into index.html (`%APP_NAME%`: the title and the Home Screen name) and into
// the manifest, whose other fields are src/manifest.json.
function appName() {
  const rest = JSON.parse(readFileSync(new URL('./src/manifest.json', import.meta.url), 'utf8'));
  return {
    name: 'odomo-app-name',
    transformIndexHtml: (html) => html.replaceAll('%APP_NAME%', APP_NAME),
    generateBundle() {
      const {id, ...others} = rest;
      const manifest = {id, name: APP_NAME, short_name: APP_NAME, ...others};
      this.emitFile({type: 'asset', fileName: 'manifest.webmanifest', source: JSON.stringify(manifest, null, 2) + '\n'});
    },
  };
}

// LITE-1a. The build writes straight into core's package data, which is what
// the wheel carries and what `omodachid` serves at /odomo/ (`odomo.py`). It is
// committed, and `scripts/verify_odomo.py` rebuilds it from this directory and
// compares byte for byte, so the output must depend on nothing but the source
// and the locked toolchain: relative URLs (`base: './'`, the page lives under
// /odomo/ on whatever name the host is reached by), no source maps, no
// modulepreload polyfill, content hashes in every asset name.
//
// Not `dist/` or `build/`: core's .gitignore drops those at any depth.
export default defineConfig({
  base: './',
  plugins: [appName()],
  // React's own production switch, without the dev server's process shim.
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  build: {
    outDir: fileURLToPath(new URL('../src/omodachi_core/data/odomo', import.meta.url)),
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
    // Every asset is a file with a hashed name, never a data: URI folded into
    // the bundle, so the CSP's `img-src`/`font-src` story stays simple.
    assetsInlineLimit: 0,
    modulePreload: {polyfill: false},
    reportCompressedSize: false,
    target: 'es2022',
  },
});
