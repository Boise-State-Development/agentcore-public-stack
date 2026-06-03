---
title: Deployment Overview
description: How code and infrastructure ship to AWS.
sidebar:
  order: 1
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

Deploy order: platform.yml (CDK) then backend.yml (per-image) then frontend-deploy.yml. Application code ships out-of-band via AWS APIs.
