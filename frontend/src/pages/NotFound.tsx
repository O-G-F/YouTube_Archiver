import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div>
      <h1 className="page-title">Not found</h1>
      <p className="page-sub">That page doesn’t exist.</p>
      <Link to="/">← back to dashboard</Link>
    </div>
  );
}
