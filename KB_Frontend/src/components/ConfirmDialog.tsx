import type { ReactNode } from "react";

import { useDialog } from "../hooks/useDialog";

type Props = {
  /** Accessible name for the dialog (announced on open). */
  label: string;
  /** Close/dismiss the dialog — bound to Escape and focus restoration. */
  onClose: () => void;
  children: ReactNode;
};

/**
 * A small modal confirm dialog used by the create-class and curation
 * preview→confirm steps. Traps focus, closes on Escape, and restores focus to
 * the trigger on unmount (see {@link useDialog}). Visual style comes from `.modal`.
 */
export function ConfirmDialog({ label, onClose, children }: Props) {
  const ref = useDialog<HTMLDivElement>(onClose);
  return (
    <div ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={label}>
      {children}
    </div>
  );
}
