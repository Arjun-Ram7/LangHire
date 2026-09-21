interface ListedJob {
  company?: string;
  title?: string;
  url: string;
}

const clean = (value: string | undefined, fallback: string) =>
  (value || fallback).replace(/\s+/g, " ").trim();

/**
 * A plain-text list of jobs, one numbered "Company — Role" line each, in the order given.
 * With `withLinks` the posting URL follows on its own line, so a saved list of selected jobs
 * can be worked through or pasted elsewhere.
 */
export function formatJobList(heading: string, jobs: ListedJob[], withLinks = false): string {
  const lines = jobs.flatMap((job, index) => {
    const line = `${index + 1}. ${clean(job.company, "Unknown company")} — ${clean(job.title, "Untitled role")}`;
    return withLinks ? [line, `   ${job.url}`] : [line];
  });
  return [`${heading} (${jobs.length})`, "", ...lines, ""].join("\n");
}
