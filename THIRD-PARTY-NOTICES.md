# Third-party dependencies

Vouch's own code is Apache-2.0. Dependencies retain their own licenses.

| Direct dependency | Use | Declared license |
| --- | --- | --- |
| jaz-lang 0.2.0a4 | Pinned execution runtime | Apache-2.0 |
| Typer | Command-line interface | MIT |
| Textual | Experimental terminal interface | MIT |

The repository does not vendor their source. Their distributions include
their license materials. `uv.lock` records the resolved dependency graph,
including transitive packages; this table is not a complete transitive
license inventory. When redistributing an environment or vendoring code,
retain applicable notices and review the actual distributions' licenses.

JAZ source: https://github.com/jaz-lang/jaz
