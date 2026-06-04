import { NavLink, Outlet } from "react-router-dom";

const LINKS: { to: string; label: string; section?: string }[] = [
  { to: "/", label: "Dashboard", section: "Overview" },
  { to: "/jobs", label: "Jobs" },
  { to: "/videos", label: "Videos", section: "Browse" },
  { to: "/search", label: "Search" },
  { to: "/library", label: "Library" },
  { to: "/collections", label: "Collections" },
  { to: "/archive", label: "Add / Archive", section: "Actions" },
  { to: "/takeout", label: "Takeout" },
  { to: "/settings", label: "Settings / Doctor", section: "System" },
];

export default function Layout() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          YouTube Archiver
          <small>Admin console · Phase 5A</small>
        </div>
        <nav className="nav">
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
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
