# Inventory and costing

`products.stock_quantity` is the on-hand quantity; `inventory_movements` is an append-only ledger (updates and
deletes are rejected by triggers). Movement types: `opening_balance`, `purchase`, `sale`, `sales_return`,
`adjustment`. Every row has product, signed quantity, unit cost, reference type/id, timestamp, user and reason.
`adjustment` is distinguished by `reference_type`: `manual_adjustment`, `damage`, `stocktake`, `return_damage`.
`reporting.stock_ledger_discrepancies` lists any product whose stock ≠ Σmovements (tests assert it is empty
after every workflow). Negative stock is impossible (service check + `CHECK(stock_quantity>=0)`).

**Moving weighted-average cost** (`accounting.moving_weighted_average_cost`):
`(oldQty·oldCost + recvQty·recvCost) / (oldQty+recvQty)`, integer piasters, rounded half-up; zero stock → the new
cost; nothing received → unchanged. `products.purchase_price` is the current average; each purchase line is kept in
`purchase_items`; each invoice line snapshots `unit_cost` at sale, so later purchases never rewrite history.
Returns re-enter stock at the *original sale cost* in the movement record; they do not change the average.

**Operations (admin only, reason mandatory):** purchase receiving (atomic: invoice + lines + stock + cost + audit),
manual adjustment, damage write-off, stocktake (applies only differences, atomically).

**Unknown cost policy (Batch 2, no schema change):** `products` has no `cost_known` column, so a product whose
recorded cost is `<= 0` is treated as having an *unknown* cost (`accounting.is_cost_unknown`), never as "free".
* Sales and stock movements snapshot `-1` (`accounting.cost_snapshot`) instead of `0`.
* Reports treat `invoice_items.unit_cost <= 0` as unknown (including historical rows stored as `0`; those rows are
  not modified): their revenue is excluded from audited profit and shown as `unknown_cost_revenue`.
* `reporting.inventory_value` values only known-cost units; unknown units are reported separately
  (`unknown_cost_units`, `unknown_cost_products`), so the true value is at least `value`.
* Receiving purchases for a product with `stock > 0` and unknown cost raises `UnknownCostError` and writes nothing,
  unless the administrator supplies `opening_costs={product_id: unit_cost}` (the UI asks for it explicitly). That
  decision is audited (`unknown_cost_resolved`). The software never invents the cost.
* **Still requires a schema migration (not done here):** a durable `products.cost_known` flag (so a genuinely free
  item can be distinguished from an unknown cost) and a guided "set opening cost" screen. Until then a recorded cost
  of 0 is always treated as unknown.

**Historical cost:** invoice lines with `unit_cost = -1` have unknown cost; nothing is invented. They are excluded from
audited profit and counted in `unknown_cost_units`. Opening stock of migrated databases is anchored by one
`opening_balance` movement per product at migration time (a baseline, not reconstructed history).

**Not implemented:** purchase returns to suppliers; a full parent/variant catalogue (SKUs are grouped by normalised
product name via `product_groups`); a guided count-sheet UI for stocktake (the service exists; no screen);
supplier payments/balances; a UI for damage write-off.
