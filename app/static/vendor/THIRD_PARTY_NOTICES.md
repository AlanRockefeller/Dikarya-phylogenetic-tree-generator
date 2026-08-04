# Third-party browser asset notices

Last audited: 2026-07-18. Paths below are relative to `app/static/`.
`VENDORED_HASHES.txt` records SHA-256 hashes for the immutable files. The
locally modified `js/phylotree.js` is intentionally documented here rather
than treated as an immutable upstream artifact.

## JavaScript and CSS

| Component | Version | Local files | Upstream source | License and attribution |
| --- | --- | --- | --- | --- |
| D3 | 7.9.0 | `vendor/d3.v7.min.js` | Exact match for [`d3@7.9.0/dist/d3.min.js`](https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js); source at [d3/d3](https://github.com/d3/d3/tree/v7.9.0) | ISC; copyright 2010–2023 Mike Bostock. See `licenses/D3-LICENSE.txt`. |
| Lodash | 4.18.1 | `vendor/lodash-4.min.js` | Exact match for [`lodash@4.18.1/lodash.min.js`](https://cdn.jsdelivr.net/npm/lodash@4.18.1/lodash.min.js); source at [lodash/lodash](https://github.com/lodash/lodash) | MIT; copyright OpenJS Foundation and other contributors; based on Underscore.js 1.8.3. See `licenses/Lodash-LICENSE.txt`. |
| Swagger UI | 5.17.14 | `vendor/swagger-ui-bundle.js`, `vendor/swagger-ui-standalone-preset.js`, `vendor/swagger-ui.css` | Exact matches for [`swagger-ui-dist@5.17.14`](https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/); source at [swagger-api/swagger-ui v5.17.14](https://github.com/swagger-api/swagger-ui/tree/v5.17.14) | Apache-2.0; copyright 2020–2021 SmartBear Software Inc. See `licenses/Swagger-UI-LICENSE.txt`, `swagger-ui-bundle.js.LICENSE.txt`, and `swagger-ui-standalone-preset.js.LICENSE.txt`. The CSS also contains normalize.css 7.0.0 under MIT, whose notice remains embedded in the file. |
| Tailwind CSS Play CDN | 3.4.17 | `vendor/tailwind.js` | Exact match for [`cdn.tailwindcss.com/3.4.17`](https://cdn.tailwindcss.com/3.4.17); source at [tailwindlabs/tailwindcss v3.4.17](https://github.com/tailwindlabs/tailwindcss/tree/v3.4.17) | MIT; copyright Tailwind Labs, Inc. See `licenses/Tailwind-CSS-LICENSE.txt`. License banners for bundled dependencies remain embedded in the file. |
| Underscore.js | 1.13.6 | `vendor/underscore-1.13.6-min.js` | Exact match for [`underscore@1.13.6/underscore-umd-min.js`](https://cdn.jsdelivr.net/npm/underscore@1.13.6/underscore-umd-min.js); source at [jashkenas/underscore 1.13.6](https://github.com/jashkenas/underscore/tree/1.13.6) | MIT; copyright Jeremy Ashkenas, DocumentCloud and Investigative Reporters & Editors. See `licenses/Underscore-LICENSE.txt`. |
| Font Awesome Free | 6.4.0 | `vendor/fontawesome.min.css` | Exact match for [`@fortawesome/fontawesome-free@6.4.0/css/all.min.css`](https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css); source at [FortAwesome/Font-Awesome 6.4.0](https://github.com/FortAwesome/Font-Awesome/tree/6.4.0) | Code: MIT; font files: SIL OFL 1.1; icon designs: CC BY 4.0. Copyright 2023 Fonticons, Inc. See `licenses/Font-Awesome-Free-LICENSE.txt`. |
| Inter font-face wrapper | Inter 4.001 | `vendor/inter.css` | Dikarya wrapper for the Inter files listed below, downloaded from `fonts.gstatic.com` on 2026-05-11; family source at [rsms/inter](https://github.com/rsms/inter) and [Google Fonts](https://fonts.google.com/specimen/Inter) | The wrapper is project code; the referenced fonts are SIL OFL 1.1. See `licenses/Inter-LICENSE.txt`. |
| phylotree.js | 2.2.1 plus Dikarya changes | `js/phylotree.js` | Based on [`phylotree@2.2.1/dist/phylotree.js`](https://cdn.jsdelivr.net/npm/phylotree@2.2.1/dist/phylotree.js), with inline dated comments identifying Dikarya changes; source at [veg/phylotree.js](https://github.com/veg/phylotree.js) | MIT; copyright 2016 iGEM/UCSD evolutionary biology and bioinformatics group. See `licenses/phylotree-LICENSE.txt`. Its separately loaded Underscore and Lodash dependencies are documented above. |

## Webfonts

| Component | Version | Local files | Upstream source | License and attribution |
| --- | --- | --- | --- | --- |
| Font Awesome Free | 6.4.0 | `webfonts/fa-brands-400.woff2`, `fa-regular-400.woff2`, `fa-solid-900.woff2`, `fa-v4compatibility.woff2`, and the matching `.ttf` files | Every file hash exactly matches [`@fortawesome/fontawesome-free@6.4.0/webfonts/`](https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/webfonts/) | Font software: SIL OFL 1.1 with reserved font name “Font Awesome”; icon designs: CC BY 4.0. Copyright 2023 Fonticons, Inc. Brand names and logos may be trademarks of their owners. See `licenses/Font-Awesome-Free-LICENSE.txt`. |
| Inter | 4.001 (embedded font revision) | `webfonts/inter/inter-400.ttf`, `inter-500.ttf`, `inter-600.ttf`, `inter-700.ttf` | Static TrueType files served by Google Fonts (`fonts.gstatic.com`) on 2026-05-11; upstream family source at [rsms/inter](https://github.com/rsms/inter) and [google/fonts/ofl/inter](https://github.com/google/fonts/tree/main/ofl/inter) | SIL OFL 1.1; copyright 2020 The Inter Project Authors. See `licenses/Inter-LICENSE.txt`. |

## Swagger UI bundled dependency notices

Swagger UI's minified files point to companion `.LICENSE.txt` files. The
5.17.14 `swagger-ui-dist` package omitted those companions, so this repository
supplies them locally. Swagger UI's production bundle includes permissively
licensed dependencies such as classnames, extend, buffer, fast-json-patch,
repeat-string, and DOMPurify. Their copyright/license notices and the exact
Swagger dependency manifest are linked from
`swagger-ui-bundle.js.LICENSE.txt`; the standalone-preset companion does the
same for its bundle. No upstream JavaScript was changed.
