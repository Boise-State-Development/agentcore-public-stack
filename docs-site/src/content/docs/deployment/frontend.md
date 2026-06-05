---
title: Frontend Deploy
description: Publish the SPA to S3 and CloudFront.
sidebar:
  order: 4
---

`frontend-deploy.yml` builds the Angular SPA and publishes it to the S3 bucket
behind CloudFront. It runs on changes under `frontend/ai.client/**` and on
manual dispatch.

## What `frontend-deploy.yml` does

1. **Build** — `npm run build` in `frontend/ai.client/`, producing the
   production bundle under `dist/ai.client/browser/`.
2. **Sync** — `aws s3 sync` copies that directory to the SPA bucket.
3. **Invalidate** — issues a CloudFront invalidation so clients pick up the new
   bundle.

## Content-hash and invalidation

The build is content-hashed, like the backend images. If no source changed, the
sync is effectively a no-op and the invalidation is minimal — just `index.html`
and `index.csr.html` — rather than a full distribution purge. CloudFront can take
a few minutes to propagate after a first deploy.

## When to re-run

Re-run whenever you change the SPA under `frontend/ai.client/**`. It depends only
on the SPA bucket and distribution created by `platform.yml`, so there's no need
to re-run the platform workflow alongside it.
