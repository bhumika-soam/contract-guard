import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { ImpactReport } from "@/types/impactReport"

import fieldRenamed from "@/mocks/impact-reports/drift-field-renamed.json"
import typeChanged from "@/mocks/impact-reports/drift-type-changed.json"
import endpointRemoved from "@/mocks/impact-reports/drift-endpoint-removed.json"

export const Route = createFileRoute("/impact-report")({
  component: ImpactReportPage,
  head: () => ({
    meta: [{ title: "Impact Report - ContractGuard" }],
  }),
})

const scenarios: { key: string; label: string; data: ImpactReport }[] = [
  { key: "field-renamed", label: "Field Renamed", data: fieldRenamed as ImpactReport },
  { key: "type-changed", label: "Type Changed", data: typeChanged as ImpactReport },
  { key: "endpoint-removed", label: "Endpoint Removed", data: endpointRemoved as ImpactReport },
]

const severityStyles: Record<ImpactReport["severity"], string> = {
  high: "bg-red-100 text-red-700 hover:bg-red-100 dark:bg-red-900/40 dark:text-red-300 dark:hover:bg-red-900/40",
  medium: "bg-amber-100 text-amber-700 hover:bg-amber-100 dark:bg-amber-900/40 dark:text-amber-300 dark:hover:bg-amber-900/40",
  low: "bg-yellow-50 text-yellow-700 hover:bg-yellow-50 dark:bg-yellow-900/30 dark:text-yellow-300 dark:hover:bg-yellow-900/30",
}

const verifyStyles: Record<ImpactReport["verify_status"], string> = {
  pass: "bg-green-100 text-green-700 hover:bg-green-100 dark:bg-green-900/40 dark:text-green-300 dark:hover:bg-green-900/40",
  fail: "bg-red-100 text-red-700 hover:bg-red-100 dark:bg-red-900/40 dark:text-red-300 dark:hover:bg-red-900/40",
}

function ImpactReportCard({ report }: { report: ImpactReport }) {
  return (
    <Card className="border-l-4 border-l-emerald-500 bg-emerald-950/[0.015]">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-4">
          <p className="text-lg font-semibold leading-snug">
            {report.summary_plain_english}
          </p>
          <Badge className={severityStyles[report.severity]}>
            {report.severity}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground mt-1">
          <span className="font-medium text-foreground">{report.method}</span>{" "}
          {report.endpoint}
          <span className="mx-2 text-muted-foreground/50">·</span>
          <span className="capitalize">{report.change_type}</span>
        </p>
      </CardHeader>

      <CardContent className="flex flex-col gap-6">
        {/* Affected files */}
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400 mb-2">
            Affected files
          </p>
          <ul className="text-sm text-muted-foreground list-disc list-inside space-y-0.5">
            {report.affected_files.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
        </div>

        {/* Patch info */}
        <div className="rounded-lg border border-border bg-muted/50 px-4 py-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400 mb-1.5">
            Patch
          </p>
          <p className="text-sm font-medium mb-1">
            {report.patch_applied ? "✓ Patch applied" : "✘ Patch not applied"}
          </p>
          <p className="text-sm text-muted-foreground">
            {report.patch_description}
          </p>
        </div>

        {/* Verify status */}
        <div className="flex items-center gap-3 rounded-lg border border-border bg-muted/50 px-4 py-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
            Verify
          </p>
          <Badge className={verifyStyles[report.verify_status]}>
            {report.verify_status}
          </Badge>
        </div>

        {/* Collapsible log */}
        <details className="text-sm">
          <summary className="cursor-pointer font-medium text-muted-foreground hover:text-foreground transition-colors">
            View verify log
          </summary>
          <pre className="mt-2 whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs">
            {report.verify_log}
          </pre>
        </details>

        {/* Schema diff */}
        {(report.old_schema_fragment || report.new_schema_fragment) && (
          <details className="text-sm">
            <summary className="cursor-pointer font-medium text-muted-foreground hover:text-foreground transition-colors">
              View schema diff
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-3 text-xs">
              <div>
                <p className="font-semibold mb-1">Old</p>
                <pre className="rounded-lg bg-muted p-3">
                  {report.old_schema_fragment
                    ? JSON.stringify(report.old_schema_fragment, null, 2)
                    : "—"}
                </pre>
              </div>
              <div>
                <p className="font-semibold mb-1">New</p>
                <pre className="rounded-lg bg-muted p-3">
                  {report.new_schema_fragment
                    ? JSON.stringify(report.new_schema_fragment, null, 2)
                    : "—"}
                </pre>
              </div>
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  )
}

function ImpactReportPage() {
  const [active, setActive] = useState(scenarios[0].key)

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="text-2xl font-bold mb-1">ContractGuard — Impact Report</h1>
      <div className="h-1 w-16 rounded-full bg-emerald-500 mb-6" />

      <Tabs value={active} onValueChange={setActive}>
        <TabsList className="bg-emerald-950/10 dark:bg-emerald-900/20">
          {scenarios.map((s) => (
            <TabsTrigger
              key={s.key}
              value={s.key}
              className="data-[state=active]:bg-emerald-600 data-[state=active]:text-white data-[state=active]:shadow-none"
            >
              {s.label}
            </TabsTrigger>
          ))}
        </TabsList>

        {scenarios.map((s) => (
          <TabsContent key={s.key} value={s.key} className="mt-4">
            <ImpactReportCard report={s.data} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}