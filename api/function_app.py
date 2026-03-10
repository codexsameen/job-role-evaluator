import azure.functions as func
import json
import logging
import os
import re
import uuid
import yaml
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
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
# JD fetch helper
# ---------------------------------------------------------------------------

_LINKEDIN_SELECTORS = [
    "div.show-more-less-html__markup",
    "div.description__text",
]

_GENERIC_SELECTORS = [
    "main",
    "article",
    "[class*='job-description']",
    "[class*='jobDescription']",
    "[class*='job_description']",
    "[id*='job-description']",
    "[id*='jobDescription']",
    "[class*='description']",
]

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

def _clean_text(text: str) -> str:
    text = re.sub(r'\r\n|\r', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def fetch_jd_from_url(url: str) -> str:
    """
    Fetch a job posting URL and return the job description as clean plain text.
    Raises ValueError if nothing useful can be extracted.
    Raises httpx.HTTPError on network / HTTP failures.
    """
    is_linkedin = "linkedin.com" in url

    with httpx.Client(follow_redirects=True, timeout=15) as client:
        response = client.get(url, headers=_FETCH_HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()

    selectors = _LINKEDIN_SELECTORS if is_linkedin else _GENERIC_SELECTORS
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = _clean_text(el.get_text(separator="\n"))
            if len(text) > 200:
                return text

    body = soup.find("body")
    if body:
        text = _clean_text(body.get_text(separator="\n"))
        if len(text) > 200:
            return text

    raise ValueError("Could not extract meaningful job description from URL")


# ---------------------------------------------------------------------------
# Content + prompt helpers
# ---------------------------------------------------------------------------

def load_content():
    base = os.path.dirname(__file__)
    candidates = [
        os.path.join(base, 'content.json'),
        os.path.join(base, '..', 'content.json'),
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    raise FileNotFoundError(f"content.json not found. Tried: {candidates}")

def load_prompt_template():
    base = os.path.dirname(__file__)
    candidates = [
        os.path.join(base, 'prompts', 'evaluate.yaml'),
        '/home/site/wwwroot/api/prompts/evaluate.yaml',
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    raise FileNotFoundError(f"evaluate.yaml not found. Tried: {candidates}")

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
# PATCH /api/queue/{id}
# ---------------------------------------------------------------------------
@app.route(route="queue/{id}", methods=["PATCH"])
def patch_queue_item(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    item_id = req.route_params.get("id")
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        container = get_container()
        item = container.read_item(item=item_id, partition_key=user_id)
        item.update(body)
        container.replace_item(item=item_id, body=item)
        return func.HttpResponse(json.dumps(item), status_code=200, mimetype="application/json")
    except Exception as e:
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Item not found", status_code=404)
        logging.exception("PATCH /api/queue/{id} failed")
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
# POST /api/fetch-jd
# ---------------------------------------------------------------------------
@app.route(route="fetch-jd", methods=["POST"])
def fetch_jd(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    if user_id is None:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    url = body.get("url", "").strip()
    if not url:
        return func.HttpResponse("url is required", status_code=400)

    try:
        text = fetch_jd_from_url(url)
        return func.HttpResponse(
            json.dumps({"text": text}),
            status_code=200,
            mimetype="application/json",
        )
    except ValueError as e:
        logging.warning("JD extraction failed for %s: %s", url, e)
        return func.HttpResponse(str(e), status_code=422)
    except Exception as e:
        logging.exception("fetch_jd failed for %s", url)
        return func.HttpResponse(f"Failed to fetch URL: {e}", status_code=502)


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
        return func.HttpResponse(f"AI service error: {str(e)}", status_code=500)

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