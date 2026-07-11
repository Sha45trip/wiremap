const express = require("express");
const users = require("./routes/users");

const app = express();

app.use("/api/users", users);

app.get("/health", (req, res) => res.json({ ok: true }));

// planted: mutating route without auth middleware -> missing_auth
app.post("/webhook", (req, res) => {
  res.json({});
});

app.listen(3000);
