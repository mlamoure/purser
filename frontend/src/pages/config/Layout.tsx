import { NavLink, Outlet } from "react-router-dom";
import { cn } from "../../lib/utils";

const tabs = [
  ["simplefin", "SimpleFIN"],
  ["categories", "Categories"],
  ["rules", "Rules"],
  ["memories", "Memories"],
  ["prices", "Prices"],
  ["settings", "Settings"],
] as const;

export default function ConfigLayout() {
  return (
    <div className="space-y-6">
      <div className="flex gap-1 border-b border-border">
        {tabs.map(([to, label]) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                "-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground",
                isActive && "border-primary text-foreground",
              )
            }
          >
            {label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </div>
  );
}
