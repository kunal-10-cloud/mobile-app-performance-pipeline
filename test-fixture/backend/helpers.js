// Backend-fixture JS — exercises the algorithm rules + the JS variant of
// sequential await. Mirrors the style of the Python fixture: each block
// triggers exactly one rule. Do NOT "fix" these.

// ── PLANT: algorithms.nested_iteration ───────────────────────────────────
function findMatches(items, others) {
  return items.filter(i => others.filter(o => o.id === i.refId).length > 0);
}

// ── PLANT: algorithms.linear_array_lookup_in_loop ────────────────────────
// .includes() lookup inside a for-loop where a Set would be O(1).
function flagSelected(items, selectedIds) {
  const out = [];
  for (const item of items) {
    const on = selectedIds.includes(item.id);
    out.push({ ...item, on });
  }
  return out;
}

// ── PLANT: backend.sequential_fetch_chain ────────────────────────────────
async function loadDashboard(userId) {
  const user = await fetch(`/api/users/${userId}`).then(r => r.json());
  const orders = await fetch(`/api/orders?user=${userId}`).then(r => r.json());
  const notifs = await fetch(`/api/notifications?user=${userId}`).then(r => r.json());
  return { user, orders, notifs };
}

module.exports = { findMatches, flagSelected, loadDashboard };
