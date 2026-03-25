# job-role-evaluator

> AI-powered job description evaluator that scores roles against your personal priorities

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)
![Azure Functions](https://img.shields.io/badge/Azure_Functions-serverless-0078D4?logo=azure-functions&logoColor=white)
![Azure Static Web Apps](https://img.shields.io/badge/Azure_Static_Web_Apps-deployed-0078D4?logo=microsoft-azure&logoColor=white)
![Built with love](https://forthebadge.com/badges/built-with-love.svg)

<!-- demo gif -->

---

## Features

- **Personalised rubric** — generate a custom scoring rubric from your profile: target role, location, skills, compensation expectations, career goals, and preferred work arrangement
- **Paste or fetch JDs** — submit job descriptions directly or scrape them from a URL (LinkedIn-aware)
- **AI scoring** — per-section breakdown with knock-out detection; each dimension is scored and reasoned over by GPT
- **Pipeline management** — sort and filter evaluations by verdict badge, interest level, or application status
- **Interest & status tracking** — mark interest levels and track application workflow with full history timelines
- **Bayesian learning** — section weights rebalance automatically based on which roles you pursue
- **Dark/light theme** — responsive layout with iPhone safe-area support
- **Per-user isolation** — GitHub OAuth via Azure Static Web Apps; every user's data is partitioned in Cosmos DB

---

## Architecture

```mermaid
flowchart TD
    Browser["Browser — Vanilla JS SPA"]

    subgraph SWA["Azure Static Web Apps"]
        Static["Static Assets"]
        Auth["GitHub OAuth"]
        Proxy["API Proxy /api/*"]
    end

    subgraph Functions["Azure Functions — Python"]
        Profile["Profile + Preferences"]
        Rubric["Rubric Generation (async)"]
        Roles["Roles + Applications CRUD"]
        Fetch["JD Fetch + Scrape"]
        Evaluate["Evaluate vs Rubric (async)"]
        Bayes["Bayesian Weight Update"]
    end

    CosmosDB[("Cosmos DB NoSQL — profiles + entities")]

    Browser  -->|HTTPS request| SWA
    Static   -.->|serve assets| Browser
    Auth     -.->|OAuth redirect| Browser
    Proxy    -->|forward| Functions
    Profile  <-->|read / write| CosmosDB
    Rubric   <-->|read / write| CosmosDB
    Roles    <-->|read / write| CosmosDB
    Evaluate <-->|read / write| CosmosDB
    Bayes    <-->|read / write| CosmosDB

    classDef client   fill:#1f2937,stroke:#6b7280,color:#f9fafb
    classDef hosting  fill:#1e40af,stroke:#1d4ed8,color:#dbeafe
    classDef compute  fill:#5b21b6,stroke:#6d28d9,color:#ede9fe
    classDef storage  fill:#065f46,stroke:#047857,color:#d1fae5

    class Browser client
    class Static,Auth,Proxy hosting
    class Profile,Rubric,Roles,Fetch,Evaluate,Bayes compute
    class CosmosDB storage
```

---

## Tech Stack

| Layer      | Technology                                     |
|------------|------------------------------------------------|
| Frontend   | Vanilla HTML/CSS/JS (no build step)            |
| Backend    | Python 3.9, Azure Functions (serverless)       |
| AI         | Azure OpenAI (GPT-5-mini)                      |
| Database   | Azure Cosmos DB (NoSQL)                        |
| Auth       | GitHub OAuth via Azure Static Web Apps         |
| Hosting    | Azure Static Web Apps                          |
| CI/CD      | GitHub Actions                                 |

---

## Data Model

Two Cosmos containers, both partitioned by `/userId`:

| Container  | Document types | id pattern |
|------------|---------------|------------|
| `profiles` | Profile       | `<userId>` |
| `profiles` | Preferences   | `prefs-<userId>` |
| `entities` | Rubric        | `rubric-<userId>` |
| `entities` | Role          | `<uuid>` |
| `entities` | Application   | `app-<uuid>` |

**Profile** — identity and preferences (role title, location, skills, compensation range, etc.)

**Preferences** — learned Bayes weights derived from interest/status history; written only by the bayes-update endpoint

**Rubric** — AI-generated scoring rubric (5 fixed sections, personalised items); has its own generation lifecycle (`status: generating | done | error`)

**Role** — the job opportunity and AI evaluation output (JD text, scores, reasoning, knock-out flags); immutable after `evalStatus = evaluated`

**Application** — mutable user state layered on a role (interest level + history, pipeline status + history, notes)

---

## Data Flow

```mermaid
sequenceDiagram
    actor User
    participant SPA as Browser (SPA)
    participant API as Azure Functions
    participant AI as OpenAI
    participant DB as Cosmos DB

    User->>SPA: Visit app
    SPA->>SPA: Redirect to GitHub OAuth
    User->>SPA: Authenticated

    User->>SPA: Fill in profile
    SPA->>API: PUT /api/profile
    API->>DB: Save profile

    User->>SPA: Generate rubric
    SPA->>API: POST /api/rubric/generate
    API-->>SPA: 202 Accepted (async)
    API->>DB: Save rubric {status: generating}
    loop Poll until ready
        SPA->>API: GET /api/rubric
        API-->>SPA: rubric status
    end
    API->>AI: Generate rubric prompt
    AI-->>API: Rubric JSON
    API->>DB: Save rubric {status: done}

    User->>SPA: Paste / fetch JD
    SPA->>API: POST /api/fetch-jd (optional)
    API-->>SPA: Extracted JD text
    SPA->>API: POST /api/roles
    API->>DB: Create Role + Application
    SPA->>API: POST /api/evaluate
    API-->>SPA: 202 Accepted (async)
    API->>AI: Evaluate JD vs rubric
    AI-->>API: Scored result
    API->>DB: Patch role {evalStatus: evaluated}
    loop Poll until ready
        SPA->>API: GET /api/roles/{id}
        API-->>SPA: joined role + application
    end

    SPA->>User: Pipeline table + detail drawer

    User->>SPA: Mark interest
    SPA->>API: PATCH /api/applications/{roleId}
    SPA->>API: POST /api/preferences/bayes-update
    API->>DB: Update preferences
```

---

## Project Structure

```
job-role-evaluator/
├── index.html                   # Single-page app shell
├── app.js                       # All frontend logic (~1 500 lines)
├── styles.css                   # Design tokens, dark/light themes, responsive layout
├── serve.py                     # Local Flask dev server (proxies /api/* to Functions)
├── staticwebapp.config.json     # SWA routing, auth rules, cache headers
├── content.json                 # Default rubric content (reference only)
│
└── api/                         # Azure Functions backend
    ├── function_app.py          # All route handlers (14 endpoints)
    ├── migrate_v2.py            # One-time migration script (v1 → v2 data model)
    ├── requirements.txt         # Python dependencies
    ├── host.json                # Functions runtime config
    ├── local.settings.json      # Local env vars (never commit — contains secrets)
    ├── shared/
    │   ├── auth.py              # Extract user ID from SWA principal header
    │   └── db.py                # Cosmos DB client + per-entity helpers
    └── prompts/
        ├── evaluate.yaml        # System + user prompts for JD evaluation
        └── generate_rubric.yaml # System + user prompts for rubric generation
```

---

## Local Development

### Prerequisites

- Python 3.9+
- [Azure Functions Core Tools v4](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- An Azure Cosmos DB account (or the [emulator](https://learn.microsoft.com/en-us/azure/cosmos-db/local-emulator))
- An Azure OpenAI resource with a `gpt-5-mini` deployment (or compatible)

### Steps

1. **Clone the repo**

   ```bash
   git clone https://github.com/<your-org>/job-role-evaluator.git
   cd job-role-evaluator
   ```

2. **Install Python dependencies**

   ```bash
   pip install -r api/requirements.txt
   ```

3. **Configure environment variables**

   Copy `api/local.settings.json` and fill in your credentials:

   ```json
   {
     "IsEncrypted": false,
     "Values": {
       "FUNCTIONS_WORKER_RUNTIME": "python",
       "AzureWebJobsStorage": "",
       "COSMOS_CONNECTION_STRING": "<your-cosmos-connection-string>",
       "OPENAI_ENDPOINT": "<your-azure-openai-endpoint>",
       "OPENAI_API_KEY": "<your-api-key>",
       "OPENAI_DEPLOYMENT": "gpt-5-mini"
     }
   }
   ```

   > **Never commit `local.settings.json`** — it contains secrets.

4. **Start the Azure Functions backend** (in a separate terminal)

   ```bash
   cd api
   func start
   ```

5. **Start the frontend dev server**

   ```bash
   python serve.py
   ```

6. **Open** `http://localhost:8080`

   The Flask dev server serves static files and proxies `/api/*` to the locally running Functions host at `http://localhost:7071`.

---

## API Reference

### Profile

| Method  | Path           | Description                                                        |
|---------|----------------|--------------------------------------------------------------------|
| `GET`   | `/api/profile` | Fetch profile; includes merged Bayes weights from Preferences      |
| `PUT`   | `/api/profile` | Save or update profile fields (rubric/Bayes fields rejected)       |

### Rubric

| Method  | Path                   | Description                                          |
|---------|------------------------|------------------------------------------------------|
| `GET`   | `/api/rubric`          | Fetch rubric document (includes generation status)   |
| `PUT`   | `/api/rubric`          | Save a manually edited rubric                        |
| `POST`  | `/api/rubric/generate` | Kick off async rubric generation (returns 202)       |

### Roles

| Method   | Path              | Description                                                         |
|----------|-------------------|---------------------------------------------------------------------|
| `GET`    | `/api/roles`      | Fetch all roles (up to 500, sorted by date); returns joined objects |
| `POST`   | `/api/roles`      | Create a new Role + Application atomically                          |
| `GET`    | `/api/roles/{id}` | Fetch a single role (joined with its application)                   |
| `PATCH`  | `/api/roles/{id}` | Update eval lifecycle fields only (`evalStatus`)                    |
| `DELETE` | `/api/roles/{id}` | Delete a role and its application                                   |
| `DELETE` | `/api/roles`      | Delete all roles and applications for the current user              |

### Applications

| Method  | Path                          | Description                                              |
|---------|-------------------------------|----------------------------------------------------------|
| `PATCH` | `/api/applications/{roleId}`  | Update user-facing fields: interest, status, notes       |

### Misc

| Method  | Path                              | Description                                            |
|---------|-----------------------------------|--------------------------------------------------------|
| `POST`  | `/api/fetch-jd`                   | Scrape a job description from a URL                    |
| `POST`  | `/api/evaluate`                   | Evaluate a JD against the rubric (returns 202)         |
| `POST`  | `/api/preferences/bayes-update`   | Recompute section weights from interest/status history |

All endpoints read the authenticated user identity from the `x-ms-client-principal` header injected by Azure Static Web Apps. In local development the user ID defaults to `local-dev-user`.

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Commit your changes: `git commit -m "feat: describe your change"`
4. Push and open a Pull Request against `main`

Please keep PRs focused — one feature or fix per PR. Include a short description of _why_ the change is needed.

---

_Made with ❤️ from London_
