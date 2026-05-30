Core v0.9.0 functionality is basically there. What remains is release validation, not much new feature work.

  Missing v0.9.0 items, excluding operator docs:

  1. Auth-enabled live web smoke
      - Run stack with BRIGADE_REQUIRE_AUTH=true.
      - Issue/use a token.
      - Verify web load, /api/auth/me, denied observer writes, and one owner/operator write path.
  2. Clean empty-volume stack pass
      - Backup first.
      - Start from empty volumes.
      - Run migrations.
      - Create first user.
      - Initialize/onboard MVP agents.
      - Confirm health/db/status/web still pass.
  3. Backup, wipe, reseed, restore validation
      - v07-wipe-reseed.sh --confirm-wipe still needs a controlled rerun.
      - Restore path still needs proof after wipe.
  4. Partial migration failure recovery test
      - The reporting code exists, but we still need the deliberate failed-migration scenario to prove recovery guidance is usable.
  5. Clean-stack datastore sentinels
      - Qdrant and Neo4j inspections pass against current live state.
      - Still need a clean-stack sentinel pass proving fresh user chat/knowledge/provenance writes show up and survive recreate.
  6. Local Ollama internal smoke
      - Since local Ollama is now the default/internal connection, do one bounded live agent run with Ollama if the local model is available.
