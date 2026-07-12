export default function handler(req, res) {
  // no method switch -> defaults to GET
  res.json({ ok: true });
}
