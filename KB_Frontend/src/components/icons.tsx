/* Lightweight inline icon set. Each icon inherits `currentColor` and a 1.7
   stroke so it reads crisply at small UI sizes.

   FunnelIcon, LogoutIcon and GearIcon reuse path data from Feather Icons
   (https://github.com/feathericons/feather), MIT License, Copyright (c) 2013-2023
   Cole Bemis. The full notice is in NOTICE at the repository root. */

type IconProps = { size?: number; className?: string };

function svg(size: number, className: string | undefined, children: React.ReactNode) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export const ChevronIcon = ({ size = 13, className }: IconProps) =>
  svg(size, className, <path d="M9 6l6 6-6 6" />);

export const MenuIcon = ({ size = 18, className }: IconProps) =>
  svg(size, className, <path d="M4 7h16M4 12h16M4 17h16" />);

export const FolderIcon = ({ size = 16, className }: IconProps) =>
  svg(
    size,
    className,
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />,
  );

export const NoteIcon = ({ size = 16, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z" />
      <path d="M14 3v5h5" />
      <path d="M9 13h6M9 17h4" />
    </>,
  );

export const CloseIcon = ({ size = 16, className }: IconProps) =>
  svg(size, className, <path d="M6 6l12 12M18 6L6 18" />);

export const SaveIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M5 4h11l3 3v13H5z" />
      <path d="M8 4v5h7M8 14h8v6H8z" />
    </>,
  );

export const TrashIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
      <path d="M10 11v6M14 11v6" />
    </>,
  );

export const GraphIcon = ({ size = 22, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <circle cx="6" cy="18" r="2.4" />
      <circle cx="18" cy="6" r="2.4" />
      <circle cx="18" cy="17" r="2.4" />
      <path d="M8 16.5l8-9M8.2 17.6l7.6-.6" />
    </>,
  );

export const DiamondIcon = ({ size = 14, className }: IconProps) =>
  svg(size, className, <path d="M12 3l9 9-9 9-9-9 9-9z" />);

export const SparkIcon = ({ size = 16, className }: IconProps) =>
  svg(
    size,
    className,
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />,
  );

// Path data from Feather Icons (`filter`), MIT; see the header.
export const FunnelIcon = ({ size = 15, className }: IconProps) =>
  svg(size, className, <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />);

export const LayersIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M12 3l9 5-9 5-9-5 9-5z" />
      <path d="M3 13l9 5 9-5" />
    </>,
  );

export const ActivityIcon = ({ size = 16, className }: IconProps) =>
  svg(size, className, <path d="M3 12h4l3 8 4-16 3 8h4" />);

export const RecrawlIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </>,
  );

// Path data from Feather Icons (`log-out`), MIT; see the header.
export const LogoutIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </>,
  );

export const DatabaseIcon = ({ size = 16, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5" />
      <path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
    </>,
  );

// Path data from Feather Icons (`settings`), MIT; see the header.
export const GearIcon = ({ size = 18, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </>,
  );

export const RefreshIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v5h-5" />
    </>,
  );

export const CheckIcon = ({ size = 14, className }: IconProps) =>
  svg(size, className, <path d="M5 12.5l4.5 4.5L19 7" />);

export const MergeArrowIcon = ({ size = 15, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M4 12h15" />
      <path d="M13 6l6 6-6 6" />
    </>,
  );

export const PencilIcon = ({ size = 14, className }: IconProps) =>
  svg(
    size,
    className,
    <>
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
      <path d="M14.5 5.5l3 3" />
    </>,
  );
