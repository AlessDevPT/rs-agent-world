"""Identity verification: the `AuthProvider` interface and its Supabase impl.

This file exists for two reasons beyond convention, both found while adding
the outbound-HTTP contract in T2.

**Without it, import-linter cannot see this package.** `grimp` walks
packages, and a directory with no `__init__.py` is an implicit namespace
package that its walk does not enter. So every architecture contract in
`pyproject.toml` has been silently skipping the module that verifies every
user's token since Phase 1 - passing, and not looking.

**Without it, a non-editable install omits this package.**
`[tool.setuptools.packages.find]` does not pick up namespace packages, so a
built wheel would be missing `agentworld.infra.auth` entirely and would fail
at the first import. CI installs with `-e`, which adds a path hook and hides
this, so the gap was invisible there too.
"""
