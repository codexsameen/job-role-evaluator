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
from shared.db import get_entities_container, get_profiles_container, get_rubric, get_preferences

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
        sections_text += f"\n## Section {section['id']}: {section['title']} (weight: {section['weight']} pts, {len(section['items'])} items — scores array must have {len(section['items'])} elements)\n"
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

    section_ids = ", ".join(str(s["id"]) for s in content["sections"])

    template = _load_yaml_prompt("evaluate.yaml")
    user_message = template["user"].format(
        candidate_profile=candidate_profile,
        jd_text=jd_text,
        sections_text=sections_text,
        knockout_text=knockout_text,
        section_ids=section_ids,
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

_POSITIVE_STATUSES = {"applied", "phone-screen", "interview", "offer"}

def compute_bayes_weights(entries, rubric, prior_strength=15):
    """
    Dirichlet-style empirical Bayes update.
    Returns (bayes_weights_dict, observation_count).

    Prior: α_k = prior_strength * (rubric_weight_k / 100)
    Each positive entry adds its normalised section ratios to pos_sums;
    each negative entry adds 0.5× to neg_sums (asymmetric — negatives are
    weaker evidence since many are just exploratory dismissals).
    Posterior weight_k ∝ α_k + pos_k − 0.5·neg_k, floored at 0.1 to keep
    all sections active, then renormalised to sum=100.
    """
    sections    = rubric.get("sections", [])
    section_ids = [str(s["id"]) for s in sections]
    prior_alpha = {str(s["id"]): prior_strength * (s["weight"] / 100.0) for s in sections}

    pos_sums = {sid: 0.0 for sid in section_ids}
    neg_sums = {sid: 0.0 for sid in section_ids}
    n_pos = n_neg = 0

    for entry in entries:
        if entry.get("evalStatus") != "evaluated":
            continue
        scores   = entry.get("scores", {})
        interest = entry.get("interest", "")
        status   = entry.get("status", "")

        is_pos = interest == "interested" or status in _POSITIVE_STATUSES
        is_neg = interest == "not-interested" or status == "rejected"
        if not is_pos and not is_neg:
            continue

        ratios = {}
        for s in sections:
            sid       = str(s["id"])
            max_pts   = sum(item["max"] for item in s["items"])
            raw       = scores.get(sid, [])
            ratios[sid] = (sum(raw) / max_pts) if max_pts > 0 else 0.0

        if is_pos:
            n_pos += 1
            for sid in section_ids:
                pos_sums[sid] += ratios[sid]
        else:
            n_neg += 1
            for sid in section_ids:
                neg_sums[sid] += ratios[sid]

    posterior = {
        sid: max(prior_alpha[sid] + pos_sums[sid] - 0.5 * neg_sums[sid], 0.1)
        for sid in section_ids
    }
    total_p = sum(posterior.values())
    weights = {sid: round((v / total_p) * 100, 1) for sid, v in posterior.items()}

    # Fix floating-point rounding so weights sum to exactly 100
    diff = round(100.0 - sum(weights.values()), 1)
    if diff:
        largest = max(weights, key=lambda k: weights[k])
        weights[largest] = round(weights[largest] + diff, 1)

    return weights, n_pos + n_neg


def compute_scores(content, raw_scores):
    weighted = {}
    clamped_scores = {}
    for section in content["sections"]:
        sid        = str(section["id"])
        items      = section["items"]
        max_w      = section["weight"]
        raw        = raw_scores.get(sid, [])
        clamped    = [max(0, min(int(raw[i]), items[i]["max"])) if i < len(raw) else 0
                      for i in range(len(items))]
        clamped_scores[sid] = clamped
        actual_max  = sum(item["max"] for item in items)
        section_sum = sum(clamped)
        weighted[sid] = round((section_sum / actual_max) * max_w) if actual_max > 0 else 0

    total = sum(weighted.values())
    return weighted, total, clamped_scores


# ---------------------------------------------------------------------------
# Fixed rubric dimensions — IDs, titles, and weights are immutable.
# The LLM personalises only the items and signals within each section.
# ---------------------------------------------------------------------------
FIXED_SECTIONS = {
    1: ("Compensation & Benefits", 30),
    2: ("Role & Technical Fit",    25),
    3: ("Growth & Learning",       20),
    4: ("Culture & Team",          15),
    5: ("Work Arrangement",        10),
}


# ---------------------------------------------------------------------------
# Rubric validation (shared by generate and manual-edit endpoints)
# ---------------------------------------------------------------------------
def _validate_rubric(rubric):
    sections = rubric.get("sections", [])
    if len(sections) != len(FIXED_SECTIONS):
        raise ValueError(f"Expected {len(FIXED_SECTIONS)} sections, got {len(sections)}")
    for s in sections:
        sid = s.get("id")
        expected = FIXED_SECTIONS.get(sid)
        if not expected:
            raise ValueError(f"Unexpected section id {sid}")
        exp_title, exp_weight = expected
        if s.get("title") != exp_title:
            raise ValueError(f"Section {sid} title must be '{exp_title}'")
        if s.get("weight") != exp_weight:
            raise ValueError(f"Section {sid} weight must be {exp_weight}")
        if not s.get("items"):
            raise ValueError(f"Section {sid} has no items")
    if not rubric.get("knockouts"):
        raise ValueError("No knockouts defined")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _patch_role_item(user_id: str, role_id: str, patch: dict):
    """Read a Role document, apply patch, write back. Validates type == 'role'."""
    entities = get_entities_container()
    item = entities.read_item(item=role_id, partition_key=user_id)
    if item.get("type") != "role":
        raise ValueError(f"Document {role_id} is not a role (type={item.get('type')})")
    item.update(patch)
    entities.replace_item(item=role_id, body=item)
    return item

def _join_roles_and_apps(items):
    """Merge role + application documents into flat joined objects."""
    roles = {doc["id"]: doc for doc in items if doc.get("type") == "role"}
    apps  = {doc["roleId"]: doc for doc in items if doc.get("type") == "application"}
    result = []
    for role_id, role in roles.items():
        app = apps.get(role_id, {})
        result.append({**role, **app, "id": role_id})
    return result

_PROFILE_ALLOWED_FIELDS = {
    "roleTitle", "location", "skills", "backgroundSummary", "careerGoals",
    "companySizePreference", "workArrangement", "compMin", "compMax",
    "currencySymbol", "evalTimings", "rubricTimings",
}

_APPLICATION_ALLOWED_FIELDS = {
    "interest", "interestHistory", "status", "statusHistory", "notes", "addedAt",
}


app = func.FunctionApp()


# ---------------------------------------------------------------------------
# GET /api/profile
# ---------------------------------------------------------------------------
@app.route(route="profile", methods=["GET"])
def get_profile(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    try:
        profiles = get_profiles_container()
        try:
            doc = profiles.read_item(item=user_id, partition_key=user_id)
        except Exception as e:
            if "404" in str(e) or "NotFound" in str(e):
                return func.HttpResponse("{}", status_code=200, mimetype="application/json")
            raise

        # Merge preferences (bayes weights) into the response
        prefs = get_preferences(user_id)
        if prefs:
            doc["bayesWeights"]          = prefs.get("bayesWeights")
            doc["bayesObservationCount"] = prefs.get("bayesObservationCount", 0)

        return func.HttpResponse(json.dumps(doc), status_code=200, mimetype="application/json")
    except Exception as e:
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

    disallowed = set(body.keys()) - _PROFILE_ALLOWED_FIELDS
    if disallowed:
        return func.HttpResponse(
            f"Fields not allowed on profile: {', '.join(sorted(disallowed))}",
            status_code=400,
        )

    try:
        profiles = get_profiles_container()
        try:
            existing = profiles.read_item(item=user_id, partition_key=user_id)
        except Exception:
            existing = {"id": user_id, "userId": user_id, "type": "profile"}

        existing.update(body)
        existing["id"]        = user_id
        existing["userId"]    = user_id
        existing["type"]      = "profile"
        existing["updatedAt"] = datetime.now(timezone.utc).isoformat()

        # Compute displayName server-side
        year = str(datetime.now(timezone.utc).year)
        parts = [p for p in [existing.get("roleTitle"), existing.get("location"), year] if p]
        existing["displayName"] = " · ".join(parts) if parts else "Job Role Evaluator"

        profiles.upsert_item(body=existing)
        return func.HttpResponse(json.dumps(existing), status_code=200, mimetype="application/json")
    except Exception as e:
        logging.exception("PUT /api/profile failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# GET /api/rubric
# ---------------------------------------------------------------------------
@app.route(route="rubric", methods=["GET"])
def get_rubric_handler(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    try:
        doc = get_rubric(user_id)
        if doc is None:
            return func.HttpResponse("{}", status_code=200, mimetype="application/json")

        # Heal stuck "generating" state if thread was lost (e.g. host recycle)
        if doc.get("status") == "generating":
            started = doc.get("generationStartedAt")
            if started:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(started)
                if age.total_seconds() > 300:  # 5 minutes
                    doc["status"] = "error"
                    doc.pop("generationStartedAt", None)
                    get_entities_container().upsert_item(body=doc)

        return func.HttpResponse(json.dumps(doc), status_code=200, mimetype="application/json")
    except Exception as e:
        logging.exception("GET /api/rubric failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# PUT /api/rubric  — save a manually edited rubric
# ---------------------------------------------------------------------------
@app.route(route="rubric", methods=["PUT"])
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
        entities = get_entities_container()
        doc = {
            "id":          f"rubric-{user_id}",
            "userId":      user_id,
            "type":        "rubric",
            "status":      "done",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "meta":        rubric.get("meta", {}),
            "sections":    rubric.get("sections", []),
            "knockouts":   rubric.get("knockouts", []),
        }
        entities.upsert_item(doc)
        return func.HttpResponse(status_code=200)
    except Exception as e:
        logging.exception("PUT /api/rubric failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# POST /api/rubric/generate
# ---------------------------------------------------------------------------
@app.route(route="rubric/generate", methods=["POST"])
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

        client     = get_openai_client()
        deployment = os.environ["OPENAI_DEPLOYMENT"]
        entities   = get_entities_container()
        now        = datetime.now(timezone.utc).isoformat()

        # Write "generating" state immediately, return 202
        rubric_doc = {
            "id":                  f"rubric-{user_id}",
            "userId":              user_id,
            "type":                "rubric",
            "status":              "generating",
            "generationStartedAt": now,
        }
        entities.upsert_item(body=rubric_doc)

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
                            rubric_doc["status"] = "error"
                            entities.upsert_item(body=rubric_doc)
                            return

                rubric_doc.update({
                    "status":      "done",
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "meta":        rubric.get("meta", {}),
                    "sections":    rubric.get("sections", []),
                    "knockouts":   rubric.get("knockouts", []),
                })
                rubric_doc.pop("generationStartedAt", None)
                entities.upsert_item(body=rubric_doc)
                logging.info("Rubric generated successfully for user %s", user_id)

            except Exception:
                logging.exception("Background rubric generation failed for user %s", user_id)
                try:
                    rubric_doc["status"] = "error"
                    entities.upsert_item(body=rubric_doc)
                except Exception:
                    pass

        threading.Thread(target=_do_generation, daemon=True).start()
        return func.HttpResponse(status_code=202)

    except Exception as e:
        logging.exception("POST /api/rubric/generate failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# GET /api/roles
# ---------------------------------------------------------------------------
@app.route(route="roles", methods=["GET"])
def get_roles(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    try:
        entities = get_entities_container()
        params   = [{"name": "@userId", "value": user_id}]
        roles = list(entities.query_items(
            query="SELECT TOP 500 * FROM c WHERE c.userId = @userId AND c.type = 'role' ORDER BY c.createdAt DESC",
            parameters=params,
        ))
        apps = list(entities.query_items(
            query="SELECT * FROM c WHERE c.userId = @userId AND c.type = 'application'",
            parameters=params,
        ))
        joined = _join_roles_and_apps(roles + apps)
        return func.HttpResponse(
            json.dumps(joined),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("GET /api/roles failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# POST /api/roles
# ---------------------------------------------------------------------------
@app.route(route="roles", methods=["POST"])
def post_role(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    now     = datetime.now(timezone.utc).isoformat()
    role_id = str(uuid.uuid4())

    # Split incoming payload into role and application fields
    app_fields = {k: body[k] for k in _APPLICATION_ALLOWED_FIELDS if k in body}
    role_fields = {k: v for k, v in body.items() if k not in _APPLICATION_ALLOWED_FIELDS}

    role_doc = {
        "id":        role_id,
        "userId":    user_id,
        "type":      "role",
        "createdAt": now,
        **role_fields,
    }
    app_doc = {
        "id":      f"app-{role_id}",
        "userId":  user_id,
        "type":    "application",
        "roleId":  role_id,
        "addedAt": now,
        **app_fields,
    }

    try:
        entities = get_entities_container()
        entities.execute_item_batch(
            batch_operations=[
                ("create", role_doc, {}),
                ("create", app_doc,  {}),
            ],
            partition_key=user_id,
        )
        return func.HttpResponse(
            json.dumps({**role_doc, **app_doc, "id": role_id}),
            status_code=201,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("POST /api/roles failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# GET /api/roles/{id}
# ---------------------------------------------------------------------------
@app.route(route="roles/{id}", methods=["GET"])
def get_role(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    role_id = req.route_params.get("id")
    try:
        entities = get_entities_container()
        role = entities.read_item(item=role_id, partition_key=user_id)
        try:
            application = entities.read_item(item=f"app-{role_id}", partition_key=user_id)
        except Exception:
            application = {}
        joined = {**role, **application, "id": role_id}
        return func.HttpResponse(json.dumps(joined), status_code=200, mimetype="application/json")
    except Exception as e:
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Role not found", status_code=404)
        logging.exception("GET /api/roles/{id} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# PATCH /api/roles/{id}  — eval lifecycle fields only (evalStatus)
# ---------------------------------------------------------------------------
@app.route(route="roles/{id}", methods=["PATCH"])
def patch_role(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    role_id = req.route_params.get("id")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    # Only evalStatus is writable via this endpoint
    allowed = {"evalStatus"}
    disallowed = set(body.keys()) - allowed
    if disallowed:
        return func.HttpResponse(
            f"Fields not allowed on role via PATCH: {', '.join(sorted(disallowed))}",
            status_code=400,
        )

    try:
        item = _patch_role_item(user_id, role_id, body)
        return func.HttpResponse(json.dumps(item), status_code=200, mimetype="application/json")
    except Exception as e:
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Role not found", status_code=404)
        logging.exception("PATCH /api/roles/{id} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# PATCH /api/applications/{roleId}  — user-facing mutable fields only
# ---------------------------------------------------------------------------
@app.route(route="applications/{roleId}", methods=["PATCH"])
def patch_application(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    role_id = req.route_params.get("roleId")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    disallowed = set(body.keys()) - _APPLICATION_ALLOWED_FIELDS
    if disallowed:
        return func.HttpResponse(
            f"Fields not allowed on application: {', '.join(sorted(disallowed))}",
            status_code=400,
        )

    try:
        entities = get_entities_container()
        app_id   = f"app-{role_id}"
        item     = entities.read_item(item=app_id, partition_key=user_id)
        item.update(body)
        entities.replace_item(item=app_id, body=item)
        return func.HttpResponse(json.dumps(item), status_code=200, mimetype="application/json")
    except Exception as e:
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Application not found", status_code=404)
        logging.exception("PATCH /api/applications/{roleId} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# DELETE /api/roles/{id}
# ---------------------------------------------------------------------------
@app.route(route="roles/{id}", methods=["DELETE"])
def delete_role(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"
    role_id = req.route_params.get("id")

    if not role_id:
        return func.HttpResponse("Missing role id", status_code=400)

    try:
        entities = get_entities_container()
        entities.execute_item_batch(
            batch_operations=[
                ("delete", role_id,          {}),
                ("delete", f"app-{role_id}", {}),
            ],
            partition_key=user_id,
        )
        return func.HttpResponse(status_code=204)
    except Exception as e:
        err = str(e)
        if "404" in err or "NotFound" in err:
            return func.HttpResponse("Role not found", status_code=404)
        logging.exception("DELETE /api/roles/{id} failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# DELETE /api/roles
# ---------------------------------------------------------------------------
@app.route(route="roles", methods=["DELETE"])
def delete_roles(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"

    try:
        entities = get_entities_container()
        query    = "SELECT c.id FROM c WHERE c.userId = @userId AND c.type IN ('role', 'application')"
        params   = [{"name": "@userId", "value": user_id}]
        items    = list(entities.query_items(query=query, parameters=params))

        # Batch in chunks of 50 (role + app pairs fit within the 100-op limit)
        BATCH_SIZE = 50
        for i in range(0, len(items), BATCH_SIZE):
            chunk      = items[i:i + BATCH_SIZE]
            operations = [("delete", item["id"], {}) for item in chunk]
            entities.execute_item_batch(batch_operations=operations, partition_key=user_id)

        return func.HttpResponse(
            json.dumps({"deleted": len(items)}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("DELETE /api/roles failed")
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
    role_id = body.get("role_id", "").strip()

    if not jd_text:
        return func.HttpResponse("jd_text is required", status_code=400)
    if not role_id:
        return func.HttpResponse("role_id is required", status_code=400)

    # Load rubric from entities
    rubric = get_rubric(user_id)
    if not rubric or rubric.get("status") != "done":
        return func.HttpResponse(
            "No rubric found. Please generate a rubric from your profile first.",
            status_code=400,
        )

    # Load profile for candidate context
    profile = {}
    try:
        profile = get_profiles_container().read_item(item=user_id, partition_key=user_id)
    except Exception:
        pass

    content           = rubric
    candidate_profile = _profile_to_candidate_string(profile) or None
    system_message, user_message = build_prompt(content, jd_text, candidate_profile)

    client     = get_openai_client()
    deployment = os.environ["OPENAI_DEPLOYMENT"]

    def _do_evaluation():
        try:
            completion = client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user",   "content": user_message},
                ],
            )
            raw_text = completion.choices[0].message.content.strip()
            logging.info("Model raw output: %s", raw_text)

            try:
                model_output = json.loads(raw_text)
            except json.JSONDecodeError:
                logging.error("Model returned non-JSON: %s", raw_text)
                _patch_role_item(user_id, role_id, {"evalStatus": "error"})
                return

            raw_scores    = model_output.get("scores", {})
            reasoning     = model_output.get("reasoning", {})
            knockouts     = model_output.get("knockouts", [])
            company       = model_output.get("company", "Unknown")
            role          = model_output.get("role", "Unknown")
            comp_min      = model_output.get("comp_min")
            comp_max      = model_output.get("comp_max")
            comp_currency = model_output.get("comp_currency")

            expected_ids = {str(s["id"]) for s in content["sections"]}
            actual_ids   = set(raw_scores.keys())
            if expected_ids != actual_ids:
                logging.error("Score key mismatch: expected %s, got %s", expected_ids, actual_ids)
                _patch_role_item(user_id, role_id, {"evalStatus": "error"})
                return

            weighted, total, clamped_scores = compute_scores(content, raw_scores)

            if total == 0:
                logging.warning("Total score is 0 for %s — %s. Possible scoring failure.", company, role)

            patch = {
                "company":      company,
                "role":         role,
                "url":          url,
                "total":        total,
                "weighted":     weighted,
                "scores":       clamped_scores,
                "reasoning":    reasoning,
                "knockouts":    knockouts,
                "compMin":      comp_min,
                "compMax":      comp_max,
                "compCurrency": comp_currency,
                "evalStatus":   "evaluated",
                "evaluatedAt":  datetime.now(timezone.utc).isoformat(),
            }
            _patch_role_item(user_id, role_id, patch)
            logging.info("Evaluation complete for role %s", role_id)

        except Exception:
            logging.exception("Background evaluation failed for role %s", role_id)
            try:
                _patch_role_item(user_id, role_id, {"evalStatus": "error"})
            except Exception:
                pass

    threading.Thread(target=_do_evaluation, daemon=True).start()
    return func.HttpResponse(status_code=202)


# ---------------------------------------------------------------------------
# POST /api/preferences/bayes-update
# ---------------------------------------------------------------------------
@app.route(route="preferences/bayes-update", methods=["POST"])
def bayes_update(req: func.HttpRequest) -> func.HttpResponse:
    user_id = get_user_id(req) or "local-dev-user"

    try:
        rubric = get_rubric(user_id)
        if not rubric or rubric.get("status") != "done":
            return func.HttpResponse("No rubric found", status_code=400)

        prefs          = get_preferences(user_id)
        prior_strength = prefs.get("bayesPriorStrength", 15)

        entities = get_entities_container()

        roles_query = (
            "SELECT * FROM c WHERE c.userId = @userId "
            "AND c.type = 'role' AND c.evalStatus = 'evaluated'"
        )
        apps_query = "SELECT * FROM c WHERE c.userId = @userId AND c.type = 'application'"
        params     = [{"name": "@userId", "value": user_id}]

        roles = list(entities.query_items(query=roles_query, parameters=params))
        apps  = list(entities.query_items(query=apps_query,  parameters=params))

        apps_by_role = {a["roleId"]: a for a in apps}
        entries      = [{**r, **apps_by_role.get(r["id"], {})} for r in roles]

        bayes_weights, obs_count = compute_bayes_weights(entries, rubric, prior_strength)

        prefs_doc = {
            "id":                    f"prefs-{user_id}",
            "userId":                user_id,
            "type":                  "preferences",
            "bayesWeights":          bayes_weights,
            "bayesObservationCount": obs_count,
            "bayesPriorStrength":    prior_strength,
            "updatedAt":             datetime.now(timezone.utc).isoformat(),
        }
        get_profiles_container().upsert_item(prefs_doc)

        return func.HttpResponse(
            json.dumps({"bayesWeights": bayes_weights, "bayesObservationCount": obs_count}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("POST /api/preferences/bayes-update failed")
        return func.HttpResponse(f"Internal server error: {e}", status_code=500)
