import type { LucideIcon } from "lucide-react";
import {
  Banknote,
  Car,
  Caravan,
  ChartLine,
  CreditCard,
  Gem,
  HandCoins,
  House,
  Landmark,
  LandPlot,
  Package,
  Palette,
  PiggyBank,
  Sailboat,
  Wallet,
  Wrench,
} from "lucide-react";

/** What a hand-valued asset (type `property`) can be (#567): a label and an
 * icon, never behaviour. Order is the picker's. */
export const ASSET_KINDS = [
  "house",
  "land",
  "car",
  "boat",
  "rv",
  "jewelry",
  "art",
  "equipment",
  "other",
] as const;
export type AssetKind = (typeof ASSET_KINDS)[number];

export const ASSET_KIND_LABELS: Record<AssetKind, string> = {
  house: "House",
  land: "Land",
  car: "Car",
  boat: "Boat",
  rv: "RV",
  jewelry: "Jewelry",
  art: "Art & collectibles",
  equipment: "Equipment",
  other: "Other",
};

export const assetKindLabel = (kind: string | null | undefined): string =>
  ASSET_KIND_LABELS[(kind ?? "other") as AssetKind] ?? ASSET_KIND_LABELS.other;

const TYPE_ICONS: Record<string, LucideIcon> = {
  checking: Landmark,
  savings: PiggyBank,
  cash: Banknote,
  credit_card: CreditCard,
  brokerage: ChartLine,
  loan: HandCoins,
};

const KIND_ICONS: Record<AssetKind, LucideIcon> = {
  house: House,
  land: LandPlot,
  car: Car,
  boat: Sailboat,
  rv: Caravan,
  jewelry: Gem,
  art: Palette,
  equipment: Wrench,
  other: Package,
};

export function accountIconFor(type: string, kind?: string | null): LucideIcon {
  if (type === "property") return KIND_ICONS[(kind ?? "other") as AssetKind] ?? Package;
  return TYPE_ICONS[type] ?? Wallet;
}

/** An account's icon: decorative, always beside its name or type in text, so
 * screen readers skip it. It takes the surrounding text colour, which keeps
 * it readable in both themes. */
export function AccountIcon({
  type,
  kind,
  className = "h-4 w-4",
}: {
  type: string;
  kind?: string | null;
  className?: string;
}) {
  const Icon = accountIconFor(type, kind);
  return <Icon aria-hidden="true" focusable="false" className={`shrink-0 ${className}`} />;
}
