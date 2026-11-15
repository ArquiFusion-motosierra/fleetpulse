// supply-sourcing puede ofrecer varios almacenes del mismo parque con igual ETA y costo: una fila por grupo
export function groupOptions(options = []) {
  const out = [];
  options.forEach((o) => {
    const same = o.source_kind === "transfer" &&
      out.find((g) => g.source_kind === "transfer" && g.eta_h === o.eta_h && g.cost_usd === o.cost_usd && g.total_cost_usd === o.total_cost_usd);
    if (same) same.zones.push(o.from_zone);
    else out.push({ ...o, zones: o.from_zone ? [o.from_zone] : [] });
  });
  return out;
}
