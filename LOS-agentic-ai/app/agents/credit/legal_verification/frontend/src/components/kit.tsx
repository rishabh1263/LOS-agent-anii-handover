import { Badge, SimpleGrid, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertTriangle, IconCheck } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { ApiError } from "../services/api";
import type { Workspace } from "../types";
import { t } from "../utils/text";

/** meaning -> Mantine colour: red mismatch, yellow review, green match; navy/gray for workflow states */
export const ruleColor = (s: string) => ({ FAIL: "red", REVIEW: "yellow", PASS: "green" } as Record<string, string>)[s] ?? "gray";
export const recoColor = (r: string | null | undefined) =>
  ({ ESCALATE: "red", REVIEW_REQUIRED: "yellow", CLEAR_FOR_REVIEW: "green" } as Record<string, string>)[r ?? ""] ?? "gray";
export const statusColor = (s: string) =>
  ({ NEW: "gray", DOCUMENTS_UPLOADED: "sapphire", WITH_AGENCY: "yellow", REPORT_RECEIVED: "cyan", VALIDATED: "teal",
     DECIDED: "green", WAIVED: "orange" } as Record<string, string>)[s] ?? "gray";
const SOURCE_COLOR: Record<string, string> = { APPLICATION: "sapphire", SALE_DEED: "violet", CORRECTED: "orange", AGENCY: "teal", AGENT: "gray" };

export function SourceBadge({ source, labels }: { source: string; labels: Record<string, string> }) {
  return <Badge size="xs" variant="light" color={SOURCE_COLOR[source] ?? "gray"}>{labels[source] ?? source}</Badge>;
}

export function notifyOk(message: string) {
  notifications.show({ message, color: "green", icon: <IconCheck size={16} />, autoClose: 3500 });
}

export function notifyError(e: unknown) {
  notifications.show({ message: e instanceof ApiError ? e.message : t("common.failed"), color: "red",
                       icon: <IconAlertTriangle size={16} />, autoClose: 7000 });
}

/** label / value pairs in a responsive grid -- a missing value shows as a dash, never "undefined" */
export function FieldGrid({ items, cols = { base: 1, sm: 2, lg: 3 } }: {
  items: { id: string; label: string; value: ReactNode; wide?: boolean }[]; cols?: Record<string, number>;
}) {
  return (
    <SimpleGrid cols={cols} spacing="lg" verticalSpacing="sm">
      {items.map((f) => (
        <Stack key={f.id} gap={2} className={f.wide ? "grid-wide" : undefined}>
          <Text size="xs" c="dimmed" fw={600}>{f.label}</Text>
          {f.value === null || f.value === undefined || f.value === ""
            ? <Text size="sm" c="gray.5">—</Text> : <Text size="sm" fw={700} className="value">{f.value}</Text>}
        </Stack>
      ))}
    </SimpleGrid>
  );
}

export type Act = (key: string, run: () => Promise<Workspace | void>, ok?: string) => Promise<boolean>;
