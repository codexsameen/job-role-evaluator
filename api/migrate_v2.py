"""
migrate_v2.py — One-time migration from v1 schema to v2.

v1 schema:
  profiles  container: Profile + embedded rubric + bayes fields
  queue     container: Role + Application merged in one document

v2 schema:
  profiles  container: Profile doc (id=userId) + Preferences doc (id="prefs-<userId>")
  entities  container: Rubric (id="rubric-<userId>") + Role (id=<uuid>) + Application (id="app-<uuid>")

Usage:
  cd api
  python migrate_v2.py

Set COSMOS_CONNECTION_STRING in the environment or in local.settings.json before running.
"""

import json
import os
import sys
from datetime import datetime, timezone

# Load local.settings.json if present (dev convenience)
_settings_path = os.path.join(os.path.dirname(__file__), "local.settings.json")
if os.path.exists(_settings_path):
    with open(_settings_path) as f:
        _settings = json.load(f)
    for k, v in _settings.get("Values", {}).items():
        os.environ.setdefault(k, v)

from azure.cosmos import CosmosClient, PartitionKey

CONN_STR = os.environ.get("COSMOS_CONNECTION_STRING")
if not CONN_STR:
    print("ERROR: COSMOS_CONNECTION_STRING not set", file=sys.stderr)
    sys.exit(1)

client   = CosmosClient.from_connection_string(CONN_STR)
database = client.get_database_client("job-evaluator")

# Source containers
profiles_src = database.get_container_client("profiles")
queue_src    = database.get_container_client("queue")

# Destination containers (create if not exist)
database.create_container_if_not_exists(
    id="entities",
    partition_key=PartitionKey(path="/userId"),
)
entities_dst  = database.get_container_client("entities")
profiles_dst  = profiles_src  # same container, we upsert cleaned docs back in


def _now():
    return datetime.now(timezone.utc).isoformat()


def migrate_profiles():
    print("=== Migrating profiles ===")
    query = "SELECT * FROM c"
    docs  = list(profiles_src.query_items(query=query, enable_cross_partition_query=True))
    print(f"  Found {len(docs)} profile documents")

    rubrics_written = 0
    prefs_written   = 0
    profiles_written = 0

    for doc in docs:
        user_id = doc.get("userId") or doc.get("id")
        if not user_id:
            print(f"  SKIP: document has no userId: {doc.get('id')}")
            continue

        # --- Rubric ---
        rubric_blob = doc.pop("rubric", None)
        rubric_status = doc.pop("rubricStatus", None)
        rubric_generated_at = doc.pop("rubricGeneratedAt", None)
        rubric_started_at   = doc.pop("rubricGenerationStartedAt", None)

        if rubric_blob:
            rubric_doc = {
                "id":          f"rubric-{user_id}",
                "userId":      user_id,
                "type":        "rubric",
                "status":      "done",
                "generatedAt": rubric_generated_at or _now(),
                "meta":        rubric_blob.get("meta", {}),
                "sections":    rubric_blob.get("sections", []),
                "knockouts":   rubric_blob.get("knockouts", []),
            }
            entities_dst.upsert_item(rubric_doc)
            rubrics_written += 1
        elif rubric_status == "generating" and rubric_started_at:
            # Preserve in-progress state (unlikely in practice)
            rubric_doc = {
                "id":                  f"rubric-{user_id}",
                "userId":              user_id,
                "type":                "rubric",
                "status":              "generating",
                "generationStartedAt": rubric_started_at,
            }
            entities_dst.upsert_item(rubric_doc)
            rubrics_written += 1

        # --- Preferences ---
        bayes_weights = doc.pop("bayesWeights", None)
        obs_count     = doc.pop("bayesObservationCount", None)
        prior_strength = doc.pop("bayesPriorStrength", 15)

        if bayes_weights is not None:
            prefs_doc = {
                "id":                    f"prefs-{user_id}",
                "userId":                user_id,
                "type":                  "preferences",
                "bayesWeights":          bayes_weights,
                "bayesObservationCount": obs_count or 0,
                "bayesPriorStrength":    prior_strength,
                "updatedAt":             _now(),
            }
            profiles_dst.upsert_item(prefs_doc)
            prefs_written += 1

        # --- Clean profile doc ---
        doc["type"]      = "profile"
        doc["id"]        = user_id
        doc["userId"]    = user_id
        doc["updatedAt"] = doc.get("updatedAt") or _now()
        profiles_dst.upsert_item(doc)
        profiles_written += 1

    print(f"  Profiles written:    {profiles_written}")
    print(f"  Rubrics written:     {rubrics_written}")
    print(f"  Preferences written: {prefs_written}")


