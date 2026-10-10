"""Configure the user-authorized demo_db read-only account; preserve existing access."""
import json
from pathlib import Path

from local_wsl_provision import query, verifier

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local/runtime/wsl"
ROLE = "enterprise_mind_db_readonly"


def main() -> None:
    if query("SELECT current_user") != "postgres":
        raise RuntimeError("requires postgres local peer identity")
    before = query("SELECT count(*)||','||md5(string_agg(row_to_json(t)::text,'|' ORDER BY id)) FROM public.items t", "demo_db")
    work = WORK / "demo-permissions-before.json"
    if not work.exists():
        snapshot = query("SELECT row_to_json(t) FROM (SELECT datname,datacl::text FROM pg_database WHERE datname='demo_db') t")
        work.write_text(json.dumps({"database_acl": json.loads(snapshot), "items_fingerprint": before}, indent=2))
    existing = query(f"SELECT rolcanlogin::int||','||rolsuper::int||','||rolcreatedb::int||','||rolcreaterole::int||','||rolreplication::int||','||rolbypassrls::int||','||rolinherit::int FROM pg_roles WHERE rolname='{ROLE}'")
    if existing and existing != "1,0,0,0,0,0,0":
        raise RuntimeError("existing readonly role properties mismatch")
    if query(f"SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON m.member=r.oid WHERE r.rolname='{ROLE}'"):
        raise RuntimeError("readonly role must not inherit another role")
    if not existing:
        password = (ROOT / ".local/runtime/secrets/database_readonly_password").read_text().strip()
        encoded = verifier(password)
        query(f"BEGIN; SET LOCAL log_statement='none'; SET LOCAL log_min_duration_statement=-1; SET LOCAL log_min_error_statement='panic'; CREATE ROLE {ROLE} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{encoded}'; COMMIT;")
    # PUBLIC TEMP would also grant TEMP to the new account. Preserve every other
    # existing login's effective TEMP access as explicit grants before revoking it.
    preserved = query(f"SELECT rolname FROM pg_roles WHERE rolcanlogin AND rolname<>'{ROLE}' AND has_database_privilege(oid,'demo_db','TEMP')").splitlines()
    grants = []
    for name in preserved:
        quoted = '"' + name.replace('"', '""') + '"'
        grants.append(f"GRANT TEMP ON DATABASE demo_db TO {quoted};")
    query("BEGIN; " + " ".join(grants) + f"""
REVOKE TEMP ON DATABASE demo_db FROM PUBLIC;
REVOKE ALL ON DATABASE demo_db FROM {ROLE};
GRANT CONNECT ON DATABASE demo_db TO {ROLE};
GRANT USAGE ON SCHEMA public TO {ROLE};
GRANT SELECT ON TABLE public.items TO {ROLE};
ALTER ROLE {ROLE} SET default_transaction_read_only = on;
-- MCP rejects sessions that can log statement errors or query parameters.
-- Scope these settings to this account and database; preserve other workloads.
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_statement = 'none';
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_min_error_statement = 'panic';
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_min_messages = 'panic';
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_parameter_max_length_on_error = 0;
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_min_duration_statement = -1;
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_min_duration_sample = -1;
ALTER ROLE {ROLE} IN DATABASE demo_db SET log_transaction_sample_rate = 0;
COMMIT;
""", "demo_db")
    privileges = query(f"SET ROLE {ROLE}; SELECT has_database_privilege(current_database(),'CREATE')::int||','||has_database_privilege(current_database(),'TEMP')::int||','||has_schema_privilege('public','CREATE')::int||','||has_table_privilege('public.items','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')::int||','||has_table_privilege('public.items','SELECT')::int;", "demo_db")
    if privileges != "0,0,0,0,1":
        raise RuntimeError("readonly privilege verification failed")
    after = query("SELECT count(*)||','||md5(string_agg(row_to_json(t)::text,'|' ORDER BY id)) FROM public.items t", "demo_db")
    if after != before:
        raise RuntimeError("demo data fingerprint changed")
    print(json.dumps({"database": "demo_db", "readonly_role": ROLE, "items_rows": int(before.split(',')[0]), "data_preserved": True, "existing_login_temp_access_preserved": preserved, "no_create_temp_or_dml": True}))


if __name__ == "__main__":
    main()
