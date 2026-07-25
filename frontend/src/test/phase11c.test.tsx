import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import { ErrorBox, Loading, Empty } from "../components/ui";
import Layout from "../components/Layout";
import { useModalA11y } from "../lib/useModalA11y";

function ModalHarness() {
  const [open, setOpen] = useState(false);
  useModalA11y(open, () => setOpen(false));
  return (
    <div>
      <button onClick={() => setOpen(true)}>open</button>
      {open && <div role="dialog" aria-modal="true">dialog body</div>}
    </div>
  );
}

describe("phase 11c — accessibility contract", () => {
  it("Loading announces politely as a status region", () => {
    render(<Loading what="videos" />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("aria-live", "polite");
    expect(el).toHaveTextContent(/Loading videos/);
    // the spinner glyph is decorative and must not be announced
    expect(el.querySelector(".spin")).toHaveAttribute("aria-hidden", "true");
  });

  it("ErrorBox is an assertive alert (and renders nothing when clear)", () => {
    const { container, rerender } = render(<ErrorBox error="Something failed" />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Something failed");
    rerender(<ErrorBox error={null} />);
    expect(container.querySelector(".error-box")).toBeNull();
  });

  it("Empty renders its children", () => {
    render(<Empty>Nothing here yet</Empty>);
    expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
  });

  it("Layout exposes a skip link that targets the labelled main region", () => {
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    );
    const skip = screen.getByRole("link", { name: /skip to content/i });
    expect(skip).toHaveAttribute("href", "#main-content");
    const main = document.getElementById("main-content");
    expect(main).not.toBeNull();
    expect(main?.tagName.toLowerCase()).toBe("main");
    // primary navigation is programmatically labelled for screen readers
    expect(screen.getByRole("navigation", { name: /primary/i })).toBeInTheDocument();
  });
});

describe("phase 11c — modal keyboard accessibility", () => {
  it("Escape closes the dialog and focus returns to the trigger", () => {
    render(<ModalHarness />);
    const trigger = screen.getByRole("button", { name: "open" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    // focus is restored to whatever was focused when the dialog opened
    expect(document.activeElement).toBe(trigger);
  });
});
