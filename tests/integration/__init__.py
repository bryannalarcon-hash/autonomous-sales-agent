# Integration tests — exercise real subsystems (DB, network). DB-dependent modules skip
# themselves when DATABASE_URL is unset so the unit suite still runs without Postgres.
