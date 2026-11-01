import { cacheLife } from "next/cache";
import { snapshot } from "./github-snapshot";

export const REPO = "Rishi-Bhati/intermux";

export type RepoData = {
  stats: { stars: number; forks: number; downloads: number; commits: number; issues: number };
  /** The default branch, used to link to files. */
  branch: string;
  /** When each commit was made, oldest first (milliseconds). */
  commitTimes: number[];
  /** When each star was given, oldest first. Only present when GitHub allows the list to be read. */
  starTimes?: number[];
  releases: Array<{ tag: string; t: number }>;
  /** When this data was gathered. */
  end: number;
};

type Json = Record<string, unknown>;

async function get<T>(path: string, accept = "application/vnd.github+json"): Promise<T> {
  const token = process.env.GITHUB_TOKEN;
  const res = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    headers: {
      Accept: accept,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) throw new Error(`GitHub ${res.status} for ${path}`);
  return (await res.json()) as T;
}

/** Walk through paginated results, newest page first is not needed: we sort afterwards. */
async function pages<T>(path: string, accept: string | undefined, max: number): Promise<T[]> {
  const all: T[] = [];
  for (let page = 1; page <= max; page++) {
    const sep = path.includes("?") ? "&" : "?";
    const batch = await get<T[]>(`${path}${sep}per_page=100&page=${page}`, accept);
    all.push(...batch);
    if (batch.length < 100) break;
  }
  return all;
}

const time = (iso: string) => Date.parse(iso);

/**
 * Live numbers from GitHub, kept for a few hours. If GitHub cannot be reached (rate limit, offline
 * build), the stored snapshot is used instead, so the footer always has something true to show.
 *
 * The dated list of stars is only available to an authenticated request. Set GITHUB_TOKEN to turn
 * the footer chart from commits into a real star history.
 */
export async function getRepoData(): Promise<RepoData> {
  "use cache";
  cacheLife("hours");

  try {
    const [repo, releases, commits] = await Promise.all([
      get<Json>(""),
      pages<Json>("/releases", undefined, 3),
      pages<{ commit: { author: { date: string } } }>("/commits", undefined, 6),
    ]);

    let starTimes: number[] | undefined;
    try {
      const stars = await pages<{ starred_at?: string }>(
        "/stargazers",
        "application/vnd.github.star+json",
        10,
      );
      const times = stars.flatMap((s) => (s.starred_at ? [time(s.starred_at)] : [])).sort((a, b) => a - b);
      if (times.length >= 2) starTimes = times;
    } catch {
      // Not readable without access to the list. The chart falls back to commits.
    }

    const commitTimes = commits.map((c) => time(c.commit.author.date)).sort((a, b) => a - b);
    const downloads = releases.reduce((sum, r) => {
      const assets = (r.assets as Array<{ download_count: number }> | undefined) ?? [];
      return sum + assets.reduce((n, a) => n + a.download_count, 0);
    }, 0);

    return {
      stats: {
        stars: repo.stargazers_count as number,
        forks: repo.forks_count as number,
        downloads,
        commits: commitTimes.length,
        issues: repo.open_issues_count as number,
      },
      branch: (repo.default_branch as string) ?? "main",
      commitTimes,
      starTimes,
      releases: releases
        .filter((r) => r.published_at)
        .map((r) => ({ tag: r.tag_name as string, t: time(r.published_at as string) }))
        .sort((a, b) => a.t - b.t),
      end: Date.now(),
    };
  } catch {
    return snapshot;
  }
}
