"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { ExternalLink } from "lucide-react";
import type { SaleRow } from "@/lib/queries";
import { Thumb } from "@/components/thumb";
import { money, dateOnly, relativeTime } from "@/lib/format";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Consoles and phones are tracked for comps only, so a sale in those families
 *  is price research rather than a missed trade. Worth showing at a glance.
 *  A null family is a row written before the column existed, which is a GPU —
 *  the same convention `alertScopeRejection` uses. */
function familyOf(sale: SaleRow): string {
  return sale.family ?? "gpu";
}

function FamilyChip({ family }: { family: string }) {
  const tone =
    family === "gpu"
      ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
      : family === "phone"
        ? "bg-violet-500/10 text-violet-700 dark:text-violet-400"
        : "bg-amber-500/10 text-amber-700 dark:text-amber-500";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${tone}`}>
      {family}
    </span>
  );
}

/** A sale the comps pool refuses. The row stays — it is a real closure — but it
 *  is not evidence any reference price was built from, and the page used to
 *  present the two as the same thing. */
function PoolChip({ reason }: { reason: string }) {
  return (
    <span
      title={`Not in any comps pool: ${reason}. See compsPoolRejection in lib/constants.ts.`}
      className="rounded bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-rose-700 dark:text-rose-400"
    >
      {reason}
    </span>
  );
}

export function SalesTable({
  sales,
  unpricedClosures,
  outsidePool,
}: {
  sales: SaleRow[];
  unpricedClosures: number;
  outsidePool: number;
}) {
  const models = useMemo(
    () => [...new Set(sales.map((s) => s.model_key).filter((m): m is string => !!m))].sort(),
    [sales],
  );
  const families = useMemo(
    () => [...new Set(sales.map(familyOf))].sort(),
    [sales],
  );

  const [model, setModel] = useState("all");
  const [family, setFamily] = useState("all");
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sales.filter((s) => {
      if (model !== "all" && s.model_key !== model) return false;
      if (family !== "all" && familyOf(s) !== family) return false;
      if (q && !(s.title ?? "").toLowerCase().includes(q)) return false;
      return true;
    });
  }, [sales, model, family, query]);

  // Median rather than mean, for the same reason the tracker uses one: a single
  // mistyped price should not move the headline.
  const stats = useMemo(() => {
    if (filtered.length === 0) return null;
    const prices = filtered.map((s) => s.sold_price).sort((a, b) => a - b);
    const median = prices[Math.floor(prices.length / 2)];
    const days = filtered
      .map((s) => s.days_to_sale)
      .filter((d): d is number => d !== null)
      .sort((a, b) => a - b);
    return {
      count: filtered.length,
      median,
      total: prices.reduce((a, b) => a + b, 0),
      medianDays: days.length ? days[Math.floor(days.length / 2)] : null,
    };
  }, [filtered]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">Family</Label>
          <Select value={family} onValueChange={(v) => setFamily(v ?? "all")}>
            <SelectTrigger className="w-36" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All families</SelectItem>
              {families.map((f) => (
                <SelectItem key={f} value={f}>
                  {f}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">Model</Label>
          <Select value={model} onValueChange={(v) => setModel(v ?? "all")}>
            <SelectTrigger className="w-52" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All models</SelectItem>
              {models.map((m) => (
                <SelectItem key={m} value={m}>
                  {m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">Search title</Label>
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="RTX 4070…"
            className="h-8 w-52"
          />
        </div>
      </div>

      {stats ? (
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-4">
          <div className="bg-card px-3 py-2.5">
            <div className="text-lg font-semibold tabular-nums">{stats.count.toLocaleString()}</div>
            <div className="text-xs text-muted-foreground">confirmed sales</div>
          </div>
          <div className="bg-card px-3 py-2.5">
            <div className="text-lg font-semibold tabular-nums">{money(stats.median)}</div>
            <div className="text-xs text-muted-foreground">median price</div>
          </div>
          <div className="bg-card px-3 py-2.5">
            <div className="text-lg font-semibold tabular-nums">
              {stats.medianDays === null ? "—" : `${stats.medianDays}d`}
            </div>
            <div className="text-xs text-muted-foreground">median days on sale</div>
          </div>
          <div className="bg-card px-3 py-2.5">
            <div className="text-lg font-semibold tabular-nums">{money(stats.total)}</div>
            <div className="text-xs text-muted-foreground">total value observed</div>
          </div>
        </div>
      ) : null}

      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[42%]">Item</TableHead>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Sold for</TableHead>
              <TableHead className="text-right">Listed at</TableHead>
              <TableHead className="text-right">On sale</TableHead>
              <TableHead className="text-right">Sold</TableHead>
              <TableHead className="w-8" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="py-10 text-center text-sm text-muted-foreground">
                  No confirmed sales match these filters.
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((s) => {
                // last_price is where the listing ended up; sold_price is what it
                // carried while reserved. They usually agree, and when they don't
                // the difference is a late price cut worth seeing.
                const cut =
                  s.last_price !== null && s.last_price !== s.sold_price ? s.last_price : null;
                return (
                  <TableRow
                    key={s.item_id}
                    className={s.pool_rejection ? "opacity-55" : undefined}
                  >
                    <TableCell>
                      <div className="flex items-center gap-2.5">
                        <Thumb src={s.image_url} alt={s.title ?? ""} className="size-9 shrink-0" />
                        <span className="line-clamp-2 text-sm">{s.title ?? "—"}</span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-mono text-xs">{s.model_key ?? "—"}</span>
                        <FamilyChip family={familyOf(s)} />
                        {s.pool_rejection ? <PoolChip reason={s.pool_rejection} /> : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-right font-medium tabular-nums">
                      {money(s.sold_price)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                      {cut === null ? "—" : money(cut)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                      {s.days_to_sale === null ? "—" : `${s.days_to_sale}d`}
                    </TableCell>
                    <TableCell className="text-right whitespace-nowrap">
                      <div className="text-xs tabular-nums">{dateOnly(s.closed_at)}</div>
                      <div className="text-[11px] text-muted-foreground">
                        {relativeTime(s.closed_at)}
                      </div>
                    </TableCell>
                    <TableCell>
                      {s.web_url ? (
                        <Link
                          href={s.web_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-muted-foreground hover:text-foreground"
                          aria-label="Open on Wallapop"
                        >
                          <ExternalLink className="size-3.5" />
                        </Link>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {outsidePool > 0 ? (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium tabular-nums">{outsidePool.toLocaleString()}</span> of the
          sales above are dimmed because no reference price was learned from them — the comps pool
          refuses a low-confidence classification, a whole machine, or a price outside its band.
          They are real closures and stay listed; they are just not evidence of what anything is
          worth. The gate is <code>compsPoolRejection</code>, the port of{" "}
          <code>db.sold_comps</code>.
        </p>
      ) : null}

      <p className="text-xs text-muted-foreground">
        A further <span className="font-medium tabular-nums">{unpricedClosures.toLocaleString()}</span>{" "}
        listings closed without ever being seen reserved. They might have sold or might have been
        withdrawn — the tracker refuses to guess, so no price is attributed and they are excluded
        from every reference price. That gap is why comps build slowly.
      </p>
    </div>
  );
}
