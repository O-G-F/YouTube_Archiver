import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Bool, StateBadge, StatusBadge } from "../components/ui";

describe("badges", () => {
  it("renders a job status badge with the right class", () => {
    const { container } = render(<StatusBadge status="success" />);
    const el = container.querySelector(".badge");
    expect(el).toBeInTheDocument();
    expect(el).toHaveClass("ok");
    expect(el).toHaveTextContent("success");
  });

  it("renders a state badge (em dash when null)", () => {
    render(<StateBadge state={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders a boolean badge", () => {
    const { container } = render(<Bool value={true} />);
    expect(container.querySelector(".badge")).toHaveTextContent("yes");
  });
});
