# Vendored backup schema resources

These files are the exact schema dumps published by the upstream applications
at the pinned commits below. They are runtime resources: `rikkelivo` reads them
to create, validate and migrate databases without any network access.

| Directory | Upstream | Commit | Files |
| --- | --- | --- | --- |
| `rikkahub/room_<N>.json` | [rikkahub/rikkahub](https://github.com/rikkahub/rikkahub) Room schema export `app/schemas/me.rerere.rikkahub.data.db.AppDatabase/<N>.json` | `eeef60b22ba24383daf51f3c6c2e5b75bbab6bba` (RikkaHub 3.1.0) | Room versions 24–31 |
| `kelivo/drift_<N>.json` | [Chevey339/kelivo](https://github.com/Chevey339/kelivo) Drift schema export `drift_schemas/app_database/drift_schema_v<N>.json` | `4742d2e2861d963a65d8f642eeba9782566cd426` (Kelivo 1.3.1) | Drift schema versions 1–3 |

`cuplivo/cuplivo` at `282dc0b08f5a5f4ea4d644b02c60cdac668a0860` (Cuplivo 4.3.0)
publishes the same SQLite schema (Drift 1–3); the frozen SQL3 shape is shared,
while application semantics differ.

Both upstream projects are licensed under the GNU Affero General Public
License, version 3. The files here are unmodified upstream data exports; they
are reproduced solely so the converter can read and write the documented
backup formats. They are not a source-code derivative of either application.

Do not edit these files in place: the Room identity hashes and Drift SQL are
the contract the apps validate their own databases against.
