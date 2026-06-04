import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { fmtCount } from "../lib/format";
import { JobBadges } from "../components/JobBadges";
import { Comments } from "../components/Comments";
import { LiveChat } from "../components/LiveChat";
import type { Comment, Job, LiveChatMessage } from "../api/types";

describe("fmtCount", () => {
  it("abbreviates large numbers", () => {
    expect(fmtCount(0)).toBe("0");
    expect(fmtCount(999)).toBe("999");
    expect(fmtCount(1500)).toBe("1.5K");
    expect(fmtCount(12000)).toBe("12K");
    expect(fmtCount(3_400_000)).toBe("3.4M");
  });
});

function job(partial: Partial<Job>): Job {
  return {
    id: 1,
    type: "comments_refresh",
    status: "failed",
    url: null,
    profile_name: null,
    video_id: null,
    collection_id: null,
    parent_job_id: null,
    rq_job_id: null,
    priority: 0,
    progress: 0,
    error_message: null,
    log_path: null,
    command_path: null,
    meta: null,
    created_at: "2026-01-01T00:00:00",
    started_at: null,
    finished_at: null,
    ...partial,
  };
}

describe("JobBadges", () => {
  it("shows a 429 badge when classification.rate_limited", () => {
    render(
      <JobBadges
        job={job({
          status: "failed",
          classification: { rate_limited: true, partial: false, retryable: true, reasons: ["rate_limited"], warnings: [], summary: "Rate limited" },
        })}
      />
    );
    expect(screen.getByText("429")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("renders without classification (falls back to status)", () => {
    render(<JobBadges job={job({ status: "success" })} />);
    expect(screen.getByText("success")).toBeInTheDocument();
  });
});

describe("Comments threading", () => {
  function c(id: string, parent: string | null, text = "x"): Comment {
    return {
      id: Number(id.replace(/\D/g, "")) || 1,
      video_id: 1,
      comment_id: id,
      parent_comment_id: parent,
      author_name: "A",
      text,
      like_count: 1,
      published_at: null,
      is_deleted_or_missing: false,
    };
  }
  it("nests replies under their parent", () => {
    render(<Comments comments={[c("c1", "root", "parent text"), c("c2", "c1", "reply text")]} />);
    expect(screen.getByText("parent text")).toBeInTheDocument();
    expect(screen.getByText("reply text")).toBeInTheDocument();
    // reply is rendered inside a .replies container
    expect(document.querySelector(".replies")).toBeTruthy();
  });
  it("shows an empty state with no comments", () => {
    render(<Comments comments={[]} />);
    expect(screen.getByText(/No comments stored/)).toBeInTheDocument();
  });
});

describe("LiveChat", () => {
  function m(over: Partial<LiveChatMessage>): LiveChatMessage {
    return {
      id: 1,
      video_id: 1,
      message_id: "m1",
      author_name: "B",
      message: "hi",
      time_text: "0:01",
      message_type: "text",
      amount: null,
      amount_text: null,
      currency: null,
      is_superchat: false,
      is_member_message: false,
      is_deleted_or_missing: false,
      ...over,
    };
  }
  it("marks super chats with their amount", () => {
    const { container } = render(
      <LiveChat messages={[m({ is_superchat: true, amount_text: "¥1,000", message: "thanks" })]} />
    );
    expect(screen.getByText("¥1,000")).toBeInTheDocument();
    expect(container.querySelector(".lc-row.superchat")).toBeTruthy();
  });
});
