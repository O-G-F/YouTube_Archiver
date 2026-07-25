import { NavLink, Outlet } from "react-router-dom";

const LINKS: { to: string; label: string; section?: string }[] = [
  { to: "/", label: "Dashboard", section: "Overview" },
  { to: "/jobs", label: "Jobs" },
  { to: "/videos", label: "Videos", section: "Browse" },
  { to: "/search", label: "Search" },
  { to: "/library", label: "Library" },
  { to: "/liked-videos", label: "Liked videos" },
  { to: "/collections", label: "Collections" },
  { to: "/archive", label: "Add / Archive", section: "Actions" },
  { to: "/takeout", label: "Takeout" },
  { to: "/settings", label: "System / Settings", section: "System" },
];

export default function Layout() {
  return (
    <div className="app">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="sidebar">
        <div className="brand">
          YouTube Archiver
          <small>Admin console · local single-user</small>
        </div>
        <nav className="nav" aria-label="Primary">
          {LINKS.map((l) => (
            <div key={l.to}>
              {l.section && <div className="sep">{l.section}</div>}
              <NavLink to={l.to} end={l.to === "/"}>
                {l.label}
              </NavLink>
            </div>
          ))}
        </nav>
      </aside>
      <main className="main" id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
