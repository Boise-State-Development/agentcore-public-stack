---
name: web-search
description: Search the web for current information
type: tool
---

# Web Search Skill

Use this skill to search the web for up-to-date information that may not
be in your training data.

## When to Use

- User asks about current events or recent information
- User needs real-time data (weather, stock prices, news)
- User asks "what is the latest..." or "search for..."

## Tools

| Tool | Purpose |
|------|---------|
| `fetch_url_content` | Fetch and extract content from a URL |

## Usage Pattern

1. Use `fetch_url_content` to retrieve content from a specific URL
2. Summarize the relevant information for the user
3. Cite the source URL in your response
