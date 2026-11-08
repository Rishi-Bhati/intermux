# InterMux website

The marketing site and documentation for [InterMux](https://github.com/Rishi-Bhati/intermux), built with Next.js and deployed on Vercel.

## Local development

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The docs live at `/docs`.

```bash
npm run build   # production build
npm run lint    # eslint
npx tsc --noEmit
```

## Deploy on Vercel

The site lives in `website/` inside the main InterMux repository. The repository root is the Python application, so the Vercel project must point at this folder.

1. Import the repository at [vercel.com/new](https://vercel.com/new).
2. Set **Root Directory** to `website`.
3. Keep the **Next.js** framework preset. `vercel.json` in this folder also sets it.
4. Add the environment variables below in **Project Settings → Environment Variables** (Production).
5. Deploy. Every push to `main` then deploys automatically.

### Environment variables

| Name | Required | Purpose |
|------|----------|---------|
| `SITE_URL` | Recommended | Public production URL, for example `https://intermux.dev`. Used for canonical and share-card URLs and for the short `https://<domain>/install` commands. |
| `GITHUB_TOKEN` | Optional | A GitHub token with public repository read access. Raises the API rate limit and enables the star-history chart. Without it the footer falls back to the bundled snapshot. |

If `SITE_URL` is unset, Vercel's deployment URL is used for absolute share-card URLs, and install commands keep pointing at the raw GitHub scripts.

## Short install URLs

`next.config.ts` redirects `/install`, `/install.sh` and `/install.ps1` to the scripts in the main repository under `get/`. Once `SITE_URL` is set, the site shows the short commands:

```bash
curl -fsSL https://<domain>/install | sh
```
