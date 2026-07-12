export default function handler(req, res) {
  // pages router: [id] -> :id; switches on method -> GET + DELETE
  if (req.method === "GET") {
    return res.json({ id: req.query.id });
  }
  if (req.method === "DELETE") {
    return res.status(204).end();
  }
  res.status(405).end();
}
