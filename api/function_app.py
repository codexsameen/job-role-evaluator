import azure.functions as func
import json
import logging
import uuid
from datetime import datetime, timezone

from shared.auth import get_user_id
from shared.db import get_container

app = func.FunctionApp()


# ---------------------------------------------------------------------------
# GET /api/queue  — fetch all items for the current user
# ---------------------------------------------------------------------------
@app.route(route="queue", methods=["GET"])
def get_queue(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        container = get_container()
        query = "SELECT * FROM c WHERE c.userId = @userId ORDER BY c.createdAt DESC"
        params = [{"name": "@userId", "value": user_id}]
        items = list(container.query_items(query=query, parameters=params))
        return func.HttpResponse(
            json.dumps(items),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("GET /api/queue failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# POST /api/queue  — write a new job item scoped to userId
# ---------------------------------------------------------------------------
@app.route(route="queue", methods=["POST"])
def post_queue(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    item = {
        "id": str(uuid.uuid4()),
        "userId": user_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        **body,
    }

    # Never let the caller override these fields
    item["userId"] = user_id

    try:
        container = get_container()
        container.create_item(body=item)
        return func.HttpResponse(
            json.dumps(item),
            status_code=201,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("POST /api/queue failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# DELETE /api/queue/{id}  — delete a single item by id + userId
# ---------------------------------------------------------------------------
@app.route(route="queue/{id}", methods=["DELETE"])
def delete_queue_item(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    item_id = req.route_params.get("id")
    if not item_id:
        return func.HttpResponse("Missing item id", status_code=400)

    try:
        container = get_container()
        container.delete_item(item=item_id, partition_key=user_id)
        return func.HttpResponse(status_code=204)
    except Exception as e:
        # Cosmos raises a 404-style exception if the item doesn't exist
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Item not found", status_code=404)
        logging.exception("DELETE /api/queue/{id} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# DELETE /api/queue  — delete ALL items for userId
# ---------------------------------------------------------------------------
@app.route(route="queue", methods=["DELETE"])
def delete_queue(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        container = get_container()
        query = "SELECT c.id FROM c WHERE c.userId = @userId"
        params = [{"name": "@userId", "value": user_id}]
        items = list(container.query_items(query=query, parameters=params))

        for item in items:
            container.delete_item(item=item["id"], partition_key=user_id)

        return func.HttpResponse(
            json.dumps({"deleted": len(items)}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("DELETE /api/queue failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)