def migrate_queue():
    print("=== Migrating queue ===")
    query = "SELECT * FROM c"
    docs  = list(queue_src.query_items(query=query, enable_cross_partition_query=True))
    print(f"  Found {len(docs)} queue documents")

    _APP_FIELDS = {
        "interest", "interestHistory", "status", "statusHistory", "notes", "addedAt",
    }

    roles_written = 0
    apps_written  = 0
    errors        = 0

    for doc in docs:
        user_id  = doc.get("userId")
        role_id  = doc.get("id")
        if not user_id or not role_id:
            print(f"  SKIP: missing userId or id in document: {doc}")
            errors += 1
            continue

        # --- Application doc ---
        app_fields = {k: doc[k] for k in _APP_FIELDS if k in doc}
        app_doc = {
            "id":     f"app-{role_id}",
            "userId": user_id,
            "type":   "application",
            "roleId": role_id,
            **app_fields,
        }
        if "addedAt" not in app_doc:
            app_doc["addedAt"] = doc.get("createdAt") or _now()

        # --- Role doc ---
        role_doc = {k: v for k, v in doc.items() if k not in _APP_FIELDS}
        role_doc["type"] = "role"

        try:
            entities_dst.upsert_item(role_doc)
            roles_written += 1
            entities_dst.upsert_item(app_doc)
            apps_written += 1
        except Exception as e:
            print(f"  ERROR migrating queue item {role_id}: {e}")
            errors += 1

    print(f"  Roles written:        {roles_written}")
    print(f"  Applications written: {apps_written}")
    print(f"  Errors:               {errors}")
    return errors


def verify():
    print("=== Verification ===")
    queue_count = len(list(
        queue_src.query_items("SELECT c.id FROM c", enable_cross_partition_query=True)
    ))
    role_count = len(list(
        entities_dst.query_items(
            "SELECT c.id FROM c WHERE c.type = 'role'",
            enable_cross_partition_query=True,
        )
    ))
    app_count = len(list(
        entities_dst.query_items(
            "SELECT c.id FROM c WHERE c.type = 'application'",
            enable_cross_partition_query=True,
        )
    ))
    rubric_count = len(list(
        entities_dst.query_items(
            "SELECT c.id FROM c WHERE c.type = 'rubric'",
            enable_cross_partition_query=True,
        )
    ))

    print(f"  queue items:        {queue_count}")
    print(f"  roles in entities:  {role_count}  (expected: {queue_count})")
    print(f"  apps in entities:   {app_count}   (expected: {queue_count})")
    print(f"  rubrics in entities:{rubric_count}")

    ok = (role_count == queue_count and app_count == queue_count)
    print(f"  {'✓ PASS' if ok else '✗ FAIL — counts do not match'}")
    return ok


if __name__ == "__main__":
    migrate_profiles()
    errors = migrate_queue()
    ok = verify()
    if errors or not ok:
        print("\nMigration completed with errors — review output above before switching traffic.")
        sys.exit(1)
    else:
        print("\nMigration complete. Old 'queue' container is untouched and can be archived once the new code is verified in production.")
