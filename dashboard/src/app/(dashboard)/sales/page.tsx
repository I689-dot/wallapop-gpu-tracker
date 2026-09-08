import { SalesTable } from "@/components/sales/sales-table";
import { getSales } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function SalesPage() {
  const { sales, unpricedClosures, outsidePool, truncated } = await getSales();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Sales</h1>
        <p className="text-sm text-muted-foreground">
          Every sale the tracker has confirmed a price for, newest first — a listing seen
          reserved, at the price someone committed to, that then disappeared. These are the
          only prices the reference values are built from.
        </p>
      </div>

      {truncated ? (
        <p className="rounded-md border border-destructive/40 px-3 py-2 text-xs text-destructive">
          The scan limit was reached, so the oldest sales are missing. Raise MAX_SCAN_ROWS in
          src/lib/queries.ts.
        </p>
      ) : null}

      {sales.length === 0 ? (
        <p className="py-10 text-center text-sm text-muted-foreground">
          No confirmed sales yet — a listing has to be seen reserved and then disappear before
          the tracker will attribute a price to it.
        </p>
      ) : (
        <SalesTable
          sales={sales}
          unpricedClosures={unpricedClosures}
          outsidePool={outsidePool}
        />
      )}
    </div>
  );
}
