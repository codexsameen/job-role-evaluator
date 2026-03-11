import azure.functions as func
import json
import logging
import os
import re
import threading
import uuid
import yaml
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

from shared.auth import get_user_id
from shared.db import get_container, get_profiles_container

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


def _load_yaml_prompt(filename):
    base = os.path.dirname(__file__)
    candidates = [
        os.path.join(base, 'prompts', filename),
        os.path.join('/home/site/wwwroot/api/prompts', filename),
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    raise FileNotFoundError(f"{filename} not found. Tried: {candidates}")

def build_prompt(content, jd_text, candidate_profile=None):
    if candidate_profile is None:
        candidate_profile = "(no candidate profile provided)"

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

    template = _load_yaml_prompt("evaluate.yaml")
    user_message = template["user"].format(
        candidate_profile=candidate_profile,
        jd_text=jd_text,
        sections_text=sections_text,
        knockout_text=knockout_text,
    )
    return template["system"], user_message


def _profile_to_candidate_string(profile):
    """Convert a profile dict to a bullet-point string for the evaluate prompt."""
    lines = []
    if profile.get("roleTitle"):
        lines.append(f"- Targeting: {profile['roleTitle']} roles")
    if profile.get("location"):
        lines.append(f"- Based in: {profile['location']}")
    if profile.get("skills"):
        lines.append(f"- Key skills: {profile['skills']}")
    if profile.get("backgroundSummary"):
        lines.append(f"- Background: {profile['backgroundSummary']}")
    if profile.get("careerGoals"):
        lines.append(f"- Career goals: {profile['careerGoals']}")
    if profile.get("companySizePreference"):
        lines.append(f"- Preferred company size: {profile['companySizePreference']}")
    if profile.get("workArrangement"):
        lines.append(f"- Work arrangement: {profile['workArrangement']}")
    if profile.get("compMin") and profile.get("compMax"):
        currency = profile.get("currencySymbol", "£")
        lines.append(f"- Target compensation: {currency}{profile['compMin']}–{currency}{profile['compMax']}")
    return "\n".join(lines) if lines else None


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
        query = "SELECT TOP 500 * FROM c WHERE c.userId = @userId ORDER BY c.createdAt DESC"
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

        BATCH_SIZE = 100  # Cosmos transactional batch limit
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i:i + BATCH_SIZE]
            operations = [("delete", item["id"], {}) for item in chunk]
            container.execute_item_batch(batch_operations=operations, partition_key=user_id)
        return func.HttpResponse(
            json.dumps({"deleted": len(items)}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("DELETE /api/queue failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# GET /api/profile
# ---------------------------------------------------------------------------
@app.route(route="profile", methods=["GET"])
def get_profile(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    try:
        profiles = get_profiles_container()
        doc = profiles.read_item(item=user_id, partition_key=user_id)

        # Heal stuck "generating" state if thread was lost (e.g. host recycle)
        if doc.get("rubricStatus") == "generating":
            started = doc.get("rubricGenerationStartedAt")
            if started:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(started)
                if age.total_seconds() > 300:  # 5 minutes
                    doc["rubricStatus"] = "error"
                    doc.pop("rubricGenerationStartedAt", None)
                    profiles.upsert_item(body=doc)

        return func.HttpResponse(json.dumps(doc), status_code=200, mimetype="application/json")
    except Exception as e:
        if "404" in str(e) or "NotFound" in str(e):
            return func.HttpResponse("{}", status_code=200, mimetype="application/json")
        logging.exception("GET /api/profile failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# PUT /api/profile
# ---------------------------------------------------------------------------
@app.route(route="profile", methods=["PUT"])
def put_profile(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        profiles = get_profiles_container()
        try:
            existing = profiles.read_item(item=user_id, partition_key=user_id)
        except Exception:
            existing = {"id": user_id, "userId": user_id}

        existing.update(body)
        existing["id"] = user_id
        existing["userId"] = user_id

        # Compute displayName
        parts = [p for p in [
            existing.get("roleTitle"),
            existing.get("location"),
            "2026",
        ] if p]
        existing["displayName"] = " · ".join(parts) if parts else "Job Role Evaluator"

        profiles.upsert_item(body=existing)
        return func.HttpResponse(json.dumps(existing), status_code=200, mimetype="application/json")
    except Exception as e:
        logging.exception("PUT /api/profile failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# Rubric validation (shared by generate and manual-edit endpoints)
# ---------------------------------------------------------------------------
def _validate_rubric(rubric):
    weight_sum = sum(s["weight"] for s in rubric.get("sections", []))
    if weight_sum != 100:
        raise ValueError(f"Weights sum to {weight_sum}, expected 100")
    for s in rubric["sections"]:
        if not s.get("id") or not s.get("title") or not s.get("items"):
            raise ValueError("Section missing required keys")
    if not rubric.get("knockouts"):
        raise ValueError("No knockouts defined")


# ---------------------------------------------------------------------------
# PUT /api/profile/rubric  — save a manually edited rubric
# ---------------------------------------------------------------------------
@app.route(route="profile/rubric", methods=["PUT"])
def save_rubric(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"

    try:
        rubric = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        _validate_rubric(rubric)
    except (ValueError, KeyError) as e:
        return func.HttpResponse(str(e), status_code=400)

    try:
        profiles = get_profiles_container()
        try:
            existing = profiles.read_item(item=user_id, partition_key=user_id)
        except Exception:
            return func.HttpResponse("Profile not found — save profile first", status_code=404)

        existing["rubric"] = rubric
        profiles.upsert_item(existing)
        return func.HttpResponse(status_code=200)
    except Exception as e:
        logging.exception("PUT /api/profile/rubric failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# POST /api/profile/generate-rubric
# ---------------------------------------------------------------------------
@app.route(route="profile/generate-rubric", methods=["POST"])
def generate_rubric(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"

    try:
        profiles = get_profiles_container()
        try:
            profile = profiles.read_item(item=user_id, partition_key=user_id)
        except Exception:
            return func.HttpResponse("Profile not found — save profile first", status_code=404)

        template = _load_yaml_prompt("generate_rubric.yaml")
        user_message = template["user"].format(
            role_title=profile.get("roleTitle", ""),
            location=profile.get("location", ""),
            currency_symbol=profile.get("currencySymbol", "£"),
            comp_min=profile.get("compMin", ""),
            comp_max=profile.get("compMax", ""),
            skills=profile.get("skills", ""),
            background_summary=profile.get("backgroundSummary", ""),
            career_goals=profile.get("careerGoals", ""),
            company_size_preference=profile.get("companySizePreference", ""),
            work_arrangement=profile.get("workArrangement", ""),
        )

        client = get_openai_client()
        deployment = os.environ["OPENAI_DEPLOYMENT"]

        # Mark as generating and return 202 immediately so the HTTP proxy
        # does not time out waiting for the LLM call to complete.
        profile["rubricStatus"] = "generating"
        profile["rubricGenerationStartedAt"] = datetime.now(timezone.utc).isoformat()
        profiles.upsert_item(body=profile)

        def _do_generation():
            try:
                def _call_llm():
                    return client.chat.completions.create(
                        model=deployment,
                        messages=[
                            {"role": "system", "content": template["system"]},
                            {"role": "user",   "content": user_message},
                        ],
                    )

                rubric = None
                for attempt in range(2):
                    try:
                        completion = _call_llm()
                        raw = completion.choices[0].message.content.strip()
                        rubric = json.loads(raw)
                        _validate_rubric(rubric)
                        break
                    except (json.JSONDecodeError, ValueError, KeyError) as e:
                        logging.warning("Rubric generation attempt %d failed: %s", attempt + 1, e)
                        if attempt == 1:
                            profile["rubricStatus"] = "error"
                            profiles.upsert_item(body=profile)
                            return

                profile["rubric"] = rubric
                profile["rubricGeneratedAt"] = datetime.now(timezone.utc).isoformat()
                profile["rubricStatus"] = "done"
                profiles.upsert_item(body=profile)
                logging.info("Rubric generated successfully for user %s", user_id)

            except Exception:
                logging.exception("Background rubric generation failed for user %s", user_id)
                try:
                    profile["rubricStatus"] = "error"
                    profiles.upsert_item(body=profile)
                except Exception:
                    pass

        threading.Thread(target=_do_generation, daemon=True).start()
        return func.HttpResponse(status_code=202)

    except Exception as e:
        logging.exception("POST /api/profile/generate-rubric failed")
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

    # Load profile + rubric from Cosmos; fall back to disk content.json
    profile = {}
    try:
        profiles = get_profiles_container()
        profile = profiles.read_item(item=user_id, partition_key=user_id)
    except Exception:
        pass  # no profile yet — use defaults

    if not profile.get("rubric"):
        return func.HttpResponse("No rubric found. Please generate a rubric from your profile first.", status_code=400)

    content = profile["rubric"]

    candidate_profile = _profile_to_candidate_string(profile) or None
    system_message, user_message = build_prompt(content, jd_text, candidate_profile)

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