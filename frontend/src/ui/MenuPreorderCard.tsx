import React, { useCallback, useEffect, useMemo, useState } from "react";
import type { PreorderCartLine } from "./dialogTypes";

type MenuPos = {
  menu_item_id: string;
  title: string;
  price: number;
  section: string;
};

type MenuPreorderCardProps = {
  organizationId: string;
  storeId: string;
  initialLines: PreorderCartLine[];
  loading: boolean;
  onSubmitCart: (lines: PreorderCartLine[]) => void;
};

function collectPositions(menu: Record<string, unknown>): MenuPos[] {
  const out: MenuPos[] = [];
  const items = menu.items;
  if (!Array.isArray(items)) return out;
  for (const card of items) {
    if (!card || typeof card !== "object") continue;
    const c = card as Record<string, unknown>;
    const cardName = String(c.name || "").trim() || "Меню";
    const tree = c.tree;
    if (!Array.isArray(tree)) continue;
    for (const root of tree) {
      if (root && typeof root === "object") walk(root as Record<string, unknown>, cardName, out);
    }
  }
  return out;
}

function walk(node: Record<string, unknown>, cardName: string, out: MenuPos[]): void {
  const rawItems = node.items;
  if (Array.isArray(rawItems)) {
    for (const it of rawItems) {
      if (!it || typeof it !== "object") continue;
      const row = it as Record<string, unknown>;
      const mid = row.menu_item_id;
      if (mid == null || String(mid).trim() === "") continue;
      const prod = row.product as Record<string, unknown> | undefined;
      const title = String(row.title || prod?.name || "").trim();
      const price = Number(row.price ?? 0) || 0;
      const sub = String(node.name || "").trim();
      const section = sub ? `${cardName}: ${sub}` : cardName;
      out.push({
        menu_item_id: String(mid).trim(),
        title: title || "—",
        price,
        section
      });
    }
  }
  const ch = node.children;
  if (Array.isArray(ch)) {
    for (const c of ch) {
      if (c && typeof c === "object") walk(c as Record<string, unknown>, cardName, out);
    }
  }
}

const QTY_MAX = 99;

