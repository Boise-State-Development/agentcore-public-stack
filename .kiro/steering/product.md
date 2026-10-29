# Product Overview

AgentCore Public Stack is a production-ready multi-agent conversational AI system built with AWS Bedrock AgentCore and Strands Agents.

## Core Capabilities

- **Multi-Agent Orchestration**: Strands Agent framework with Bedrock Claude models (Haiku, Sonnet)
- **Multi-Protocol Tool Integration**: 30 tools across 4 protocols (Direct, AWS SDK, MCP+SigV4, A2A)
- **Multimodal I/O**: Native support for images, documents, charts, and screenshots
- **Persistent Memory**: Two-tier system with session history and cross-session user preferences
- **Dynamic Tool Filtering**: User-selectable tools with real-time filtering to optimize token usage
- **Token Optimization**: Prompt caching strategy reducing input token costs

## Key Features

- **AgentCore Runtime**: Containerized Strands Agent deployment as managed AWS service
- **AgentCore Memory**: Persistent conversation storage with relevance-scored retrieval
- **AgentCore Gateway**: MCP tool integration with SigV4 authentication (12 tools via Lambda)
- **AgentCore Code Interpreter**: Built-in code execution for data analysis and visualization
- **AgentCore Browser**: Web automation via headless browser with Amazon Nova Act AI model
- **RBAC & Quota Management**: Role-based access control with usage tracking and limits

## Use Cases

- Financial research agent with stock analysis and SEC ingestion
- Technical research assistant using multi-agent architecture
- Web automation agent via AgentCore Browser + Nova Act
- RAG-enabled chatbot using AgentCore Memory
- Multi-protocol research assistant (MCP, A2A, AWS SDK)

## Architecture

**Browser / API-key client** → CloudFront (`/*` → S3 SPA; `/api/*` → ALB, CloudFront-only ingress)
                                      ↓
                                **App API** (FastAPI BFF on ECS Fargate: auth, RBAC, quotas, `/chat/stream` SSE proxy, `/chat/api-converse`)
                                      ↓
                                **AgentCore Runtime** (Inference API + Strands Agent)
                                      ↓
                    ┌─────────────────┼─────────────────┐
                    ↓                 ↓                 ↓
            **Amazon Bedrock**   **Built-in & local tools**   **AgentCore services**
            (+ external          (documents, files,           (Memory, Code Interpreter,
             providers)           charts, web fetch)           Browser, Gateway → MCP servers)
