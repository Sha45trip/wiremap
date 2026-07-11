const express = require("express");
const { requireAuth } = require("../middleware/auth");

const router = express.Router();

router.get("/", (req, res) => res.json([]));

router.get("/:id", getUser);

// near-miss: auth middleware present -> no missing_auth
router.post("/", requireAuth, (req, res) => res.json({}));

// planted: mutating without auth -> missing_auth
router.delete("/:id", (req, res) => res.json({}));

function getUser(req, res) {
  res.json({});
}

module.exports = router;
