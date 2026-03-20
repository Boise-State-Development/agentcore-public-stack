<div align="center">

<!-- IMAGE PLACEHOLDER: Animated GIF or hero banner showing the AgentCore chat interface in action — a user asking a question, the agent streaming a response, and a tool being invoked (e.g., Code Interpreter generating a chart). Dimensions: ~800x400px, optimized for GitHub rendering. -->
<!-- Example: ![AgentCore Demo](docs/images/hero-demo.gif) -->

# 🤖 AgentCore Public Stack

**An open-source, production-ready Generative AI platform for institutions**
*Built by Boise State University, designed for everyone.*

[![1. Deploy Infrastructure (VPC, ALB, ECS)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/infrastructure.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/infrastructure.yml)
[![2. Deploy RAG Ingestion](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/rag-ingestion.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/rag-ingestion.yml)
[![2. Deploy SageMaker Fine-Tuning (Optional)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/sagemaker-fine-tuning.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/sagemaker-fine-tuning.yml)
[![3. Deploy Inference API (AgentCore Runtime)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/inference-api.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/inference-api.yml)
[![4. Deploy App API (Backend)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/app-api.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/app-api.yml)
[![5. Deploy Frontend (CloudFront)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend.yml)
[![5. Deploy Gateway (Lambda Tools)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/gateway.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/gateway.yml)
[![6. Seed Bootstrap Data](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml)

![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat&logo=python&logoColor=white)
![Angular](https://img.shields.io/badge/Angular-v21-DD0031?style=flat&logo=angular&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-Bedrock_AgentCore-FF9900?style=flat&logo=amazonwebservices&logoColor=white)
![Tailwind](https://img.shields.io/badge/Tailwind_CSS-v4.1-06B6D4?style=flat&logo=tailwindcss&logoColor=white)
![License](https://img.shields.io/badge/License-PolyForm_Noncommercial-blue?style=flat)

[Features](#-key-features) · [Architecture](#-architecture) · [Admin Dashboard](#-admin-dashboard) · [Deployment](#-deployment) · [Contributing](#-contributing)

</div>

---

> 🚀 **Ready to deploy?** Fork the repo and follow the [**GitHub Actions Quick Start**](.github/README-ACTIONS.md) to go from zero to an AWS environment in under an hour.

---

<!-- IMAGE PLACEHOLDER: Screenshot of the chat interface showing a multi-turn conversation with the AI assistant. The screenshot should show: (1) a user message with an uploaded image or document, (2) the assistant streaming a response with rich markdown formatting, and (3) a tool invocation panel (e.g., Code Interpreter or web search). Dimensions: ~900x500px. -->
<!-- Example: ![Chat Interface](docs/images/chat-interface.png) -->

## 🤔 What Is This?

AgentCore Public Stack is the working codebase behind [boisestate.ai](https://boisestate.ai), a full-stack AI assistant platform serving students, faculty, and staff at Boise State University. It combines **AWS Bedrock AgentCore** and **Strands Agents** into a turnkey system that any institution can fork, deploy, and make their own — without writing a single line of agent code.

The platform ships with a modern chat interface, a full admin dashboard, multi-model support, tool and MCP server management, quota enforcement, cost tracking, and role-based access control — all configurable through the UI.

---

## 🎯 Why Boise State Built This

Most institutions face the same AI dilemma: vendor-hosted per-seat subscriptions don't scale, and building from scratch is too expensive.

| The Problem | Our Answer |
|-------------|------------|
| 💸 **Per-seat pricing doesn't scale** | Consumption-based billing — pay only for tokens used, not seats purchased |
| 🔐 **Student data leaves your control** | All data stays inside your AWS account. Bedrock model interactions are never shared with vendors |
| 🔒 **Vendor lock-in limits flexibility** | Swap models freely — Claude, Llama, Mistral, GPT, Gemini — no contracts, no lock-in |
| 🧩 **Fragmented tool ecosystems** | One platform with MCP servers connecting Canvas, Google Workspace, PeopleSoft, and more |
| ⚖️ **Inequitable access** | Every student gets the same models and tools — no premium tiers required |

At 30,000 users, commercial subscriptions would cost upwards of **$7.2M per year**. This platform delivers the same (and more) capabilities at a fraction of the cost.

---

## ✨ Key Features

### 🔄 Model Flexibility

Add, swap, or disable AI models without redeploying. The platform supports **any model available through AWS Bedrock** — Claude, Llama, Mistral, and more — as well as **external providers** like OpenAI and Google Gemini via API. Administrators configure models through the dashboard, set per-model pricing, and control which roles have access. When a better model launches, add it in minutes.

### 💰 Cost-Effectiveness

Every interaction is tracked at the token level. Automatic **prompt caching** reduces repeated input costs. **Turn-based message buffering** cuts memory API calls by 75%. Quota tiers let administrators set spending limits by role, department, or individual — with soft warnings before hard stops. Real-time cost analytics surface exactly where budget is going.

### 🛡️ Security and Data Privacy

Your data never leaves your AWS account. The platform uses **OIDC authentication** (Entra ID, Cognito, Google) with PKCE, **role-based access control** at every layer, and **SigV4-signed requests** for all MCP tool communication. There are no external data pipelines, no third-party analytics, and no model training on your interactions.

### 🔌 Independent MCP and Tool Deployment

Connect institutional systems through **MCP (Model Context Protocol) servers** — no code changes, no redeployment. Register an MCP server URL in the admin dashboard and its tools are automatically discovered, assigned to roles, and made available to users. Deploy MCP servers on your own schedule, on your own infrastructure, completely independent of the core platform.

**Example integrations:**
- 📚 **Canvas LMS** — Course materials, grades, assignments
- 📁 **Google Workspace** — Drive search, Docs, Calendar
- 🎓 **PeopleSoft** — Student records, registration
- 🔍 **Library Systems** — Research databases, catalog search
- 🔧 **Any custom API** — Wrap it in an MCP server and plug it in

### 🖼️ Multimodal Capabilities

Users can upload images (PNG, JPEG, GIF, WebP) and documents (PDF, CSV, DOCX) directly into conversations. The agent can generate charts and diagrams. More features to come.

<!-- IMAGE PLACEHOLDER: Side-by-side screenshot showing multimodal capabilities — on the left, a user uploading a PDF document and asking the agent to summarize it; on the right, the agent using Code Interpreter to generate a matplotlib chart from uploaded CSV data. Dimensions: ~900x400px. -->
<!-- Example: ![Multimodal Capabilities](docs/images/multimodal-demo.png) -->

### 🧪 Fine-Tuning

Train and run inference on open-source models directly from the platform. Users with admin-granted access can upload datasets, launch **SageMaker training jobs** on models like BERT, RoBERTa, GPT-2, and more, then run **batch inference** on trained models — all with real-time progress tracking, quota enforcement, and automatic 30-day artifact retention. No ML infrastructure setup required.

### 🧠 Memory and Context

A two-tier memory system combines **short-term session history** with **long-term user preferences**. The agent remembers coding style preferences, language choices, and learned facts across sessions — personalization without compromising privacy.

---

## 📊 Admin Dashboard

The admin dashboard gives institutional administrators full control over the platform through a web interface — no code or CLI required.

<!-- IMAGE PLACEHOLDER: Screenshot of the admin dashboard landing page showing the cost analytics overview — a line chart of daily spending, a breakdown by model (pie or bar chart), top users table, and the sidebar navigation with all admin sections visible. Dimensions: ~900x500px. -->
<!-- Example: ![Admin Dashboard](docs/images/admin-dashboard.png) -->

| Feature | Description |
|---------|-------------|
| 📈 **Cost Analytics** | Real-time dashboards showing token usage and costs by user, model, and time period. Identify spending trends, view top users, and export reports for budget planning. |
| 🤖 **Model Management** | Add, configure, enable, or disable AI models from any supported provider. Set per-model pricing, control access by role, and adjust availability instantly. |
| 🧰 **Tool Catalog** | Browse, enable, and configure all available tools — local, built-in, and MCP-sourced. Sync tools from registered MCP servers and control availability per role. |
| 🔗 **MCP Server Registration** | Register external MCP servers by URL. Tools are auto-discovered from the server manifest, assigned RBAC permissions, and made available to users — no redeployment needed. |
| 🔑 **OAuth Provider Management** | Configure third-party OAuth integrations (Google, Microsoft, GitHub, custom) so MCP tools can authenticate on behalf of users against external services. |
| 👥 **Role Management** | Create application roles with granular permissions over models, tools, and quotas. Map roles to JWT claims from your identity provider for automatic assignment. |
| 🏛️ **Auth Provider Configuration** | Configure OIDC authentication providers including issuer URLs, client credentials, claim mappings, and login page branding — all from the UI. |
| 👤 **User Management** | Search and browse users, view individual cost breakdowns, quota status, and role assignments. Apply per-user overrides when needed. |
| 📏 **Quota Tiers** | Define quota tiers with monthly and daily cost limits, soft warning thresholds, and hard stops. Assign tiers to roles, email domains, or individual users. |
| ⚡ **Quota Overrides** | Grant temporary exceptions — unlimited access for a research sprint, elevated limits for a class project — with automatic expiration dates. |
| 🔎 **Quota Inspector** | Debug quota resolution for any user. See which tier resolved, current usage against limits, and recent enforcement events (warnings, blocks, resets). |
| 📋 **Quota Events** | Monitor all quota enforcement activity in real time. Filter by event type, export to CSV, and audit enforcement decisions. |
| 🧪 **Fine-Tuning Access** | Grant or revoke fine-tuning access per user. Set monthly compute-hour quotas and monitor usage across training and inference jobs. |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              User Interface                                │
│                     Angular v21 + Tailwind CSS v4.1+                       │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │ SSE Streaming
                                      v
┌─────────────────────────────────────────────────────────────────────────────┐
│                              App API (FastAPI)                              │
│         Authentication · Session Management · Admin Controls · RBAC        │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      v
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Inference API (Strands Agent)                       │
│        Turn-based Session Manager · Dynamic Tool Filtering · Caching       │
└───────┬─────────────────┬─────────────────┬─────────────────┬──────────────┘
        │                 │                 │                 │
        v                 v                 v                 v
┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  Local Tools  │ │ Built-in Tools│ │  MCP Tools    │ │ Runtime Tools │
│  (Direct)     │ │ (AWS SDK)     │ │  (Gateway)    │ │ (A2A)         │
│               │ │               │ │               │ │               │
│ Custom Python │ │ Code Interp.  │ │ Configurable  │ │ Multi-agent   │
│ tools         │ │ Browser       │ │ via Admin UI  │ │ collaboration │
└───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘
```

<!-- IMAGE PLACEHOLDER: A polished, designed version of the architecture diagram above — using a tool like Excalidraw, draw.io, or Figma. Use color-coded boxes, clean arrows, and icons for each layer. This would replace or complement the ASCII diagram for a more professional look. Dimensions: ~900x500px. -->
<!-- Example: ![Architecture Diagram](docs/images/architecture-diagram.png) -->

## 🛠️ Technology Stack

| Layer | Technologies |
|-------|-------------|
| **Frontend** | Angular v21, TypeScript, Tailwind CSS v4.1+ |
| **Backend** | Python 3.13+, FastAPI |
| **Agent Framework** | Strands Agents SDK |
| **Cloud Services** | AWS Bedrock AgentCore (Runtime, Memory, Gateway, Code Interpreter, Browser), Amazon SageMaker |
| **Infrastructure** | AWS CDK (TypeScript), ECS Fargate, CloudFront, DynamoDB |
| **Authentication** | OIDC (Entra ID, Cognito, Google) with PKCE |
| **CI/CD** | GitHub Actions with full CDK deployment automation |

---

## 🚀 Deployment

The fastest path to production is the **GitHub Actions pipeline**, which automates the entire AWS deployment — infrastructure, backend services, frontend, and MCP gateway — through a series of workflow runs.

**[GitHub Actions Quick Start](.github/README-ACTIONS.md)** | **[Step-by-Step Deployment Guide](.github/docs/deploy/step-01-prerequisites.md)**

### What Gets Deployed

| Component | AWS Service | Purpose |
|-----------|-------------|---------|
| Networking | VPC, ALB, Security Groups | Isolated network with load balancing |
| Fine-Tuning *(optional)* | SageMaker, S3, DynamoDB | Model training, batch inference, artifact storage |
| RAG Ingestion | Lambda, S3 | Document ingestion for retrieval-augmented generation |
| Inference API | ECS Fargate | Agent orchestration with Bedrock |
| App API | ECS Fargate | Authentication, admin, session management |
| Frontend | S3 + CloudFront | Angular SPA with global CDN |
| MCP Gateway | Lambda + API Gateway | Serverless MCP tool endpoints |
| Data | DynamoDB | Users, sessions, costs, quotas, roles |

<!-- IMAGE PLACEHOLDER: Screenshot of the GitHub Actions tab showing all deployment workflows with green checkmarks — Infrastructure, App API, Inference API, Frontend, Gateway, RAG Ingestion, and Bootstrap Data pipelines all passing. Dimensions: ~800x400px. -->
<!-- Example: ![Deployment Pipelines](docs/images/github-actions.png) -->

<!-- ### 💻 Local Development

```bash
# Full setup (one-time)
./setup.sh

# Start all services
./start.sh
``` -->

See [backend/README.md](backend/README.md) for detailed backend setup, including authentication provider bootstrapping.

---

## 📁 Project Structure

```
agentcore-public-stack/
├── backend/
│   └── src/
│       ├── agents/main_agent/       # Agent core: factory, tools, memory, streaming
│       └── apis/
│           ├── app_api/             # Application API (port 8000)
│           ├── inference_api/       # Inference API (port 8001)
│           └── shared/              # Shared utilities
├── frontend/ai.client/              # Angular SPA
│   └── src/app/
│       ├── auth/                    # OIDC authentication
│       ├── session/                 # Chat UI
│       ├── admin/                   # Admin dashboard
│       └── services/               # State management
├── infrastructure/                  # AWS CDK stacks
│   └── lib/
└── .github/
    ├── workflows/                   # CI/CD pipelines
    └── docs/deploy/                 # Deployment guides
```

---

## 🤝 Contributing

Contributions are welcome! Please open an issue to discuss proposed changes before submitting a pull request.

---

## 📄 License

This project is licensed under the **PolyForm Noncommercial License 1.0.0**.
You may use, modify, and distribute this software for noncommercial purposes only.
Commercial use, including use in a product or service that generates revenue,
is prohibited without a separate commercial license.

See the [LICENSE](LICENSE) file for the full license text.
For commercial licensing inquiries, please contact: [techtransfer@boisestate.edu](mailto:techtransfer@boisestate.edu)

---

<div align="center">

**Built with ❤️ at Boise State University**

[Report a Bug](https://github.com/Boise-State-Development/agentcore-public-stack/issues) · [Request a Feature](https://github.com/Boise-State-Development/agentcore-public-stack/issues) · [Deployment Guide](.github/README-ACTIONS.md)

</div>
