# Third-party notices

Vowdo's own source is licensed under Apache-2.0; see [LICENSE](LICENSE).
The native binaries embed Deno and its runtime dependencies. License texts
are included in the source repository and binary distribution:

- Deno (MIT): [license](typescript/licenses/deno-LICENSE)
- V8 (BSD-3-Clause): [license](typescript/licenses/v8-LICENSE)
- SQLite (public domain): [dedication](typescript/licenses/sqlite-PUBLIC-DOMAIN)
- Build versions: [toolchain](typescript/licenses/toolchain.txt)

The TypeScript source uses only Deno and Node built-ins; no remote module
or third-party package dependency is imported. The earlier Python release
and its attribution remain available at the immutable `v0.1.0rc1` tag.
