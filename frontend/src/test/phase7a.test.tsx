import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobBadges, JobClassificationNote } from "../components/JobBadges";
import type { Job } from "../api/types";

function job(over: Partial<Job>): Job {
  return {
    id: 1,
    type: "metadata_refresh",
    status: "partial_success",
    url: null,
    profile_name: null,
    video_id: 5,
    collection_id: null,
    parent_job_id: null,
    rq_job_id: null,
    priority: 0,
    progress: 100,
    error_message: null,
    log_path: null,
    command_path: null,
    meta: null,
    created_at: "2026-01-01T00:00:00",
    started_at: null,
    finished_at: null,
    retry_count: 0,
    ...over,
  };
}

describe("Phase 7A job badges / classification", () => {
  it("shows a subs badge when subtitles_failed", () => {
    const { container } = render(
      <JobBadges
        job={job({
          classification: {
            rate_limited: true,
            partial: true,
            retryable: true,
            reasons: ["rate_limited", "subtitles_failed"],
            warnings: [],
            summary: "Partial",
          },
        })}
      />
    );
    expect(screen.getByText("subs")).toBeInTheDocument();
    expect(screen.getByText("429")).toBeInTheDocument();
  });

  it("classification note lists reasons + retryable", () => {
    render(
      <JobClassificationNote
        job={job({
          classification: {
            rate_limited: false,
            partial: true,
            retryable: true,
            reasons: ["subtitles_failed"],
            warnings: ["Subtitle download failed"],
            summary: "Partial success",
          },
        })}
      />
    );
    expect(screen.getByText("subtitles_failed")).toBeInTheDocument();
    expect(screen.getByText("retryable")).toBeInTheDocument();
  });

  it("note is hidden for a clean success", () => {
    const { container } = render(
      <JobClassificationNote
        job={job({
          status: "success",
          classification: { rate_limited: false, partial: false, retryable: false, reasons: [], warnings: [], summary: "Success" },
        })}
      />
    );
    expect(container.querySelector(".flash")).toBeNull();
  });
});