export const MenuPreorderCard: React.FC<MenuPreorderCardProps> = ({
  organizationId,
  storeId,
  initialLines,
  loading,
  onSubmitCart
}) => {
  const [positions, setPositions] = useState<MenuPos[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [selected, setSelected] = useState<Map<string, number>>(() => new Map());

  const groups = useMemo(() => {
    const m = new Map<string, MenuPos[]>();
    for (const p of positions) {
      const k = p.section || "Меню";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(p);
    }
    return [...m.entries()];
  }, [positions]);

  useEffect(() => {
    const m = new Map<string, number>();
    for (const ln of initialLines) {
      if (ln.menu_item_id) m.set(ln.menu_item_id, Math.max(1, Math.min(99, ln.quantity || 1)));
    }
    setSelected(m);
  }, [initialLines]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoadError(null);
      try {
        const url = `/api/menus/${encodeURIComponent(organizationId)}/stores/${encodeURIComponent(storeId)}/menus/tree`;
        const resp = await fetch(url, { credentials: "include" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as Record<string, unknown>;
        const pos = collectPositions(data);
        if (!cancelled) {
          setPositions(pos);
          const sec = [...new Set(pos.map(p => p.section))];
          setExpanded(new Set(sec));
        }
      } catch {
        if (!cancelled) {
          setLoadError("Не удалось загрузить меню.");
          setPositions([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [organizationId, storeId]);

  const toggleExpand = useCallback((id: string) => {
    setExpanded(prev => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);

  const toggleItem = useCallback((id: string) => {
    setSelected(prev => {
      const n = new Map(prev);
      if (n.has(id)) n.delete(id);
      else n.set(id, 1);
      return n;
    });
  }, []);

  const incrementItem = useCallback((id: string) => {
    setSelected(prev => {
      const cur = prev.get(id) ?? 0;
      if (cur >= QTY_MAX) return prev;
      const n = new Map(prev);
      n.set(id, cur < 1 ? 1 : cur + 1);
      return n;
    });
  }, []);

  const decrementItem = useCallback((id: string) => {
    setSelected(prev => {
      const cur = prev.get(id);
      if (!cur || cur <= 1) {
        const n = new Map(prev);
        n.delete(id);
        return n;
      }
      const n = new Map(prev);
      n.set(id, cur - 1);
      return n;
    });
  }, []);

  const stopRowClick = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const totals = useMemo(() => {
    let count = 0;
    let sum = 0;
    for (const [id, q] of selected) {
      const p = positions.find(x => x.menu_item_id === id);
      if (!p) continue;
      count += q;
      sum += p.price * q;
    }
    return { count, sum };
  }, [selected, positions]);

  const handleForm = () => {
    const lines: PreorderCartLine[] = [];
    for (const [id, q] of selected) {
      const p = positions.find(x => x.menu_item_id === id);
      if (!p) continue;
      lines.push({
        menu_item_id: id,
        quantity: q,
        title: p.title,
        price: p.price,
        section: p.section
      });
    }
    onSubmitCart(lines);
  };

  return (
    <div className="restaurant-card menu-preorder-card">
      <div className="restaurant-card-top">
        <h3 className="restaurant-card-title">Меню</h3>
      </div>
      {loadError && <p className="menu-preorder-error">{loadError}</p>}
      <div className="menu-preorder-scroll">
        {groups.length === 0 && !loadError ? <p className="menu-preorder-muted">Загрузка…</p> : null}
        {groups.map(([section, items]) => {
          const isOpen = expanded.has(section);
          return (
            <div key={section} className="menu-preorder-node">
              <button
                type="button"
                className="menu-preorder-node-toggle"
                onClick={() => toggleExpand(section)}
                aria-expanded={isOpen}
              >
                <span className="menu-preorder-chevron">{isOpen ? "▼" : "▶"}</span>
                <span>{section}</span>
              </button>
              {isOpen && (
                <div className="menu-preorder-node-body">
                  {items.map(p => {
                    const checked = selected.has(p.menu_item_id);
                    const qty = selected.get(p.menu_item_id) ?? 1;
                    return (
                      <label
                        key={p.menu_item_id}
                        className={`menu-preorder-item${checked ? " menu-preorder-item--checked" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleItem(p.menu_item_id)}
                          className="menu-preorder-checkbox"
                        />
                        <span className="menu-preorder-item-title">{p.title}</span>
                        <span className="menu-preorder-item-right">
                          {checked && (
                            <span
                              className="menu-preorder-qty"
                              role="group"
                              aria-label={`Количество: ${p.title}`}
                              onClick={stopRowClick}
                            >
                              <button
                                type="button"
                                className="menu-preorder-qty-btn"
                                aria-label={`Уменьшить количество: ${p.title}`}
                                onClick={() => decrementItem(p.menu_item_id)}
                              >
                                −
                              </button>
                              <span className="menu-preorder-qty-value" aria-live="polite">
                                {qty}
                              </span>
                              <button
                                type="button"
                                className="menu-preorder-qty-btn"
                                aria-label={`Увеличить количество: ${p.title}`}
                                disabled={qty >= QTY_MAX}
                                onClick={() => incrementItem(p.menu_item_id)}
                              >
                                +
                              </button>
                            </span>
                          )}
                          <span className="menu-preorder-item-price">{Math.round(p.price)} ₽</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="menu-preorder-footer">
        <div className="menu-preorder-footer-row">
          <span>Позиций: {totals.count}</span>
          <span>Сумма: {Math.round(totals.sum)} ₽</span>
        </div>
        <button
          type="button"
          className="form-action-primary"
          disabled={loading || totals.count === 0}
          onClick={handleForm}
        >
          Сформировать
        </button>
      </div>
    </div>
  );
};