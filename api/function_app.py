import azure.functions as func
import json
import logging
import os
import uuid
import yaml
from datetime import datetime, timezone

from openai import OpenAI

from shared.auth import get_user_id
from shared.db import get_container

# ---------------------------------------------------------------------------
# OpenAI client — initialised once at module load
# ---------------------------------------------------------------------------
_openai_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            base_url=os.environ["OPENAI_ENDPOINT"],
            api_key=os.environ["OPENAI_API_KEY"],
        )
    return _openai_client


# ---------------------------------------------------------------------------
# Content + prompt helpers
# ---------------------------------------------------------------------------

def load_content():
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'content.json'))
    with open(path) as f:
        return json.load(f)

def load_prompt_template():
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'prompts', 'evaluate.yaml'))
    with open(path) as f:
        return yaml.safe_load(f)

def build_prompt(content, jd_text):
    sections_text = ""
    for section in content["sections"]:
        sections_text += f"\n## Section {section['id']}: {section['title']} (weight: {section['weight']} pts)\n"
        for i, item in enumerate(section["items"]):
            sections_text += (
                f"  Item {i}: {item['question']}\n"
                f"    Signals: {item['signals']}\n"
                f"    Max score: {item['max']}\n"
            )

    knockout_text = "\n".join(
        f"  {i}: {ko['label']} — {ko['detail']}"
        for i, ko in enumerate(content["knockouts"])
    )

    template = load_prompt_template()
    user_message = template["user"].format(
        jd_text=jd_text,
        sections_text=sections_text,
        knockout_text=knockout_text,
    )
    return template["system"], user_message


# ---------------------------------------------------------------------------
# Scoring — all arithmetic done in Python, never by the model
# ---------------------------------------------------------------------------

def compute_scores(content, raw_scores):
    weighted = {}
    for section in content["sections"]:
        sid         = str(section["id"])
        items       = section["items"]
        max_w       = section["weight"]
        raw         = raw_scores.get(sid, [])
        clamped     = [max(0, min(int(raw[i]), items[i]["max"])) if i < len(raw) else 0
                       for i in range(len(items))]
        actual_max  = sum(item["max"] for item in items)
        section_sum = sum(clamped)
        weighted[sid] = round((section_sum / actual_max) * max_w) if actual_max > 0 else 0

    total = sum(weighted.values())
    return weighted, total


app = func.FunctionApp()


# ---------------------------------------------------------------------------
# GET /api/queue
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
# POST /api/queue
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
        "id":        str(uuid.uuid4()),
        "userId":    user_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        **body,
    }
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
# DELETE /api/queue/{id}
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
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Item not found", status_code=404)
        logging.exception("DELETE /api/queue/{id} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# DELETE /api/queue
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


# ---------------------------------------------------------------------------
# POST /api/evaluate
# ---------------------------------------------------------------------------
@app.route(route="evaluate", methods=["POST"])
def evaluate(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    jd_text = body.get("jd_text", "").strip()
    url     = body.get("url", "").strip()

    if not jd_text:
        return func.HttpResponse("jd_text is required", status_code=400)

    try:
        content = load_content()
    except Exception as e:
        logging.exception("Failed to load content.json")
        return func.HttpResponse("Server configuration error", status_code=500)

    system_message, user_message = build_prompt(content, jd_text)

    try:
        client     = get_openai_client()
        deployment = os.environ["OPENAI_DEPLOYMENT"]
        completion = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user",   "content": user_message},
            ],
            max_completion_tokens=8000,
        )
    except Exception as e:
        logging.exception("OpenAI call failed")
        return func.HttpResponse(f"AI service error: {e}", status_code=502)

    raw_text = completion.choices[0].message.content.strip()

    try:
        model_output = json.loads(raw_text)
    except json.JSONDecodeError:
        logging.error("Model returned non-JSON: %s", raw_text)
        return func.HttpResponse("AI returned invalid response", status_code=502)

    raw_scores = model_output.get("scores", {})
    reasoning  = model_output.get("reasoning", {})
    knockouts  = model_output.get("knockouts", [])
    company    = model_output.get("company", "Unknown")
    role       = model_output.get("role", "Unknown")

    weighted, total = compute_scores(content, raw_scores)

    result = {
        "company":   company,
        "role":      role,
        "url":       url,
        "total":     total,
        "weighted":  weighted,
        "scores":    raw_scores,
        "reasoning": reasoning,
        "knockouts": knockouts,
    }

    return func.HttpResponse(
        json.dumps(result),
        status_code=200,
        mimetype="application/json",
    )