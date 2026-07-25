import { useEffect, useRef } from "react";

/**
 * Minimal modal accessibility for the app's simple overlay dialogs:
 *  - Escape closes the dialog (keyboard users are not trapped)
 *  - focus returns to whatever was focused when the dialog opened
 *
 * Call at the top level of the component that owns the modal, passing whether
 * the modal is currently open and a stable-or-inline close callback.
 */
export function useModalA11y(active: boolean, onClose: () => void) {
  // keep the latest onClose without re-running the effect (callers pass inline arrows)
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!active) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        previouslyFocused.focus();
      }
    };
  }, [active]);
}
