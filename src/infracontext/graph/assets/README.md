# Vendored assets

## vis-network-9.1.9.min.js

- Origin: <https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js>
- Version: 9.1.9 (standalone UMD build; keep in sync with `_VIS_VERSION` in `../render.py`)
- SHA-256: `f53f833ddb9bf97efe856bb0637d4fe88f39e39999c7e94a4b8afc8de8a1a2e5`
- License: dual-licensed (Apache-2.0 OR MIT) — see the banner in the file.

Inlined into `ic graph render` HTML output so rendered pages work offline.
`--cdn` restores the `<script src>` form. When bumping the version, update
`_VIS_VERSION`, re-download, re-verify the hash, and confirm the bundle still
contains no `</script>` sequence (it is embedded in an inline `<script>` tag).
