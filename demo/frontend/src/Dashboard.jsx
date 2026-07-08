export function Dashboard() {
  const loadStats = () =>
    fetch("/api/stats/overview").then(r => r.json()); // no backend route -> orphan
  return <div onClick={loadStats}>stats</div>;
}
